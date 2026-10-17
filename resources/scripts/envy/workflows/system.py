"""Apply, rollback, cleanup, editor, and setup workflows."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from envy import log
from envy.config import refine_all
from envy.evaluation import machine_manifest, manifest_settings
from envy.keys.storage import device_public_keys, read_sops_yaml_keys
from envy.utils import AGE_KEY_FILE, SECRETS_FILE, age_key_file_for_home, run_cmd
from envy.host import current_machine_file, initialize_machine, require_current_machine_file
from envy.journal import record_operation
from envy.nix_trust import ensure_nix_daemon_trust
from envy.process import CommandError, run_process
from envy.utils import (
    ENVY_ROOT,
    PLATFORM,
    SETUP_SCRIPT,
    SYSTEM_PROFILE,
    esudo,
    flake_target,
    run_apply,
    run_hm,
)


def verify_apply_identity() -> None:
    """Ensure the configured identity is an active secrets recipient."""
    settings = manifest_settings(machine_manifest(refresh=True))
    configured_home = Path(settings.get("envy.user.home", str(Path.home())))
    configured_key = age_key_file_for_home(configured_home)
    key_file = configured_key if configured_key.exists() else AGE_KEY_FILE
    local_public_keys = device_public_keys(key_file)
    recipients = read_sops_yaml_keys()
    if not local_public_keys or not any(key in recipients.values() for key in local_public_keys):
        raise RuntimeError("local age key is not an active secrets recipient")
    if SECRETS_FILE.exists():
        run_cmd(["sops", "--decrypt", str(SECRETS_FILE)], age_key_file=key_file)


def refine_before_apply() -> None:
    machine_path = current_machine_file()
    if not machine_path.exists():
        log.warn("host", "machine configuration is missing", path=str(machine_path))
        if not sys.stdin.isatty():
            log.hint(f"Run: envy host init {machine_path.stem}")
            raise typer.Exit(code=1)
        if not typer.confirm("Create it from hosts/default.nix now?", default=None):
            raise typer.Abort()
        mode = typer.prompt("Creation mode (import/copy)", default="import")
        initialize_machine(machine_path.stem, mode)
    ensure_nix_daemon_trust(platform=PLATFORM)
    secrets_already_present = SECRETS_FILE.exists()
    try:
        # Validate the identity before refine can publish a new or existing
        # encrypted document.
        verify_apply_identity()
    except (CommandError, OSError, RuntimeError, ValueError) as exc:
        log.error("secrets", str(exc))
        raise typer.Exit(code=1) from exc
    report = refine_all(write=True, strict=True, include_secrets=True)
    if not report.ok:
        raise typer.Exit(code=1)
    if not secrets_already_present and SECRETS_FILE.exists():
        try:
            verify_apply_identity()
        except (CommandError, OSError, RuntimeError, ValueError) as exc:
            log.error("secrets", str(exc))
            raise typer.Exit(code=1) from exc


@record_operation("apply")
def apply_configuration() -> None:
    refine_before_apply()
    require_current_machine_file()
    run_apply()


@record_operation("bootstrap")
def bootstrap_configuration() -> None:
    refine_before_apply()
    require_current_machine_file()
    if PLATFORM == "darwin":
        run_apply()
    else:
        log.step("hm", "bootstrapping Home Manager from flake")
        run_hm("switch", "--flake", flake_target(), "--impure")
    log.ok("bootstrap", "bootstrap completed successfully")


@record_operation(
    "rollback",
    detail=lambda target=None, **_: (
        {"generation": target} if target and target != "list" else {}
    ),
    skip=lambda target=None, *, dry_run=False, **_: dry_run or target == "list",
)
def rollback_configuration(target: str | None = None, *, dry_run: bool = False) -> None:
    if dry_run and target != "list":
        preview_rollback(target)
        return
    if PLATFORM == "darwin":
        rollback_darwin(target)
    else:
        rollback_linux(target)


def preview_rollback(target: str | None) -> None:
    from envy.workflows.generations import (
        closure_diff,
        current_generation,
        previous_generation,
        require_generation,
    )

    current = current_generation()
    if target is None:
        selected = previous_generation()
    else:
        try:
            selected = require_generation(int(target))
        except ValueError as exc:
            raise typer.BadParameter("generation must be a number") from exc
    if current is None:
        log.error("rollback", "current generation could not be identified")
        raise typer.Exit(code=1)
    if selected is None:
        log.error("rollback", "previous generation does not exist")
        raise typer.Exit(code=1)
    try:
        diff = closure_diff(current.target, selected.target)
    except RuntimeError as exc:
        log.error("rollback", str(exc))
        raise typer.Exit(code=1) from exc
    log.info(
        "rollback", "dry run",
        current=current.number, target=selected.number,
    )
    log.console.print(diff or "[green]No closure differences.[/green]")
    log.hint(f"Run: envy rollback {selected.number}")


def rollback_darwin(target: str | None = None) -> None:
    profile = str(SYSTEM_PROFILE)
    if target is None:
        log.step("rollback", "rolling back to previous generation")
        esudo("-H", "nix-env", "-p", profile, "--rollback", capture=True)
        log.step("rollback", "re-activating previous configuration")
        esudo("-H", profile + "/activate", capture=False)
        log.ok("rollback", "rollback complete")
        return
    if target == "list":
        log.step("rollback", "current system generations")
        esudo("-H", "nix-env", "-p", profile, "--list-generations", capture=True)
        return
    try:
        generation = int(target)
    except ValueError as exc:
        raise typer.BadParameter("generation must be a number or 'list'") from exc
    log.step("rollback", "switching system profile", generation=generation)
    esudo("-H", "nix-env", "-p", profile, "--set-generation", str(generation), capture=True)
    esudo("-H", profile + "/activate", capture=False)
    log.ok("rollback", "generation activated", generation=generation)


def rollback_linux(target: str | None = None) -> None:
    if target is None:
        log.step("rollback", "rolling back to previous generation")
        run_hm("switch", "--rollback")
        log.ok("rollback", "rollback successful")
        return
    if target == "list":
        log.step("rollback", "available Home Manager generations")
        run_hm("generations")
        return
    try:
        generation = int(target)
    except ValueError as exc:
        raise typer.BadParameter("generation must be a number or 'list'") from exc
    state_dir = Path(os.environ.get("NIX_STATE_DIR", "/nix/var/nix"))
    user = os.environ.get("USER", "")
    activation = state_dir / "profiles" / "per-user" / user / f"home-manager-{generation}-link" / "activate"
    if not activation.exists():
        log.error("rollback", "Home Manager generation does not exist", generation=generation)
        log.hint("Run: envy rollback list")
        raise typer.Exit(code=1)
    log.step("rollback", "activating Home Manager generation", generation=generation)
    run_process([str(activation)], check=True)
    log.ok("rollback", "generation activated", generation=generation)


def open_editor() -> None:
    editor = os.environ.get("EDITOR", "vim")
    log.step("editor", f"opening envY repository in $EDITOR ({editor})")
    run_process([editor, str(ENVY_ROOT)], check=True)


@record_operation(
    "clean",
    detail=lambda **kw: {
        "olderThan": kw.get("older_than") or "all",
        "brew": kw.get("brew", False),
    },
)
def clean_generations(*, yes: bool, older_than: str | None, brew: bool) -> None:
    policy = f"older than {older_than}" if older_than else "all old generations"
    log.warn("nix", "cleanup will permanently delete generations", policy=policy)
    if not yes:
        if not sys.stdin.isatty():
            log.error("nix", "non-interactive cleanup requires --yes")
            raise typer.Exit(code=1)
        if not typer.confirm("Continue with destructive cleanup?", default=None):
            raise typer.Abort()
    command = ["nix-collect-garbage"]
    command.extend(["--delete-older-than", older_than] if older_than else ["-d"])
    log.step("nix", "cleaning up old generations", policy=policy)
    if PLATFORM == "darwin":
        esudo("-H", *command, capture=False)
        run_process(command, check=True, activity="system Nix garbage collection")
        run_process(["nix-store", "--optimise"], check=True, activity="Nix store optimisation")
        if brew:
            run_process(["brew", "cleanup"], check=True, activity="Homebrew cleanup")
    else:
        run_process(command, check=True, activity="Nix garbage collection")
    log.ok("nix", "cleanup complete")


def run_setup() -> None:
    log.step("setup", "running setup")
    result = run_process(["/bin/bash", str(SETUP_SCRIPT)], check=False)
    if result.returncode == 2:
        log.warn("setup", "configuration cancelled")
        return
    if result.returncode != 0:
        log.error("setup", "configuration failed", exit_code=result.returncode)
        raise typer.Exit(code=result.returncode)
    log.ok("setup", "configuration complete")

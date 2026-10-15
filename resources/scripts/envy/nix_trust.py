"""Nix daemon trust preflight backed by the bootstrap-safe shell helper."""

from __future__ import annotations

import getpass
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from envy import log
from envy.config import read_machine_nix
from envy.process import run_process
from envy.utils import ENVY_ROOT, esudo


@dataclass(frozen=True)
class NixTrustContext:
    mode: str
    user: str


def nix_trust_is_applicable(platform: str | None = None) -> bool:
    return (platform or sys.platform) in {"linux", "darwin"}


def current_nix_trust_context() -> NixTrustContext:
    try:
        values = read_machine_nix()
    except OSError:
        values = {}
    mode = str(values.get("envy.mirrors.mode", os.environ.get("ENVY_MIRROR", "china"))).casefold()
    if mode not in {"upstream", "china"}:
        mode = "china"
    user = str(values.get("envy.user.name", "")).strip() or getpass.getuser()
    return NixTrustContext(mode=mode, user=user)


def nix_trust_script() -> Path:
    repository_script = ENVY_ROOT / "resources" / "scripts" / "nix-trust.sh"
    if repository_script.is_file():
        return repository_script
    return Path(__file__).resolve().parent.parent / "nix-trust.sh"


def run_nix_trust(
    action: str,
    *,
    context: NixTrustContext | None = None,
    capture: bool = False,
    quiet: bool = False,
):
    selected = context or current_nix_trust_context()
    quiet_args = ["--quiet"] if quiet else []
    command = [
        "bash",
        str(nix_trust_script()),
        action,
        "--mode",
        selected.mode,
        "--user",
        selected.user,
        *( ["--reload"] if action == "repair" else []),
        *quiet_args,
    ]
    if action == "repair":
        # The helper edits the system Nix configuration. Route it through the
        # same sops-backed sudo wrapper as the rest of envy so apply does not
        # ask for a second interactive password prompt.
        return esudo(
            *command,
            capture=capture,
            activity="Nix daemon trust repair",
        )
    return run_process(
        command,
        capture=capture,
        check=False,
        activity=None,
    )


def ensure_nix_daemon_trust(*, platform: str | None = None) -> None:
    """Repair daemon trust before a command performs any Nix evaluation."""
    if not nix_trust_is_applicable(platform):
        return
    context = current_nix_trust_context()
    log.step("nix", "checking daemon cache trust", mode=context.mode)
    result = run_nix_trust("repair", context=context)
    if result.returncode == 0:
        return
    log.error("nix", "daemon cache trust repair failed", exit_code=result.returncode)
    log.hint("Run: envy mirror trust status")
    raise typer.Exit(code=result.returncode)

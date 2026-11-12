"""One CLI for storage plans, quarantine, archives, recovery, and collection."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from envy import jsonio, log
from envy.evaluation import machine_manifest
from envy.storage import lifecycle, maintenance, planner
from envy.storage.backend import Archive
from envy.storage.model import StorageError, policy_from_manifest, rule_details
from envy.storage.state import locked, read_record, records
from envy.utils import ENVY_ROOT, state_dir
from envy.workflows.system import clean_generations


app = typer.Typer(name="storage", help="Manage the storage lifecycle with machine-owned rules.",
                  no_args_is_help=True)


def _context():
    manifest = machine_manifest()
    if not isinstance(manifest, dict):
        raise StorageError("Machine manifest unavailable; run envy host check")
    root = state_dir() / "storage"
    return policy_from_manifest(manifest, root, ENVY_ROOT), root


def _perform(command, json_output, operation):
    try:
        data = operation()
    except (StorageError, OSError) as exc:
        if json_output:
            jsonio.emit_error(f"storage {command}", str(exc), code="storage_error")
        else:
            log.error("storage", str(exc))
        raise typer.Exit(1) from exc
    failed = bool(data.get("errors")) or data.get("ok") is False
    if json_output:
        jsonio.emit(f"storage {command}", data=data, ok=not failed,
                    error={"code": "partial_failure", "message": "Storage operation failed; inspect rule results and errors"}
                    if failed else None)
    else:
        if command == "plan" or "candidates" in data:
            table = Table(title=f"Storage plan {data.get('id', '')}")
            for name in ("Rule", "Targets", "Entries", "GiB", "Blocked", "Note"):
                table.add_column(name)
            for item in data.get("rules", []):
                table.add_row(item.get("id", ""), item.get("targets", ""),
                              str(item.get("candidates", 0)),
                              f"{item.get('bytes', 0) / 2**30:.3f}",
                              str(item.get("blocked", 0)), item.get("reason", ""))
            log.console.print(table)
            for entry in data.get("skipped", [])[:10]:
                log.warn("storage", entry["reason"], path=entry["path"])
            if data.get("id"):
                log.hint("Inspect every candidate: envy storage plan --json")
                log.hint(f"Execute this plan: envy storage run {data['id']} --yes")
        elif command == "validate":
            table = Table(title="Storage rule validation")
            for name in ("Rule", "Root", "Check", "Error"):
                table.add_column(name)
            for item in data.get("rules", []):
                table.add_row(item.get("rule", ""), item.get("root", ""),
                              "skipped" if item.get("skipped") else "ok" if item.get("ok") else "failed",
                              item.get("error") or item.get("reason", ""))
            log.console.print(table)
        elif command == "maintain":
            table = Table(title="Storage maintenance")
            for name in ("Action", "Available", "Commands", "Note"):
                table.add_column(name)
            for item in data.get("items", []):
                table.add_row(item.get("action", ""), "yes" if item.get("available") else "no",
                              " ; ".join(" ".join(command) for command in item.get("commands", [])),
                              item.get("note", ""))
            log.console.print(table)
            if not data.get("execute"):
                log.hint("Execute selected actions with: envy storage maintain --yes --nix/--docker/--go/--uv/--npm/--pip/--brew")
        else:
            log.console.print_json(data=data)
    if failed:
        raise typer.Exit(1)
    return data


@app.command("plan")
def cmd_plan(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")):
    """Save a candidate plan without moving or deleting files."""
    def operation():
        policy, state = _context()
        with locked(state):
            return planner.plan(policy, state)
    _perform("plan", json_output, operation)


@app.command("run")
def cmd_run(
    plan_id: Optional[str] = typer.Argument(None, help="Saved plan ID; omit to create a fresh plan."),
    yes: bool = typer.Option(False, "--yes", help="Execute the planned lifecycle actions."),
    scheduled: bool = typer.Option(False, "--scheduled", hidden=True),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Preview by default; execute an approved plan or the configured scheduled policy."""
    def operation():
        policy, state = _context()
        with locked(state):
            if scheduled and policy.mode == "manual":
                raise StorageError("Scheduled storage execution is disabled in manual mode")
            if scheduled and (plan_id or yes):
                raise StorageError("Scheduled runs use only the declared machine policy")
            should_execute = policy.mode == "auto" if scheduled else yes
            current = planner.plan(policy, state) if plan_id is None else read_record(state, "plans", plan_id)
            selected = current["id"]
            if not should_execute:
                return current
            result = lifecycle.execute(policy, state, selected)
            if scheduled:
                expiry = lifecycle.purge(policy, state, execute=True)
                result["expiry"] = expiry
                result["errors"].extend(expiry["errors"])
            return result
    _perform("run", json_output, operation)


def _password_status(value: str) -> dict:
    if not value:
        return {"configured": False, "exists": False, "private": False}
    try:
        path = Path(value).resolve(strict=True)
        info = path.stat()
        private = (stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.getuid()}
                   and info.st_mode & 0o077 == 0)
        return {"configured": True, "exists": True, "private": private}
    except FileNotFoundError:
        return {"configured": True, "exists": False, "private": False}
    except (OSError, RuntimeError, StorageError) as exc:
        return {"configured": True, "exists": None, "private": False, "error": str(exc)}


@app.command("status")
def cmd_status(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")):
    """Show configured rules and durable archive/quarantine records."""
    def operation():
        policy, state = _context()
        with locked(state):
            return {"mode": policy.mode, "rules": [rule.id for rule in policy.rules],
                    "ruleDetails": [rule_details(rule) for rule in policy.rules],
                    "excluded": list(policy.excluded),
                    "maintenance": list(policy.maintenance),
                    "archiveConfigured": bool(policy.repository and policy.password_file),
                    "archive": {"configured": bool(policy.repository and policy.password_file),
                                "mountSource": policy.mount_source,
                                "passwordFile": _password_status(policy.password_file),
                                "repositoryChecked": False},
                    "latestPlan": max(records(state, "plans"), key=lambda plan: plan["createdAt"], default=None),
                    "runs": records(state, "runs")}
    _perform("status", json_output, operation)


@app.command("purge")
def cmd_purge(
    yes: bool = typer.Option(False, "--yes", help="Delete expired, unchanged quarantine files."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Preview or release expired quarantine files; archive snapshots are retained."""
    def operation():
        policy, state = _context()
        with locked(state):
            return lifecycle.purge(policy, state, execute=yes)
    _perform("purge", json_output, operation)


@app.command("restore")
def cmd_restore(
    run_id: str = typer.Argument(help="Storage run ID from status."),
    target: Path = typer.Option(..., "--target", help="New directory for recovered files."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Recover a run into a new directory, preserving original path structure."""
    def operation():
        policy, state = _context()
        with locked(state):
            return lifecycle.restore(policy, state, run_id, target)
    _perform("restore", json_output, operation)


@app.command("maintain")
def cmd_maintain(
    nix: bool = typer.Option(False, "--nix", help="Collect unreferenced Nix store paths, keeping generations."),
    docker: bool = typer.Option(False, "--docker", help="Prune Docker build cache and dangling images older than 336h."),
    go: bool = typer.Option(False, "--go", help="Clean the Go build cache."),
    uv: bool = typer.Option(False, "--uv", help="Maintain the uv cache."),
    npm: bool = typer.Option(False, "--npm", help="Maintain the npm cache."),
    pip: bool = typer.Option(False, "--pip", help="Maintain the pip cache."),
    brew: bool = typer.Option(False, "--brew", help="Maintain Homebrew caches."),
    yes: bool = typer.Option(False, "--yes", help="Execute instead of previewing."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Preview or run explicit native maintenance; never part of the schedule."""
    def operation():
        policy, state = _context()
        actions = [name for name, enabled in (("nix", nix), ("docker", docker), ("go", go),
                                             ("uv", uv), ("npm", npm), ("pip", pip),
                                             ("brew", brew)) if enabled]
        with locked(state):
            if yes:
                return maintenance.maintain(policy, actions or None)
            return maintenance.preview(policy, actions or None)
    _perform("maintain", json_output, operation)


@app.command("validate")
def cmd_validate(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")):
    """Run organize check against every generated rule config; no scan, no writes."""
    def operation():
        policy, state = _context()
        return planner.validate(policy, state)
    _perform("validate", json_output, operation)


@app.command("init")
def cmd_init(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")):
    """Initialize the configured dedicated restic archive repository."""
    def operation():
        policy, state = _context()
        with locked(state):
            archive = Archive(policy, state)
            archive.command(["init", "--repository-version", "2"])
            return {"repositoryId": archive.identity()}
    _perform("init", json_output, operation)


@app.command("check")
def cmd_check(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")):
    """Check archive structure and read back all stored data."""
    def operation():
        policy, state = _context()
        with locked(state):
            archive = Archive(policy, state)
            identity = archive.identity()
            archive.command(["check", "--read-data"])
            return {"repositoryId": identity, "verified": True}
    _perform("check", json_output, operation)


@app.command("gc")
def cmd_gc(
    older_than: str = typer.Option("30d", "--older-than", help="Nix generation retention."),
    yes: bool = typer.Option(False, "--yes", help="Confirm generation cleanup."),
    brew: bool = typer.Option(True, "--brew/--no-brew", help="Clean Homebrew on Darwin."),
):
    """Delete old Nix generations and collect caches; maintain --nix keeps generations."""
    log.warn("storage", "gc deletes old Nix generations; maintain --nix keeps generations")
    clean_generations(yes=yes, older_than=older_than, brew=brew)

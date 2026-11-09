"""One CLI for storage plans, quarantine, archives, recovery, and collection."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from envy import jsonio, log
from envy.evaluation import machine_manifest
from envy.storage import lifecycle, planner
from envy.storage.backend import Archive
from envy.storage.model import StorageError, policy_from_manifest
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
    failed = bool(data.get("errors"))
    if json_output:
        jsonio.emit(f"storage {command}", data=data, ok=not failed,
                    error={"code": "partial_failure", "message": "Some files were retained; inspect errors"}
                    if failed else None)
    else:
        if command == "plan" or "candidates" in data:
            table = Table(title=f"Storage plan {data.get('id', '')}")
            for name in ("Rule", "Action", "Bytes", "Path"):
                table.add_column(name)
            for item in data.get("candidates", []):
                table.add_row(item.get("rule", ""), item.get("action", "expire"),
                              str(item["size"]), item["path"])
            log.console.print(table)
            for entry in data.get("skipped", []):
                log.warn("storage", entry["reason"], path=entry["path"])
            if data.get("id"):
                log.hint(f"Execute this plan: envy storage run {data['id']} --yes")
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


@app.command("status")
def cmd_status(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")):
    """Show configured rules and durable archive/quarantine records."""
    def operation():
        policy, state = _context()
        with locked(state):
            return {"mode": policy.mode, "rules": [rule.id for rule in policy.rules],
                    "archiveConfigured": bool(policy.repository),
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
    """Collect old Nix generations and package-manager caches through their native tools."""
    clean_generations(yes=yes, older_than=older_than, brew=brew)

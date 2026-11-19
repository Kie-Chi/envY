"""One CLI for storage plans, quarantine, archives, recovery, and collection."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table
from rich.text import Text

from envy import jsonio, log
from envy.evaluation import machine_manifest
from envy.storage import contracts, inspection, lifecycle, maintenance, planner, pruning, receipts
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
            jsonio.emit_error(f"storage {command}", str(exc), code=getattr(exc, "code", "storage_error"))
        else:
            log.error("storage", str(exc))
        raise typer.Exit(1) from exc
    failed = bool(data.get("errors")) or bool(data.get("unknown")) or data.get("ok") is False or data.get("state") in {"partial", "unknown"}
    if json_output:
        jsonio.emit(f"storage {command}", data=data, ok=not failed,
                    error={"code": "unknown_operation" if data.get("state") == "unknown" else "partial_failure", "message": "Storage operation failed; inspect rule results and errors"}
                    if failed else None, warnings=data.get("warnings", []))
    else:
        if data.get("contractVersion") == 1 and data.get("kind") in {"preview", "result"}:
            log.console.print(f"Storage {data['operation']}: {data['state']}", markup=False)
            log.console.print("Request: " + str(data["request"]), markup=False)
            jsonio.write_document(data["preview"] if data["kind"] == "preview" else data["result"])
            if data["kind"] == "preview":
                log.console.print("Preview token: " + data["previewToken"], markup=False)
                log.console.print("Expires at: " + str(data["expiresAt"]), markup=False)
                log.hint("Confirm the same request with --preview-token TOKEN --yes; cancellation runs nothing.")
            for item in [*data["errors"], *data["unknown"]]:
                log.console.print(str(item), markup=False)
        elif command != "prune" and (command == "plan" or "candidates" in data):
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
        elif command == "inspect":
            table = Table(title="Storage inspection (report only)")
            for name in ("Item", "Allocated GiB", "Logical GiB", "Status", "Note"):
                table.add_column(name)
            for entry in data.get("items", []):
                def amount(key):
                    value = entry.get(key)
                    return "unknown" if value is None else f"{value / 2**30:.3f}"
                table.add_row(entry["id"], amount("allocatedBytes"), amount("logicalBytes"),
                              entry["status"], entry["note"])
            log.console.print(table)
            native = data.get("reports", {}).get("docker", {})
            if native:
                native_table = Table(title="Docker (internal objects; rows are not additive)")
                for name in ("Type", "Count", "Size GiB", "Native reclaimable GiB", "Policy estimate GiB"):
                    native_table.add_column(name)
                for name in ("images", "buildCache", "volumes", "containers"):
                    group = native.get(name, {})
                    def native_amount(key):
                        value = group.get(key)
                        return "unknown" if value is None else f"{value / 2**30:.3f}"
                    native_table.add_row(name, str(group.get("count", "unknown")),
                                         native_amount("logicalBytes"), native_amount("nativeReclaimableBytes"),
                                         native_amount("policyEligibleBytes"))
                log.console.print(native_table)
                log.console.print("Docker status: " + native.get("status", "unknown"), markup=False)
                if native.get("reason"):
                    log.console.print(str(native["reason"]), markup=False)
            applications = data.get("reports", {}).get("applications", {}).get("reports", {})
            if applications:
                codex = applications.get("codex", {})
                current = codex.get("current") or {}
                log.console.print("Codex current: " + str(current.get("version") or "unknown")
                                  + "; rollback: " + str(codex.get("rollbackVersion") or "unknown")
                                  + "; process coverage: " + codex.get("processStatus", "unknown"), markup=False)
                vscode = applications.get("vscode", {})
                log.console.print("VS Code updater running: " + str(vscode.get("updaterRunning"))
                                  + "; update completion: " + vscode.get("completionStatus", "unknown"), markup=False)
            for project in data.get("reports", {}).get("projects", {}).get("projects", []):
                log.console.print("Project: " + str(project.get("root") or project.get("requestedPath"))
                                  + "; status: " + project.get("status", "unknown"), markup=False)
            log.hint("Full paths, ownership and activity evidence: envy storage inspect --json")
            for warning in data.get("warnings", []):
                log.warn("storage", warning)
        elif command == "prune":
            _show_prune(data)
        elif command == "maintain":
            table = Table(title="Storage maintenance")
            for name in ("Action", "State", "Commands", "Note"):
                table.add_column(name)
            for item in data.get("items", []):
                outcome = item.get("status") or (
                    "completed" if item.get("available") and data.get("execute")
                    else "available" if item.get("available")
                    else "failed" if item.get("output") else "unavailable")
                table.add_row(item.get("action", ""), outcome,
                              " ; ".join(" ".join(command) for command in item.get("commands", [])),
                              item.get("reason") or item.get("note", ""))
            log.console.print(table)
            for item in data.get("items", []):
                native = item.get("inspection")
                if native:
                    log.console.print("Docker report status: " + native.get("status", "unknown"), markup=False)
                    for group_name in ("images", "buildCache"):
                        group = native.get(group_name, {})
                        eligible = group.get("policyEligibleBytes")
                        amount = "unknown" if eligible is None else f"{eligible / 2**30:.3f} GiB estimate"
                        log.console.print(group_name + " policy eligible: " + amount, markup=False)
            if not data.get("execute"):
                log.hint("Execute selected actions with: envy storage maintain --yes --nix/--docker/--go/--go-modcache/--uv/--npm/--pip/--brew")
        else:
            jsonio.write_document(data)
    if failed:
        raise typer.Exit(1)
    return data


def _preview_mode(preview: bool, token: str | None, yes: bool) -> bool:
    if preview and (yes or token is not None):
        raise StorageError("--preview cannot be combined with --yes or --preview-token", code="invalid_preview")
    if token is not None and not yes:
        raise StorageError("--preview-token requires --yes", code="invalid_preview")
    return preview or token is not None


def _approved_flow(operation: str, policy, state, selected: dict, preview: bool, token: str | None):
    return (receipts.preview(operation, policy, state, selected) if preview
            else receipts.confirm(operation, policy, state, selected, token))


@app.command("capabilities")
def cmd_capabilities(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")):
    """Describe commands and maintenance policy without scanning or probing tools."""
    _perform("capabilities", json_output, lambda: contracts.capabilities(machine_manifest()))


@app.command("inspect")
def cmd_inspect(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    project: list[Path] = typer.Option([], "--project", help="Inspect generated artifacts in this explicit Git project."),
):
    """Inspect known local storage hotspots without plans, cleanup or service startup."""
    _perform("inspect", json_output, lambda: inspection.inspect(Path.home(), projects=project))


def _prune_interactive() -> bool:
    return sys.stdin.isatty()


def _native_target(item: dict) -> str:
    target = item.get("target") or item.get("binding", {}).get("target") or {}
    if not isinstance(target, dict):
        return str(target)
    for key in ("resolvedPath", "path", "storeDir", "endpoint", "cache", "prefix"):
        if isinstance(target.get(key), str) and target[key]:
            return target[key]
    return "—"


def _native_effect(action: str) -> str:
    return {
        "nix": "Collect unreferenced store paths",
        "docker": "Prune unused build cache and dangling images older than 336h",
        "go": "Delete all Go build cache (no age cutoff)",
        "go-modcache": "Delete all module downloads (no age cutoff); future builds may download again",
        "uv": "Prune unreachable cache objects",
        "npm": "Verify and garbage-collect cache (writes cache)",
        "pip": "Delete all pip cache (no age cutoff)",
        "brew": "Remove old installed kegs, stale locks and downloads; cache cutoff 30d",
    }.get(action, "Native maintenance")


def _show_prune(data: dict) -> None:
    if data.get("cancelled"):
        log.console.print("Prune cancelled; no cleanup was performed.")
        return
    if data.get("execute"):
        summary = data["summary"]
        counts = [("deleted", summary["deletedItems"]),
                  ("archived and released", summary["archivedItems"]),
                  ("skipped", summary["skippedItems"]), ("retained", summary["retainedItems"])]
        log.console.print("Prune: " + (", ".join(f"{count} {name}" for name, count in counts if count)
                                      or "no files changed") + ".", markup=False)
        deleted = data.get("deletedBytesEstimate", 0)
        if deleted:
            log.console.print(f"Deleted size estimate: {deleted / 2**20:.2f} MiB")
        native_items = data.get("maintenance", {}).get("items", [])
        native_errors = {entry["action"]: entry.get("reason", "Cleanup failed")
                         for entry in data.get("errors", []) if entry.get("action")}
        shown_errors = set()
        shown_logs = {}
        for item in native_items:
            action = item["action"]
            outcome = item.get("status")
            if data.get("maintenanceState") in {"unknown", "running"} and outcome in {None, "pending", "running"}:
                outcome = "skipped" if outcome == "pending" else "unknown"
            outcome = outcome or ("failed" if action in native_errors
                                   else "completed" if item.get("available") else "unavailable")
            line = action + ": " + outcome
            removed = item.get("reportedRemovedItems")
            if removed is not None:
                line += f"; {removed} removed"
            reclaimed = item.get("reportedReclaimedBytes")
            if reclaimed is not None:
                size = f"{reclaimed} bytes" if reclaimed < 2**20 else f"{reclaimed / 2**20:.2f} MiB"
                line += f"; {size} reported"
            reported_summary = item.get("reportedSummary")
            if reported_summary:
                line += "; " + reported_summary
            reason = item.get("reason") or native_errors.get(action)
            if reason:
                if reason != reported_summary:
                    line += "; " + reason
                shown_errors.add(action)
            log.console.print(line, markup=False, highlight=False)
            for output in item.get("output", []):
                logs = output.get("logs") or {}
                for channel in ("stdout", "stderr"):
                    path = logs.get(channel + "Path")
                    if path:
                        shown_logs[path] = shown_logs.get(path, False) or channel in logs.get("truncatedStreams", [])
                if logs.get("logError"):
                    log.console.print(f"{action} output: {logs["logError"]}", markup=False, highlight=False)
                elif logs.get("outputErrors"):
                    log.console.print(f"{action} output: incomplete ({", ".join(logs["outputErrors"])})",
                                      markup=False, highlight=False)
        parents = {str(Path(path).parent) for path in shown_logs}
        if len(parents) == 1:
            log.console.print("Logs: " + next(iter(parents)), markup=False, highlight=False)
            truncated = [Path(path).name for path, cut in shown_logs.items() if cut]
            if truncated:
                log.console.print("Truncated logs: " + ", ".join(truncated), markup=False, highlight=False)
        else:
            for path, cut in shown_logs.items():
                log.console.print("Log: " + path + (" (truncated)" if cut else ""), markup=False, highlight=False)
        for entry in data.get("retained", []):
            log.console.print("Retained staged data: " + entry["heldPath"], markup=False)
        for entry in data.get("skipped", []):
            log.console.print(entry.get("path", "") + ": " + entry.get("reason", "skipped"), markup=False)
        for entry in data.get("errors", []):
            if entry.get("action") not in shown_errors:
                reason = entry.get("reason", "Cleanup failed")
                if entry.get("path"):
                    reason = entry["path"] + ": " + reason
                log.error("prune", reason)
            if entry.get("heldPath"):
                log.console.print("Staging path: " + entry["heldPath"], markup=False)
        return
    table = Table(title="Prune current storage")
    for column in ("Rule / action", "Operation", "MiB estimate", "Target", "Effect / status"):
        table.add_column(column)
    for candidate in data["candidates"]:
        table.add_row(candidate["rule"], "archive" if candidate["action"] == "archive" else "delete",
                      f"{candidate['size'] / 2**20:.2f}", Text(candidate["path"]),
                      Text(candidate.get("blocked") or ""))
    native_items = data["maintenance"]["items"]
    for item in native_items:
        effect = _native_effect(item["action"])
        if item.get("reason"):
            effect += "; " + item["reason"]
        table.add_row(item["action"], "native", "", Text(_native_target(item)), Text(effect))
    log.console.print(table)
    log.console.print("Eligible files/directories: " + str(data["summary"]["eligibleItems"])
                      + "; skipped: " + str(len(data["skipped"])), markup=False)
    log.console.print("Native maintenance actions: " + str(len(native_items)), markup=False)
    for warning in data["warnings"]:
        log.console.print(warning, markup=False)


@app.command("prune")
def cmd_prune(
    yes: bool = typer.Option(False, "--yes", "-f", "--force", help="Skip the single confirmation prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scan and show the scope without cleaning."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON; execution also requires --yes."),
):
    """Scan disposable caches and archive rules, then reclaim space immediately."""
    def operation():
        if dry_run and yes:
            raise StorageError("--dry-run cannot be combined with --yes/-f/--force", code="invalid_prune")
        if not dry_run and not yes and (json_output or not _prune_interactive()):
            raise StorageError("Use --yes to execute noninteractively, or --dry-run to inspect", code="confirmation_required")
        policy, state = _context()
        with locked(state):
            prepared = pruning.prepare(policy, state)
            if dry_run:
                return prepared
            if not yes:
                _show_prune(prepared)
                if not typer.confirm("Immediately delete the listed caches, archive and release selected files, "
                                     "and run native maintenance?",
                                     default=False):
                    return {**prepared, "cancelled": True}
            started = set()

            def on_output(action, channel, text):
                if not text:
                    return
                if channel == "start":
                    started.add(action)
                    log.diagnostic_console.print(text + ":", markup=False, highlight=False)
                    return
                if action not in started:
                    started.add(action)
                    log.diagnostic_console.print(action + ":", markup=False, highlight=False)
                log.diagnostic_console.print(text, end="", markup=False, highlight=False)

            return pruning.execute(policy, state, prepared, on_output=on_output)
    _perform("prune", json_output, operation)


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
    preview: bool = typer.Option(False, "--preview", help="Create a bound preview for an explicitly saved plan."),
    preview_token: Optional[str] = typer.Option(None, "--preview-token", help="Confirm this preview token with --yes."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Preview by default; execute an approved plan or the configured scheduled policy."""
    def operation():
        modern = _preview_mode(preview, preview_token, yes)
        if scheduled and modern:
            raise StorageError("Scheduled runs do not accept preview flags", code="invalid_preview")
        policy, state = _context()
        with locked(state):
            if modern:
                selected = receipts.request("run", plan_id=plan_id)
                return _approved_flow("run", policy, state, selected, preview, preview_token)
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
                    "runs": records(state, "runs"), "prunes": records(state, "prunes"),
                    "operations": receipts.summaries(state)}
    _perform("status", json_output, operation)


@app.command("purge")
def cmd_purge(
    yes: bool = typer.Option(False, "--yes", help="Delete expired, unchanged quarantine files."),
    preview: bool = typer.Option(False, "--preview", help="Create a bound preview of quarantine deletion."),
    preview_token: Optional[str] = typer.Option(None, "--preview-token", help="Confirm this preview token with --yes."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Preview or release expired quarantine files; archive snapshots are retained."""
    def operation():
        modern = _preview_mode(preview, preview_token, yes)
        policy, state = _context()
        with locked(state):
            if modern:
                return _approved_flow("purge", policy, state, receipts.request("purge"), preview, preview_token)
            return lifecycle.purge(policy, state, execute=yes)
    _perform("purge", json_output, operation)


@app.command("restore")
def cmd_restore(
    run_id: str = typer.Argument(help="Storage run ID from status."),
    target: Path = typer.Option(..., "--target", help="New directory for recovered files."),
    preview: bool = typer.Option(False, "--preview", help="Read recovery metadata without creating the target."),
    preview_token: Optional[str] = typer.Option(None, "--preview-token", help="Confirm this preview token with --yes."),
    yes: bool = typer.Option(False, "--yes", help="Confirm recovery with --preview-token."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Recover a run into a new directory, preserving original path structure."""
    def operation():
        modern = _preview_mode(preview, preview_token, yes)
        if yes and not preview_token:
            raise StorageError("Restore --yes requires --preview-token", code="invalid_preview")
        policy, state = _context()
        with locked(state):
            if modern:
                selected = receipts.request("restore", run_id=run_id, target=target)
                return _approved_flow("restore", policy, state, selected, preview, preview_token)
            return lifecycle.restore(policy, state, run_id, target)
    _perform("restore", json_output, operation)


@app.command("maintain")
def cmd_maintain(
    nix: bool = typer.Option(False, "--nix", help="Collect unreferenced Nix store paths, keeping generations."),
    docker: bool = typer.Option(False, "--docker", help="Prune Docker build cache and dangling images older than 336h."),
    go: bool = typer.Option(False, "--go", help="Clean the Go build cache."),
    go_modcache: bool = typer.Option(False, "--go-modcache", help="Remove all Go module downloads; no age cutoff, requires policy opt-in."),
    uv: bool = typer.Option(False, "--uv", help="Maintain the uv cache."),
    npm: bool = typer.Option(False, "--npm", help="Maintain the npm cache."),
    pip: bool = typer.Option(False, "--pip", help="Maintain the pip cache."),
    brew: bool = typer.Option(False, "--brew", help="Maintain Homebrew caches."),
    yes: bool = typer.Option(False, "--yes", help="Execute instead of previewing."),
    preview: bool = typer.Option(False, "--preview", help="Create a bound preview; explicit action flags are required."),
    preview_token: Optional[str] = typer.Option(None, "--preview-token", help="Confirm this preview token with --yes."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
):
    """Preview or run explicit native maintenance; never part of the schedule."""
    def operation():
        modern = _preview_mode(preview, preview_token, yes)
        policy, state = _context()
        flags = {"nix": nix, "docker": docker, "go": go, "go-modcache": go_modcache,
                 "uv": uv, "npm": npm, "pip": pip, "brew": brew}
        actions = [name for name in maintenance.ACTIONS if flags[name]]
        with locked(state):
            if modern:
                selected = receipts.request("maintain", actions=actions)
                return _approved_flow("maintain", policy, state, selected, preview, preview_token)
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

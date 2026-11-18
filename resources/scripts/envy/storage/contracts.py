"""Frontend storage capabilities, independent of scans and execution previews."""

from __future__ import annotations

import re

from envy.storage import maintenance
from envy.storage.model import StorageError


CONTRACT_VERSION = 1


def _flag(name: str, effect: str) -> dict:
    return {"flag": name, "effect": effect}


def _command(name: str, *, effect: str, preview: bool = False,
             flags: tuple[dict, ...] = (), arguments: tuple[str, ...] = (),
             json_output: bool = True, tui: str = "native", internal: bool = False) -> dict:
    return {"id": name, "argv": ["storage", name], "json": json_output,
            "tui": tui, "internal": internal, "preview": preview,
            "sideEffects": effect, "arguments": list(arguments), "flags": list(flags)}


def _archive_target(raw: dict) -> dict:
    archive = raw.get("archive", {})
    repository = archive.get("repository") if isinstance(archive, dict) else None
    safe = isinstance(repository, str) and (bool(re.fullmatch(r"sftp:[A-Za-z0-9_.@-]+:/[^\s\x00]*", repository))
                                           or (repository.startswith("/") and not re.search(r"[\x00-\x1f\x7f]|://", repository)))
    return {"kind": "archive", "label": "Dedicated restic archive", "status": "set" if safe else "unknown",
            "configured": bool(repository), **({"location": repository} if safe else {
                "reason": "No safe public archive location is declared; inspect machine policy in the CLI."})}


def capabilities(manifest: dict) -> dict:
    """Read only maintenance booleans; never expand rules or expose archive values."""
    if not isinstance(manifest, dict):
        raise StorageError("Machine manifest unavailable; run envy host check")
    raw = manifest.get("storage", {})
    if not isinstance(raw, dict):
        raise StorageError("Invalid manifest storage policy")
    opt_ins = raw.get("maintenance", {})
    if not isinstance(opt_ins, dict) or any(
        type(opt_ins.get(name, False)) is not bool for name in maintenance.ACTIONS
    ):
        raise StorageError("Storage maintenance flags must be booleans")
    enabled = tuple(name for name in maintenance.ACTIONS if opt_ins.get(name, False))
    actions = maintenance.action_metadata(enabled)
    commands = [
        _command("capabilities", effect="Read evaluated maintenance policy only; no scan or tool probe."),
        _command("inspect", effect="Read local storage and native reports; no plan or cleanup.",
                 flags=(_flag("--project", "Repeatable explicit Git project to inspect."),)),
        _command("status", effect="Read rules and saved records; may initialize and lock local state."),
        _command("plan", effect="Scan candidates and save a plan in local state; no move or deletion."),
        _command("run", preview=True, arguments=("plan_id?",),
                 effect="Without --yes, read or save a plan; execution may quarantine or archive files.",
                 flags=(_flag("--yes", "Execute the saved or freshly created plan on the legacy path."),
                        _flag("--preview", "Create a bound preview of an explicitly saved, trusted plan; never create a plan implicitly."),
                        _flag("--preview-token", "Confirm the same request with --yes before expiry; one claim only."),
                        _flag("--scheduled", "Internal scheduler only: use machine mode; reject plan_id and --yes. "
                              "Auto mode executes and purges; report mode saves a plan; manual mode rejects."))),
        _command("prune", tui="CLIhandoff",
                 effect="Scan current disposable caches and archive rules; confirm once to delete immediately and run policy-enabled native maintenance.",
                 flags=(_flag("--yes/--force", "Skip confirmation; -f is the same alias. Keep age, path and activity checks."),
                        _flag("--dry-run", "Scan without cleanup; cannot be combined with --yes or --force."))),
        _command("purge", preview=True, effect="Preview expired quarantine; execution deletes unchanged expired files.",
                 flags=(_flag("--yes", "Execute quarantine deletion; retain archive snapshots."),
                        _flag("--preview", "Save a bound, metadata-only preview of selected expired runs."),
                        _flag("--preview-token", "Confirm the same request with --yes before expiry."))),
        _command("restore", preview=True, arguments=("run_id",),
                 effect="Immediately write recovered files on the legacy path; --preview may perform read-only archive verification and never creates the recovery target.",
                 flags=(_flag("--target", "Required new absolute recovery directory; never overwrite an existing target."),
                        _flag("--preview", "Save a bound recovery preview without creating the target; may perform read-only archive identity/content verification, require credentials and take substantial time."),
                        _flag("--preview-token", "Confirm the same run and target with --yes before expiry."),
                        _flag("--yes", "Confirm only with --preview-token."))),
        _command("maintain", preview=True,
                 effect="Preview native actions; --yes executes policy-enabled maintenance. Never scheduled.",
                 flags=tuple(_flag(action["flag"], "Select " + action["id"] + "; requires policy opt-in.")
                             for action in actions) + (_flag("--yes", "Execute selected native actions."),
                                                       _flag("--preview", "Save a bound native target preview; require explicit nonempty action flags."),
                                                       _flag("--preview-token", "Confirm the same explicit action selection with --yes."))),
        _command("validate", effect="Run organize check on generated rule configuration; no scan or writes."),
        _command("init", effect="Initialize the dedicated archive repository and local state.", tui="CLIhandoff"),
        _command("check", effect="Read back all archive data; may initialize and lock local state.", tui="CLIhandoff"),
        _command("gc", json_output=False, tui="CLIhandoff",
                 effect="Delete old Nix generations and collect caches; maintain --nix retains generations.",
                 flags=(_flag("--older-than", "Generation retention; default 30d."),
                        _flag("--yes", "Confirm destructive cleanup; otherwise prompt interactively."),
                        _flag("--brew/--no-brew", "Darwin Homebrew cleanup; enabled by default."))),
    ]
    for command in commands:
        if command["json"]:
            command["flags"].append(_flag("--json", "Emit one envY JSON envelope."))
        if command["tui"] == "CLIhandoff":
            name = command["id"]
            if name == "prune":
                command["handoff"] = {
                    "argv": ["envy", "storage", "prune"],
                    "target": {"kind": "disposable-caches-and-archives", "label": "Current machine prune policy", "status": "set"},
                    "backend": {"program": "envy", "fixedArguments": ["storage", "prune"]},
                    "exit": {"success": 0, "failure": "nonzero", "return": "Return to storage and refresh explicitly."},
                    "sideEffects": command["sideEffects"], "interactive": True,
                    "confirmation": "One CLI prompt or explicit --yes/-f/--force"}
                continue
            command["handoff"] = {
                "argv": ["envy", "storage", name, *(["--older-than", "30d", "--brew"] if name == "gc" else ["--json"])],
                "target": {"kind": "nix-generations-and-caches", "label": "Local Nix generations and caches", "status": "set"} if name == "gc" else _archive_target(raw),
                "backend": {"program": "nix-collect-garbage" if name == "gc" else "restic",
                            "fixedArguments": ["--delete-older-than", "30d"] if name == "gc" else
                            ["init", "--repository-version", "2"] if name == "init" else ["check", "--read-data"]},
                "exit": {"success": 0, "failure": "nonzero", "return": "Return to storage after the process exits; refresh explicitly."},
                "sideEffects": command["sideEffects"],
                "interactive": name == "gc", "confirmation": "CLI prompt or explicit --yes" if name == "gc" else "Explicit CLI invocation"}
    return {"contractVersion": CONTRACT_VERSION, "actions": actions, "commands": commands,
            "tuiPlan": "Native preview/confirmation for run, purge, maintain and restore; init/check/gc use CLI handoff.",
            "operations": {"contractVersion": 1, "previewFlag": "--preview", "tokenFlag": "--preview-token",
                           "confirmFlag": "--yes", "previewTTLSeconds": 300,
                           "previewExcludes": ["--yes", "--preview-token"], "implicitPlan": False},
            "maintenanceSelection": {
                "cliDefault": "All policy-enabled actions when no action flags are supplied.",
                "frontendDefault": [], "emptySelection": "reject",
                "executeFlag": "--yes", "actionOrder": list(maintenance.ACTIONS)},
            "internalInvocations": [{"argv": ["storage", "run", "--scheduled"],
                                     "tui": "none", "internal": True}],
            "modes": ["manual", "report", "auto"]}


def maintenance_argv(selected: list[str], *, execute: bool = False) -> list[str]:
    """Build explicit frontend argv; an empty selection must never use CLI defaults."""
    if not selected:
        raise StorageError("Select at least one maintenance action")
    if any(name not in maintenance.ACTIONS for name in selected):
        raise StorageError("Unknown maintenance action selection")
    flags = ["--" + name for name in dict.fromkeys(selected)]
    return ["storage", "maintain", *flags, *(["--yes"] if execute else []), "--json"]

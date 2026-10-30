"""Manage stable personal interaction habits through machine policy."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import typer
from rich.table import Table

from envy import log
from envy.config.machine_nix import machine_config_file
from envy.config.updates import set_config_value
from envy.evaluation import machine_manifest, manifest_software_groups
from envy.mutation import offer_mutation_commit
from envy.workflows.system import apply_configuration


app = typer.Typer(
    name="habit",
    help="Manage machine-policy interaction habits and their implementations.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


@dataclass(frozen=True)
class Requirement:
    group: str
    item: str

    def to_dict(self) -> dict[str, str]:
        return {"group": self.group, "item": self.item}


@dataclass(frozen=True)
class Implementation:
    context: str
    backend: str
    binding: str
    ownership: str
    note: str
    requirements: tuple[Requirement, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "context": self.context,
            "backend": self.backend,
            "binding": self.binding,
            "ownership": self.ownership,
            "note": self.note,
            "requirements": [item.to_dict() for item in self.requirements],
        }


@dataclass(frozen=True)
class Habit:
    id: str
    label: str
    gesture: str
    semantic: str
    implementations: tuple[Implementation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "gesture": self.gesture,
            "semantic": self.semantic,
            "implementations": [item.to_dict() for item in self.implementations],
        }


@dataclass(frozen=True)
class HabitCheck:
    habit: str
    context: str
    status: str
    message: str
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "habit": self.habit,
            "context": self.context,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


_ENTRY_KEYS = (
    "id",
    "label",
    "gesture",
    "semantic",
    "context",
    "backend",
    "binding",
    "ownership",
)
_OWNERSHIP = {"declarative", "application"}
_TERMINAL_SCRATCHPAD_GESTURES = {
    "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F12",
}
_GLOBAL_LAUNCHER_NAMED_KEYS = {
    "space": "Space",
    "return": "Return",
    "tab": "Tab",
    "escape": "Escape",
}
_HABIT_POLICY_PATHS = {
    "terminal-scratchpad": "envy.habits.terminalScratchpad.gesture",
    "global-launcher": "envy.habits.globalLauncher.gesture",
}


def complete_habit_ids(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete the stable habit IDs owned by the machine policy schema."""
    del ctx
    descriptions = {
        "terminal-scratchpad": "toggle the terminal scratchpad",
        "global-launcher": "open the global launcher",
    }
    return [
        (habit_id, descriptions.get(habit_id, "managed habit"))
        for habit_id in sorted(_HABIT_POLICY_PATHS)
        if habit_id.startswith(incomplete)
    ]


def complete_habit_gestures(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete safe gesture values for the selected habit."""
    params = getattr(ctx, "params", {}) if ctx is not None else {}
    habit_id = params.get("habit_id") if isinstance(params, dict) else None
    if habit_id == "terminal-scratchpad":
        values = sorted(_TERMINAL_SCRATCHPAD_GESTURES)
    elif habit_id == "global-launcher":
        values = [
            "Option+Space", "Option+Return", "Option+Tab", "Option+Escape",
            *[f"Option+F{number}" for number in range(1, 13)],
        ]
    else:
        values = []
    return [(value, "managed gesture") for value in values if value.startswith(incomplete)]


def normalize_policy_gesture(habit_id: str, gesture: str) -> tuple[str, str]:
    """Return the managed machine-policy path and canonical gesture."""
    path = _HABIT_POLICY_PATHS.get(habit_id)
    if path is None:
        supported = ", ".join(sorted(_HABIT_POLICY_PATHS))
        raise typer.BadParameter(f"unknown habit '{habit_id}'; supported habits: {supported}")

    if habit_id == "terminal-scratchpad":
        canonical = gesture.strip().upper()
        if canonical not in _TERMINAL_SCRATCHPAD_GESTURES:
            allowed = ", ".join(sorted(_TERMINAL_SCRATCHPAD_GESTURES))
            raise typer.BadParameter(f"terminal scratchpad gesture must be one of: {allowed}")
        return path, canonical

    compact = "".join(gesture.strip().split())
    modifier, separator, key = compact.partition("+")
    normalized_key = _GLOBAL_LAUNCHER_NAMED_KEYS.get(key.casefold())
    if normalized_key is None and re.fullmatch(r"f(?:[1-9]|1[0-2])", key, re.IGNORECASE):
        normalized_key = key.upper()
    if normalized_key is None and re.fullmatch(r"[A-Za-z0-9]", key):
        normalized_key = key.upper()
    if modifier.casefold() != "option" or separator != "+" or normalized_key is None:
        raise typer.BadParameter(
            "global launcher gesture must use Option+<key>, for example Option+Space"
        )
    return path, f"Option+{normalized_key}"


def habits_from_manifest(manifest: dict[str, Any] | None) -> tuple[list[Habit], list[str]]:
    """Normalize module-owned habit entries and collect contract errors."""
    if not isinstance(manifest, dict):
        return [], ["evaluated machine manifest is unavailable"]
    entries = manifest.get("habits", [])
    if not isinstance(entries, list):
        return [], ["manifest habits must be a list"]

    grouped: dict[str, list[tuple[dict[str, str], Implementation]]] = {}
    errors: list[str] = []
    for position, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict):
            errors.append(f"habit entry {position} is not an attribute set")
            continue
        values: dict[str, str] = {}
        missing = []
        for key in _ENTRY_KEYS:
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                missing.append(key)
            else:
                values[key] = value.strip()
        if missing:
            errors.append(f"habit entry {position} is missing: {', '.join(missing)}")
            continue
        if values["ownership"] not in _OWNERSHIP:
            errors.append(
                f"habit '{values['id']}'/{values['context']} has unsupported ownership "
                f"'{values['ownership']}'"
            )
            continue

        requirements: list[Requirement] = []
        raw_requirements = raw.get("requirements", [])
        if not isinstance(raw_requirements, list):
            errors.append(f"habit '{values['id']}'/{values['context']} requirements must be a list")
            continue
        malformed_requirement = False
        for requirement in raw_requirements:
            if not isinstance(requirement, dict):
                malformed_requirement = True
                break
            group = requirement.get("group")
            item = requirement.get("item")
            if not isinstance(group, str) or not group.strip() or not isinstance(item, str) or not item.strip():
                malformed_requirement = True
                break
            requirements.append(Requirement(group.strip(), item.strip()))
        if malformed_requirement:
            errors.append(f"habit '{values['id']}'/{values['context']} has an invalid software requirement")
            continue

        implementation = Implementation(
            context=values["context"],
            backend=values["backend"],
            binding=values["binding"],
            ownership=values["ownership"],
            note=str(raw.get("note", "")).strip(),
            requirements=tuple(requirements),
        )
        grouped.setdefault(values["id"], []).append((values, implementation))

    habits: list[Habit] = []
    for habit_id, implementations in grouped.items():
        first, _ = implementations[0]
        expected = (first["label"], first["gesture"], first["semantic"])
        contexts: set[str] = set()
        normalized: list[Implementation] = []
        for values, implementation in implementations:
            actual = (values["label"], values["gesture"], values["semantic"])
            if actual != expected:
                errors.append(
                    f"habit '{habit_id}' uses inconsistent label, gesture, or semantic text "
                    f"between contexts"
                )
                continue
            if implementation.context in contexts:
                errors.append(f"habit '{habit_id}' declares context '{implementation.context}' more than once")
                continue
            contexts.add(implementation.context)
            normalized.append(implementation)
        if normalized:
            habits.append(Habit(
                id=habit_id,
                label=first["label"],
                gesture=first["gesture"],
                semantic=first["semantic"],
                implementations=tuple(sorted(normalized, key=lambda value: value.context)),
            ))
    return sorted(habits, key=lambda value: value.id), errors


def _software_state(manifest: dict[str, Any]) -> dict[str, tuple[set[str], set[str]]]:
    state: dict[str, tuple[set[str], set[str]]] = {}
    for group_id, group in manifest_software_groups(manifest).items():
        selection = group.get("selection") if isinstance(group, dict) else None
        if not isinstance(selection, dict):
            continue
        effective = {
            str(item["id"])
            for item in selection.get("effective", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        excluded = {
            item for item in selection.get("exclude", []) if isinstance(item, str)
        }
        state[group_id] = (effective, excluded)
    return state


def check_habits(manifest: dict[str, Any] | None) -> tuple[list[HabitCheck], bool]:
    """Check contract consistency and selected software prerequisites."""
    habits, errors = habits_from_manifest(manifest)
    results = [
        HabitCheck("contract", "manifest", "error", error, {})
        for error in errors
    ]
    if not isinstance(manifest, dict):
        return results, True

    software = _software_state(manifest)
    for habit in habits:
        for implementation in habit.implementations:
            for requirement in implementation.requirements:
                effective, excluded = software.get(requirement.group, (set(), set()))
                details = requirement.to_dict()
                if requirement.item in effective:
                    results.append(HabitCheck(
                        habit.id,
                        implementation.context,
                        "ok",
                        f"required software is selected: {requirement.group}/{requirement.item}",
                        details,
                    ))
                elif requirement.item in excluded:
                    results.append(HabitCheck(
                        habit.id,
                        implementation.context,
                        "error",
                        f"required software is excluded by machine policy: {requirement.group}/{requirement.item}",
                        details,
                    ))
                else:
                    results.append(HabitCheck(
                        habit.id,
                        implementation.context,
                        "error",
                        f"required software is not effective: {requirement.group}/{requirement.item}",
                        details,
                    ))
            if implementation.ownership == "application":
                results.append(HabitCheck(
                    habit.id,
                    implementation.context,
                    "info",
                    f"manual verification required: {implementation.backend} owns its expected binding ({implementation.binding}); envY does not read or overwrite it",
                    {"binding": implementation.binding, "backend": implementation.backend},
                ))
            else:
                results.append(HabitCheck(
                    habit.id,
                    implementation.context,
                    "ok",
                    f"Nix renders binding from the implementation source: {implementation.binding}",
                    {"binding": implementation.binding, "backend": implementation.backend},
                ))
    failed = any(result.status == "error" for result in results)
    return results, failed


def _manifest_or_exit(refresh: bool) -> dict[str, Any]:
    manifest = machine_manifest(refresh=refresh)
    if not isinstance(manifest, dict):
        log.error("habit", "evaluated habit contracts are unavailable")
        log.hint("Run: envy config check")
        raise typer.Exit(code=1)
    return manifest


def _payload(manifest: dict[str, Any], habits: list[Habit], errors: list[str]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "machine": manifest.get("id", "current"),
        "platform": manifest.get("platform"),
        "habits": [habit.to_dict() for habit in habits],
        "contractErrors": errors,
    }


def _render_habit_list(habits: list[Habit], errors: list[str], machine: object) -> None:
    table = Table(title=f"Habit contracts - {machine}")
    table.add_column("Gesture", style="cyan", no_wrap=True)
    table.add_column("Habit")
    table.add_column("Contexts")
    table.add_column("Semantic")
    for habit in habits:
        contexts = ", ".join(item.context for item in habit.implementations)
        table.add_row(habit.gesture, habit.label, contexts, habit.semantic)
    log.console.print(table)
    for error in errors:
        log.warn("habit", error)


def _render_habit(habit: Habit) -> None:
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="cyan", no_wrap=True)
    summary.add_column()
    summary.add_row("Gesture", habit.gesture)
    summary.add_row("Semantic", habit.semantic)
    log.console.print(summary)

    table = Table(title=habit.label)
    table.add_column("Context", style="cyan", no_wrap=True)
    table.add_column("Backend")
    table.add_column("Native binding")
    table.add_column("Ownership")
    table.add_column("Requirements")
    for implementation in habit.implementations:
        requirements = ", ".join(
            f"{item.group}/{item.item}" for item in implementation.requirements
        ) or "—"
        table.add_row(
            implementation.context,
            implementation.backend,
            implementation.binding,
            implementation.ownership,
            requirements,
        )
        if implementation.note:
            table.add_row("", f"[dim]{implementation.note}[/dim]", "", "", "")
    log.console.print(table)


def _render_checks(results: list[HabitCheck], machine: object) -> None:
    table = Table(title=f"Habit contract check - {machine}")
    table.add_column("State", no_wrap=True)
    table.add_column("Habit", style="cyan")
    table.add_column("Context")
    table.add_column("Result")
    styles = {"ok": "[green]OK[/green]", "info": "[blue]INFO[/blue]", "error": "[red]ERROR[/red]"}
    for result in results:
        table.add_row(
            styles.get(result.status, result.status.upper()),
            result.habit,
            result.context,
            result.message,
        )
    log.console.print(table)


@app.command(name="list")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_list(
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """List the selected machine's desired interaction habits."""
    manifest = _manifest_or_exit(refresh)
    habits, errors = habits_from_manifest(manifest)
    if json_output:
        log.console.print_json(json.dumps(_payload(manifest, habits, errors), ensure_ascii=False))
        return
    _render_habit_list(habits, errors, manifest.get("id", "current"))


@app.command(name="show")
def cmd_show(
    habit_id: str = typer.Argument(
        help="Stable habit ID, for example terminal-scratchpad.", autocompletion=complete_habit_ids,
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Show one interaction habit and each platform/session implementation."""
    manifest = _manifest_or_exit(refresh)
    habits, errors = habits_from_manifest(manifest)
    habit = next((item for item in habits if item.id == habit_id), None)
    if habit is None:
        log.error("habit", "habit contract was not declared", habit=habit_id)
        if errors:
            log.hint("The evaluated contract has errors; run: envy habit check")
        raise typer.Exit(code=1)
    if json_output:
        payload = _payload(manifest, [habit], errors)
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    _render_habit(habit)
    for error in errors:
        log.warn("habit", error)


@app.command(name="check")
def cmd_check(
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Validate declared habit contracts and their selected software prerequisites."""
    manifest = _manifest_or_exit(refresh)
    results, failed = check_habits(manifest)
    payload = {
        "schemaVersion": 1,
        "machine": manifest.get("id", "current"),
        "platform": manifest.get("platform"),
        "failed": sum(result.status == "error" for result in results),
        "results": [result.to_dict() for result in results],
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        _render_checks(results, manifest.get("id", "current"))
    if failed:
        raise typer.Exit(code=1)


@app.command(name="set")
def cmd_set(
    habit_id: str = typer.Argument(
        help="Habit ID, for example terminal-scratchpad.", autocompletion=complete_habit_ids,
    ),
    gesture: str = typer.Argument(
        help="Desired gesture, for example F12 or Option+Space.", autocompletion=complete_habit_gestures,
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply the repaired desktop configuration now."),
):
    """Write one desired habit gesture into the selected machine policy."""
    path, canonical = normalize_policy_gesture(habit_id, gesture)
    set_config_value(path, canonical)
    offer_mutation_commit(
        [machine_config_file()],
        f"chore(habit): set {habit_id} to {canonical}",
    )
    log.ok("habit", "desired gesture updated", habit=habit_id, gesture=canonical)
    if apply:
        log.step("habit", "applying the updated desktop configuration")
        apply_configuration()
    else:
        log.hint("Run: envy habit repair  # or add --apply to this command")


@app.command(name="repair")
def cmd_repair(
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache before applying."),
):
    """Apply the selected machine's desired Habit policy to its desktop backend."""
    manifest = _manifest_or_exit(refresh)
    results, failed = check_habits(manifest)
    if failed:
        _render_checks(results, manifest.get("id", "current"))
        log.error("habit", "cannot repair while a required implementation is unavailable")
        raise typer.Exit(code=1)
    log.step("habit", "applying desired habit policy", machine=manifest.get("id", "current"))
    apply_configuration()

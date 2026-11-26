"""Doctor orchestration and terminal rendering."""

from collections.abc import Callable, Iterable

from rich.table import Table
from rich.text import Text

from envy import log
from envy.jsonio import write_document
from envy.doctor.checks import agents, apps, config, habits, network, system
from envy.doctor.model import (
    SECTION_AGENTS,
    SECTION_AUTH,
    SECTION_CONFIG,
    SECTION_DOCTOR,
    SECTION_INSTALL,
    SECTION_PRIVACY,
    SECTION_RUNTIME,
    SECTION_SECRETS,
    SECTION_STATE,
    SECTION_SYNC,
    SECTION_SYSTEM,
    CheckResult,
    DoctorSection,
)
from envy.doctor.selection import DoctorSelection, filter_results, parse_only, selection_errors

CheckFn = Callable[[], list[CheckResult]]

CHECKS: dict[str, CheckFn] = {
    "config": config.run_checks,
    "system": system.run_checks,
    "apps": apps.run_checks,
    "network": network.run_checks,
    "agents": agents.run_checks,
    "habits": habits.run_checks,
}

DEFAULT_CHECKS = ["config", "system", "apps", "agents", "habits"]


CONFIG_RESULT_SECTIONS: set[DoctorSection] = {
    SECTION_DOCTOR,
    SECTION_CONFIG,
    SECTION_SECRETS,
}

APP_RESULT_SECTIONS: set[DoctorSection] = {
    SECTION_INSTALL,
    SECTION_RUNTIME,
    SECTION_STATE,
    SECTION_AUTH,
    SECTION_SYNC,
    SECTION_PRIVACY,
    SECTION_SYSTEM,
}


def run_sections(
    sections: list[str],
    selected: Iterable[str] | None = None,
    *,
    log_progress: bool = True,
) -> list[CheckResult]:
    selection = parse_only(selected)
    allow_apps = "apps" in sections
    errors = selection_errors(selection, allow_apps=allow_apps)
    if selection.has_parse_errors:
        return errors

    results: list[CheckResult] = []
    for section in sections:
        if not _scope_needed(section, selection):
            continue
        check = CHECKS[section]
        if log_progress:
            log.step("doctor", f"checking {section}")
        if section == "apps":
            results.extend(apps.run_checks(selection=selection))
        elif section == "habits":
            checked = check()
            if SECTION_STATE in selection.sections and SECTION_RUNTIME not in selection.sections:
                for result in checked:
                    result.section = SECTION_STATE
            results.extend(checked)
        else:
            results.extend(check())
    return errors + filter_results(results, selection)


def _scope_needed(section: str, selection: DoctorSelection) -> bool:
    if selection.apps and section != "apps":
        return False
    if not selection.sections:
        return True
    if section == "habits":
        return bool(selection.sections & {SECTION_RUNTIME, SECTION_STATE})
    if section == "agents":
        return SECTION_AGENTS in selection.sections
    if section == "config":
        return bool(selection.sections & CONFIG_RESULT_SECTIONS)
    if section == "apps":
        return bool(selection.sections & APP_RESULT_SECTIONS)
    if section in {"system", "network"}:
        return SECTION_SYSTEM in selection.sections
    return True


def render(results: list[CheckResult]) -> None:
    table = Table(title="envy doctor", expand=True)
    table.add_column("Status", width=6, no_wrap=True)
    table.add_column("Section", width=11, no_wrap=True)
    table.add_column("Check", min_width=18, max_width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("Result", min_width=24, no_wrap=True, overflow="ellipsis", ratio=1)

    style = {
        "ok": "green",
        "warn": "yellow",
        "error": "red",
        "info": "cyan",
    }

    for result in results:
        table.add_row(
            Text(result.status.upper(), style=style[result.status]),
            Text(result.section),
            Text(result.name),
            Text(result.message),
        )

    log.console.print(table)

    hints = [result for result in results if result.hint]
    if hints:
        log.console.print("[bold]Hints[/bold]")
        for result in hints:
            log.console.print(Text(f"- {result.section}/{result.name}: {result.hint}", style="dim"))

    actions: list[tuple[str, str]] = []
    for result in results:
        action = result.action
        if not isinstance(action, dict):
            continue
        kind = action.get("kind")
        label = f"{result.section}/{result.name}"
        if kind == "run" and isinstance(action.get("argv"), list):
            argv = [str(part) for part in action["argv"]]
            if argv == ["envy", "apply"]:
                actions.append((label, "envy apply"))
        elif kind == "open-app" and isinstance(action.get("name"), str):
            actions.append((label, f"open {action['name']}"))
        elif kind == "open-settings":
            actions.append((label, "open macOS Privacy & Security settings"))
    if actions:
        log.console.print("[bold]Suggested actions[/bold]")
        grouped: dict[str, list[str]] = {}
        for label, action in actions:
            grouped.setdefault(action, []).append(label)
        for action, labels in grouped.items():
            source = labels[0] if len(labels) == 1 else f"{len(labels)} checks"
            log.console.print(Text(f"- {action}  ({source})", style="dim"))

    errors = sum(1 for result in results if result.failed)
    warnings = sum(1 for result in results if result.warned)
    if errors:
        log.error("doctor", "completed with errors", errors=errors, warnings=warnings)
    elif warnings:
        log.warn("doctor", "completed with warnings", warnings=warnings)
    else:
        log.ok("doctor", "all checks passed")


def render_json(
    results: list[CheckResult], *, strict: bool = False, command: str | None = None,
) -> None:
    payload = {
        "schemaVersion": 1,
        "summary": {
            "ok": sum(result.status == "ok" for result in results),
            "warn": sum(result.status == "warn" for result in results),
            "error": sum(result.status == "error" for result in results),
            "info": sum(result.status == "info" for result in results),
            "exitCode": exit_code(results, strict=strict),
        },
        "results": [result.to_dict() for result in results],
    }
    if command is not None:
        failed = exit_code(results, strict=strict) != 0
        payload = {
            **payload, "command": command, "ok": not failed, "data": payload,
            "warnings": [result.message for result in results if result.warned],
            **({"error": {"code": "doctor-check-failed", "message": "Doctor checks failed"}} if failed else {}),
        }
    write_document(payload)


def exit_code(results: list[CheckResult], *, strict: bool = False) -> int:
    if any(result.failed for result in results):
        return 1
    if strict and any(result.warned for result in results):
        return 1
    return 0

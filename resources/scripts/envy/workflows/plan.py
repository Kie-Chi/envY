"""Build and compare a frozen selected machine without activating it."""
from __future__ import annotations

import typer
from rich.panel import Panel
from rich.table import Table

from envy import log
from envy.behavior import (REPORT_VERSION, ROLLBACK_BOUNDARY, activation_preview,
                           comparison_report, emit_report, section, semantic_snapshot)
from envy.journal import baseline_for_generation
from envy.provenance import SnapshotError, build_snapshot
from envy.utils import current_machine_id, platform_name
from envy.workflows.generations import closure_diff, current_generation


def plan_configuration(*, json_output: bool = False) -> None:
    machine = current_machine_id()
    payload = {"reportVersion": REPORT_VERSION, "machine": machine, "platform": platform_name(),
               "current": None, "target": None, "changed": None, "closureDiff": None, "source": None,
               "sections": {}, "activation": section("unavailable", "Preview did not complete", coverage="partial", actions=[]),
               "rollbackBoundary": ROLLBACK_BOUNDARY}
    if not json_output:
        log.step("plan", "building frozen selected machine without activation", machine=machine)
    try:
        source, manifest, target = build_snapshot(machine)
        payload.update(source=source, target=str(target))
        semantics = semantic_snapshot(manifest)
        payload["sections"] = comparison_report(None, semantics)
        payload["activation"] = activation_preview(semantics)
        current = current_generation()
        payload.update(current=current.to_dict() if current else None, changed=current is None or current.target != target)
        payload["sections"] = comparison_report(baseline_for_generation(current), semantics)
        if current is None:
            closure = section("unknown", "Current generation could not be identified")
        else:
            try:
                diff = closure_diff(current.target, target)
                payload["closureDiff"] = diff
                closure = section("compared", diff=diff, changed=bool(diff))
            except RuntimeError:
                closure = section("unavailable", "Nix closure comparison failed")
        payload["sections"]["closure"] = closure
    except (SnapshotError, OSError, RuntimeError, ValueError) as exc:
        payload.update(getattr(exc, "partial", {}))
        for name in ("closure", "settings", "software", "sources", "behavior"):
            payload["sections"].setdefault(name, section("unavailable", "Preview did not complete"))
        if json_output:
            emit_report("plan", payload, ok=False, error=str(exc))
        else:
            log.error("plan", "selected machine preview failed")
            log.hint(str(exc))
        raise typer.Exit(code=getattr(exc, "code", 1)) from exc
    if json_output:
        emit_report("plan", payload)
        return
    table = Table(title=f"envY plan - {machine}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Current", str(current.target) if current else "<unknown>")
    table.add_row("Target", str(target))
    table.add_row("Changed", "yes" if payload["changed"] else "no")
    for name, item in payload["sections"].items():
        table.add_row(name, f"{item['status']}: {item.get('reason') or ('changed' if item.get('changed') else 'unchanged')}")
    log.console.print(table)
    closure = payload["sections"]["closure"]
    text = (payload["closureDiff"] or "No Nix closure differences.") if closure["status"] == "compared" else closure["reason"]
    log.console.print(Panel(text, title="Closure diff", border_style="green" if closure["status"] == "compared" else "yellow"))
    for action in payload["activation"].get("actions", []):
        log.info("activation", action["summary"], manager=action["manager"],
                 preview=action["mergePreview"]["status"])
    log.hint(ROLLBACK_BOUNDARY)

"""Build and compare the selected machine without activating it."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from envy import log
from envy.process import run_process
from envy.utils import ENVY_ROOT, current_machine_id, machine_build_attr, platform_name
from envy.workflows.generations import closure_diff, current_generation


def plan_configuration(*, json_output: bool = False) -> None:
    machine = current_machine_id()
    attr = machine_build_attr(machine)
    if not json_output:
        log.step("plan", "building selected machine without activation", machine=machine)
    result = run_process(
        ["nix", "build", "--no-link", "--print-out-paths", "--impure", attr],
        cwd=ENVY_ROOT, capture=True, check=False,
        activity=f"build plan for {machine}",
    )
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        detail = lines[-1] if lines else "plan build failed"
        if json_output:
            log.console.print_json(json.dumps({
                "schemaVersion": 1, "machine": machine, "ok": False,
                "error": detail[:500],
            }, ensure_ascii=False))
        else:
            log.error("plan", "selected machine build failed")
            log.hint(detail[:500])
        raise typer.Exit(code=result.returncode or 1)
    outputs = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not outputs:
        log.error("plan", "Nix build returned no output path")
        raise typer.Exit(code=1)
    target = Path(outputs[-1])
    current = current_generation()
    diff = ""
    if current is not None:
        try:
            diff = closure_diff(current.target, target)
        except RuntimeError as exc:
            if not json_output:
                log.warn("plan", "closure diff is unavailable", reason=str(exc))
    payload = {
        "schemaVersion": 1,
        "machine": machine,
        "platform": platform_name(),
        "ok": True,
        "current": current.to_dict() if current is not None else None,
        "target": str(target),
        "changed": current is None or current.target != target,
        "closureDiff": diff,
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title=f"envY plan - {machine}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Current", str(current.target) if current is not None else "<unknown>")
    table.add_row("Target", str(target))
    table.add_row("Changed", "yes" if payload["changed"] else "no")
    log.console.print(table)
    log.console.print(Panel(
        diff or "No Nix closure differences.",
        title="Closure diff",
        border_style="yellow" if diff else "green",
    ))
    log.hint("This is a closure preview; activation scripts and runtime side effects are not fully simulated.")

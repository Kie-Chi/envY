"""One coherent, situation-aware snapshot for the CLI and TUI dashboard."""

from __future__ import annotations

import json
from typing import Any

from rich.panel import Panel
from rich.table import Table

from envy import log
from envy.config import read_machine_nix, read_secrets_yaml
from envy.doctor.runner import DEFAULT_CHECKS, exit_code, run_sections
from envy.evaluation import (
    last_manifest_error,
    machine_manifest,
    manifest_settings,
    manifest_software_groups,
)
from envy.process import run_process
from envy.schemas.config import MACHINE_FIELDS, SECRET_FIELDS
from envy.utils import (
    ENVY_ROOT,
    current_machine_id,
    platform_name,
    read_device_metadata,
)
from envy.workflows.generations import generations


def _entry_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item.get("id")) if isinstance(item, dict) else str(item)
        for item in value
        if (isinstance(item, str) or isinstance(item, dict) and item.get("id"))
    ]


def _config_snapshot(manifest: dict[str, Any] | None) -> dict[str, Any]:
    evaluated = manifest_settings(manifest)
    values = evaluated or read_machine_nix()
    device = read_device_metadata()
    secrets, _ = read_secrets_yaml()
    from envy.agent import config_view

    agent_view = config_view(manifest)
    return {
        "schemaVersion": 1,
        "source": "evaluated" if evaluated else "source-fallback",
        "platform": platform_name(),
        "device": {
            "machineId": device.get("machine_id", ""),
            "sopsLabel": device.get("sops_label", ""),
        },
        "values": {
            field.path: values.get(field.path, "") for field in MACHINE_FIELDS
        } | agent_view["values"],
        "fields": [
            {
                "path": field.path,
                "value": values.get(field.path, ""),
                "choices": list(field.choices),
            }
            for field in MACHINE_FIELDS
        ] + agent_view["fields"],
        "llm": agent_view["llm"],
        "agents": agent_view["agents"],
        "secrets": {
            field.yaml_path: bool(secrets.get(field.path, ""))
            for field in SECRET_FIELDS
        },
    }


def _software_snapshot(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if manifest is None:
        # Counts are unknown, not zero: a failed or timed-out evaluation must
        # not look like a machine without software.
        return {
            "schemaVersion": 1,
            "machine": current_machine_id(),
            "available": False,
            "error": last_manifest_error() or "machine manifest is unavailable",
            "groups": None,
            "included": None,
            "effective": None,
            "excluded": None,
        }
    groups = manifest_software_groups(manifest)
    included = 0
    effective = 0
    excluded = 0
    for group in groups.values():
        selection = group.get("selection") if isinstance(group, dict) else None
        if not isinstance(selection, dict):
            continue
        include_ids = set(_entry_ids(selection.get("include")))
        effective_ids = set(_entry_ids(selection.get("effective")))
        exclude_ids = set(_entry_ids(selection.get("exclude")))
        included += len(include_ids)
        effective += len(effective_ids)
        excluded += len((include_ids | exclude_ids) - effective_ids)
    return {
        "schemaVersion": 1,
        "machine": manifest.get("id", current_machine_id()),
        "available": True,
        "groups": len(groups),
        "included": included,
        "effective": effective,
        "excluded": excluded,
    }


def _git_snapshot() -> dict[str, Any]:
    branch_result = run_process(
        ["git", "branch", "--show-current"],
        cwd=ENVY_ROOT, capture=True, check=False,
    )
    status_result = run_process(
        ["git", "status", "--porcelain=v1"],
        cwd=ENVY_ROOT, capture=True, check=False,
    )
    branch = (branch_result.stdout or "").strip() or None
    changes = [line for line in (status_result.stdout or "").splitlines() if line]
    upstream_result = run_process(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=ENVY_ROOT, capture=True, check=False,
    )
    upstream = (upstream_result.stdout or "").strip() or None
    ahead = behind = None
    if upstream:
        counts = run_process(
            ["git", "rev-list", "--left-right", "--count", f"{upstream}...HEAD"],
            cwd=ENVY_ROOT, capture=True, check=False,
        )
        parts = (counts.stdout or "").split()
        if counts.returncode == 0 and len(parts) == 2 and all(part.isdigit() for part in parts):
            behind, ahead = (int(parts[0]), int(parts[1]))
    return {
        "branch": branch,
        "dirty": bool(changes),
        "changes": len(changes),
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
    }


def _generation_snapshot() -> dict[str, Any]:
    rows = generations()
    current = next((row for row in rows if row.current), None)
    return {
        "current": current.to_dict() if current is not None else None,
        "count": len(rows),
    }


def _doctor_snapshot() -> dict[str, Any]:
    results = run_sections(DEFAULT_CHECKS, log_progress=False)
    return {
        "schemaVersion": 1,
        "summary": {
            "ok": sum(result.status == "ok" for result in results),
            "warn": sum(result.status == "warn" for result in results),
            "error": sum(result.status == "error" for result in results),
            "info": sum(result.status == "info" for result in results),
            "exitCode": exit_code(results),
        },
        "results": [result.to_dict() for result in results],
    }


def _recommendations(payload: dict[str, Any]) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    doctor = payload["doctor"]["summary"]
    git = payload["git"]
    generation = payload["generation"]
    software = payload["software"]
    if not software.get("available", True):
        recommendations.append({
            "label": "Check machine evaluation",
            "command": "envy host check",
            "reason": f"machine manifest could not be evaluated: {software['error']}",
        })
    if doctor["error"]:
        recommendations.append({
            "label": "Inspect health errors",
            "command": "envy doctor",
            "reason": f"{doctor['error']} doctor error(s) need attention",
        })
    if git["dirty"]:
        recommendations.extend([
            {
                "label": "Preview configuration",
                "command": "envy plan",
                "reason": f"{git['changes']} repository path(s) changed",
            },
            {
                "label": "Apply selected machine",
                "command": "envy apply",
                "reason": "activate the reviewed declarative state",
            },
        ])
    if generation["current"] is None:
        recommendations.append({
            "label": "Bootstrap configuration",
            "command": "envy bootstrap",
            "reason": "no active envY generation was detected",
        })
    if doctor["warn"] and not doctor["error"]:
        recommendations.append({
            "label": "Review health warnings",
            "command": "envy doctor",
            "reason": f"{doctor['warn']} warning(s) are available",
        })
    if git.get("ahead"):
        recommendations.append({
            "label": "Push reviewed changes",
            "command": "envy push",
            "reason": f"{git['ahead']} commit(s) are ahead of {git.get('upstream')}",
        })
    if not recommendations:
        recommendations.append({
            "label": "No action required",
            "command": "",
            "reason": "the selected machine is healthy and the repository is clean",
        })
    return recommendations


def snapshot(*, refresh: bool = False) -> dict[str, Any]:
    manifest = machine_manifest(refresh=refresh)
    payload = {
        "schemaVersion": 1,
        "machine": current_machine_id(),
        "platform": platform_name(),
        "config": _config_snapshot(manifest),
        "software": _software_snapshot(manifest),
        "git": _git_snapshot(),
        "generation": _generation_snapshot(),
        "doctor": _doctor_snapshot(),
    }
    payload["recommendations"] = _recommendations(payload)
    return payload


def render(payload: dict[str, Any]) -> None:
    config = payload["config"]
    software = payload["software"]
    git = payload["git"]
    generation = payload["generation"]
    doctor = payload["doctor"]["summary"]
    current = generation.get("current") or {}

    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    table.add_row("Machine", payload["machine"])
    table.add_row("Platform", payload["platform"])
    table.add_row("Configuration", config["source"])
    table.add_row(
        "Repository",
        f"{git.get('branch') or '<detached>'} · "
        f"{'dirty (' + str(git['changes']) + ')' if git['dirty'] else 'clean'}",
    )
    sync = "no upstream"
    if git.get("upstream"):
        sync = f"{git['upstream']} · ahead {git.get('ahead', '?')} / behind {git.get('behind', '?')}"
    table.add_row("Git sync", sync)
    table.add_row(
        "Generation",
        str(current.get("number", "not detected")),
    )
    if software.get("available", True):
        software_text = (
            f"{software['effective']} effective · {software['excluded']} excluded · "
            f"{software['groups']} groups"
        )
    else:
        software_text = f"unavailable · {software['error']}"
    table.add_row("Software", software_text)
    table.add_row(
        "Doctor",
        f"{doctor['ok']} OK · {doctor['warn']} warnings · {doctor['error']} errors",
    )
    log.console.print(Panel(table, title="envY status", border_style="cyan"))

    log.console.print("[bold]Recommended next actions[/bold]")
    for index, recommendation in enumerate(payload["recommendations"], start=1):
        command = recommendation["command"]
        prefix = f"[cyan]{index}.[/cyan] "
        if command:
            log.console.print(f"{prefix}[bold]{command}[/bold]")
            log.console.print(f"   [dim]{recommendation['reason']}[/dim]")
        else:
            log.console.print(f"{prefix}{recommendation['reason']}")


def show(*, json_output: bool = False, refresh: bool = False) -> None:
    payload = snapshot(refresh=refresh)
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        render(payload)

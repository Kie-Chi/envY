"""Read-only generation inventory, closure diffs, and rollback previews."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import typer
from rich.table import Table

from envy import log
from envy.behavior import REPORT_VERSION, ROLLBACK_BOUNDARY, comparison_report, emit_report, section
from envy.journal import baseline_for_generation
from envy.process import run_process
from envy.utils import PLATFORM


@dataclass(frozen=True)
class Generation:
    number: int
    path: Path
    target: Path
    created_at: str
    current: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "path": str(self.path),
            "target": str(self.target),
            "createdAt": self.created_at,
            "current": self.current,
        }


def _profile_candidates() -> list[tuple[Path, str, Path]]:
    if PLATFORM == "darwin":
        return [(Path("/nix/var/nix/profiles"), "system", Path("/run/current-system"))]
    user = os.environ.get("USER", "")
    return [
        (Path.home() / ".local/state/nix/profiles", "home-manager", Path.home() / ".local/state/nix/profiles/home-manager"),
        (Path("/nix/var/nix/profiles/per-user") / user, "home-manager", Path("/nix/var/nix/profiles/per-user") / user / "home-manager"),
    ]


def generations() -> list[Generation]:
    for directory, prefix, current_link in _profile_candidates():
        paths = list(directory.glob(f"{prefix}-*-link")) if directory.exists() else []
        if not paths:
            continue
        try:
            current_target = current_link.resolve(strict=True)
        except OSError:
            current_target = None
        rows = []
        for path in paths:
            match = re.fullmatch(rf"{re.escape(prefix)}-(\d+)-link", path.name)
            if match is None:
                continue
            try:
                target = path.resolve(strict=True)
                created = datetime.fromtimestamp(path.lstat().st_mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                continue
            rows.append(Generation(
                number=int(match.group(1)),
                path=path,
                target=target,
                created_at=created,
                current=current_target == target,
            ))
        return sorted(rows, key=lambda row: row.number, reverse=True)
    return []


def require_generation(number: int) -> Generation:
    match = next((row for row in generations() if row.number == number), None)
    if match is None:
        raise typer.BadParameter(f"generation does not exist: {number}")
    return match


def current_generation() -> Generation | None:
    return next((row for row in generations() if row.current), None)


def previous_generation() -> Generation | None:
    rows = generations()
    current = next((index for index, row in enumerate(rows) if row.current), None)
    if current is not None and current + 1 < len(rows):
        return rows[current + 1]
    return None


def complete_generation_numbers(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete real generation numbers with age and current-state context."""
    del ctx
    try:
        rows = generations()
    except (OSError, RuntimeError, ValueError):
        return []
    return [
        (
            str(row.number),
            f"{row.created_at}{' [current]' if row.current else ''}",
        )
        for row in rows
        if str(row.number).startswith(incomplete)
    ]


def closure_diff(before: Path, after: Path) -> str:
    result = run_process(
        ["nix", "store", "diff-closures", str(before), str(after)],
        capture=True, check=False,
    )
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        detail = lines[-1] if lines else "closure diff failed"
        raise RuntimeError(detail[:500])
    return (result.stdout or "").strip()


app = typer.Typer(
    name="history",
    help="Inspect configuration generations and closure differences",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def cmd_history(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List known configuration generations."""
    if ctx.invoked_subcommand is not None:
        return
    payload = {"reportVersion": REPORT_VERSION, "platform": PLATFORM, "generations": [],
               "rollbackBoundary": ROLLBACK_BOUNDARY}
    try:
        rows = generations()
        payload["generations"] = [dict(row.to_dict(), semanticStatus="bound" if baseline_for_generation(row) is not None else "unknown") for row in rows]
    except (OSError, RuntimeError, ValueError) as exc:
        if json_output:
            emit_report("history", payload, ok=False, error=str(exc))
        else:
            log.error("history", str(exc))
        raise typer.Exit(1) from exc
    if json_output:
        emit_report("history", payload)
        return
    if not rows:
        log.warn("history", "no configuration generations were found")
        return
    table = Table(title="envY generations")
    table.add_column("Generation", justify="right")
    table.add_column("Current")
    table.add_column("Created")
    table.add_column("Store target")
    for row in rows:
        table.add_row(
            str(row.number), "yes" if row.current else "",
            row.created_at, str(row.target),
        )
    log.console.print(table)


@app.command(name="diff")
def cmd_diff(
    before: int = typer.Argument(
        ..., help="Older generation number",
        autocompletion=complete_generation_numbers,
    ),
    after: int = typer.Argument(
        ..., help="Newer generation number",
        autocompletion=complete_generation_numbers,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Compare the Nix closures of two generations."""
    payload = {"reportVersion": REPORT_VERSION, "before": None, "after": None,
               "closureDiff": None,
               "sections": {name: section("unknown", "Generation semantic evidence is not available")
                            for name in ("settings", "software", "sources", "behavior")},
               "rollbackBoundary": ROLLBACK_BOUNDARY}
    try:
        left = require_generation(before)
        payload["before"] = left.to_dict()
        right = require_generation(after)
        payload["after"] = right.to_dict()
        payload["sections"] = comparison_report(baseline_for_generation(left), baseline_for_generation(right))
        # Missing history is unknown, not a failed current-source evaluation.
        for item in payload["sections"].values():
            if item["status"] == "unavailable":
                item.update(status="unknown", reason="No verified semantic baseline is bound to this generation")
        diff = closure_diff(left.target, right.target)
        payload["closureDiff"] = diff
        payload["sections"]["closure"] = section("compared", diff=diff, changed=bool(diff))
    except (RuntimeError, OSError, ValueError, typer.BadParameter) as exc:
        payload["sections"]["closure"] = section("unavailable", "Generation closure comparison did not complete")
        if json_output:
            emit_report("history.diff", payload, ok=False, error=str(exc))
        else:
            log.error("history", str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        emit_report("history.diff", payload)
        return
    log.console.print(diff or "[green]No closure differences.[/green]")
    log.hint(ROLLBACK_BOUNDARY)

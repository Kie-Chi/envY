"""Read-only generation inventory, closure diffs, and rollback previews."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import typer
from rich.table import Table

from envy import log
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
    rows = generations()
    if json_output:
        log.console.print_json(json.dumps({
            "schemaVersion": 1,
            "platform": PLATFORM,
            "generations": [row.to_dict() for row in rows],
        }, ensure_ascii=False))
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
    left = require_generation(before)
    right = require_generation(after)
    try:
        diff = closure_diff(left.target, right.target)
    except RuntimeError as exc:
        log.error("history", str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        log.console.print_json(json.dumps({
            "schemaVersion": 1,
            "before": left.to_dict(),
            "after": right.to_dict(),
            "closureDiff": diff,
        }, ensure_ascii=False))
        return
    log.console.print(diff or "[green]No closure differences.[/green]")

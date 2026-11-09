"""Typer app for `envy doctor`."""

import typer

from envy.doctor.selection import ONLY_HELP
from envy.doctor.model import ALL_SECTIONS
from envy.schemas.apps import ALL_APP_SPECS, APP_ALIASES
from envy.doctor.runner import CHECKS, DEFAULT_CHECKS, exit_code, render, render_json, run_sections


def _run(
    sections: list[str],
    only: list[str] | None,
    *,
    strict: bool,
    json_output: bool,
) -> None:
    results = run_sections(sections, only, log_progress=not json_output)
    if json_output:
        render_json(results, strict=strict)
    else:
        render(results)
    raise typer.Exit(exit_code(results, strict=strict))


def complete_doctor_selection(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete repeatable/comma-separated doctor section and app filters."""
    del ctx
    prefix = incomplete.rsplit(",", 1)[-1].strip()
    completed = incomplete[: len(incomplete) - len(prefix)] if prefix else incomplete
    candidates: list[tuple[str, str]] = []
    sections = [(name, f"doctor section: {name}") for name in ALL_SECTIONS]
    apps = [(name, f"app check: {name}") for name in sorted(ALL_APP_SPECS)]
    aliases = [
        (alias, f"app alias for {target}")
        for alias, target in sorted(APP_ALIASES.items())
        if alias != target
    ]
    if ":" in prefix:
        kind, value = prefix.split(":", 1)
        kind = kind.casefold()
        if kind == "section":
            sections = [(f"section:{name}", description) for name, description in sections]
            apps = []
            aliases = []
        elif kind == "app":
            apps = [(f"app:{name}", description) for name, description in apps]
            aliases = [(f"app:{name}", description) for name, description in aliases]
            sections = []
        else:
            return []
        prefix = f"{kind}:{value}"
    for value, description in [*sections, *apps, *aliases]:
        if value.startswith(prefix):
            candidates.append((completed + value, description))
    return candidates

app = typer.Typer(
    name="doctor",
    help="Check envY health, app state, login hints, and platform permissions.",
    rich_markup_mode="rich",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def cmd_doctor(
    ctx: typer.Context,
    only: list[str] = typer.Option(
        None, "--only", "-o", help=ONLY_HELP, autocompletion=complete_doctor_selection,
    ),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are present."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Run all doctor checks."""
    if ctx.invoked_subcommand is not None:
        return
    _run(DEFAULT_CHECKS, only, strict=strict, json_output=json_output)


@app.command(name="all")
def cmd_all(
    only: list[str] = typer.Option(
        None, "--only", "-o", help=ONLY_HELP, autocompletion=complete_doctor_selection,
    ),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are present."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Run all doctor checks."""
    _run(list(CHECKS.keys()), only, strict=strict, json_output=json_output)


@app.command(name="config")
def cmd_config(
    only: list[str] = typer.Option(
        None, "--only", "-o", help=ONLY_HELP, autocompletion=complete_doctor_selection,
    ),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are present."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Check envY configuration and secrets."""
    _run(["config"], only, strict=strict, json_output=json_output)


@app.command(name="apps")
def cmd_apps(
    only: list[str] = typer.Option(
        None, "--only", "-o", help=ONLY_HELP, autocompletion=complete_doctor_selection,
    ),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are present."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Check app installation, running status, local state, and login hints."""
    _run(["apps"], only, strict=strict, json_output=json_output)


@app.command(name="system")
def cmd_system(
    only: list[str] = typer.Option(
        None, "--only", "-o", help=ONLY_HELP, autocompletion=complete_doctor_selection,
    ),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are present."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Check host prerequisites, apply runner, Git state, and workflow leftovers."""
    _run(["system"], only, strict=strict, json_output=json_output)


@app.command(name="network")
def cmd_network(
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are present."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Probe evaluated mirror endpoints without changing network configuration."""
    _run(["network"], None, strict=strict, json_output=json_output)


@app.command(name="agents")
def cmd_agents(
    only: list[str] = typer.Option(
        None, "--only", "-o", help=ONLY_HELP, autocompletion=complete_doctor_selection,
    ),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are present."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Check managed Pi links, MCP executables, and offline extension loading."""
    _run(["agents"], only, strict=strict, json_output=json_output)

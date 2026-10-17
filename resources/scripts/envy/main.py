import json
import os
import shutil

import typer
from typing import Optional

from envy import log
from envy.remote_agent import app as remote_agent_app
from envy.agent import app as agent_app
from envy import _check_schema_api
from envy.config import app as config_app
from envy.doctor import app as doctor_app
from envy.habit import app as habit_app
from envy.journal import show_log
from envy.key import app as key_app
from envy.mirror import app as mirror_app
from envy.overview import show as show_overview
from envy.host import app as host_app, complete_platforms
from envy.software import app as software_app
from envy.process import run_process
from envy.workflows.check import check_or_exit
from envy.workflows.update import update_homebrew, update_inputs
from envy.workflows.plan import plan_configuration
from envy.workflows.generations import app as history_app
from envy.workflows import system as system_workflow
from envy.workflows import git as git_workflow
from envy.utils import ENVY_ROOT, PLATFORM, current_machine_id


# ==========================================
# COMPLETION CALLBACKS
# ==========================================


def complete_git_remotes(ctx, incomplete):
    """Complete git remote names for envy push."""
    return git_workflow.complete_remotes(incomplete)


def complete_git_branches(ctx, incomplete):
    """Complete local Git branch names for push/sync branch options."""
    return git_workflow.complete_branches(incomplete)


def complete_rollback_target(ctx, incomplete):
    """Complete rollback target: 'list' or generation numbers."""
    candidates = []
    if "list".startswith(incomplete):
        candidates.append(("list", "List all available generations"))
    try:
        from envy.workflows.generations import complete_generation_numbers

        candidates.extend(complete_generation_numbers(ctx, incomplete))
    except (OSError, RuntimeError, ValueError):
        pass
    return candidates


def complete_journal_operations(ctx, incomplete):
    """Complete the recorded operation names for envy log --operation."""
    operations = [
        ("apply", "Apply the configuration"),
        ("bootstrap", "Bootstrap the configuration"),
        ("sync", "Synchronize, merge if needed, and apply"),
        ("push", "Commit and push changes"),
        ("update-inputs", "Update flake inputs"),
        ("update-brew", "Update Homebrew metadata"),
        ("rollback", "Activate a previous generation"),
        ("clean", "Garbage-collect generations"),
    ]
    return [item for item in operations if item[0].startswith(incomplete)]


def complete_flake_inputs(ctx, incomplete):
    """Complete root flake input names from the local lock file only."""
    del ctx
    lock_path = ENVY_ROOT / "flake.lock"
    try:
        payload = json.loads(lock_path.read_text())
        nodes = payload.get("nodes", {})
        root = nodes.get("root", {}).get("inputs", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    if not isinstance(root, dict) or not isinstance(nodes, dict):
        return []
    values = []
    for name in sorted(root):
        if not isinstance(name, str) or not name.startswith(incomplete):
            continue
        node_name = root.get(name)
        node = nodes.get(node_name, {}) if isinstance(node_name, str) else {}
        locked = node.get("locked", {}) if isinstance(node, dict) else {}
        kind = locked.get("type", "input") if isinstance(locked, dict) else "input"
        values.append((name, f"flake input ({kind})"))
    return values


# ==========================================
# CLI APP
# ==========================================


cli = typer.Typer(
    name="envy",
    help="A friendly manager for your Nix configuration.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

update_app = typer.Typer(
    name="update",
    help="Update flake inputs or Homebrew metadata with validation.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@cli.callback()
def main_callback(
    debug: bool = typer.Option(False, "--debug", "-e", help="Enable debug mode"),
):
    """envy — configuration manager"""
    log.reset_consoles()
    if debug:
        os.environ["ENVY_DEBUG"] = "1"
    _check_schema_api()
    log.debug(
        "envy", "diagnostics enabled", platform=PLATFORM,
        machine=current_machine_id(), repository=str(ENVY_ROOT),
        python=os.sys.version.split()[0], pid=os.getpid(),
    )


# ==========================================
# SUBCOMMANDS
# ==========================================

@cli.command(name="apply")
@cli.command(name="a", rich_help_panel="Aliases")
@cli.command(name="switch", rich_help_panel="Aliases")
def cmd_apply():
    """Apply the configuration (home-manager on Linux, nix-darwin on macOS)."""
    system_workflow.apply_configuration()


@cli.command(name="sync")
@cli.command(name="s", rich_help_panel="Aliases")
def cmd_sync(
    remote: Optional[str] = typer.Option(
        None, "--remote", "-r", help="Synchronize one remote; omit to inspect all remotes",
        autocompletion=complete_git_remotes,
    ),
    branch: str = typer.Option(
        "master", "--branch", "-b", help="Shared cross-platform branch to synchronize",
        autocompletion=complete_git_branches,
    ),
    no_apply: bool = typer.Option(False, "--no-apply", help="Synchronize Git without applying"),
    build_only: bool = typer.Option(False, "--build-only", help="Build the selected machine without applying"),
):
    """Synchronize the shared branch, merge compatible remote history, and apply."""
    git_workflow.sync(
        remote=remote, branch=branch, no_apply=no_apply, build_only=build_only,
    )


@update_app.callback(invoke_without_command=True)
def cmd_update(ctx: typer.Context):
    """Update all flake inputs, validate every machine, then refresh Homebrew on Darwin."""
    if ctx.invoked_subcommand is not None:
        return
    update_inputs(validate=True)
    if PLATFORM == "darwin":
        update_homebrew()
    log.hint("Run: envy apply")


@update_app.command(name="inputs")
def cmd_update_inputs(
    input_name: Optional[str] = typer.Argument(
        None, help="One flake input; omit for all", autocompletion=complete_flake_inputs,
    ),
    no_check: bool = typer.Option(False, "--no-check", help="Skip all-machine validation"),
):
    """Update one or all flake inputs and roll back flake.lock on validation failure."""
    update_inputs(input_name, validate=not no_check)


@update_app.command(name="brew")
def cmd_update_brew():
    """Refresh Homebrew metadata only."""
    if PLATFORM != "darwin":
        log.error("update", "Homebrew update is available only on Darwin")
        raise typer.Exit(code=1)
    update_homebrew()


@cli.command(name="check")
def cmd_check_all(
    all_machines: bool = typer.Option(False, "--all", help="Check every Darwin and Linux machine"),
    changed: bool = typer.Option(False, "--changed", help="Check machines affected by worktree changes"),
    selected_platform: Optional[str] = typer.Option(
        None, "--platform", help="Restrict checks to darwin or linux",
        autocompletion=complete_platforms,
    ),
    build: bool = typer.Option(False, "--build", help="Build selected local-platform targets"),
):
    """Evaluate current, changed, or all cross-platform machine targets."""
    check_or_exit(
        all_machines=all_machines,
        changed=changed,
        selected_platform=selected_platform,
        build=build,
    )


@cli.command(name="plan")
def cmd_plan(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Build the selected machine and compare it with the active closure."""
    plan_configuration(json_output=json_output)


@cli.command(name="_apply-needs-tty", hidden=True)
def cmd_apply_needs_tty(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Report whether `envy apply` requires an interactive TTY (internal probe)."""
    from envy.utils import _get_sudo_passwd, _sudo_ticket_available

    has_noninteractive_auth = _get_sudo_passwd() is not None or _sudo_ticket_available()
    needs_tty = PLATFORM == "darwin" and not has_noninteractive_auth
    if json_output:
        print(json.dumps({"needsTty": needs_tty}))
    else:
        log.info(
            "apply",
            "requires interactive TTY" if needs_tty else "can run non-interactively",
        )


@cli.command(name="tui")
def cmd_tui():
    """Launch the interactive Rust/Ratatui frontend."""
    binary = shutil.which("envy-tui")
    if binary is not None:
        os.execv(binary, [binary])

    manifest = ENVY_ROOT / "resources" / "scripts" / "envy-tui" / "Cargo.toml"
    cargo = shutil.which("cargo")
    if cargo is not None and manifest.is_file():
        launcher = ENVY_ROOT / "envy"
        backend = os.environ.get("ENVY_BIN")
        if not backend:
            backend = str(launcher) if launcher.is_file() else "envy"
        environment = os.environ.copy()
        environment["ENVY_BIN"] = backend
        os.execvpe(
            cargo,
            [cargo, "run", "--quiet", "--manifest-path", str(manifest), "--"],
            environment,
        )

    log.error(
        "tui",
        "envy-tui is not installed",
        hint="enter the Nix devShell or apply the envY package",
    )
    raise typer.Exit(code=1)


@cli.command(name="init")
@cli.command(name="i", rich_help_panel="Aliases")
@cli.command(name="bootstrap", rich_help_panel="Aliases")
def cmd_init():
    """Bootstrap configuration (home-manager on Linux, nix-darwin on macOS)."""
    system_workflow.bootstrap_configuration()


@cli.command(name="rollback")
@cli.command(name="r", rich_help_panel="Aliases")
def cmd_rollback(
    target: str = typer.Argument(None, help="Generation number or 'list'", autocompletion=complete_rollback_target),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the generation closure diff without activating"),
):
    """Rollback to a previous configuration generation."""
    system_workflow.rollback_configuration(target, dry_run=dry_run)


@cli.command(name="edit")
@cli.command(name="e", rich_help_panel="Aliases")
def cmd_edit():
    """Open the envY repository in your default editor."""
    system_workflow.open_editor()


@cli.command(name="push")
@cli.command(name="p", rich_help_panel="Aliases")
def cmd_push(
    msg: str = typer.Argument("chore: update configuration", help="Commit message"),
    remote: str = typer.Argument(None, help="Remote name (omit to push all)", autocompletion=complete_git_remotes),
    branch: str = typer.Option(
        "master", "--branch", "-b", help="Shared cross-platform branch expected for the push",
        autocompletion=complete_git_branches,
    ),
    machine_only: bool = typer.Option(
        False, "--machine-only",
        help="Require every worktree and outgoing change to be a machine file",
    ),
    self_only: bool = typer.Option(
        False, "--self",
        help="Require every worktree and outgoing change to belong to the selected machine",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation; scope safety guards still apply",
    ),
):
    """Synchronize without applying, stop on conflicts, then commit and push changes."""
    git_workflow.push(
        msg=msg,
        remote=remote,
        branch=branch,
        machine_only=machine_only,
        self_only=self_only,
        yes=yes,
    )


@cli.command(name="status")
@cli.command(name="st", rich_help_panel="Aliases")
@cli.command(name="overview", rich_help_panel="Aliases")
def cmd_status(
    json_output: bool = typer.Option(False, "--json", help="Emit one coherent dashboard snapshot"),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache"),
):
    """Show machine, repository, generation, software, and health status."""
    show_overview(json_output=json_output, refresh=refresh)


@cli.command(name="log")
@cli.command(name="logs", rich_help_panel="Aliases")
def cmd_log(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    limit: int = typer.Option(
        50, "--limit", "-n", min=1, max=1000, help="Number of recent records to show"
    ),
    failed: bool = typer.Option(False, "--failed", help="Only show failed operations"),
    operation: Optional[str] = typer.Option(
        None, "--operation", "-o", help="Filter by operation name",
        autocompletion=complete_journal_operations,
    ),
):
    """Show recent state-changing operations from the journal."""
    show_log(
        json_output=json_output, limit=limit, failed=failed, operation=operation,
    )


@cli.command(name="diff")
@cli.command(name="d", rich_help_panel="Aliases")
@cli.command(name="dif", rich_help_panel="Aliases")
def cmd_diff():
    """Show the Git diff of the envY repository."""
    run_process(["git", "diff"], cwd=ENVY_ROOT, check=True)


@cli.command(name="git")
def cmd_git(
    args: list[str] = typer.Argument(None, help="Git arguments"),
):
    """Execute Git commands in the envY repository."""
    git_args = args if args else []
    run_process(["git", *git_args], cwd=ENVY_ROOT, check=True)


@cli.command(name="clean")
@cli.command(name="gc", rich_help_panel="Aliases")
def cmd_clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm generation deletion"),
    older_than: Optional[str] = typer.Option(
        None, "--older-than", help="Delete only generations older than a Nix duration, e.g. 30d"
    ),
    brew: bool = typer.Option(
        True, "--brew/--no-brew", help="Run Homebrew cleanup on Darwin"
    ),
):
    """Run Nix garbage collection to clean old generations."""
    system_workflow.clean_generations(yes=yes, older_than=older_than, brew=brew)


@cli.command(name="setup")
@cli.command(name="configure", rich_help_panel="Aliases")
def cmd_setup():
    """Run the setup script to configure secrets."""
    system_workflow.run_setup()


# Register validated update workflow and alias.
cli.add_typer(update_app, name="update")
cli.add_typer(update_app, name="u", rich_help_panel="Aliases")

# Register config subgroup — "c" alias registered separately
cli.add_typer(config_app, name="config")
cli.add_typer(config_app, name="c", rich_help_panel="Aliases")

# Software policy and registry search are independent from scalar config.
cli.add_typer(software_app, name="software")
cli.add_typer(software_app, name="sw", rich_help_panel="Aliases")

# Remote agent always works back on this machine over SSH.
cli.add_typer(remote_agent_app, name="remote-agent")
cli.add_typer(remote_agent_app, name="ra", rich_help_panel="Aliases")

# Machine-owned agent instances and their non-sensitive endpoint metadata.
cli.add_typer(agent_app, name="agent")
cli.add_typer(agent_app, name="agents", rich_help_panel="Aliases")

# Register key subgroup — "k" alias registered separately
cli.add_typer(key_app, name="key")
cli.add_typer(key_app, name="k", rich_help_panel="Aliases")

# Register host subgroup — "h" alias registered separately
cli.add_typer(host_app, name="host")
cli.add_typer(host_app, name="h", rich_help_panel="Aliases")

# Register doctor subgroup — "dr" alias registered separately
cli.add_typer(doctor_app, name="doctor")
cli.add_typer(doctor_app, name="dr", rich_help_panel="Aliases")

# Mirror inspection and generated per-target selection; configuration remains machine-owned.
cli.add_typer(mirror_app, name="mirror")

# Personal interaction habits are versioned machine policy.
cli.add_typer(habit_app, name="habit")
cli.add_typer(habit_app, name="habits", rich_help_panel="Aliases")

# Read-only generation inventory and closure comparison.
cli.add_typer(history_app, name="history")

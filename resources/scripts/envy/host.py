"""Per-machine host configuration commands for envy."""

import json
import re
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from envy import log
from envy.evaluation import informative_nix_error
from envy.jsonio import emit, emit_error
from envy.mutation import offer_mutation_commit
from envy.process import run_process
from envy.secure_io import atomic_write_bytes, atomic_write_text
from envy.utils import (
    DEVICE_LABEL_FILE,
    ENVY_ROOT,
    current_machine_id,
    flake_target,
    machine_build_attr,
    machine_config_dir,
    platform_name,
    set_device_machine_id,
)


HOSTS_DIR = ENVY_ROOT / "hosts"
MACHINES_DIR = machine_config_dir()
DEFAULT_MACHINE = HOSTS_DIR / "default.nix"
MACHINE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


app = typer.Typer(
    name="host",
    help="Create and inspect per-machine Nix configurations",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


def machine_file(machine_id: str) -> Path:
    return MACHINES_DIR / f"{machine_id}.nix"


def machine_entries() -> list[tuple[str, str, Path]]:
    entries = []
    for platform in ("darwin", "linux"):
        directory = HOSTS_DIR / platform
        entries.extend(
            (platform, path.stem, path)
            for path in directory.glob("*.nix")
            if path.is_file()
        )
    return sorted(entries, key=lambda entry: (entry[0], entry[1]))


def complete_machine_ids(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete machine IDs available for the current platform."""
    del ctx
    current = current_machine_id()
    return [
        (
            path.stem,
            "currently selected" if path.stem == current else f"{platform_name()} machine",
        )
        for path in sorted(MACHINES_DIR.glob("*.nix"))
        if path.is_file() and path.stem.startswith(incomplete)
    ]


def complete_all_machine_ids(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete repository machines across Darwin and Linux."""
    del ctx
    try:
        entries = machine_entries()
        current = current_machine_id()
        current_platform = platform_name()
    except (OSError, RuntimeError, ValueError):
        return []
    return [
        (
            machine_id,
            "currently selected"
            if machine_id == current and platform == current_platform
            else f"{platform} machine",
        )
        for platform, machine_id, _ in entries
        if machine_id.startswith(incomplete)
    ]


def complete_platforms(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete supported host platform selectors."""
    del ctx
    return [
        (platform, f"{platform} machine")
        for platform in ("darwin", "linux")
        if platform.startswith(incomplete)
    ]


def complete_matrix_groups(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete canonical groups without evaluating every repository machine."""
    del ctx
    try:
        from envy.software.model import groups_for_platform

        by_key: dict[str, tuple[str, set[str]]] = {}
        for platform in ("darwin", "linux"):
            for group in groups_for_platform(platform):
                label, platforms = by_key.setdefault(group.key, (group.label, set()))
                platforms.add(platform)
        return [
            (group_id, f"{label} ({'/'.join(sorted(platforms))})")
            for group_id, (label, platforms) in sorted(by_key.items())
            if group_id.startswith(incomplete)
        ]
    except (ImportError, OSError, RuntimeError, ValueError):
        return []


def resolve_machine_entry(machine_id: str, selected_platform: str | None = None):
    matches = [
        entry for entry in machine_entries()
        if entry[1] == machine_id and (
            selected_platform is None or entry[0] == selected_platform
        )
    ]
    if not matches:
        raise typer.BadParameter(f"machine configuration does not exist: {machine_id}")
    if len(matches) > 1:
        platforms = ", ".join(entry[0] for entry in matches)
        raise typer.BadParameter(
            f"machine ID exists on multiple platforms ({platforms}); pass a platform option"
        )
    return matches[0]


def evaluate_machine_manifest(platform: str, machine_id: str) -> dict:
    if platform == "darwin":
        attr = f"path:.#darwinConfigurations.{machine_id}.config.envy.machine.manifest"
    elif platform == "linux":
        attr = f"path:.#homeConfigurations.{machine_id}.config.envy.machine.manifest"
    else:
        raise ValueError(f"unsupported machine platform: {platform}")
    result = run_process(
        ["nix", "eval", "--impure", attr, "--json"],
        cwd=ENVY_ROOT, capture=True, check=False, timeout=60,
    )
    if result.returncode != 0:
        detail = informative_nix_error(result.stderr or "", fallback="manifest evaluation failed")
        raise RuntimeError(f"cannot evaluate {platform}/{machine_id}: {detail}")
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid manifest JSON for {platform}/{machine_id}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid manifest for {platform}/{machine_id}")
    return value


def manifest_diff(left: dict, right: dict) -> dict[str, object]:
    left_settings = left.get("settings") if isinstance(left.get("settings"), dict) else {}
    right_settings = right.get("settings") if isinstance(right.get("settings"), dict) else {}
    settings = [{
        "path": path,
        "left": left_settings.get(path),
        "right": right_settings.get(path),
    } for path in sorted(set(left_settings) | set(right_settings))
      if left_settings.get(path) != right_settings.get(path)]
    # Agent and endpoint policy is represented as structured manifest data so
    # arbitrary adapter names remain possible. Surface one stable diff row per
    # structured section without flattening secret values.
    for section in ("llm", "agents"):
        if left.get(section) != right.get(section):
            settings.append({
                "path": f"envy.{section}",
                "left": left.get(section),
                "right": right.get(section),
            })
    settings.sort(key=lambda row: row["path"])

    def effective(manifest):
        software = manifest.get("software") if isinstance(manifest.get("software"), dict) else {}
        groups = software.get("groups") if isinstance(software.get("groups"), dict) else {}
        values = set()
        for group_id, group in groups.items():
            selection = group.get("selection") if isinstance(group, dict) else None
            items = selection.get("effective") if isinstance(selection, dict) else None
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    values.add((str(group_id), item["id"]))
        return values

    left_effective = effective(left)
    right_effective = effective(right)
    return {
        "settings": settings,
        "software": {
            "leftOnly": [
                {"group": group, "item": item}
                for group, item in sorted(left_effective - right_effective)
            ],
            "rightOnly": [
                {"group": group, "item": item}
                for group, item in sorted(right_effective - left_effective)
            ],
        },
    }


def complete_init_modes(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete supported host initialization modes."""
    del ctx
    modes = (
        ("import", "inherit future changes from hosts/default.nix"),
        ("copy", "create an independent snapshot of hosts/default.nix"),
    )
    return [item for item in modes if item[0].startswith(incomplete)]


def validate_machine_id(machine_id: str) -> str:
    value = machine_id.strip()
    if not MACHINE_ID_PATTERN.fullmatch(value):
        raise typer.BadParameter(
            "machine ID may contain only letters, digits, underscores, and hyphens"
        )
    return value


def suggested_machine_id() -> str:
    return current_machine_id()


def current_machine_file() -> Path:
    return machine_file(current_machine_id())


def require_current_machine_file() -> Path:
    path = current_machine_file()
    if path.exists():
        return path
    log.error("host", "machine configuration is missing", path=str(path))
    log.hint("Run: envy host init")
    raise typer.Exit(code=1)


def initialize_machine(
    machine_id: str,
    mode: str,
    force: bool = False,
    *,
    select: bool = True,
) -> Path:
    machine_id = validate_machine_id(machine_id)
    mode = mode.strip().lower()
    if mode not in {"import", "copy"}:
        raise typer.BadParameter("mode must be 'import' or 'copy'")
    if not DEFAULT_MACHINE.exists():
        log.error("host", "default machine configuration is missing", path=str(DEFAULT_MACHINE))
        raise typer.Exit(code=1)

    target = machine_file(machine_id)
    MACHINES_DIR.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        log.error("host", "machine configuration already exists", path=str(target))
        log.hint("Use --force to replace it after an explicit confirmation.")
        raise typer.Exit(code=1)

    if target.exists():
        if not typer.confirm(f"Replace {target}?", default=None):
            raise typer.Abort()
        backup = target.with_suffix(target.suffix + ".bak")
        atomic_write_bytes(
            backup,
            target.read_bytes(),
            mode=target.stat().st_mode & 0o777,
        )
        log.info("host", "backed up existing machine configuration", path=str(backup))

    if mode == "import":
        content = """{ ... }:

{
  imports = [ ../default.nix ];

  # `envy config refine` writes non-sensitive machine values here.
  # Add machine-specific envy.* overrides here.
}
"""
        atomic_write_text(target, content)
    else:
        source = DEFAULT_MACHINE.read_text()
        header = (
            "# Machine configuration copied from hosts/default.nix.\n"
            "# It does not inherit later default policy changes.\n\n"
        )
        atomic_write_text(target, header + source)

    if select:
        set_device_machine_id(machine_id)
    log.ok(
        "host",
        "machine configuration created" + (" and selected" if select else ""),
        path=str(target), mode=mode,
    )
    log.hint(f"Edit the file, then run: envy host check {machine_id}")
    return target


@app.command(name="init")
def cmd_init(
    machine_id: Optional[str] = typer.Argument(
        None, help="Machine ID; defaults to the local device label", autocompletion=complete_machine_ids,
    ),
    mode: Optional[str] = typer.Option(
        None, "--mode", "-m", help="Creation mode: import or copy",
        autocompletion=complete_init_modes,
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Back up and replace an existing machine file"),
):
    """Create machine.nix by importing or copying hosts/default.nix."""
    selected_id = machine_id or typer.prompt("Machine ID", default=suggested_machine_id())
    selected_mode = mode or typer.prompt("Creation mode (import/copy)", default="import")
    target = initialize_machine(selected_id, selected_mode, force=force)
    offer_mutation_commit([target], f"feat(host): initialize {target.stem}")


@app.command(name="list")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List machine configurations in the repository."""
    current = current_machine_id()
    table = Table(title="envy machines")
    table.add_column("Platform")
    table.add_column("Machine")
    table.add_column("Current")
    table.add_column("File")
    local_platform = platform_name()
    entries = list(machine_entries())
    if json_output:
        payload = {
            "schemaVersion": 1,
            "machines": [{
                "platform": platform,
                "machineId": machine_id,
                "current": platform == local_platform and machine_id == current,
                "file": str(path),
            } for platform, machine_id, path in entries],
        }
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    for platform, machine_id, path in entries:
        table.add_row(
            platform,
            machine_id,
            "yes" if platform == local_platform and machine_id == current else "",
            str(path),
        )
    log.console.print(table)


@app.command(name="select")
def cmd_select(
    machine_id: str = typer.Argument(
        ..., help="Existing machine ID to select locally",
        autocompletion=complete_machine_ids,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Assume yes; select without interactive prompts"
    ),
):
    """Select which versioned machine file local envy commands operate on."""
    try:
        selected = validate_machine_id(machine_id)
    except typer.BadParameter as exc:
        if json_output:
            emit_error("host.select", str(exc), code="invalid-machine")
            raise typer.Exit(code=1) from exc
        raise
    path = machine_file(selected)
    if not path.exists():
        if json_output:
            emit_error(
                "host.select",
                f"machine configuration is missing: {path}",
                code="invalid-machine",
            )
        else:
            log.error("host", "machine configuration is missing", path=str(path))
        raise typer.Exit(code=1)
    set_device_machine_id(selected)
    if json_output:
        emit("host.select", data={
            "machine": selected,
            "platform": platform_name(),
            "metadata": str(DEVICE_LABEL_FILE),
            "flakeTarget": flake_target(),
        })
        return
    log.ok("host", "machine selected locally", machine=selected, metadata=str(DEVICE_LABEL_FILE))


@app.command(name="status")
@app.command(name="st", rich_help_panel="Aliases")
def cmd_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show the selected machine and flake target."""
    machine_id = current_machine_id()
    path = machine_file(machine_id)
    table = Table(title="envy host")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Machine ID", machine_id)
    table.add_row("Platform", platform_name())
    table.add_row("Device metadata", str(DEVICE_LABEL_FILE))
    table.add_row("Machine file", str(path))
    table.add_row("File exists", "yes" if path.exists() else "no")
    table.add_row("Flake target", flake_target())
    branch_result = run_process(
        ["git", "branch", "--show-current"],
        cwd=ENVY_ROOT, capture=True, check=False,
    )
    branch = (branch_result.stdout or "").strip()
    if json_output:
        payload = {
            "schemaVersion": 1,
            "machineId": machine_id,
            "platform": platform_name(),
            "deviceMetadata": str(DEVICE_LABEL_FILE),
            "machineFile": str(path),
            "fileExists": path.exists(),
            "flakeTarget": flake_target(),
            "gitBranch": branch or None,
        }
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table.add_row("Git branch", branch or "<detached>")
    log.console.print(table)


@app.command(name="diff")
def cmd_diff(
    left: str = typer.Argument(
        ..., help="First machine ID", autocompletion=complete_all_machine_ids,
    ),
    right: str = typer.Argument(
        ..., help="Second machine ID", autocompletion=complete_all_machine_ids,
    ),
    left_platform: str | None = typer.Option(
        None, "--left-platform", help="darwin or linux",
        autocompletion=complete_platforms,
    ),
    right_platform: str | None = typer.Option(
        None, "--right-platform", help="darwin or linux",
        autocompletion=complete_platforms,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Compare evaluated scalar settings and effective software for two machines."""
    left_entry = resolve_machine_entry(left, left_platform)
    right_entry = resolve_machine_entry(right, right_platform)
    try:
        left_manifest = evaluate_machine_manifest(left_entry[0], left_entry[1])
        right_manifest = evaluate_machine_manifest(right_entry[0], right_entry[1])
    except RuntimeError as exc:
        log.error("host", str(exc))
        raise typer.Exit(code=1) from exc
    differences = manifest_diff(left_manifest, right_manifest)
    payload = {
        "schemaVersion": 1,
        "left": {"platform": left_entry[0], "machineId": left_entry[1]},
        "right": {"platform": right_entry[0], "machineId": right_entry[1]},
        "diff": differences,
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    settings = differences["settings"]
    software = differences["software"]
    table = Table(title=f"Machine diff - {left} → {right}")
    table.add_column("Kind")
    table.add_column("Path/Group")
    table.add_column(left)
    table.add_column(right)
    for row in settings:
        table.add_row("setting", row["path"], str(row["left"]), str(row["right"]))
    for row in software["leftOnly"]:
        table.add_row("software", row["group"], row["item"], "-")
    for row in software["rightOnly"]:
        table.add_row("software", row["group"], "-", row["item"])
    if not settings and not software["leftOnly"] and not software["rightOnly"]:
        table.add_row("-", "no evaluated differences", "", "")
    log.console.print(table)


@app.command(name="matrix")
def cmd_matrix(
    group: str | None = typer.Option(
        None, "--group", "-g", help="Restrict to one software group",
        autocompletion=complete_matrix_groups,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show which evaluated machines make each software item effective."""
    manifests = []
    for platform, machine_id, _ in machine_entries():
        if not json_output:
            log.step("host", "evaluating matrix machine", platform=platform, machine=machine_id)
        try:
            manifests.append((platform, machine_id, evaluate_machine_manifest(platform, machine_id)))
        except RuntimeError as exc:
            log.error("host", str(exc))
            raise typer.Exit(code=1) from exc
    coverage: dict[tuple[str, str], list[str]] = {}
    for platform, machine_id, manifest in manifests:
        software = manifest.get("software") if isinstance(manifest.get("software"), dict) else {}
        groups = software.get("groups") if isinstance(software.get("groups"), dict) else {}
        for group_id, value in groups.items():
            if group is not None and group_id != group:
                continue
            selection = value.get("selection") if isinstance(value, dict) else None
            effective = selection.get("effective") if isinstance(selection, dict) else None
            for item in effective if isinstance(effective, list) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    coverage.setdefault((str(group_id), item["id"]), []).append(
                        f"{platform}/{machine_id}"
                    )
    rows = [{"group": key[0], "item": key[1], "machines": machines}
            for key, machines in sorted(coverage.items())]
    payload = {
        "schemaVersion": 1,
        "machines": [
            {"platform": platform, "machineId": machine_id}
            for platform, machine_id, _ in manifests
        ],
        "software": rows,
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="Machine software matrix")
    table.add_column("Group")
    table.add_column("Item")
    table.add_column("Effective on")
    for row in rows:
        table.add_row(row["group"], row["item"], ", ".join(row["machines"]))
    log.console.print(table)


@app.command(name="check")
def cmd_check(
    machine_id: Optional[str] = typer.Argument(
        None, help="Machine ID; defaults to the selected machine",
        autocompletion=complete_machine_ids,
    ),
):
    """Evaluate the selected platform's machine derivation without applying it."""
    selected = validate_machine_id(machine_id or current_machine_id())
    path = machine_file(selected)
    if not path.exists():
        log.error("host", "machine configuration is missing", path=str(path))
        raise typer.Exit(code=1)
    attr = machine_build_attr(selected, drv_path=True)
    log.step("host", "evaluating machine configuration", machine=selected)
    result = run_process(
        ["nix", "eval", "--impure", attr, "--raw"],
        cwd=ENVY_ROOT, check=False,
    )
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
    log.ok("host", "machine configuration evaluates successfully", machine=selected)

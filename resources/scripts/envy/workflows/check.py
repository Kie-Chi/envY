"""Cross-platform machine evaluation and local build workflows."""

from __future__ import annotations

import re

import typer

from envy import log
from envy.process import run_process
from envy.utils import ENVY_ROOT, current_machine_id, platform_name


def machine_entries() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for platform in ("darwin", "linux"):
        directory = ENVY_ROOT / "hosts" / platform
        result.extend((platform, path.stem) for path in directory.glob("*.nix") if path.is_file())
    return sorted(result)


def machine_drv_attr(platform: str, machine_id: str) -> str:
    if platform == "darwin":
        return f"path:.#darwinConfigurations.{machine_id}.config.system.build.toplevel.drvPath"
    return f"path:.#homeConfigurations.{machine_id}.activationPackage.drvPath"


def machine_build_attr(platform: str, machine_id: str) -> str:
    if platform == "darwin":
        return f"path:.#darwinConfigurations.{machine_id}.config.system.build.toplevel"
    return f"path:.#homeConfigurations.{machine_id}.activationPackage"


def changed_machine_entries() -> list[tuple[str, str]]:
    result = run_process(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=ENVY_ROOT, capture=True, check=True,
    )
    entries = result.stdout.split("\0") if result.stdout else []
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if len(entry) < 4:
            index += 1
            continue
        status = entry[:2]
        paths.append(entry[3:])
        index += 2 if "R" in status or "C" in status else 1

    selected: set[tuple[str, str]] = set()
    shared = False
    for path in paths:
        match = re.fullmatch(r"hosts/(darwin|linux)/([^/]+)\.nix", path)
        if match:
            selected.add((match.group(1), match.group(2)))
        else:
            shared = True
    available = machine_entries()
    return available if shared else sorted(selected & set(available))


def select_entries(
    *,
    all_machines: bool = False,
    changed: bool = False,
    selected_platform: str | None = None,
) -> list[tuple[str, str]]:
    if all_machines:
        entries = machine_entries()
    elif changed:
        entries = changed_machine_entries()
    else:
        entries = [(platform_name(), current_machine_id())]
    if selected_platform:
        entries = [entry for entry in entries if entry[0] == selected_platform]
    return entries


def run_machine_checks(
    entries: list[tuple[str, str]],
    *,
    build: bool = False,
) -> list[tuple[str, str, bool]]:
    results: list[tuple[str, str, bool]] = []
    for platform, machine_id in entries:
        label = f"{platform}/{machine_id}"
        if build and platform != platform_name():
            log.warn("check", "cannot build a foreign platform locally; evaluating only", machine=label)
        should_build = build and platform == platform_name()
        command = (
            ["nix", "build", "--impure", "--no-link", machine_build_attr(platform, machine_id)]
            if should_build
            else ["nix", "eval", "--impure", "--raw", machine_drv_attr(platform, machine_id)]
        )
        log.step("check", "building machine" if should_build else "evaluating machine", machine=label)
        result = run_process(command, cwd=ENVY_ROOT, capture=True, check=False)
        ok = result.returncode == 0
        results.append((platform, machine_id, ok))
        if ok:
            log.ok("check", "machine passed", machine=label)
        else:
            log.error("check", "machine failed", machine=label, exit_code=result.returncode)
            lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
            if lines:
                log.hint(lines[-1][:500])
    return results


def check_or_exit(
    *,
    all_machines: bool = False,
    changed: bool = False,
    selected_platform: str | None = None,
    build: bool = False,
) -> None:
    if all_machines and changed:
        raise typer.BadParameter("--all and --changed are mutually exclusive")
    if selected_platform not in {None, "darwin", "linux"}:
        raise typer.BadParameter("--platform must be darwin or linux")
    entries = select_entries(
        all_machines=all_machines,
        changed=changed,
        selected_platform=selected_platform,
    )
    if not entries:
        log.info("check", "no machine targets selected")
        return
    results = run_machine_checks(entries, build=build)
    failed = [f"{platform}/{machine}" for platform, machine, ok in results if not ok]
    if failed:
        log.error("check", "machine checks failed", failed=len(failed), total=len(results))
        raise typer.Exit(code=1)
    log.ok("check", "all selected machines passed", total=len(results))

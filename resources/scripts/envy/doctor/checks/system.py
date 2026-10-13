"""Host prerequisites, runner, Git, and interrupted-workflow checks."""

from __future__ import annotations

import shutil
from pathlib import Path

from envy.doctor.model import SECTION_SYSTEM, CheckResult, error, info, ok, warn
from envy.evaluation import machine_manifest
from envy.process import run_process
from envy.utils import AGE_KEY_DIR, ENVY_ROOT, platform_name


def run_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    required = ["nix", "git", "sops", "age", "age-keygen"]
    for command in required:
        resolved = shutil.which(command)
        if resolved:
            results.append(ok(SECTION_SYSTEM, f"command {command}", resolved))
        else:
            results.append(error(
                SECTION_SYSTEM,
                f"command {command}",
                "missing from PATH",
                hint="Enter the repository devShell or rerun envy apply.",
            ))

    runner = "darwin-rebuild" if platform_name() == "darwin" else "home-manager"
    resolved_runner = shutil.which(runner)
    if resolved_runner:
        results.append(ok(SECTION_SYSTEM, "apply runner", resolved_runner))
    else:
        results.append(warn(
            SECTION_SYSTEM,
            "apply runner",
            f"{runner} is missing; envY will use the repository-locked fallback",
        ))

    if machine_manifest() is None:
        results.append(error(
            SECTION_SYSTEM,
            "manifest evaluation",
            "selected machine manifest is unavailable",
            hint="Run: envy check",
        ))
    else:
        results.append(ok(SECTION_SYSTEM, "manifest evaluation", "selected machine evaluated"))

    branch = run_process(
        ["git", "branch", "--show-current"], cwd=ENVY_ROOT, capture=True, check=False
    )
    current_branch = (branch.stdout or "").strip()
    if branch.returncode != 0:
        results.append(error(SECTION_SYSTEM, "Git branch", "cannot inspect repository branch"))
    elif current_branch == "master":
        results.append(ok(SECTION_SYSTEM, "Git branch", "master"))
    else:
        results.append(warn(
            SECTION_SYSTEM,
            "Git branch",
            current_branch or "detached HEAD",
            hint="Shared-machine workflows normally use master.",
        ))

    status = run_process(
        ["git", "status", "--porcelain=v1"], cwd=ENVY_ROOT, capture=True, check=False
    )
    if status.returncode != 0:
        results.append(error(SECTION_SYSTEM, "Git worktree", "cannot inspect worktree"))
    elif (status.stdout or "").strip():
        count = len((status.stdout or "").splitlines())
        results.append(info(SECTION_SYSTEM, "Git worktree", f"{count} changed path(s)"))
    else:
        results.append(ok(SECTION_SYSTEM, "Git worktree", "clean"))

    leftovers = _workflow_leftovers()
    if leftovers:
        results.append(error(
            SECTION_SYSTEM,
            "workflow leftovers",
            ", ".join(path.name for path in leftovers),
            hint="Inspect these private temporary files, then rerun the interrupted key/setup operation.",
        ))
    else:
        results.append(ok(SECTION_SYSTEM, "workflow leftovers", "none"))

    if platform_name() == "darwin" and shutil.which("sudo"):
        sudo = run_process(["sudo", "-n", "true"], capture=True, check=False)
        if sudo.returncode == 0:
            results.append(ok(SECTION_SYSTEM, "sudo", "non-interactive credential is available"))
        else:
            results.append(info(
                SECTION_SYSTEM,
                "sudo",
                "interactive authentication will be required for system operations",
            ))
    from envy.doctor.checks.sources import run_checks as source_checks
    results.extend(source_checks(machine_manifest()))
    return results


def _workflow_leftovers() -> list[Path]:
    patterns = [
        (ENVY_ROOT / "secrets", ".secrets-plain-*"),
        (ENVY_ROOT / "secrets", ".secrets-encrypted-*"),
        (ENVY_ROOT / "secrets", ".recovery-encrypted-*"),
        (AGE_KEY_DIR, "rotate_*.txt"),
    ]
    paths: list[Path] = []
    for directory, pattern in patterns:
        if directory.exists():
            paths.extend(directory.glob(pattern))
    return sorted(paths)

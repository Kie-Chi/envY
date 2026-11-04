"""Scoped Git commits for repository files changed by envy key and setup."""

import subprocess
from pathlib import Path

import typer

from envy import log
from envy.utils import ENVY_ROOT, SOPS_YAML, SECRETS_FILE, RECOVERY_KEY_FILE
from envy.git_safety import SecretSafetyError
from envy.git_commit import commit_staged_files, stage_repo_files
from envy.keys.identity import get_sops_label
from envy.keys import prompts


def _stage_repo_files(files: list[Path]) -> list[str]:
    """Stage explicit repository files and return changed relative pathspecs."""
    return stage_repo_files(files, repository=ENVY_ROOT)


def _commit_staged_files(changed: list[str], message: str) -> None:
    commit_staged_files(
        changed,
        message,
        confirm=prompts.confirm,
        repository=ENVY_ROOT,
    )


def git_commit_sops_files(operation: str = "") -> None:
    if not (ENVY_ROOT / ".git").exists():
        return

    label = get_sops_label()
    scope = f"sops/{operation}" if operation else "sops"

    changed = _stage_repo_files([SOPS_YAML, SECRETS_FILE, RECOVERY_KEY_FILE])

    if not changed:
        log.hint("sops files: no changes to commit")
        return

    names = [Path(f).name for f in changed]
    msg = f"chore({scope}): update keys on {label} ({', '.join(names)})"
    _commit_staged_files(changed, msg)


def _key_repo_snapshot() -> tuple[tuple[bool, bytes], ...]:
    return tuple(
        (path.exists(), path.read_bytes() if path.exists() else b"")
        for path in (SOPS_YAML, SECRETS_FILE, RECOVERY_KEY_FILE)
    )


def _run_key_operation(operation: str, function, *args, **kwargs):
    """Commit only repository files changed by one successful key transaction."""
    before = _key_repo_snapshot()
    try:
        result = function(*args, **kwargs)
    except (OSError, RuntimeError, ValueError, SecretSafetyError, subprocess.SubprocessError) as exc:
        log.error("key", str(exc))
        raise typer.Exit(code=1) from exc
    if _key_repo_snapshot() != before:
        git_commit_sops_files(operation)
    return result


def git_commit_setup_files(machine_path: Path) -> None:
    """Offer one scoped commit for files managed by an envy setup save."""
    if not (ENVY_ROOT / ".git").exists():
        return

    changed = _stage_repo_files([
        machine_path,
        SOPS_YAML,
        SECRETS_FILE,
        RECOVERY_KEY_FILE,
    ])
    if not changed:
        log.hint("setup files: no changes to commit")
        return

    names = [Path(path).name for path in changed]
    message = (
        f"chore(setup): update {machine_path.stem} configuration "
        f"({', '.join(names)})"
    )
    _commit_staged_files(changed, message)

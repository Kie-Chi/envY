"""Shared-branch synchronization and scope-aware multi-remote push workflows."""

from __future__ import annotations

import re

import typer

from envy import log
from envy.git_safety import SecretSafetyError, assert_git_secret_safety
from envy.host import require_current_machine_file
from envy.journal import record_operation
from envy.process import run_process
from envy.utils import ENVY_ROOT, current_machine_id, machine_build_attr, platform_name, run_apply
from envy.workflows.system import refine_before_apply


def complete_remotes(incomplete: str) -> list[str]:
    """Return configured Git remotes matching a shell-completion prefix."""
    return [name for name in git_output("remote").split() if name.startswith(incomplete)]


def complete_branches(incomplete: str) -> list[str]:
    """Return local branch names matching a shell-completion prefix."""
    raw = git_output("for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [name for name in raw.splitlines() if name.startswith(incomplete)]


def git_output(*args: str) -> str:
    result = run_process(["git", *args], cwd=ENVY_ROOT, capture=True, check=False)
    return (result.stdout or "").strip()


def git_output_checked(*args: str) -> str:
    result = run_process(["git", *args], cwd=ENVY_ROOT, capture=True, check=False)
    if result.returncode != 0:
        log.error("git", "Git query failed", command=" ".join(args))
        raise typer.Exit(code=result.returncode)
    return (result.stdout or "").strip()


def changed_paths() -> list[str]:
    result = run_process(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=ENVY_ROOT, capture=True, check=True,
    )
    paths: list[str] = []
    entries = (result.stdout or "").split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        if len(entry) < 4:
            index += 1
            continue
        status = entry[:2]
        paths.append(entry[3:])
        index += 2 if "R" in status or "C" in status else 1
    return paths


def ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def selected_remotes(remote: str | None) -> list[str]:
    if remote:
        return [remote]
    raw = git_output_checked("remote")
    return raw.split() if raw else []


def affected_machines(paths: list[str]) -> tuple[list[str], bool]:
    known = sorted(
        f"{platform}/{path.stem}"
        for platform in ("darwin", "linux")
        for path in (ENVY_ROOT / "hosts" / platform).glob("*.nix")
        if path.is_file()
    )
    affected: set[str] = set()
    shared = False
    for path in paths:
        match = re.fullmatch(r"hosts/(darwin|linux)/([^/]+)\.nix", path)
        if match:
            affected.add(f"{match.group(1)}/{match.group(2)}")
        else:
            shared = True
    return (known if shared else sorted(affected), shared)


def outgoing_impact(
    remotes: list[str], branch: str, new_branches: set[str],
) -> tuple[list[str], set[str], dict[str, int]]:
    paths: list[str] = []
    commits: set[str] = set()
    counts: dict[str, int] = {}
    for remote in remotes:
        revision = "HEAD" if remote in new_branches else f"{remote}/{branch}..HEAD"
        remote_commits = [
            line for line in git_output_checked("rev-list", revision).splitlines() if line
        ]
        counts[remote] = len(remote_commits)
        commits.update(remote_commits)
        if remote_commits:
            paths.extend(
                line.strip().strip('"')
                for line in git_output_checked(
                    "log", "--format=", "--name-only", revision
                ).splitlines()
                if line.strip()
            )
    return ordered_unique(paths), commits, counts


def enforce_push_scope(
    paths: list[str], *, machine_only: bool, self_only: bool,
) -> tuple[list[str], bool]:
    affected, shared = affected_machines(paths)
    if self_only:
        selected = current_machine_id()
        expected = f"hosts/{platform_name()}/{selected}.nix"
        outside = [path for path in paths if path != expected]
        if outside:
            log.error("git", "--self refuses changes outside the selected machine file", machine=selected)
            for path in outside:
                log.hint(path)
            raise typer.Exit(code=1)
    elif machine_only and shared:
        log.error("git", "--machine-only refuses shared changes")
        for path in paths:
            if not re.fullmatch(r"hosts/(?:darwin|linux)/[^/]+\.nix", path):
                log.hint(path)
        raise typer.Exit(code=1)
    return affected, shared


def enforce_secret_safety(outgoing_commits: set[str]) -> None:
    try:
        assert_git_secret_safety(outgoing_commits=outgoing_commits)
    except SecretSafetyError as exc:
        log.error("git", str(exc))
        raise typer.Exit(code=1) from exc


def show_push_impact(
    *,
    paths: list[str],
    worktree_paths: list[str],
    outgoing_commits: set[str],
    counts: dict[str, int],
    affected: list[str],
    shared: bool,
    branch: str,
) -> None:
    change_scope = "shared" if shared else ("machine-only" if paths else "history-only")
    log.info(
        "git", "push impact", change_scope=change_scope, files=len(paths),
        worktree=len(worktree_paths), outgoing_commits=len(outgoing_commits),
    )
    for remote, count in counts.items():
        log.info("git", "push destination", branch=f"{remote}/{branch}", commits=count)
    for path in paths:
        log.hint(path)
    if affected:
        log.info("host", "affected machine targets", machines=", ".join(affected))
    if shared:
        log.hint("Recommended before pushing shared changes: envy check --all")


def confirm_push_scope(
    *, shared: bool, affected: list[str], remotes: list[str], branch: str,
    has_worktree_changes: bool,
) -> bool:
    action = "Commit and push" if has_worktree_changes else "Push"
    destinations = ", ".join(f"{remote}/{branch}" for remote in remotes)
    if shared:
        return typer.confirm(
            f"{action} SHARED changes affecting {len(affected)} machine(s) to {destinations}?",
            default=None,
        )
    machines = ", ".join(affected) if affected else "no machine files"
    return typer.confirm(
        f"{action} machine-only changes for {machines} to {destinations}?", default=None
    )


def run_checked_git(args: list[str], action: str) -> None:
    result = run_process(["git", *args], cwd=ENVY_ROOT, check=False)
    if result.returncode != 0:
        log.error("git", f"{action} failed")
        raise typer.Exit(code=result.returncode)


def preflight_push_remotes(remotes: list[str], branch: str) -> set[str]:
    new_branches: set[str] = set()
    for remote in remotes:
        log.step("git", "checking push destination", branch=f"{remote}/{branch}")
        run_checked_git(["fetch", remote], "remote preflight")
        remote_ref = f"{remote}/{branch}"
        verify = run_process(
            ["git", "rev-parse", "--verify", remote_ref],
            cwd=ENVY_ROOT, capture=True, check=False,
        )
        if verify.returncode != 0:
            new_branches.add(remote)
            continue
        remote_ahead = git_output_checked("rev-list", "--count", f"HEAD..{remote_ref}")
        if not remote_ahead.isdigit():
            raise typer.Exit(code=1)
        if int(remote_ahead) > 0:
            log.error("git", "remote branch is ahead; refusing divergent commit", branch=remote_ref)
            log.hint("Stash or commit local work, then run envy sync before pushing.")
            raise typer.Exit(code=1)
    return new_branches


def git_is_ancestor(older: str, newer: str) -> bool:
    result = run_process(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=ENVY_ROOT, capture=True, check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise typer.Exit(code=result.returncode)


def select_sync_target(remote_refs: list[str]) -> str:
    """Select the newest compatible remote ref without comparing to local HEAD.

    Local divergence is handled separately by ``sync`` so it can attempt a
    merge and leave a useful conflict recovery path. Divergence between two
    remotes still stops automatically: there is no safe way to choose which
    remote history should win.
    """
    target = remote_refs[0]
    for remote_ref in remote_refs[1:]:
        if git_is_ancestor(remote_ref, target):
            continue
        if git_is_ancestor(target, remote_ref):
            target = remote_ref
            continue
        log.error(
            "git", "remote branches have diverged; refusing to create a merge commit",
            left=target, right=remote_ref,
        )
        raise typer.Exit(code=1)
    return target


def merge_sync_target(target: str) -> None:
    """Fast-forward or merge one compatible remote into the local branch."""
    if git_is_ancestor(target, "HEAD"):
        log.info("git", "local branch already contains remote history", branch=target)
        return
    if git_is_ancestor("HEAD", target):
        log.step("git", "fast-forwarding shared branch", branch=target)
        run_checked_git(["merge", "--ff-only", target], "fast-forward")
        return

    log.step("git", "merging remote history", branch=target)
    result = run_process(
        ["git", "merge", "--no-edit", target],
        cwd=ENVY_ROOT,
        check=False,
    )
    if result.returncode == 0:
        log.ok("git", "remote history merged", branch=target)
        return

    log.error("git", "automatic merge failed; manual conflict resolution is required", branch=target)
    log.hint("Inspect conflicts: git status")
    log.hint("Resolve each conflict, then stage files: git add <path>")
    log.hint("Finish the merge: git commit")
    log.hint("Or discard the attempted merge: git merge --abort")
    log.hint("After resolving, validate the repository: envy check --all")
    raise typer.Exit(code=result.returncode or 1)


@record_operation(
    "sync",
    detail=lambda **kw: {"remote": kw.get("remote") or "all", "branch": kw.get("branch")},
)
def sync(
    *, remote: str | None, branch: str, no_apply: bool, build_only: bool,
) -> None:
    if changed_paths():
        log.error("git", "working tree is not clean; refusing to synchronize")
        log.hint("Commit or stash the changes first.")
        raise typer.Exit(code=1)
    current_branch = git_output_checked("branch", "--show-current")
    if current_branch != branch:
        log.error("git", "current branch is not the configured shared branch",
                  current=current_branch or "<detached>", expected=branch)
        raise typer.Exit(code=1)
    remotes = selected_remotes(remote)
    if not remotes:
        log.error("git", "no Git remotes are configured")
        raise typer.Exit(code=1)

    remote_refs: list[str] = []
    for selected_remote in remotes:
        log.step("git", "fetching remote", remote=selected_remote)
        run_checked_git(["fetch", selected_remote], "fetch")
        remote_ref = f"{selected_remote}/{branch}"
        verify = run_process(
            ["git", "rev-parse", "--verify", remote_ref],
            cwd=ENVY_ROOT, capture=True, check=False,
        )
        if verify.returncode != 0:
            if remote is not None:
                log.error("git", "remote branch does not exist", branch=remote_ref)
                raise typer.Exit(code=1)
            log.warn("git", "remote does not provide the shared branch", branch=remote_ref)
            continue
        remote_refs.append(remote_ref)
    if not remote_refs:
        log.error("git", "no remote provides the requested shared branch", branch=branch)
        raise typer.Exit(code=1)

    target = select_sync_target(remote_refs)
    merge_sync_target(target)
    if no_apply:
        log.ok("sync", "repository synchronized; apply skipped")
        return
    refine_before_apply()
    require_current_machine_file()
    if build_only:
        machine_id = current_machine_id()
        log.step("host", "building selected machine", machine=machine_id)
        result = run_process(
            ["nix", "build", "--impure", "--no-link", machine_build_attr(machine_id)],
            cwd=ENVY_ROOT, check=False,
        )
        if result.returncode != 0:
            raise typer.Exit(code=result.returncode)
        log.ok("host", "machine build completed", machine=machine_id)
        return
    run_apply()


@record_operation(
    "push",
    detail=lambda **kw: {"remote": kw.get("remote") or "all", "branch": kw.get("branch")},
)
def push(
    *, msg: str, remote: str | None, branch: str, machine_only: bool,
    self_only: bool, yes: bool,
) -> None:
    current_branch = git_output_checked("branch", "--show-current")
    if not current_branch:
        log.error("git", "cannot push from a detached HEAD")
        raise typer.Exit(code=1)
    if current_branch != branch:
        log.error("git", "current branch is not the configured shared branch",
                  current=current_branch, expected=branch)
        log.hint(f"Switch to {branch}, or pass --branch {current_branch} intentionally.")
        raise typer.Exit(code=1)
    remotes = selected_remotes(remote)
    if not remotes:
        log.error("git", "no Git remotes are configured")
        raise typer.Exit(code=1)
    new_branches = preflight_push_remotes(remotes, current_branch)
    worktree_paths = changed_paths()
    outgoing_paths, outgoing_commits, counts = outgoing_impact(remotes, current_branch, new_branches)
    enforce_secret_safety(outgoing_commits)
    paths = ordered_unique([*worktree_paths, *outgoing_paths])
    affected, shared = enforce_push_scope(paths, machine_only=machine_only, self_only=self_only)
    if worktree_paths or outgoing_commits:
        show_push_impact(
            paths=paths, worktree_paths=worktree_paths, outgoing_commits=outgoing_commits,
            counts=counts, affected=affected, shared=shared, branch=current_branch,
        )
        if not yes and not confirm_push_scope(
            shared=shared, affected=affected, remotes=remotes, branch=current_branch,
            has_worktree_changes=bool(worktree_paths),
        ):
            raise typer.Abort()
    if worktree_paths:
        run_checked_git(["add", "-A"], "staging")
        # Revalidate the resulting index before a commit closes the recovery
        # window. This also catches a worktree race after initial preflight.
        enforce_secret_safety(outgoing_commits)
        log.step("git", "committing changes")
        run_checked_git(["commit", "-m", msg], "commit")

    succeeded: list[str] = []
    failed: list[str] = []
    untouched: list[str] = []
    for selected_remote in remotes:
        if selected_remote not in new_branches:
            count = git_output_checked(
                "rev-list", "--count", f"{selected_remote}/{current_branch}..HEAD"
            )
            if not count.isdigit():
                log.error(
                    "git", "invalid outgoing commit count",
                    remote=selected_remote, value=count or "<empty>",
                )
                raise typer.Exit(code=1)
            if int(count) == 0:
                untouched.append(selected_remote)
                continue
        command = ["git", "push"]
        if selected_remote in new_branches:
            command.append("-u")
        command.extend([selected_remote, current_branch])
        result = run_process(command, cwd=ENVY_ROOT, check=False)
        if result.returncode == 0:
            log.ok("git", "push completed", remote=selected_remote, branch=current_branch)
            succeeded.append(selected_remote)
        else:
            log.error("git", "push failed", remote=selected_remote, branch=current_branch)
            failed.append(selected_remote)
    log.info(
        "git", "push summary", succeeded=",".join(succeeded) or "none",
        failed=",".join(failed) or "none", unchanged=",".join(untouched) or "none",
    )
    if failed:
        log.error("git", f"{len(failed)} remote(s) failed after partial push processing")
        for selected_remote in failed:
            log.hint(f"Retry: git -C {ENVY_ROOT} push {selected_remote} {current_branch}")
        raise typer.Exit(code=1)

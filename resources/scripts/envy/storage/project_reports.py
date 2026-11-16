"""Read-only reports for explicitly supplied Git projects and known output paths."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
from pathlib import Path

from envy.storage import reporting
from envy.storage.inspection import item
from envy.storage.measurement import ScanBudget, ScanLimit, open_directory


CANDIDATES = ("target", "node_modules", "dist", "build", ".venv")
MARKERS = (
    "Cargo.toml", "Cargo.lock", "package.json", "package-lock.json",
    "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt", "setup.py",
    "go.mod", "go.sum",
)
COMMAND_BYTES = 64 * 1024
NOTE = ("Project output may contain tracked or irreplaceable data. Git ignore and open-file "
        "observations do not authorize deletion; report only.")


def _reason(error: OSError) -> str:
    if isinstance(error, ScanLimit):
        return "scan_budget_exhausted"
    return {
        errno.EACCES: "permission_denied", errno.EPERM: "permission_denied",
        errno.ENOENT: "path_missing", errno.ELOOP: "symlink_skipped",
        errno.ENOTDIR: "not_directory", errno.ESTALE: "directory_changed",
    }.get(error.errno, "filesystem_error")


def _open_directory(path: Path, budget: ScanBudget) -> int:
    if len(path.parts) > budget.max_depth:
        raise ScanLimit("project_path_depth_limit")
    for _ in path.parts:
        budget.check(entry=True)
    descriptor = open_directory(path)
    try:
        budget.check()
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _command(argv: list[str], budget: ScanBudget, *, env=None) -> subprocess.CompletedProcess:
    try:
        budget.check()
        result = reporting.probe(argv, budget=budget, timeout=5,
                                 max_bytes=COMMAND_BYTES, env=env)
        budget.check()
        return result
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 124 if isinstance(exc, ScanLimit) else 126,
                                           "", "")


def _command_reason(result: subprocess.CompletedProcess) -> str:
    return {124: "command_timeout_or_budget", 125: "command_output_limit",
            127: "command_unavailable"}.get(result.returncode, "command_failed")


def _git(root: Path, args: list[str], budget: ScanBudget) -> subprocess.CompletedProcess:
    # Ignore inherited repository routing and avoid optional index writes or fsmonitor hooks.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return _command(["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
                    budget, env=env)


def _git_state(root: Path, relative: str, budget: ScanBudget) -> dict:
    ignored = _git(root, ["check-ignore", "--no-index", "-q", "--", relative], budget)
    tracked = _git(root, ["ls-files", "--cached", "-z", "--", relative], budget)
    reasons = []
    ignored_value = tracked_value = None
    if ignored.returncode in (0, 1) and not ignored.stderr:
        ignored_value = ignored.returncode == 0
    else:
        reasons.append("ignore_" + _command_reason(ignored))
    if tracked.returncode == 0 and not tracked.stderr:
        # A directory match includes index entries anywhere below it, including gitlinks.
        tracked_value = bool(tracked.stdout)
    else:
        reasons.append("tracked_" + _command_reason(tracked))
    return {"ignored": ignored_value, "tracked": tracked_value, "reasons": reasons}


def _activity(path: Path, budget: ScanBudget) -> dict:
    try:
        descriptor = _open_directory(path, budget)
        os.close(descriptor)
    except OSError as exc:
        return {"status": "unknown", "reason": _reason(exc), "scope": "visible-open-files"}
    result = _command(["lsof", "-nP", "-F", "p", "+D", str(path)], budget)
    state = {"status": "unknown", "reason": None, "scope": "visible-open-files"}
    if result.returncode == 0 and any(line.startswith("p") and line[1:].isdigit()
                                      for line in result.stdout.splitlines()):
        state["status"] = "active"
    elif result.returncode == 1 and not result.stdout and not result.stderr:
        state["status"] = "inactive"
    else:
        state["reason"] = ("visibility_limited" if result.stderr and result.returncode in (0, 1)
                           else _command_reason(result))
    return state


def _unsafe_root(root: Path, home: Path) -> bool:
    return root == Path(root.anchor) or root == home or root in home.parents


def _inspect_root(root: Path, report: dict, budget: ScanBudget) -> list[dict]:
    entries = []
    budget.check()
    descriptor = _open_directory(root, budget)
    try:
        for marker in MARKERS:
            record = {"present": None, "reason": None}
            try:
                budget.check(entry=True)
                os.stat(marker, dir_fd=descriptor, follow_symlinks=False)
                record["present"] = True
            except FileNotFoundError:
                record["present"] = False
            except OSError as exc:
                record["reason"] = _reason(exc)
                report["reasons"].append(marker + ":" + record["reason"])
            report["files"][marker] = record
        for relative in CANDIDATES:
            path = root / relative
            candidate = {"relativePath": relative, "path": str(path), "present": None,
                         "status": "unknown", "reasons": [], "itemId": None}
            report["candidates"].append(candidate)
            try:
                budget.check(entry=True)
                info = os.stat(relative, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                candidate.update(present=False, status="absent")
                continue
            except OSError as exc:
                candidate["reasons"].append(_reason(exc))
                report["reasons"].append(relative + ":" + _reason(exc))
                continue
            candidate["present"] = True
            if not stat.S_ISDIR(info.st_mode):
                reason = "symlink_skipped" if stat.S_ISLNK(info.st_mode) else "not_directory"
                candidate.update(status="skipped", reasons=[reason])
                report["reasons"].append(relative + ":" + reason)
                continue
            identifier = "project-" + hashlib.sha256(os.fsencode(root)).hexdigest()[:16] + "-" + relative
            entry = item(identifier, path, "project-output", NOTE, budget)
            entry.update(projectRoot=str(root), gitRoot=report["gitRoot"], relativePath=relative,
                         git=_git_state(root, relative, budget), activity=_activity(path, budget))
            candidate.update(status=entry["status"], itemId=identifier, reasons=list(entry["reasons"]))
            report["itemIds"].append(identifier)
            entries.append(entry)
            report["reasons"].extend(relative + ":" + reason for reason in entry["reasons"])
            report["reasons"].extend(relative + ":" + reason for reason in entry["git"]["reasons"])
            if entry["activity"]["status"] == "unknown":
                report["reasons"].append(relative + ":activity_" + entry["activity"]["reason"])
    finally:
        os.close(descriptor)
    return entries


def inspect(projects: list[Path], *, home: Path, budget: ScanBudget) -> dict:
    """Inspect explicit directory scopes within verified Git roots; never discover projects."""
    reports = []
    items = []
    seen = {}
    home = home.absolute()
    for supplied in projects:
        requested = supplied.absolute()
        report = {"requestedPath": str(supplied), "root": None, "gitRoot": None, "status": "unknown",
                  "reasons": [], "files": {}, "candidates": [], "itemIds": [],
                  "action": "report"}
        reports.append(report)
        try:
            budget.check()
            if ".." in requested.parts or _unsafe_root(requested, home):
                report.update(status="rejected", reasons=["unsafe_project_root"])
                continue
            descriptor = _open_directory(requested, budget)
            os.close(descriptor)
            git = _git(requested, ["rev-parse", "--show-toplevel"], budget)
            if git.returncode or git.stderr:
                report["reasons"].append("git_root_" + _command_reason(git))
                continue
            # Remove only Git's final newline, preserving spaces in directory names.
            output = git.stdout.removesuffix("\n")
            root = Path(output)
            if not output or not root.is_absolute() or ".." in root.parts or not requested.is_relative_to(root):
                report.update(status="rejected", reasons=["invalid_git_root"])
                continue
            report.update(root=str(requested), gitRoot=str(root))
            if _unsafe_root(root, home):
                report.update(status="rejected", reasons=["unsafe_project_root"])
                continue
            if requested in seen:
                previous = seen[requested]
                report.update(status="duplicate", duplicateOf=previous["requestedPath"],
                              itemIds=list(previous["itemIds"]))
                continue
            seen[requested] = report
            items.extend(_inspect_root(requested, report, budget))
            report["status"] = "partial" if report["reasons"] else "complete"
        except (OSError, ValueError) as exc:
            reason = _reason(exc) if isinstance(exc, OSError) else "invalid_project_path"
            report.update(status="rejected" if reason in ("symlink_skipped", "not_directory")
                          else "unknown")
            report["reasons"].append(reason)
    warnings = [report["requestedPath"] + ": " + ", ".join(report["reasons"])
                for report in reports if report["reasons"]]
    if projects:
        warnings.append("Project reports do not authorize cleanup. Ignored paths can contain valuable data; "
                        "inactive means no visible open files, not proof of disuse.")
    return {"items": items, "projects": reports, "warnings": warnings}

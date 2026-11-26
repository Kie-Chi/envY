"""Bounded, read-only application evidence; no lifecycle or deletion grants."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from envy.storage import inspection
from envy.storage.measurement import ScanBudget, ScanLimit, open_directory
from envy.storage.reporting import probe, read_plist


_VERSION = re.compile(
    r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_SYSTEM_APPLICATIONS = Path("/Applications")
_NOTE = "Report only; version, size and mtime do not prove a completed update or safe removal."


def _version_key(name: str) -> tuple | None:
    match = _VERSION.fullmatch(name)
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    parts = []
    if prerelease:
        for value in prerelease.split("."):
            if value.isdigit():
                if len(value) > 1 and value.startswith("0"):
                    return None
                parts.append((0, int(value)))
            else:
                parts.append((1, value))
    return (int(major), int(minor), int(patch),
            (0, tuple(parts)) if prerelease else (1, ()))


def _reason(error: Exception) -> str:
    if isinstance(error, ScanLimit):
        return "scan_budget_exhausted"
    if isinstance(error, FileNotFoundError):
        return "absent"
    if isinstance(error, PermissionError):
        return "permission_denied"
    return "invalid_or_unavailable"


def _children(path: Path, budget: ScanBudget):
    """Stream metadata from a bound directory without following any links."""
    budget.check()
    descriptor = open_directory(path)
    try:
        with os.scandir(descriptor) as entries:
            while True:
                budget.check()
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                budget.check(entry=True)
                yield entry.name, entry.stat(follow_symlinks=False)
    finally:
        os.close(descriptor)


def _processes(budget: ScanBudget) -> dict:
    result = probe(["ps", "-axo", "pid=,comm="], budget=budget)
    if result.returncode:
        reason = {124: "process_probe_timeout", 125: "process_probe_output_limit",
                  127: "process_probe_unavailable"}.get(result.returncode, "process_probe_failed")
        return {"status": "unknown", "returncode": result.returncode, "entries": None, "reason": reason}
    entries = []
    reason = None
    try:
        for line in result.stdout.splitlines():
            budget.check()
            if not line.strip():
                continue
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                reason = "invalid_process_projection"
                continue
            if not fields[1].startswith("/"):
                reason = "non_absolute_process_executable"
                continue
            entries.append((int(fields[0]), Path(fields[1])))
    except ScanLimit:
        return {"status": "unknown", "returncode": 124, "entries": entries,
                "reason": "scan_budget_exhausted"}
    return {"status": "unknown" if reason else "complete", "returncode": 0, "entries": entries,
            "reason": reason}


def _current(releases: Path, names: set[str], budget: ScanBudget) -> dict:
    """Read only the link text; never resolve or scan through current."""
    descriptor = None
    try:
        budget.check()
        descriptor = open_directory(releases.parent)
        target = os.readlink("current", dir_fd=descriptor)
        target_path = Path(target) if Path(target).is_absolute() else releases.parent / target
        normalized = Path(os.path.normpath(target_path))
        if normalized.parent != releases or normalized.name not in names:
            return {"status": "unknown", "version": None, "reason": "current_outside_releases_or_invalid"}
        return {"status": "complete", "version": normalized.name}
    except OSError as exc:
        return {"status": "unknown", "version": None, "reason": _reason(exc)}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _codex(home: Path, budget: ScanBudget, processes: dict, items: list) -> dict:
    releases = home / ".codex/packages/app-server-daemon/releases"
    report = {"status": "complete", "path": str(releases), "versions": [],
              "current": None, "rollbackVersion": None, "processStatus": processes["status"],
              "inactiveSince": None, "processReason": processes.get("reason")}
    directories = []
    try:
        for name, info in _children(releases, budget):
            key = _version_key(name)
            if key is None or not stat.S_ISDIR(info.st_mode):
                report["versions"].append({"version": name, "status": "unknown",
                                           "protected": True, "reason": "invalid_version_or_type",
                                           "active": None, "inactiveSince": None})
            else:
                directories.append((key, name))
    except OSError as exc:
        report.update(status="absent" if isinstance(exc, FileNotFoundError) else "unknown",
                      reason=_reason(exc))
    directories.sort()
    names = {name for _, name in directories}
    current = _current(releases, names, budget)
    report["current"] = current
    current_key = _version_key(current["version"]) if current["version"] else None
    predecessors = [(key, name) for key, name in directories if current_key and key < current_key]
    rollback = predecessors[-1][1] if predecessors else None
    report["rollbackVersion"] = rollback
    protection_unknown = (current["status"] != "complete" or processes["status"] != "complete"
                          or report["status"] != "complete")
    if protection_unknown and report["status"] == "complete":
        report["status"] = "unknown"
    active_names = set()
    for _, executable in processes["entries"] or []:
        try:
            relative = executable.relative_to(releases)
        except ValueError:
            continue
        if len(relative.parts) >= 2:
            active_names.add(relative.parts[0])
    for _, name in directories:
        reasons = []
        if name == current["version"]:
            reasons.append("current")
        if name == rollback:
            reasons.append("rollback")
        if name in active_names:
            reasons.append("active")
        if protection_unknown:
            reasons.append("protection_unknown")
        # No trusted deactivation timestamp exists, including for unreferenced releases.
        reasons.append("inactive_time_unknown")
        row = inspection.item("codex-daemon-" + name, releases / name, "application-runtime", _NOTE, budget)
        items.append(row)
        report["versions"].append({"version": name, "status": "report", "itemId": row["id"],
                                   "active": True if name in active_names else (
                                       False if processes["status"] == "complete" else None),
                                   "protected": True, "protectionReasons": reasons,
                                   "inactiveSince": None})
    return report


def _validate_darwin_directory(raw: str, leaf: str, uid: int, budget: ScanBudget) -> Path:
    """Accept only the documented /var alias and a UID-owned Darwin user base."""
    text = raw.strip()
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ValueError("invalid_getconf_path")
    if text.startswith("/var/"):
        text = "/private" + text
    if text.endswith("/"):
        text = text[:-1]
    match = re.fullmatch(r"/private/var/folders/[A-Za-z0-9_-]{2}/[A-Za-z0-9_-]{1,128}/([TCX])", text)
    if match is None or match.group(1) != leaf:
        raise ValueError("invalid_getconf_path")
    path = Path(text)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != 0:
            raise ValueError("invalid_root_owner")
        for index, component in enumerate(path.parts[1:], 1):
            budget.check()
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise ValueError("invalid_directory_component")
            # Ancestors can belong to root or this UID; the user base and leaf must belong to this UID.
            owners = {uid} if index >= 5 else {0, uid}
            if before.st_uid not in owners:
                raise ValueError("foreign_directory_owner")
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino, opened.st_uid) != (before.st_dev, before.st_ino, before.st_uid):
                os.close(child)
                raise ValueError("directory_changed")
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return path


def _darwin_roots(budget: ScanBudget) -> dict:
    reports = {}
    paths = {}
    for label, variable, leaf in (("temp", "DARWIN_USER_TEMP_DIR", "T"),
                                  ("cache", "DARWIN_USER_CACHE_DIR", "C")):
        result = probe(["getconf", variable], budget=budget, max_bytes=4096)
        if result.returncode:
            reports[label] = {"status": "unknown", "path": None, "returncode": result.returncode}
            continue
        if not result.stdout.strip():
            reports[label] = {"status": "missing", "path": None, "reason": "empty_getconf_output"}
            continue
        try:
            path = _validate_darwin_directory(result.stdout, leaf, os.getuid(), budget)
        except (OSError, ValueError) as exc:
            reports[label] = {"status": "unknown" if isinstance(exc, OSError) else "invalid", "path": None,
                              "reason": _reason(exc) if isinstance(exc, OSError) else str(exc)}
            continue
        paths[label] = path
        reports[label] = {"status": "complete", "path": str(path)}
    if len(paths) != 2:
        return {"status": "unknown", "roots": reports, "userBase": None, "paths": None,
                "reason": "incomplete_getconf"}
    if paths["temp"].parent != paths["cache"].parent:
        return {"status": "invalid", "roots": reports, "userBase": None, "paths": None,
                "reason": "getconf_userbase_mismatch"}
    return {"status": "complete", "roots": reports, "userBase": str(paths["temp"].parent),
            "paths": paths}


def _bundle(path: Path, budget: ScanBudget) -> dict:
    result = {"path": str(path), "status": "unknown", "bundleIdentifier": None,
              "version": None, "buildVersion": None}
    try:
        budget.check()
        values = read_plist(path / "Contents/Info.plist", budget=budget)
        for source, target in (("CFBundleIdentifier", "bundleIdentifier"),
                               ("CFBundleShortVersionString", "version"),
                               ("CFBundleVersion", "buildVersion")):
            value = values.get(source)
            if isinstance(value, str) and 0 < len(value) <= 256:
                result[target] = value
        result["status"] = "complete" if result["bundleIdentifier"] and result["version"] else "unknown"
        if result["status"] == "unknown":
            result["reason"] = "incomplete_bundle_metadata"
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        result.update(status="absent" if isinstance(exc, FileNotFoundError) else "unknown", reason=_reason(exc))
    return result


def _home_manager_bundle(home: Path, budget: ScanBudget) -> dict:
    """Resolve only the known installed entry for metadata, never a scan root."""
    path = home / "Applications/Home Manager Apps/Visual Studio Code.app"
    try:
        budget.check()
        resolved = path.resolve(strict=True)
        budget.check()
    except (OSError, RuntimeError) as exc:
        return {"path": str(path), "status": "absent" if isinstance(exc, FileNotFoundError) else "unknown",
                "bundleIdentifier": None, "version": None, "buildVersion": None, "reason": _reason(exc)}
    result = _bundle(resolved, budget)
    result.update(path=str(path), resolvedPath=str(resolved))
    return result


def _activity(processes: dict, application: str) -> bool | None:
    if processes["entries"] is None:
        return None
    for _, executable in processes["entries"]:
        parts = executable.parts
        if application == "shipit" and (executable.name == "ShipIt" or "com.microsoft.VSCode.ShipIt" in parts):
            return True
        if application == "vscode" and ("Visual Studio Code.app" in parts or executable.name == "Code"):
            return True
        if application == "chrome" and ("Google Chrome.app" in parts or executable.name == "Google Chrome"):
            return True
    return False if processes["status"] == "complete" else None


def _staging(path: Path, prefix: str, budget: ScanBudget, items: list, *, temp: bool = False) -> dict:
    report = {"path": str(path), "status": "complete", "entries": [], "completion": None}
    try:
        for name, info in _children(path, budget):
            if not (name.startswith(("ShipIt.", "com.microsoft.VSCode.ShipIt.")) if temp
                    else name.startswith("update.")):
                continue
            if not stat.S_ISDIR(info.st_mode):
                report["entries"].append({"name": name, "status": "unknown", "reason": "invalid_staging_type",
                                          "completion": None})
                continue
            identifier = prefix + "-" + name
            row = inspection.item(identifier, path / name, "update-staging", _NOTE, budget)
            items.append(row)
            report["entries"].append({"name": name, "itemId": identifier, "status": "report",
                                      "completion": None, "completionStatus": "unknown",
                                      "inactiveSince": None,
                                      "bundle": _bundle(path / name / "Visual Studio Code.app", budget)})
    except OSError as exc:
        report.update(status="absent" if isinstance(exc, FileNotFoundError) else "unknown", reason=_reason(exc))
    return report


def inspect(home: Path, *, budget: ScanBudget, platform: str) -> dict:
    """Project process and bundle metadata without interpreting it as cleanup authority."""
    if platform != "darwin":
        return {"items": [], "reports": {"applications": {"status": "unsupported", "platform": platform}},
                "warnings": ["Application storage reports are unsupported on this platform."]}
    items = []
    processes = _processes(budget)
    codex = _codex(home, budget, processes, items)
    roots = _darwin_roots(budget)
    vscode = {"status": "report", "completion": None, "completionStatus": "unknown",
              "running": _activity(processes, "vscode"), "updaterRunning": _activity(processes, "shipit"),
              "processStatus": processes["status"], "processReason": processes.get("reason"),
              "installed": [_bundle(base / "Visual Studio Code.app", budget)
                            for base in (_SYSTEM_APPLICATIONS, home / "Applications")]
                           + [_home_manager_bundle(home, budget)],
              "staging": [_staging(home / "Library/Caches/com.microsoft.VSCode.ShipIt", "vscode-shipit",
                                   budget, items)]}
    chrome = {"status": "report", "running": _activity(processes, "chrome"),
              "processStatus": processes["status"], "processReason": processes.get("reason"),
              "codeSignClone": None, "completion": None, "completionStatus": "unknown"}
    paths = roots.pop("paths")
    if paths:
        for label, path in paths.items():
            vscode["staging"].append(_staging(path, "vscode-" + label, budget, items, temp=True))
        # X is derived only from the two validated, matching getconf roots.
        try:
            x_root = _validate_darwin_directory(str(paths["temp"].parent / "X"), "X", os.getuid(), budget)
        except (OSError, ValueError) as exc:
            chrome["codeSignClone"] = {"status": "unknown", "path": None, "completion": None,
                                       "reason": _reason(exc) if isinstance(exc, OSError) else str(exc)}
        else:
            clone = x_root / "com.google.Chrome.code_sign_clone"
            row = inspection.item("chrome-code-sign-clone", clone, "update-staging", _NOTE, budget)
            items.append(row)
            chrome["codeSignClone"] = {"itemId": row["id"], "path": str(clone), "completion": None}
    warnings = ["Application versions and mtime do not prove update completion or an inactive age; all rows report only."]
    if processes["status"] != "complete":
        warnings.append("Application process activity is unknown: " + processes["reason"] + ".")
    if roots["status"] != "complete":
        warnings.append("Darwin temporary paths are unavailable or invalid; external staging was not scanned.")
    warnings.extend(row["id"] + ": " + ", ".join(row["reasons"]) for row in items if row["reasons"])
    if codex["status"] == "unknown":
        warnings.append("Codex release protection evidence is incomplete; all releases remain protected.")
    return {"items": items, "reports": {"codex": codex, "vscode": vscode,
                                      "darwinTemporary": roots, "chrome": chrome}, "warnings": warnings}

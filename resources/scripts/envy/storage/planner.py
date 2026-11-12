"""Use organize exclusively for matching; envY owns every mutation."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from pathlib import Path

import yaml

from envy.process import run_process
from envy.secure_io import secure_temporary_path
from envy.storage.model import (MODEL_VERSION, Policy, Rule, StorageError, entry_bytes,
                                expand, newest_mtime, no_symlinks, rule_details,
                                signature, tree_digest, validate_entry)
from envy.storage.state import write_record


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _age_days(rule: Rule) -> float:
    return rule.min_age_days


def _recursive_age_filter(rule: Rule) -> dict:
    code = (
        "import os, stat, time\n"
        "cutoff = time.time() - " + repr(float(_age_days(rule) * 86400)) + "\n"
        "def fail(error):\n"
        "    raise error\n"
        "try:\n"
        "    for current, directories, files in os.walk(path, followlinks=False, onerror=fail):\n"
        "        for entry in [current] + [os.path.join(current, name) for name in directories + files]:\n"
        "            info = os.lstat(entry)\n"
        "            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):\n"
        "                return False\n"
        "            if info.st_mtime > cutoff:\n"
        "                return False\n"
        "    return True\n"
        "except OSError:\n"
        "    raise\n"
    )
    return {"python": code}


def _name_filter(rule: Rule) -> dict:
    return {"python": "return path.name in " + _json(list(rule.names)) +
            " and len(path.name) > 0"}


def active_processes(rule: Rule) -> list[str]:
    running = []
    for pattern in rule.processes:
        executable = "/usr/bin/pgrep" if Path("/usr/bin/pgrep").exists() else "pgrep"
        argv = [executable, "-f", pattern] if "/" in pattern else [executable, "-x", pattern]
        result = run_process(argv, capture=True, check=False, timeout=10)
        if result.returncode == 0:
            running.append(pattern)
        elif result.returncode != 1:
            raise StorageError("Process activity could not be verified")
    return running


def _config(rule: Rule, root: Path) -> dict:
    filters = []
    if rule.names:
        filters.append(_name_filter(rule))
    if rule.targets == "dirs" and rule.recursive_age:
        filters.append(_recursive_age_filter(rule))
    else:
        filters.append({"lastmodified": {"days": rule.min_age_days, "mode": "older"}})
    if rule.targets == "files" and rule.min_size_bytes:
        filters.append({"size": ">= " + str(rule.min_size_bytes) + " B"})
    if rule.extensions:
        filters.append({"extension": list(rule.extensions)})
    location = {"path": str(root)}
    if rule.depth is not None:
        location["max_depth"] = rule.depth - 1
    return {"name": rule.id, "locations": [location], "subfolders": rule.depth is None,
            "targets": rule.targets, "filters": filters,
            "actions": [{"echo": "envy-storage-candidate"}]}


def _candidate(path: Path, root: Path, rule: Rule, policy: Policy, skipped: list[dict],
               blocked: str = "") -> dict | None:
    try:
        info = validate_entry(path, root, rule, policy)
        if rule.targets == "files":
            if time.time() - info.st_mtime < rule.min_age_days * 86400:
                return None
            if info.st_size < rule.min_size_bytes:
                return None
            if rule.extensions and path.suffix.lstrip(".").casefold() not in {
                ext.casefold() for ext in rule.extensions
            }:
                return None
        else:
            if rule.recursive_age:
                if time.time() - newest_mtime(path) < rule.min_age_days * 86400:
                    return None
            elif time.time() - info.st_mtime < rule.min_age_days * 86400:
                return None
        entry = {"path": str(path), "root": str(root), "rule": rule.id,
                 "action": rule.action, "targets": rule.targets,
                 "source": rule.source, "overriddenFields": list(rule.overridden_fields),
                 "size": entry_bytes(path, rule.targets), "signature": signature(info)}
        if rule.targets == "dirs":
            entry["treeHash"] = tree_digest(path)
        if blocked:
            entry["blocked"] = blocked
        return entry
    except PermissionError as exc:
        raise StorageError(f"Rule {rule.id}: candidate inspection denied: {path}") from exc
    except (OSError, StorageError) as exc:
        skipped.append({"path": str(path), "rule": rule.id, "reason": str(exc)})
        return None


def _scan_error(error: OSError) -> None:
    raise error


def _newest_matching(root: Path, rule: Rule) -> float | None:
    """Newest entry that passes the rule's names and extension filters."""
    newest: float | None = None
    if rule.targets == "files":
        extensions = {ext.casefold() for ext in rule.extensions}
        for current, _directories, files in os.walk(root, followlinks=False, onerror=_scan_error):
            for name in files:
                path = Path(current) / name
                if rule.names and name not in rule.names:
                    continue
                if extensions and path.suffix.lstrip(".").casefold() not in extensions:
                    continue
                try:
                    mtime = path.lstat().st_mtime
                except FileNotFoundError:
                    continue
                newest = mtime if newest is None else max(newest, mtime)
        return newest
    base = len(root.parts)
    for current, directories, _files in os.walk(root, followlinks=False, onerror=_scan_error):
        for name in directories:
            path = Path(current) / name
            if rule.names and name not in rule.names:
                continue
            if rule.depth is not None and len(path.parts) - base > rule.depth:
                continue
            try:
                mtime = newest_mtime(path) if rule.recursive_age else path.lstat().st_mtime
            except FileNotFoundError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
        if rule.depth is not None and len(Path(current).parts) - base >= rule.depth:
            directories[:] = []
    return newest


def plan(policy: Policy, state: Path) -> dict:
    matched: list[dict] = []
    skipped: list[dict] = []
    roots_by_rule: dict[str, int] = {}
    blocked_by_rule: dict[str, str] = {}
    for rule in policy.rules:
        try:
            roots = expand(rule.path)
        except OSError as exc:
            raise StorageError(f"Rule {rule.id}: root inspection failed: {exc}") from exc
        roots_by_rule[rule.id] = len(roots)
        if not roots:
            if not rule.optional_root:
                raise StorageError(f"Rule {rule.id}: path pattern matched no roots")
            skipped.append({"path": rule.path, "rule": rule.id, "reason": "Path pattern matched no roots"})
            continue
        running = active_processes(rule)
        blocked = ("Process is running: " + ", ".join(running)) if running else ""
        if blocked:
            skipped.append({"path": rule.path, "rule": rule.id, "reason": blocked})
            blocked_by_rule[rule.id] = blocked
        for root in roots:
            try:
                no_symlinks(root)
                try:
                    info = root.stat()
                except FileNotFoundError:
                    if not rule.optional_root:
                        raise StorageError(f"Rule {rule.id}: root does not exist: {root}")
                    skipped.append({"path": str(root), "rule": rule.id, "reason": "Root does not exist"})
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    raise StorageError("Rule root is not a directory: " + str(root))
                with os.scandir(root):
                    pass
                config = {"rules": [_config(rule, root)]}
                with secure_temporary_path(state, suffix=".yaml") as config_path:
                    config_path.write_text(yaml.safe_dump(config))
                    result = run_process(["organize", "sim", "--format", "jsonl", str(config_path)],
                                         capture=True, check=False, timeout=300)
                if result.returncode:
                    raise StorageError("organize scan failed; no storage plan was accepted")
                report = None
                events = [json.loads(line) for line in (result.stdout or "").splitlines() if line.strip()]
                for event in events:
                    if event.get("type") == "REPORT":
                        report = event
                    if event.get("level") == "error":
                        raise StorageError("organize reported an incomplete scan")
                    if event.get("type") != "MSG" or event.get("msg") != "envy-storage-candidate":
                        continue
                    entry = _candidate(Path(event["path"]), root, rule, policy, skipped, blocked)
                    if entry is None:
                        continue
                    matched.append(entry)
                if report is None or report.get("error_count") != 0:
                    raise StorageError("organize scan did not finish successfully")
            except OSError as exc:
                raise StorageError(f"Rule {rule.id}: root inspection failed: {exc}") from exc
            except StorageError:
                raise
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise StorageError("Invalid organize output; no storage plan was accepted") from exc
    assert_no_conflicts(matched)
    summary = []
    for rule in policy.rules:
        entries = [entry for entry in matched if entry["rule"] == rule.id]
        reason = blocked_by_rule.get(rule.id, "")
        if not entries:
            detail = []
            newest = None
            for root in expand(rule.path):
                if root.is_dir():
                    found = _newest_matching(root, rule)
                    if found is not None:
                        newest = found if newest is None else max(newest, found)
            missing = [item["reason"] for item in skipped if item["rule"] == rule.id
                       and item["reason"] == "Root does not exist"]
            if missing:
                detail.append("root does not exist")
            elif not roots_by_rule.get(rule.id):
                detail.append("path pattern matched no roots")
            elif newest is None:
                detail.append("no entry matches the rule names or extensions")
            elif time.time() - newest < rule.min_age_days * 86400:
                detail.append("newest matching entry is %.1f days old (minimum %d)"
                              % ((time.time() - newest) / 86400, rule.min_age_days))
            elif rule.min_size_bytes:
                detail.append("matching entries are below minSizeBytes")
            else:
                detail.append("all matching entries were skipped by validation")
            reason = "; ".join([reason] + detail) if reason else detail[0]
        summary.append({**rule_details(rule),
                        "roots": roots_by_rule.get(rule.id, 0), "candidates": len(entries),
                        "bytes": sum(entry["size"] for entry in entries),
                        "blocked": sum(1 for entry in entries if entry.get("blocked")),
                        "reason": reason})
    record = {"modelVersion": MODEL_VERSION, "excluded": list(policy.excluded),
              "id": uuid.uuid4().hex, "createdAt": time.time(), "policy": policy.fingerprint,
              "candidates": list(matched), "skipped": skipped, "rules": summary,
              "bytes": sum(entry["size"] for entry in matched)}
    write_record(state, "plans", record)
    return record


def assert_inactive(path: Path, *, ancestors: bool = True) -> None:
    executable = "/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else "lsof"
    probes = [[executable, "-t", "+D", str(path)]] if path.is_dir() else [[executable, "-t", "--", str(path)]]
    if ancestors:
        probes.extend([executable, "-t", "+d", str(parent)] for parent in path.parents
                      if parent != Path.home() and Path.home() in parent.parents)
    for argv in probes:
        result = run_process(argv, capture=True, check=False, timeout=10)
        if result.returncode == 0:
            raise StorageError("File or its project directory is active")
        if result.returncode != 1 or (result.stderr or "").strip():
            raise StorageError("File activity could not be verified")


def file_hash(path: Path) -> str:
    import hashlib

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        before = signature(os.fstat(handle.fileno()))
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if signature(os.fstat(handle.fileno())) != before:
            raise StorageError("File changed while reading")
    return digest


def assert_no_conflicts(candidates: list[dict]) -> None:
    for index, candidate in enumerate(candidates):
        path = Path(candidate["path"])
        for previous in candidates[:index]:
            other = Path(previous["path"])
            if path == other or path in other.parents or other in path.parents:
                raise StorageError(f"Conflicting storage candidates from rules {previous['rule']} and "
                                   f"{candidate['rule']}: {other} overlaps {path}")


def validate(policy: Policy, state: Path) -> dict:
    """Check configs, distinguishing absent optional roots from failed inspection."""
    results = []
    for rule in policy.rules:
        metadata = {"rule": rule.id, **rule_details(rule)}
        try:
            roots = expand(rule.path)
            if not roots:
                results.append({**metadata, "root": rule.path, "ok": rule.optional_root,
                                "skipped": rule.optional_root,
                                "reason": "path pattern matched no roots",
                                "error": "" if rule.optional_root else "path pattern matched no roots"})
                continue
        except (OSError, StorageError) as exc:
            results.append({**metadata, "root": rule.path, "ok": False, "error": str(exc)})
            continue
        for root in roots:
            try:
                no_symlinks(root)
                try:
                    info = root.stat()
                except FileNotFoundError:
                    if not rule.optional_root:
                        raise StorageError("root does not exist")
                    results.append({**metadata, "root": str(root), "ok": True, "skipped": True,
                                    "reason": "root does not exist", "error": ""})
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    raise StorageError("root is not a directory")
                with os.scandir(root):
                    pass
                config = {"rules": [_config(rule, root)]}
                with secure_temporary_path(state, suffix=".yaml") as config_path:
                    config_path.write_text(yaml.safe_dump(config))
                    result = run_process(["organize", "check", str(config_path)],
                                         capture=True, check=False, timeout=120)
                results.append({**metadata, "root": str(root), "ok": result.returncode == 0,
                                "error": "" if result.returncode == 0
                                else ((result.stderr or result.stdout or "organize check failed").strip()[-300:])})
            except (OSError, StorageError) as exc:
                results.append({**metadata, "root": str(root), "ok": False, "error": str(exc)})
    return {"ok": all(item["ok"] for item in results), "rules": results,
            "excluded": list(policy.excluded)}

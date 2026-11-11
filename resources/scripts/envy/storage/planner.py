"""Use organize exclusively for matching; envY owns every mutation."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import yaml

from envy.process import run_process
from envy.secure_io import secure_temporary_path
from envy.storage.model import (Policy, Rule, StorageError, entry_bytes, expand,
                                newest_mtime, no_symlinks, signature, tree_digest,
                                validate_entry)
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
        "    return False\n"
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
                 "size": entry_bytes(path, rule.targets), "signature": signature(info)}
        if rule.targets == "dirs":
            entry["treeHash"] = tree_digest(path)
        if blocked:
            entry["blocked"] = blocked
        return entry
    except (OSError, StorageError) as exc:
        skipped.append({"path": str(path), "rule": rule.id, "reason": str(exc)})
        return None


def _newest_matching(root: Path, rule: Rule) -> float | None:
    """Newest entry that passes the rule's names and extension filters."""
    newest: float | None = None
    if rule.targets == "files":
        extensions = {ext.casefold() for ext in rule.extensions}
        for current, _directories, files in os.walk(root, followlinks=False):
            for name in files:
                path = Path(current) / name
                if rule.names and name not in rule.names:
                    continue
                if extensions and path.suffix.lstrip(".").casefold() not in extensions:
                    continue
                try:
                    mtime = path.lstat().st_mtime
                except OSError:
                    continue
                newest = mtime if newest is None else max(newest, mtime)
        return newest
    base = len(root.parts)
    for current, directories, _files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(current) / name
            if rule.names and name not in rule.names:
                continue
            if rule.depth is not None and len(path.parts) - base > rule.depth:
                continue
            try:
                mtime = newest_mtime(path) if rule.recursive_age else path.lstat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
        if rule.depth is not None and len(Path(current).parts) - base >= rule.depth:
            directories[:] = []
    return newest


def plan(policy: Policy, state: Path) -> dict:
    matched: dict[str, dict] = {}
    skipped: list[dict] = []
    roots_by_rule: dict[str, int] = {}
    blocked_by_rule: dict[str, str] = {}
    for rule in policy.rules:
        roots = expand(rule.path)
        roots_by_rule[rule.id] = len(roots)
        if not roots:
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
                if not root.exists():
                    skipped.append({"path": str(root), "rule": rule.id, "reason": "Root does not exist"})
                    continue
                if not root.is_dir():
                    raise StorageError("Rule root is not a directory: " + str(root))
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
                    key = entry["path"]
                    if key in matched and matched[key]["action"] != entry["action"]:
                        raise StorageError("Conflicting storage actions for " + key)
                    matched.setdefault(key, entry)
                if report is None or report.get("error_count") != 0:
                    raise StorageError("organize scan did not finish successfully")
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise StorageError("Invalid organize output; no storage plan was accepted") from exc
    summary = []
    for rule in policy.rules:
        entries = [entry for entry in matched.values() if entry["rule"] == rule.id]
        reason = blocked_by_rule.get(rule.id, "")
        if not entries:
            detail = []
            newest = None
            for root in expand(rule.path):
                if root.is_dir():
                    found = _newest_matching(root, rule)
                    if found is not None:
                        newest = found if newest is None else max(newest, found)
            if not roots_by_rule.get(rule.id):
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
        summary.append({"id": rule.id, "targets": rule.targets, "action": rule.action,
                        "roots": roots_by_rule.get(rule.id, 0), "candidates": len(entries),
                        "bytes": sum(entry["size"] for entry in entries),
                        "blocked": sum(1 for entry in entries if entry.get("blocked")),
                        "reason": reason})
    record = {"id": uuid.uuid4().hex, "createdAt": time.time(), "policy": policy.fingerprint,
              "candidates": list(matched.values()), "skipped": skipped, "rules": summary,
              "bytes": sum(entry["size"] for entry in matched.values())}
    write_record(state, "plans", record)
    return record


def assert_inactive(path: Path, *, ancestors: bool = True) -> None:
    executable = "/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else "lsof"
    probes = [[executable, "-t", "--", str(path)]]
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


def validate(policy: Policy, state: Path) -> dict:
    """Validate every generated organize config without scanning or writing state."""
    results = []
    for rule in policy.rules:
        roots = expand(rule.path)
        if not roots:
            results.append({"rule": rule.id, "root": rule.path, "ok": False,
                            "error": "path pattern matched no roots"})
            continue
        for root in roots:
            try:
                no_symlinks(root)
                if not root.is_dir():
                    results.append({"rule": rule.id, "root": str(root), "ok": False,
                                    "error": "root is not a directory"})
                    continue
                config = {"rules": [_config(rule, root)]}
                with secure_temporary_path(state, suffix=".yaml") as config_path:
                    config_path.write_text(yaml.safe_dump(config))
                    result = run_process(["organize", "check", str(config_path)],
                                         capture=True, check=False, timeout=120)
                results.append({"rule": rule.id, "root": str(root), "ok": result.returncode == 0,
                                "error": "" if result.returncode == 0
                                else (result.stdout or "").strip()[-300:]})
            except (OSError, StorageError) as exc:
                results.append({"rule": rule.id, "root": str(root), "ok": False,
                                "error": str(exc)})
    return {"ok": all(item["ok"] for item in results), "rules": results}

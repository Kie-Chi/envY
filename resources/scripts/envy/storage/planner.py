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
from envy.storage.model import Policy, StorageError, no_symlinks, signature, validate_file
from envy.storage.state import write_record


def plan(policy: Policy, state: Path) -> dict:
    matched: dict[str, dict] = {}
    skipped = []
    for rule in policy.rules:
        root = Path(rule.path)
        no_symlinks(root)
        if not root.exists():
            skipped.append({"path": str(root), "rule": rule.id, "reason": "Root does not exist"})
            continue
        if not root.is_dir():
            raise StorageError(f"Rule root is not a directory: {root}")
        filters = [{"lastmodified": {"days": rule.minAgeDays, "mode": "older"}},
                   {"size": f">= {rule.minSizeBytes} B"}]
        if rule.extensions:
            filters.append({"extension": list(rule.extensions)})
        config = {"rules": [{"name": rule.id, "locations": [str(root)], "subfolders": True,
                             "targets": "files", "filters": filters,
                             "actions": [{"echo": "envy-storage-candidate"}]}]}
        with secure_temporary_path(state, suffix=".yaml") as config_path:
            config_path.write_text(yaml.safe_dump(config))
            result = run_process(["organize", "sim", "--format", "jsonl", str(config_path)],
                                 capture=True, check=False, timeout=300)
        if result.returncode:
            raise StorageError("organize scan failed; no storage plan was accepted")
        report = None
        try:
            events = [json.loads(line) for line in (result.stdout or "").splitlines() if line.strip()]
            for event in events:
                if event.get("type") == "REPORT":
                    report = event
                if event.get("level") == "error":
                    raise StorageError("organize reported an incomplete scan")
                if event.get("type") != "MSG" or event.get("msg") != "envy-storage-candidate":
                    continue
                path = Path(event["path"])
                try:
                    info = validate_file(path, rule, policy)
                    if info.st_size < rule.minSizeBytes or time.time() - info.st_mtime < rule.minAgeDays * 86400:
                        continue
                    if rule.extensions and path.suffix.lstrip(".").casefold() not in {
                        ext.casefold() for ext in rule.extensions
                    }:
                        continue
                except (OSError, StorageError) as exc:
                    skipped.append({"path": str(path), "rule": rule.id, "reason": str(exc)})
                    continue
                key = str(path)
                if key in matched and matched[key]["action"] != rule.action:
                    raise StorageError(f"Conflicting storage actions for {path}")
                matched.setdefault(key, {"path": key, "rule": rule.id, "action": rule.action,
                                         "size": info.st_size, "signature": signature(info)})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise StorageError("Invalid organize output; no storage plan was accepted") from exc
        if report is None or report.get("error_count") != 0:
            raise StorageError("organize scan did not finish successfully")
    record = {"id": uuid.uuid4().hex, "createdAt": time.time(), "policy": policy.fingerprint,
              "candidates": list(matched.values()), "skipped": skipped,
              "bytes": sum(entry["size"] for entry in matched.values())}
    write_record(state, "plans", record)
    return record


def assert_inactive(path: Path, *, ancestors: bool = True) -> None:
    executable = "/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else "lsof"
    # File descriptors and a process working in an ancestor directory both count.
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

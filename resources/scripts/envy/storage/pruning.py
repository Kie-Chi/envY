"""Scan and reclaim explicitly disposable data with one CLI confirmation."""

from __future__ import annotations

import copy
import os
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path

from envy.secure_io import ensure_private_directory
from envy.storage import lifecycle, maintenance, planner, receipts
from envy.storage.backend import Archive
from envy.storage.model import Policy, StorageError, no_symlinks, signature
from envy.storage.state import records, sync_record, write_record


def _policy(policy: Policy) -> Policy:
    # Zero disables age filtering only for explicit disposable-cache pruning.
    # Keep archive ages and the original run policy, including its fingerprint.
    return replace(policy, rules=tuple(
        replace(rule, min_age_days=0) if rule.prune and rule.action == "quarantine" else rule
        for rule in policy.rules if rule.prune or rule.action == "archive"))


def prepare(policy: Policy, state: Path) -> dict:
    """Build the current scope without moving files or invoking native cleanup."""
    plan = planner.plan(_policy(policy), state)
    native = (maintenance.preview(policy, list(policy.maintenance), bind_targets=True)
              if policy.maintenance else {"execute": False, "items": []})
    candidates = plan["candidates"]
    return {"execute": False, "planId": plan["id"], "policyFingerprint": policy.fingerprint,
            "candidates": copy.deepcopy(candidates), "maintenance": native,
            "skipped": [{"path": item["path"], "rule": item["rule"], "reason": item["blocked"]}
                        for item in candidates if item.get("blocked")],
            "scanNotes": plan["skipped"], "errors": [],
            "summary": {"eligibleItems": sum(not item.get("blocked") for item in candidates),
                        "estimatedBytes": sum(item["size"] for item in candidates if not item.get("blocked")),
                        "nativeActions": list(policy.maintenance)},
            "warnings": []}


def _save(state: Path, result: dict) -> None:
    write_record(state, "prunes", result)
    sync_record(state, "prunes", result["id"])


def _available(path: Path) -> int | None:
    try:
        info = os.statvfs(path)
        return info.f_bavail * info.f_frsize
    except OSError:
        return None


def _expected_skip(error: Exception) -> bool:
    return isinstance(error, FileNotFoundError) or (
        isinstance(error, StorageError) and error.code in {"busy", "candidate_changed"})


def _delete(candidate: dict, policy: Policy, state: Path, result: dict,
            archive: Archive, proof: dict | None) -> None:
    """Stage into a private directory before removing only the selected object."""
    directory = state / "prune-staging" / result["id"]
    no_symlinks(directory)
    ensure_private_directory(directory)
    destination = directory / str(len(result["items"]))
    item = {**candidate, "status": "prepared", "heldPath": str(destination)}
    if proof is not None:
        item.update(proof)
    result["items"].append(item)
    moved = False
    try:
        lifecycle._recheck(policy, candidate)
        if proof is not None:
            if archive.content_hash(proof["snapshot"], candidate["path"]) != proof["sha256"]:
                raise StorageError("Archive content did not match; local file retained")
            # Native backup can take time; identity must still match the selected source.
            lifecycle._recheck(policy, candidate)
        _save(state, result)
        lifecycle._stage(candidate, destination)
        moved = True
        item["status"] = "staged"
        _save(state, result)
        checked = dict(candidate, **({"sha256": proof["sha256"]} if proof else {}))
        lifecycle._verify_staged(checked, destination)
        if planner.active_processes(lifecycle._rule(policy, candidate)):
            raise StorageError("A declared process started; staged data retained", code="busy")
        planner.assert_inactive(destination, ancestors=False)
        no_symlinks(directory)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if signature(os.stat(destination.name, dir_fd=descriptor, follow_symlinks=False))[:4] != candidate["signature"][:4]:
                raise StorageError("Staged entry identity changed; retained")
            if destination.is_dir() and not shutil.rmtree.avoids_symlink_attacks:
                raise StorageError("Safe directory deletion is unavailable")
            item["status"] = "deleting"
            _save(state, result)
            if candidate["targets"] == "dirs":
                shutil.rmtree(destination.name, dir_fd=descriptor)
            else:
                os.unlink(destination.name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        item["status"] = "archived" if proof is not None else "deleted"
        result["deletedBytesEstimate"] += candidate["size"]
        _save(state, result)
    except (OSError, StorageError) as exc:
        if not moved:
            item["status"] = "skipped" if _expected_skip(exc) else "failed"
            entry = {"path": candidate["path"], "reason": str(exc)}
            result["skipped" if _expected_skip(exc) else "errors"].append(entry)
        elif _expected_skip(exc) and item["status"] != "deleting":
            item["status"] = "held"
            result["retained"].append({"path": candidate["path"], "heldPath": str(destination),
                                       "reason": str(exc)})
        else:
            item["status"] = "partial" if destination.exists() else "unknown"
            result["errors"].append({"path": candidate["path"], "heldPath": str(destination),
                                     "reason": str(exc), "status": item["status"]})
        _save(state, result)


def execute(policy: Policy, state: Path, prepared: dict, *, on_output=None) -> dict:
    """Execute only the scope displayed by prepare; the caller holds the state lock."""
    if prepared.get("policyFingerprint") != policy.fingerprint:
        raise StorageError("Storage policy changed; run prune again")
    selected = _policy(policy)
    plan = receipts.trusted_plan(selected, state, prepared["planId"])
    if any(record.get("planId") == plan["id"] for record in records(state, "prunes")):
        raise StorageError("This prune plan already has an execution record")
    before = _available(policy.home)
    result = {"id": uuid.uuid4().hex, "planId": plan["id"], "execute": True,
              "createdAt": time.time(), "state": "running", "items": [], "retained": [],
              "skipped": [{"path": item["path"], "rule": item["rule"], "reason": item["blocked"]}
                          for item in plan["candidates"] if item.get("blocked")],
              "errors": [], "maintenance": {"execute": False, "items": []},
              "deletedBytesEstimate": 0, "warnings": list(prepared.get("warnings", []))}
    _save(state, result)
    eligible = [item for item in plan["candidates"] if not item.get("blocked")]
    archive = Archive(policy, state)
    archives = [item for item in eligible if item["action"] == "archive"]
    proofs = {}
    for candidate in archives:
        try:
            lifecycle._recheck(selected, candidate)
            proofs[candidate["path"]] = {"sha256": planner.file_hash(Path(candidate["path"]))}
        except (OSError, StorageError) as exc:
            result["skipped" if _expected_skip(exc) else "errors"].append(
                {"path": candidate["path"], "reason": str(exc)})
    if proofs:
        try:
            repository_id = archive.identity()
            snapshot = archive.backup(list(proofs), result["id"])
            for proof in proofs.values():
                proof.update(repositoryId=repository_id, snapshot=snapshot)
            result.update(repositoryId=repository_id, snapshot=snapshot)
            _save(state, result)
        except (OSError, StorageError) as exc:
            proofs.clear()
            result["errors"].append({"action": "archive", "reason": str(exc)})
            _save(state, result)
    for candidate in eligible:
        if candidate["action"] == "archive" and candidate["path"] not in proofs:
            continue
        _delete(candidate, selected, state, result, archive, proofs.get(candidate["path"]))
    if policy.maintenance:
        result["maintenance"] = copy.deepcopy(prepared["maintenance"])
        result["maintenanceState"] = "running"
        _save(state, result)

        def save_progress(data: dict) -> None:
            result["maintenance"] = copy.deepcopy(data)
            _save(state, result)

        try:
            native = maintenance.maintain(policy, list(policy.maintenance),
                                          approved_preview=prepared["maintenance"],
                                          report_reclaimed=True, on_progress=save_progress,
                                          output_dir=state / "prune-output" / result["id"], on_output=on_output)
            result["maintenance"] = native
            result["maintenanceState"] = "failed" if native.get("errors") else "completed"
            reasons = {item["action"]: item.get("reason") or "Native maintenance failed"
                       for item in native.get("items", [])}
            result["errors"].extend({"action": name, "reason": reasons.get(name, "Native maintenance failed")}
                                    for name in native.get("errors", []))
        except (OSError, StorageError) as exc:
            result["maintenanceState"] = "unknown"
            result["errors"].append({"action": "maintenance", "reason": str(exc)})
    after = _available(policy.home)
    result.update(finishedAt=time.time(), state="partial" if result["errors"] else "completed",
                  availableBytesBefore=before, availableBytesAfter=after,
                  availableBytesChange=after - before if before is not None and after is not None else None)
    result["summary"] = {"deletedItems": sum(item["status"] == "deleted" for item in result["items"]),
                         "archivedItems": sum(item["status"] == "archived" for item in result["items"]),
                         "skippedItems": len(result["skipped"]), "retainedItems": len(result["retained"])}
    _save(state, result)
    return result

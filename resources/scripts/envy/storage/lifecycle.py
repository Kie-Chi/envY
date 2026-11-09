"""Execute saved plans with durable recovery records before every move."""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

from envy.secure_io import ensure_private_directory
from envy.storage.backend import Archive
from envy.storage.model import Policy, StorageError, no_symlinks, signature, validate_file
from envy.storage.planner import assert_inactive, file_hash
from envy.storage.state import read_record, records, write_record


def _rule(policy: Policy, candidate: dict):
    rule = next((rule for rule in policy.rules if rule.id == candidate["rule"]), None)
    if rule is None or rule.action != candidate["action"]:
        raise StorageError("Candidate rule no longer exists")
    return rule


def _recheck(policy: Policy, candidate: dict) -> None:
    info = validate_file(Path(candidate["path"]), _rule(policy, candidate), policy)
    if signature(info) != candidate["signature"]:
        raise StorageError("Candidate changed after the plan was created")
    assert_inactive(Path(candidate["path"]))


def _open_parent(path: Path) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stage(candidate: dict, destination: Path) -> None:
    source = Path(candidate["path"])
    descriptor = _open_parent(source)
    try:
        info = os.stat(source.name, dir_fd=descriptor, follow_symlinks=False)
        if signature(info) != candidate["signature"]:
            raise StorageError("Candidate changed before moving")
        if info.st_dev != destination.parent.stat().st_dev:
            raise StorageError("Quarantine must be on the same filesystem as the source")
        os.rename(source.name, destination, src_dir_fd=descriptor)
    finally:
        os.close(descriptor)


def execute(policy: Policy, state: Path, plan_id: str) -> dict:
    plan = read_record(state, "plans", plan_id)
    if plan.get("policy") != policy.fingerprint or not 0 <= time.time() - plan["createdAt"] <= 86400:
        raise StorageError("Plan expired or policy changed; create a new plan")
    if any(record.get("plan") == plan_id for record in records(state, "runs")):
        raise StorageError("This plan already has an execution record; inspect storage status")
    batch = {"id": uuid.uuid4().hex, "plan": plan_id, "createdAt": time.time(),
             "snapshot": None, "repositoryId": None, "items": [], "errors": []}
    eligible = []
    for candidate in plan["candidates"]:
        try:
            _recheck(policy, candidate)
            eligible.append(candidate)
        except (OSError, StorageError) as exc:
            batch["errors"].append({"path": candidate["path"], "reason": str(exc)})
    archives = [candidate for candidate in eligible if candidate["action"] == "archive"]
    backend = Archive(policy, state)
    write_record(state, "runs", batch)
    if archives:
        batch["repositoryId"] = backend.identity()
        for candidate in archives:
            candidate["sha256"] = file_hash(Path(candidate["path"]))
            _recheck(policy, candidate)
        batch["snapshot"] = backend.backup([candidate["path"] for candidate in archives], batch["id"])
        # Save the exact snapshot before touching any source file.
        batch["items"] = [dict(candidate, status="backed-up") for candidate in archives]
        write_record(state, "runs", batch)
    directory = state / "quarantine" / batch["id"]
    no_symlinks(directory)
    ensure_private_directory(directory)
    for index, candidate in enumerate(eligible):
        item = next((item for item in batch["items"] if item["path"] == candidate["path"]), None)
        if item is None:
            item = dict(candidate, status="pending")
            batch["items"].append(item)
        destination = directory / str(index)
        try:
            _recheck(policy, candidate)
            if candidate["action"] == "archive":
                if backend.content_hash(batch["snapshot"], candidate["path"]) != candidate["sha256"]:
                    raise StorageError("Archive hash does not match the original")
                _recheck(policy, candidate)
            item.update(status="prepared", heldPath=str(destination),
                        expiresAt=time.time() + policy.quarantine_days * 86400)
            write_record(state, "runs", batch)
            _stage(candidate, destination)
            if signature(destination.lstat())[:4] != candidate["signature"][:4]:
                raise StorageError("Moved file identity changed; retained for manual recovery")
            item["status"] = "quarantined"
            item["heldSignature"] = signature(destination.lstat())
            write_record(state, "runs", batch)
            if candidate["action"] == "archive":
                if file_hash(destination) != candidate["sha256"]:
                    raise StorageError("Staged file changed; it has been retained for recovery")
                if signature(destination.lstat()) != item["heldSignature"]:
                    raise StorageError("Staged file changed before release")
                item["status"] = "archived-held"
                write_record(state, "runs", batch)
        except (OSError, StorageError) as exc:
            batch["errors"].append({"path": candidate["path"], "reason": str(exc)})
            write_record(state, "runs", batch)
    batch["finishedAt"] = time.time()
    write_record(state, "runs", batch)
    return batch


def _held_path(state: Path, batch: dict, item: dict) -> Path:
    path = Path(item["heldPath"])
    root = state / "quarantine" / batch["id"]
    if path.parent != root or not path.name.isdigit():
        raise StorageError("Invalid quarantine record path")
    no_symlinks(path)
    return path


def purge(policy: Policy, state: Path, *, execute: bool = False) -> dict:
    result = {"candidates": [], "errors": []}
    backend = Archive(policy, state)
    for batch in records(state, "runs"):
        for item in batch["items"]:
            if (item["status"] not in {"quarantined", "archived-held", "releasing"}
                    or item["expiresAt"] > time.time()):
                continue
            result["candidates"].append({"run": batch["id"], "path": item["path"],
                                         "size": item["size"], "action": item["action"]})
            if not execute:
                continue
            try:
                path = _held_path(state, batch, item)
                final_status = "archived" if item["action"] == "archive" else "expired"
                if item["status"] == "releasing" and not path.exists():
                    item["status"] = final_status
                    write_record(state, "runs", batch)
                    continue
                if signature(path.lstat()) != item["heldSignature"]:
                    raise StorageError("Quarantined file changed; retained")
                assert_inactive(path, ancestors=False)
                if item["action"] == "archive":
                    if backend.identity() != batch["repositoryId"]:
                        raise StorageError("Archive repository identity changed; local file retained")
                    if backend.content_hash(batch["snapshot"], item["path"]) != item["sha256"]:
                        raise StorageError("Archive content changed; local file retained")
                    if file_hash(path) != item["sha256"]:
                        raise StorageError("Held archive file changed; retained")
                if signature(path.lstat()) != item["heldSignature"]:
                    raise StorageError("Quarantined file changed before release")
                item["status"] = "releasing"
                write_record(state, "runs", batch)
                path.unlink()
                item["status"] = final_status
                write_record(state, "runs", batch)
            except (OSError, StorageError) as exc:
                result["errors"].append({"path": item["path"], "reason": str(exc)})
    return result


def restore(policy: Policy, state: Path, run_id: str, target: Path) -> dict:
    batch = read_record(state, "runs", run_id)
    no_symlinks(target)
    if not target.is_absolute() or ".." in target.parts or target.exists():
        raise StorageError("Restore target must be a new absolute directory")
    backend = Archive(policy, state)
    remote = [item for item in batch["items"] if item["action"] == "archive"
              and not ("heldPath" in item and _held_path(state, batch, item).exists())]
    if remote and backend.identity() != batch["repositoryId"]:
        raise StorageError("Configured archive repository does not match this run")
    target.mkdir(mode=0o700, parents=True)
    if remote:
        backend.restore(batch["snapshot"], target, [item["path"] for item in remote])
    restored = []
    unavailable = []
    for item in batch["items"]:
        destination = target / Path(item["path"]).relative_to("/")
        if "heldPath" in item and _held_path(state, batch, item).exists():
            held = _held_path(state, batch, item)
            expected = item.get("heldSignature", item["signature"])
            # Recover stable local edits as well as the original archived bytes.
            if signature(held.lstat())[:2] != expected[:2]:
                raise StorageError("Recovery file changed; manual inspection required")
            if destination.exists():
                if file_hash(destination) != file_hash(held):
                    raise StorageError("Restore target conflicts with a held file")
            else:
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                no_symlinks(destination.parent)
                shutil.copy2(held, destination, follow_symlinks=False)
                if file_hash(destination) != file_hash(held):
                    raise StorageError("Quarantine restore verification failed")
            restored.append(item["path"])
        elif item["action"] == "archive" and batch["snapshot"]:
            if file_hash(destination) != item["sha256"]:
                raise StorageError("Restored archive content did not match its record")
            restored.append(item["path"])
        else:
            unavailable.append({"path": item["path"], "status": item["status"]})
    return {"run": run_id, "target": str(target), "restored": restored, "unavailable": unavailable}

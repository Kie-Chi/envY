"""Execute saved plans with durable recovery records before every move."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from envy.secure_io import ensure_private_directory
from envy.storage.backend import Archive
from envy.storage.model import (MODEL_VERSION, Policy, StorageError, absolute_path, no_symlinks, signature,
                                tree_digest, validate_entry)
from envy.storage.planner import active_processes, assert_inactive, assert_no_conflicts, file_hash
from envy.storage.state import read_record, records, write_record


class RecoveryPartialError(StorageError):
    """Recovery stopped after work began; result preserves known item outcomes."""

    def __init__(self, message: str, result: dict):
        super().__init__(message, code="partial_recovery")
        self.result = {"run": None, "target": None, "restored": [], "errors": [],
                       "unknown": [], "unavailable": [], "notAttempted": [], **result}


def _recovery_reason(error: Exception) -> str:
    # Only storage errors are authored for display; native diagnostics are not receipts.
    return str(error) if isinstance(error, StorageError) else f"Recovery failed ({type(error).__name__})"


def _rule(policy: Policy, candidate: dict):
    rule = next((rule for rule in policy.rules if rule.id == candidate["rule"]), None)
    if rule is None or rule.action != candidate["action"]:
        raise StorageError("Candidate rule no longer exists")
    return rule


def _recheck(policy: Policy, candidate: dict) -> None:
    rule = _rule(policy, candidate)
    path = Path(candidate["path"])
    info = validate_entry(path, Path(candidate["root"]), rule, policy)
    if signature(info) != candidate["signature"]:
        raise StorageError("Candidate changed after the plan was created")
    if rule.targets == "dirs" and tree_digest(path) != candidate["treeHash"]:
        raise StorageError("Candidate changed after the plan was created")
    if active_processes(rule):
        raise StorageError("A declared process is running; candidate retained")
    assert_inactive(path)


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


def _verify_staged(candidate: dict, destination: Path) -> None:
    if signature(destination.lstat())[:4] != candidate["signature"][:4]:
        raise StorageError("Moved entry identity changed; retained for manual recovery")
    if candidate.get("targets") == "dirs":
        if tree_digest(destination) != candidate["treeHash"]:
            raise StorageError("Staged directory changed; retained for recovery")
    elif candidate["action"] == "archive" and file_hash(destination) != candidate["sha256"]:
        raise StorageError("Staged file changed; it has been retained for recovery")


def execute(policy: Policy, state: Path, plan_id: str) -> dict:
    plan = read_record(state, "plans", plan_id)
    if (plan.get("modelVersion") != MODEL_VERSION or plan.get("policy") != policy.fingerprint
            or not 0 <= time.time() - plan["createdAt"] <= 86400):
        raise StorageError("Plan expired or policy changed; create a new plan")
    if any(record.get("plan") == plan_id for record in records(state, "runs")):
        raise StorageError("This plan already has an execution record; inspect storage status")
    assert_no_conflicts(plan["candidates"])
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
            _verify_staged(candidate, destination)
            item["heldSignature"] = signature(destination.lstat())
            if candidate["action"] == "archive":
                if signature(destination.lstat()) != item["heldSignature"]:
                    raise StorageError("Staged file changed before release")
                item["status"] = "archived-held"
            else:
                item["status"] = "quarantined"
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


def _release(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _eligible(item: dict) -> bool:
    return (item["status"] in {"quarantined", "archived-held", "releasing"}
            and item["expiresAt"] <= time.time())


def _verify_release(policy: Policy, state: Path, batch: dict, item: dict,
                    backend: Archive) -> Path:
    if not _eligible(item):
        raise StorageError("Approved quarantine entry is no longer eligible")
    path = _held_path(state, batch, item)
    if item["status"] == "releasing" and not path.exists():
        return path
    if signature(path.lstat()) != item["heldSignature"]:
        raise StorageError("Quarantined entry changed; retained")
    assert_inactive(path, ancestors=False)
    if item["action"] == "archive":
        if backend.identity() != batch["repositoryId"]:
            raise StorageError("Archive repository identity changed; local file retained")
        if backend.content_hash(batch["snapshot"], item["path"]) != item["sha256"]:
            raise StorageError("Archive content changed; local file retained")
        if file_hash(path) != item["sha256"]:
            raise StorageError("Held archive file changed; retained")
    elif item.get("targets") == "dirs":
        if tree_digest(path) != item["treeHash"]:
            raise StorageError("Held directory changed; retained")
    if signature(path.lstat()) != item["heldSignature"]:
        raise StorageError("Quarantined entry changed before release")
    return path


def _purge_candidate(batch: dict, item: dict) -> dict:
    return {"run": batch["id"], "path": item["path"],
            "size": item["size"], "action": item["action"]}


def _release_evidence(batch: dict, item: dict, path: Path) -> dict:
    proof = {**_purge_candidate(batch, item), "expiresAt": item["expiresAt"],
             "status": item["status"], "snapshot": batch.get("snapshot"),
             "repositoryId": batch.get("repositoryId"), "itemDigest": _digest(item)}
    if path.exists():
        proof.update(_held_evidence(path, item))
    else:
        proof["status"] = "releasing-missing"
    return proof


def preview_purge(policy: Policy, state: Path) -> dict:
    result = {"candidates": [], "errors": [], "approvedRuns": [], "eligible": [],
              "policy": policy.fingerprint, "metadata": {"archiveReadRequired": False}}
    backend = Archive(policy, state)
    for batch in records(state, "runs"):
        evidence = []
        for item in batch["items"]:
            if not _eligible(item):
                continue
            try:
                if item["action"] == "archive":
                    result["metadata"]["archiveReadRequired"] = True
                path = _verify_release(policy, state, batch, item, backend)
                evidence.append(_release_evidence(batch, item, path))
                result["candidates"].append(_purge_candidate(batch, item))
            except (OSError, StorageError) as exc:
                result["errors"].append({"run": batch["id"], "path": item["path"],
                                         "reason": str(exc)})
        if evidence:
            result["approvedRuns"].append(batch["id"])
            result["eligible"].append({"run": batch["id"], "items": evidence})
    return result


def _approved_purge_items(policy: Policy, preview: dict) -> dict:
    if not isinstance(preview, dict) or preview.get("policy") != policy.fingerprint:
        raise StorageError("Purge approval policy changed; preview again")
    selected = {}
    try:
        for batch in preview["eligible"]:
            if batch["run"] in selected or not batch["items"]:
                raise ValueError("Invalid approved run")
            entries = {}
            for item in batch["items"]:
                if item["run"] != batch["run"] or item["path"] in entries:
                    raise ValueError("Invalid approved item")
                entries[item["path"]] = item
            selected[batch["run"]] = entries
        if list(selected) != preview["approvedRuns"]:
            raise ValueError("Approved run set changed")
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError("Invalid purge approval evidence") from exc
    return selected


def purge(policy: Policy, state: Path, *, execute: bool = False,
          approved_runs: list[str] | None = None, approved_preview: dict | None = None) -> dict:
    result = {"candidates": [], "errors": []}
    backend = Archive(policy, state)
    approved_items = _approved_purge_items(policy, approved_preview) if approved_preview is not None else None
    if approved_items is not None:
        if approved_runs is not None and approved_runs != list(approved_items):
            raise StorageError("Approved purge run set does not match its preview")
        approved_runs = list(approved_items)
    if approved_runs is None:
        batches = records(state, "runs")
    else:
        if (not isinstance(approved_runs, list) or any(not isinstance(run, str) for run in approved_runs)
                or len(set(approved_runs)) != len(approved_runs)):
            raise StorageError("Approved purge runs must be a unique list of run IDs")
        batches = []
        for run_id in approved_runs:
            try:
                batches.append(read_record(state, "runs", run_id))
            except StorageError as exc:
                result["errors"].append({"run": run_id, "reason": str(exc)})
    for batch in batches:
        if approved_items is not None:
            eligible = []
            for path in approved_items[batch["id"]]:
                matching = [item for item in batch["items"] if item["path"] == path]
                if len(matching) != 1:
                    result["errors"].append({"run": batch["id"], "path": path,
                                             "reason": "Approved quarantine item is missing or ambiguous"})
                else:
                    eligible.append(matching[0])
        else:
            eligible = [item for item in batch["items"] if _eligible(item)]
            if approved_runs is not None and not eligible:
                result["errors"].append({"run": batch["id"],
                                         "reason": "Approved run has no eligible quarantine entries"})
        for item in eligible:
            result["candidates"].append(_purge_candidate(batch, item))
            if not execute:
                continue
            try:
                path = _verify_release(policy, state, batch, item, backend)
                if (approved_items is not None
                        and _release_evidence(batch, item, path) != approved_items[batch["id"]][item["path"]]):
                    raise StorageError("Approved purge item evidence changed; retained")
                final_status = "archived" if item["action"] == "archive" else "expired"
                if item["status"] != "releasing" or path.exists():
                    item["status"] = "releasing"
                    write_record(state, "runs", batch)
                    _release(path)
                item["status"] = final_status
                write_record(state, "runs", batch)
            except (OSError, StorageError) as exc:
                result["errors"].append({"run": batch["id"], "path": item["path"], "reason": str(exc)})
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _open_entry(path: Path) -> int:
    parent = _open_parent(path)
    try:
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _fd_digest(descriptor: int) -> str:
    before = os.fstat(descriptor)
    if before.st_uid != os.getuid():
        raise StorageError("Recovery entry has unsafe ownership")
    if stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    elif stat.S_ISDIR(before.st_mode):
        entries = []
        for name in sorted(os.listdir(descriptor)):
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                entries.append([name, "d" if stat.S_ISDIR(info.st_mode) else "f", _fd_digest(child)])
            finally:
                os.close(child)
        digest = _digest(entries)
    else:
        raise StorageError("Recovery entry contains links or unsupported file types")
    if signature(os.fstat(descriptor)) != signature(before):
        raise StorageError("Recovery entry changed while reading")
    return digest


def _held_evidence(path: Path, item: dict) -> dict:
    descriptor = _open_entry(path)
    try:
        info = signature(os.fstat(descriptor))
        expected = item.get("heldSignature", item["signature"])
        # A prepared record predates rename, which can change ctime.
        if info != expected and not (item.get("status") == "prepared" and info[:4] == expected[:4]):
            raise StorageError("Recovery file changed; manual inspection required")
        digest = _fd_digest(descriptor)
        if item.get("targets") == "dirs":
            if tree_digest(path) != item["treeHash"]:
                raise StorageError("Recovery directory changed")
        elif item["action"] == "archive" and digest != item["sha256"]:
            raise StorageError("Held archive content changed")
        if signature(os.fstat(descriptor)) != info:
            raise StorageError("Recovery file changed while validating")
        return {"signature": info, "sha256": digest}
    finally:
        os.close(descriptor)


def _restore_selection(policy: Policy, state: Path, batch: dict, *, verify_remote: bool) -> list[dict]:
    selected = []
    backend = Archive(policy, state)
    repository_checked = False
    paths = []
    for item in batch["items"]:
        path = absolute_path(item["path"])
        if path == Path("/") or any(path == previous or path in previous.parents or previous in path.parents
                                    for previous in paths):
            raise StorageError("Restore record contains conflicting paths")
        paths.append(path)
        entry = {"path": str(path), "status": "unavailable"}
        held = _held_path(state, batch, item) if "heldPath" in item else None
        if held is not None and held.exists():
            entry.update(status="held", **_held_evidence(held, item))
        elif item["action"] == "archive" and batch.get("snapshot"):
            if (item.get("targets", "files") != "files"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(batch.get("snapshot", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(batch.get("repositoryId", "")))):
                raise StorageError("Remote recovery requires a known single-file archive contract")
            if not repository_checked:
                if backend.identity() != batch.get("repositoryId"):
                    raise StorageError("Configured archive repository does not match this run")
                repository_checked = True
            if verify_remote and backend.content_hash(batch["snapshot"], item["path"]) != item["sha256"]:
                raise StorageError("Archive content does not match the recovery record")
            entry.update(status="remote", sha256=item["sha256"])
        selected.append(entry)
    return selected


def _directory_identity(descriptor: int, path: Path) -> dict:
    info = os.fstat(descriptor)
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def _target_binding(target: Path) -> dict:
    target = absolute_path(str(target))
    if target == Path("/"):
        raise StorageError("Restore target must be a new absolute directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    parents = [_directory_identity(descriptor, Path("/"))]
    missing = []
    current = Path("/")
    try:
        for part in target.parts[1:-1]:
            current = current / part
            if missing:
                missing.append(str(current))
                continue
            try:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
            except FileNotFoundError:
                missing.append(str(current))
                continue
            os.close(descriptor)
            descriptor = following
            parents.append(_directory_identity(descriptor, current))
        if not missing:
            try:
                os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StorageError("Restore target must be a new absolute directory")
    finally:
        os.close(descriptor)
    return {"parents": parents, "missingParents": missing}


def preview_restore(policy: Policy, state: Path, run_id: str, target: Path) -> dict:
    binding = _target_binding(target)
    batch = read_record(state, "runs", run_id)
    selected = _restore_selection(policy, state, batch, verify_remote=True)
    remote = any(item["status"] == "remote" for item in selected)
    return {"run": run_id, "absoluteTarget": str(target), "policy": policy.fingerprint,
            "manifestDigest": _digest(batch), "targetBinding": binding,
            "snapshot": batch.get("snapshot"), "repositoryId": batch.get("repositoryId"),
            "items": selected,
            "metadata": {"archiveReadRequired": remote, "executionSupported": True,
                         "restoreMethod": "dump-to-fd" if remote else "held-copy", "limitations": []}}


@contextmanager
def _new_target(target: Path, binding: dict):
    if _target_binding(target) != binding:
        raise StorageError("Restore target parent identity changed after approval")
    descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
    names = []

    def check():
        for index, name in enumerate(names):
            linked = os.stat(name, dir_fd=descriptors[index], follow_symlinks=False)
            if signature(linked)[:2] != signature(os.fstat(descriptors[index + 1]))[:2]:
                raise StorageError("Restore target directory binding changed")

    try:
        expected = {entry["path"]: entry for entry in binding["parents"]}
        if _directory_identity(descriptors[0], Path("/")) != expected["/"]:
            raise StorageError("Restore target root identity changed")
        current = Path("/")
        for part in target.parts[1:]:
            check()
            current = current / part
            if str(current) not in expected:
                # Never reuse a directory that appeared after approval.
                os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            names.append(part)
            if str(current) in expected and _directory_identity(descriptor, current) != expected[str(current)]:
                raise StorageError("Restore target parent identity changed after approval")
            check()
        yield descriptors[-1], check
        check()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _check_output_binding(parent: int, name: str, destination: int) -> None:
    linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if signature(linked) != signature(os.fstat(destination)):
        raise StorageError("Restore output entry binding changed")


def _copy_entry(source: int, parent: int, name: str, check) -> None:
    check()
    info = os.fstat(source)
    if stat.S_ISDIR(info.st_mode):
        os.mkdir(name, mode=0o700, dir_fd=parent)
        destination = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            for child_name in sorted(os.listdir(source)):
                child = os.open(child_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source)
                try:
                    _copy_entry(child, destination, child_name, check)
                finally:
                    os.close(child)
            os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
            _check_output_binding(parent, name, destination)
        finally:
            os.close(destination)
    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        destination = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=parent)
        with os.fdopen(destination, "wb") as output, os.fdopen(os.dup(source), "rb") as original:
            os.lseek(source, 0, os.SEEK_SET)
            shutil.copyfileobj(original, output)
            output.flush()
            os.utime(output.fileno(), ns=(info.st_atime_ns, info.st_mtime_ns))
            _check_output_binding(parent, name, output.fileno())
    else:
        raise StorageError("Recovery entry contains links or unsupported file types")
    if signature(os.fstat(source)) != signature(info):
        raise StorageError("Recovery entry changed while copying")
    check()


def _restore_selected(policy: Policy, state: Path, batch: dict, selected: list[dict], target_fd: int,
                      check, *, existing_parents: bool = False, include_remote: bool = False) -> list[str]:
    restored = []
    directories = {}
    root_check = check
    namespace_unverified = False

    def check():
        nonlocal namespace_unverified
        try:
            root_check()
            for prefix, descriptor in directories.items():
                parent = target_fd if len(prefix) == 1 else directories[prefix[:-1]]
                linked = os.stat(prefix[-1], dir_fd=parent, follow_symlinks=False)
                if signature(linked)[:2] != signature(os.fstat(descriptor))[:2]:
                    raise StorageError("Restore output parent directory binding changed")
        except Exception:
            namespace_unverified = True
            raise

    try:
        for index, (item, entry) in enumerate(zip(batch["items"], selected)):
            if entry["status"] != "held" and not (include_remote and entry["status"] == "remote"):
                continue
            output_started = False
            try:
                check()
                parts = Path(item["path"]).parts[1:]
                parent = target_fd
                prefix = ()
                for part in parts[:-1]:
                    prefix += (part,)
                    if prefix not in directories:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=parent)
                        except FileExistsError:
                            if not existing_parents:
                                raise
                        directories[prefix] = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                      dir_fd=parent)
                    parent = directories[prefix]
                if entry["status"] == "remote":
                    check()
                    destination = os.open(parts[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600, dir_fd=parent)
                    output_started = True
                    try:
                        backend = Archive(policy, state)
                        if backend.identity() != batch["repositoryId"]:
                            raise StorageError("Archive repository identity changed before recovery")
                        backend.restore_file(batch["snapshot"], item["path"], destination, entry["sha256"])
                        if _fd_digest(destination) != entry["sha256"]:
                            raise StorageError("Restored archive content did not match its record")
                        check()
                        _check_output_binding(parent, parts[-1], destination)
                    finally:
                        os.close(destination)
                    restored.append(item["path"])
                    continue
                source = _open_entry(_held_path(state, batch, item))
                try:
                    if (signature(os.fstat(source)) != entry["signature"]
                            or _fd_digest(source) != entry["sha256"]):
                        raise StorageError("Recovery content changed after approval")
                    output_started = True
                    _copy_entry(source, parent, parts[-1], check)
                    destination = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    try:
                        if _fd_digest(destination) != entry["sha256"]:
                            raise StorageError("Quarantine restore verification failed")
                        check()
                        _check_output_binding(parent, parts[-1], destination)
                    finally:
                        os.close(destination)
                finally:
                    os.close(source)
                restored.append(item["path"])
            except Exception as exc:
                # Preserve pre-write validation and exclusive-create exception behavior.
                if not restored and (not output_started or isinstance(exc, FileExistsError)):
                    raise
                reason = _recovery_reason(exc)
                unknown = []
                if output_started:
                    unknown.append({"path": item["path"], "code": "unknown_operation",
                                    "reason": "Recovery output may be partial or replaced; its current namespace was not verified"})
                if namespace_unverified:
                    unknown.extend({"path": path, "code": "unknown_operation",
                                    "reason": "Previously verified recovery; current path was not revalidated after directory binding changed"}
                                   for path in restored)
                remaining = [{"path": later["path"], "reason": "Recovery stopped before this item was attempted"}
                             for later, evidence in zip(batch["items"][index + 1:], selected[index + 1:])
                             if evidence["status"] == "held" or (include_remote and evidence["status"] == "remote")]
                raise RecoveryPartialError(reason, {
                    "restored": list(restored), "errors": [{"path": item["path"], "reason": reason}],
                    "unknown": unknown, "notAttempted": remaining,
                }) from exc
    finally:
        for descriptor in reversed(list(directories.values())):
            os.close(descriptor)
    return restored


def restore(policy: Policy, state: Path, run_id: str, target: Path, *,
            approved_preview: dict | None = None) -> dict:
    batch = read_record(state, "runs", run_id)
    binding = _target_binding(target)
    selected = _restore_selection(policy, state, batch, verify_remote=approved_preview is not None)
    remote = [entry for entry in selected if entry["status"] == "remote"]
    unavailable = [{"path": item["path"], "status": item["status"]}
                   for item, entry in zip(batch["items"], selected) if entry["status"] == "unavailable"]
    if approved_preview is not None:
        current = {"run": run_id, "absoluteTarget": str(target), "policy": policy.fingerprint,
                   "manifestDigest": _digest(batch), "targetBinding": binding,
                   "snapshot": batch.get("snapshot"), "repositoryId": batch.get("repositoryId"),
                   "items": selected}
        if any(approved_preview.get(key) != value for key, value in current.items()):
            raise StorageError("Restore evidence changed after approval; preview again")
    if remote and approved_preview is None:
        # Legacy callers retain the native restore path.
        target.mkdir(mode=0o700, parents=True)
        Archive(policy, state).restore(batch["snapshot"], target, [entry["path"] for entry in remote])
        for entry in remote:
            if file_hash(target / Path(entry["path"]).relative_to("/")) != entry["sha256"]:
                raise StorageError("Restored archive content did not match its record")
        # Native restore may already have created the shared ancestor directories.
        descriptor = _open_entry(target)
        try:
            restored = _restore_selected(policy, state, batch, selected, descriptor,
                                         lambda: no_symlinks(target), existing_parents=True)
            restored.extend(entry["path"] for entry in remote)
        except RecoveryPartialError as exc:
            # Remote entries were already verified before the held-copy flow began.
            exc.result["restored"].extend(entry["path"] for entry in remote)
            exc.result.update(run=run_id, target=str(target), unavailable=unavailable)
            raise
        finally:
            os.close(descriptor)
    else:
        restored = None
        try:
            with _new_target(target, binding) as (descriptor, check):
                restored = _restore_selected(policy, state, batch, selected, descriptor, check, include_remote=True)
        except RecoveryPartialError as exc:
            exc.result.update(run=run_id, target=str(target), unavailable=unavailable)
            raise
        except Exception as exc:
            if restored is None:
                raise
            reason = _recovery_reason(exc)
            raise RecoveryPartialError(reason, {
                "run": run_id, "target": str(target), "restored": list(restored),
                "errors": [{"reason": reason}], "unavailable": unavailable,
                "unknown": [{"path": path, "code": "unknown_operation",
                             "reason": "Previously verified recovery; current path was not revalidated after final target check failed"}
                            for path in restored] or [{"code": "unknown_operation", "reason": reason}],
            }) from exc
    return {"run": run_id, "target": str(target), "restored": restored, "unavailable": unavailable}

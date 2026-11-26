"""Private preview receipts and single claims, under the CLI's existing state lock."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from envy.storage import source_identity
from envy.storage.model import MODEL_VERSION, Policy, StorageError, absolute_path, no_symlinks
from envy.storage.state import read_record, records, sync_record, write_record


PREVIEW_TTL = 300
PLAN_TTL = 86400


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _physical(path: Path) -> list:
    no_symlinks(path)
    info = path.stat()
    return [str(path), info.st_dev, info.st_ino, info.st_uid]


def _ownership(policy: Policy, state: Path) -> dict:
    return {"machine": policy.machine_id, "platform": policy.platform,
            "host": digest([os.uname().nodename, _physical(Path("/")),
                            _physical(policy.home), _physical(state)])}


def _binding(policy: Policy, state: Path) -> dict:
    return {**_ownership(policy, state),
            "source": source_identity.fingerprint(), "policy": policy.fingerprint}


def record_plan_source(policy: Policy, state: Path, plan: dict) -> None:
    """Called only by the successful planner producer, never when loading old JSON."""
    write_record(state, "plan-origins", {"id": plan["id"], "digest": digest(plan),
                                       "binding": _binding(policy, state)})


def trusted_plan(policy: Policy, state: Path, plan_id: str) -> dict:
    try:
        plan = read_record(state, "plans", plan_id)
        origin = read_record(state, "plan-origins", plan_id)
    except StorageError as exc:
        raise StorageError("Plan has no trusted source; generate a new storage plan",
                           code="stale_preview") from exc
    created = plan.get("createdAt")
    if (origin.get("digest") != digest(plan) or origin.get("binding") != _binding(policy, state)
            or plan.get("policy") != policy.fingerprint or plan.get("modelVersion") != MODEL_VERSION
            or not isinstance(created, (int, float)) or not 0 <= time.time() - created <= PLAN_TTL):
        raise StorageError("Plan changed, expired or has a different source; generate a new storage plan",
                           code="stale_preview")
    return plan


def request(operation: str, *, plan_id: str | None = None, run_id: str | None = None,
            target: Path | None = None, actions: list[str] | None = None) -> dict:
    from envy.storage.maintenance import ACTIONS

    result: dict = {"actions": []}
    if operation == "run":
        if not plan_id:
            raise StorageError("Select an explicitly saved storage plan", code="invalid_preview")
        result["planId"] = plan_id
    elif operation == "restore":
        if not run_id or target is None:
            raise StorageError("A run and new absolute restore target are required", code="invalid_preview")
        result.update(runId=run_id, absoluteTarget=str(absolute_path(str(target))))
    elif operation == "maintain":
        if not actions or any(name not in ACTIONS for name in actions):
            raise StorageError("Select at least one explicit maintenance action", code="invalid_preview")
        result["actions"] = [name for name in ACTIONS if name in actions]
    elif operation != "purge":
        raise StorageError("Unknown storage operation", code="invalid_preview")
    return result


def _metadata(operation: str, policy: Policy, state: Path, selected: dict) -> dict:
    from envy.storage import lifecycle, maintenance

    if operation == "run":
        return trusted_plan(policy, state, selected["planId"])
    if operation == "maintain":
        for name in selected["actions"]:
            if name not in policy.maintenance:
                raise StorageError("Maintenance action is not enabled by machine policy: " + name,
                                   code="invalid_preview")
        return maintenance.preview(policy, selected["actions"], bind_targets=True)
    if operation == "restore":
        return lifecycle.preview_restore(policy, state, selected["runId"], Path(selected["absoluteTarget"]))
    return lifecycle.preview_purge(policy, state)


def _metadata_digest(operation: str, metadata: dict) -> str:
    if operation == "maintain":
        # Report sizes and prose are observations, not the native execution binding.
        metadata = copy.deepcopy(metadata)
        for item in metadata.get("items", []):
            item.pop("inspection", None)
            item.pop("note", None)
    return digest(metadata)


def _public(record: dict) -> dict:
    return copy.deepcopy({"contractVersion": 1, "kind": record["kind"],
                          "operation": record["operation"], "previewToken": record["id"],
                          "expiresAt": record["expiresAt"], "operationId": record["operationId"],
                          "request": record["request"], "policyFingerprint": record["binding"]["policy"],
                          "sourceFingerprint": record["binding"]["source"], "state": record["state"],
                          "preview": record["preview"], "result": record.get("result"),
                          "errors": record.get("errors", []), "unknown": record.get("unknown", []),
                          "refreshedStatus": record.get("refreshedStatus", {
                              "status": "unavailable", "reason": "No execution has occurred."})})


def _persist(state: Path, record: dict) -> None:
    write_record(state, "previews", record)
    sync_record(state, "previews", record["id"])
    if digest(read_record(state, "previews", record["id"])) != digest(record):
        raise StorageError("Preview record persistence could not be confirmed", code="unknown_operation")


def preview(operation: str, policy: Policy, state: Path, selected: dict) -> dict:
    if (not isinstance(policy.machine_id, str) or not policy.machine_id
            or not isinstance(policy.platform, str) or policy.platform not in {"darwin", "linux"}):
        raise StorageError("Preview requires machine and platform from the evaluated policy manifest",
                           code="invalid_preview")
    binding = _binding(policy, state)
    metadata = _metadata(operation, policy, state, selected)
    record = {"id": secrets.token_hex(16), "operationId": uuid.uuid4().hex,
              "kind": "preview", "operation": operation, "request": selected,
              "binding": binding, "expiresAt": time.time() + PREVIEW_TTL,
              "preview": metadata, "state": "ready", "result": None,
              "previewDigest": _metadata_digest(operation, metadata)}
    if _binding(policy, state) != binding:
        raise StorageError("Storage context changed during preview; preview again", code="stale_preview")
    _persist(state, record)
    return _public(record)


def _summary(item: dict) -> dict:
    return {"operationId": item["operationId"], "operation": item["operation"],
            "request": item["request"], "state": "unknown" if item["state"] == "claimed" else item["state"],
            "errors": item.get("errors", []),
            "unknown": ([{"code": "unknown_operation", "reason": "Claim has no durable outcome; inspect run records."}]
                        if item["state"] == "claimed" else item.get("unknown", []))}


def summaries(state: Path) -> list[dict]:
    """Expose operation traceability without bearer tokens or private bindings."""
    return [_summary(item) for item in records(state, "previews")]


def _refresh(policy: Policy, state: Path) -> dict:
    """Read durable metadata only, without tool probes or archive password checks."""
    return {"mode": policy.mode, "runs": records(state, "runs"), "operations": summaries(state),
            "latestPlan": max(records(state, "plans"), key=lambda item: item["createdAt"], default=None)}


def _unknown(record: dict) -> dict:
    record = copy.deepcopy(record)
    record.update(kind="result", state="unknown", unknown=[{
        "code": "unknown_operation", "reason": "A claim exists without a durable result; inspect status before any new operation."}])
    return _public(record)


def confirm(operation: str, policy: Policy, state: Path, selected: dict, token: str) -> dict:
    """Validate and persist one claim before handing off to the existing engine."""
    from envy.storage import lifecycle, maintenance

    try:
        record = read_record(state, "previews", token)
    except StorageError as exc:
        raise StorageError("Invalid or unavailable preview token", code="invalid_preview") from exc
    if record.get("operation") != operation or record.get("request") != selected:
        raise StorageError("Preview request changed; preview again", code="stale_preview")
    binding = record.get("binding", {})
    if any(binding.get(key) != value for key, value in _ownership(policy, state).items()):
        raise StorageError("Machine or host changed; preview again", code="stale_preview")
    # Consumed receipts are read-only queries, independent of current execution authorization.
    if record.get("state") != "ready":
        return _unknown(record) if record.get("state") == "claimed" else _public(record)
    if binding != _binding(policy, state):
        raise StorageError("Policy or source changed; preview again", code="stale_preview")
    if not 0 < record["expiresAt"] - time.time() <= PREVIEW_TTL:
        raise StorageError("Preview expired; preview again", code="expired_preview")
    if _metadata_digest(operation, record["preview"]) != record.get("previewDigest"):
        raise StorageError("Invalid preview metadata", code="invalid_preview")
    try:
        current = _metadata(operation, policy, state, selected)
    except (StorageError, OSError) as exc:
        raise StorageError("Plan or target changed; preview again", code="stale_preview") from exc
    if _metadata_digest(operation, current) != record["previewDigest"]:
        raise StorageError("Plan, selection or targets changed; preview again", code="stale_preview")
    if _binding(policy, state) != record["binding"]:
        raise StorageError("Storage context changed; preview again", code="stale_preview")
    record.update(kind="result", state="claimed", claimedAt=time.time())
    _persist(state, record)
    try:
        if operation == "run":
            result = lifecycle.execute(policy, state, selected["planId"])
        elif operation == "maintain":
            result = maintenance.maintain(policy, selected["actions"], approved_preview=record["preview"])
        elif operation == "restore":
            result = lifecycle.restore(policy, state, selected["runId"], Path(selected["absoluteTarget"]),
                                       approved_preview=record["preview"])
        else:
            result = lifecycle.purge(policy, state, execute=True,
                                     approved_runs=record["preview"]["approvedRuns"],
                                     approved_preview=record["preview"])
        errors = list(result.get("errors", []))
        unknown = list(result.get("unknown", []))
        unavailable = result.get("unavailable", [])
        if unavailable:
            errors.append({"reason": "Some recovery items are unavailable", "items": unavailable})
        if result.get("ok") is False and not errors:
            errors.append({"reason": "Storage engine reported failure"})
        record.update(result=result, errors=errors, unknown=unknown,
                      state="unknown" if unknown else "partial" if errors else "complete")
    except lifecycle.RecoveryPartialError as exc:
        # Recovery verified some outputs before stopping; keep those durable outcomes.
        result = exc.result
        errors = list(result.get("errors", []))
        unknown = list(result.get("unknown", []))
        record.update(result=result, errors=errors, unknown=unknown,
                      state="unknown" if unknown else "partial")
    except Exception as exc:
        # A leaf may already have mutated files or started a command before raising.
        record.update(state="unknown", errors=[{"reason": str(exc)}],
                      unknown=[{"code": "unknown_operation", "reason": "Engine exited without a complete result; inspect durable run records."}])
    try:
        refreshed = _refresh(policy, state)
        refreshed["operations"] = [_summary(record) if item["operationId"] == record["operationId"] else item
                                   for item in refreshed["operations"]]
        record["refreshedStatus"] = {"status": "available", "data": refreshed}
    except Exception as exc:
        record["refreshedStatus"] = {"status": "unavailable", "reason": str(exc)}
    try:
        _persist(state, record)
    except (OSError, StorageError) as exc:
        record["unknown"] = [*record.get("unknown", []), {
            "code": "unknown_operation", "reason": "Result persistence failed; the claim prevents replay: " + str(exc)}]
        record["state"] = "unknown"
    return _public(record)

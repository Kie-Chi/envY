#!/usr/bin/env python3
"""Frozen input and receipt boundaries for explicit Linux lifecycle probes.

NetworkConfig dispatches to the closed guest-fetch orchestrator. The offline
export boundary remains available without claiming lifecycle execution. Fake
fixtures cannot produce runtime acceptance evidence.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import tarfile
import time

import cache
import export as export_api
import install
import snapshot
import vm

RECEIPT_PREFIX = "ENVY_RUNTIME_ACCEPTANCE_RECEIPT "
EVIDENCE = "linux-vm-envy-lifecycle"
NETWORK_EVIDENCE = "linux-vm-envy-lifecycle-networked"
SIGNATURE_SCOPE = "public-copy-require-sigs; local-content-verify-no-trust; export-is-not-narinfo"
MAX_METADATA = 16 * 1024**2
MAX_SOURCE = 128 * 1024**2
MAX_RECEIPT = 256 * 1024
STAGES = ("setup", "firstApply", "repeatApply", "faultApply", "recoveryApply")
ISO_FLAGS = ("-iso-level", "3", "-joliet", "-rock", "-file-mode", "0444",
             "-dir-mode", "0555", "-uid", "0", "-gid", "0", "-follow-links", "-graft-points")


@dataclass(frozen=True)
class FullConfig(install.InstallConfig):
    snapshot: str | None = None
    preparation_receipt: str | None = None
    preparation_sha256: str | None = None
    preparation_size: int | None = None
    export_artifact: str | None = None
    artifact_parent_identity: tuple[int, int] | None = None
    preparation_identity: tuple[int, int, int, int, int] | None = None
    snapshot_identity: tuple[int, int] | None = None
    byte_budget: int = 16 * 1024**3
    object_byte_budget: int = 4 * 1024**3
    full_timeout: float = 7200


@dataclass(frozen=True)
class NetworkConfig(FullConfig):
    source_store_path: str | None = None


def _require(condition, reason="invalid-acceptance-input"):
    if not condition:
        raise vm.ProbeError(reason)


def _keys(row, keys):
    _require(isinstance(row, dict) and set(row) == set(keys), "invalid-acceptance-receipt")


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _nar_hash(value):
    if not isinstance(value, str) or not value.startswith("sha256-"):
        return False
    try:
        decoded = base64.b64decode(value[7:], validate=True)
        return len(decoded) == 32 and base64.b64encode(decoded).decode() == value[7:]
    except ValueError:
        return False


def _path(value):
    try:
        return cache.store_path(value) == value
    except cache.CacheError:
        return False


def _same(left, right):
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _json(data):
    def unique(pairs):
        row = {}
        for key, value in pairs:
            _require(key not in row, "duplicate-json-key")
            row[key] = value
        return row
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(vm.ProbeError("nonfinite-json")))
    except (ValueError, UnicodeError):
        raise vm.ProbeError("invalid-json") from None


def _deadline(deadline):
    _require(time.monotonic() < deadline, "timeout")


def _validate_config(config):
    for value, cap in ((config.timeout, vm.MAX_TIMEOUT), (config.install_timeout, install.MAX_INSTALL_TIMEOUT),
                       (config.full_timeout, 7200)):
        _require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= cap, "invalid-timeout")
    _require(type(config.byte_budget) is int and 0 < config.byte_budget <= 64 * 1024**3)
    _require(type(config.object_byte_budget) is int and 0 < config.object_byte_budget <= config.byte_budget)
    _require(type(config.output_limit) is int and 1024 <= config.output_limit <= vm.MAX_OUTPUT)
    _require(type(config.preparation_size) is int and 0 < config.preparation_size <= MAX_METADATA)
    _require(_hash(config.preparation_sha256))
    for value, length in ((config.artifact_parent_identity, 2), (config.snapshot_identity, 2),
                          (config.preparation_identity, 5)):
        _require(isinstance(value, tuple) and len(value) == length
                 and all(type(v) is int and v >= 0 for v in value), "missing-or-invalid-physical-identity")
    for value in (config.snapshot, config.preparation_receipt, config.export_artifact):
        _require(isinstance(value, str))
        vm.safe_path(value)


def _validate_network_config(config):
    _require(isinstance(config, NetworkConfig))
    for value, cap in ((config.timeout, vm.MAX_TIMEOUT), (config.install_timeout, install.MAX_INSTALL_TIMEOUT),
                       (config.full_timeout, 7200)):
        _require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= cap, "invalid-timeout")
    _require(type(config.byte_budget) is int and 0 < config.byte_budget <= 64 * 1024**3)
    _require(type(config.object_byte_budget) is int and 0 < config.object_byte_budget <= config.byte_budget)
    _require(type(config.output_limit) is int and 1024 <= config.output_limit <= vm.MAX_OUTPUT)
    identity = config.snapshot_identity
    _require(isinstance(identity, tuple) and len(identity) == 2
             and all(type(value) is int and value >= 0 for value in identity),
             "missing-or-invalid-physical-identity")
    _require(isinstance(config.snapshot, str))
    vm.safe_path(config.snapshot)
    _require(_path(config.source_store_path) and config.source_store_path.endswith("-source"),
             "invalid-source-store-path")
    _require(all(getattr(config, name) is None for name in (
        "preparation_receipt", "preparation_sha256", "preparation_size", "export_artifact",
        "artifact_parent_identity", "preparation_identity")), "mixed-acceptance-transport")


def _physical(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _verified_file(path, size, sha256, deadline, *, expected_identity, expected_mode=None, parent_identity=None):
    """Keep the exact hashed inode and its no-follow parent bound until exit."""
    _require(type(size) is int and size > 0 and _hash(sha256))
    path = vm.safe_path(path)
    directory = snapshot._open_directory_path(path.parent)
    descriptor = None
    try:
        identity = (os.fstat(directory).st_dev, os.fstat(directory).st_ino)
        _require(parent_identity is None or identity == parent_identity, "artifact-parent-changed")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        initial = os.fstat(descriptor)
        # Physical authorization precedes every content read, even hashing.
        _require(_physical(initial) == expected_identity
                 and (expected_mode is None or stat.S_IMODE(initial.st_mode) == expected_mode),
                 "artifact-physical-identity-mismatch")
        _require(stat.S_ISREG(initial.st_mode) and initial.st_size == size and initial.st_nlink == 1,
                 "artifact-size-or-type")
        signature = snapshot._signature(initial)

        def bound():
            _deadline(deadline)
            current = snapshot._open_directory_path(path.parent)
            try:
                _require((os.fstat(current).st_dev, os.fstat(current).st_ino) == identity,
                         "artifact-parent-changed")
            finally:
                os.close(current)
            _require(snapshot._signature(os.fstat(descriptor)) == signature
                     and snapshot._signature(os.stat(path.name, dir_fd=directory, follow_symlinks=False)) == signature,
                     "artifact-binding-changed")

        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            _deadline(deadline)
            digest.update(chunk)
        _require(digest.hexdigest() == sha256, "artifact-hash-mismatch")
        bound()
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor, bound
        bound()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _source_archive(source, manifest, writer, deadline):
    """Hash the normalized NAR represented by freeze_source's regular-file tar.

    No native Nix command or store lookup is needed. As in cache.freeze_source,
    only file-derived directories enter the archive, including the manifest;
    an empty hosts/darwin directory is not transported. Nix keeps only the
    executable bit when normalizing regular file modes.
    """
    tree, total = {}, 0
    entries = [*manifest["files"], {"relativePath": snapshot.MANIFEST_NAME}]
    with tarfile.open(fileobj=writer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for entry in entries:
            _deadline(deadline)
            name = entry["relativePath"]
            data, mode = source.read(name)
            if name != snapshot.MANIFEST_NAME:
                _require(hashlib.sha256(data).hexdigest() == entry["sha256"], "snapshot-changed")
            total += len(data)
            _require(total <= MAX_SOURCE, "source-budget-exceeded")
            member = tarfile.TarInfo("source/" + name)
            member.size, member.mode = len(data), mode
            archive.addfile(member, io.BytesIO(data))
            directory = tree
            for part in name.split("/")[:-1]:
                directory = directory.setdefault(part, {})
            directory[name.split("/")[-1]] = (name, mode)
    digest = hashlib.sha256()
    nar_size = 0

    def string(value):
        nonlocal nar_size
        if isinstance(value, str):
            value = value.encode()
        nar_size += 8 + len(value) + (-len(value) % 8)
        digest.update(struct.pack("<Q", len(value)))
        digest.update(value)
        digest.update(b"\0" * (-len(value) % 8))

    def node(value):
        _deadline(deadline)
        string("(")
        string("type")
        if isinstance(value, dict):
            string("directory")
            for name in sorted(value, key=lambda name: name.encode()):
                string("entry")
                string("(")
                string("name")
                string(name)
                string("node")
                node(value[name])
                string(")")
        else:
            name, mode = value
            string("regular")
            if mode & 0o111:
                string("executable")
                string("")
            string("contents")
            string(source.read(name)[0])
        string(")")

    string("nix-archive-1")
    node(tree)
    source.check()
    return "sha256-" + base64.b64encode(digest.digest()).decode(), nar_size


def _validate_plan(plan, manifest, nar_hash, nar_size, artifact_name, config):
    _require(isinstance(plan, dict) and plan.get("schemaVersion") == 1
             and type(plan.get("schemaVersion")) is int and plan.get("ready") is True
             and plan.get("status") == "ready" and plan.get("gaps") == [], "preparation-not-ready")
    source = plan["source"]
    _keys(source, ("path", "narHash", "lockSha256"))
    _require(_path(source["path"]) and source["narHash"] == nar_hash
             and source["lockSha256"] == manifest["lockSha256"], "preparation-source-mismatch")
    progress = plan["preparation"]
    _require(progress["format"] == "nix-export" and progress["complete"] is True
             and progress["failures"] == [] and progress["signatureScope"] == SIGNATURE_SCOPE,
             "preparation-not-ready")
    eligible, selection = export_api._selection(plan, config.byte_budget, config.object_byte_budget)
    _require(bool(eligible) and not selection["failures"] and selection["capacity"]["complete"] is True
             and selection["capacity"]["fits"] is True, "incomplete-approved-closure")
    expected = [{key: row[key] for key in ("path", "narHash", "narSize", "references")}
                for _, row in sorted(eligible.items())]
    _require(_same(progress["objects"], expected) and progress["completed"] == sorted(eligible)
             and progress["exportedPaths"] == sorted(eligible), "export-object-mismatch")
    _require(source["path"] in eligible and eligible[source["path"]]["narHash"] == nar_hash
             and eligible[source["path"]]["narSize"] == nar_size, "source-not-in-export")
    for row in plan["objects"]:
        if row["status"] == "needsguestbuild":
            _require(row.get("kind") == "local" and row.get("system") == "x86_64-linux"
                     and _path(row.get("drv")) and row["drv"].endswith(".drv"), "unsupported-guest-build")
    for row in expected:
        _require(_nar_hash(row["narHash"]) and type(row["narSize"]) is int and row["narSize"] > 0)
        _require(all(_path(ref) for ref in row["references"]))
    artifact = progress["artifact"]
    _require(artifact["relativePath"] == artifact_name and artifact["verified"] is True
             and artifact["complete"] is True and type(artifact["size"]) is int
             and 0 < artifact["size"] <= config.byte_budget and _hash(artifact["sha256"]),
             "export-artifact-incomplete")
    binding = artifact["binding"]
    _keys(binding, ("device", "inode", "size", "modifiedNs", "changedNs", "mode"))
    _require(all(type(value) is int and value >= 0 for value in binding.values())
             and binding["size"] == artifact["size"] and binding["mode"] == 0o600, "invalid-artifact-binding")
    return source, expected, artifact, eligible


@contextmanager
def approved_payloads(config: FullConfig, work: Path, deadline: float):
    """Yield three explicit anonymous readonly file grafts and bounded metadata.

    work must be an owned private scratch directory, bound by
    install.pinned_work in the future runner. No supplied native store path is
    opened, and neither Nix nor QEMU nor mkisofs is invoked here.
    """
    _validate_config(config)
    with ExitStack() as stack:
        receipt_fd, receipt_bound = stack.enter_context(_verified_file(
            config.preparation_receipt, config.preparation_size, config.preparation_sha256, deadline,
            expected_identity=config.preparation_identity))
        chunks, receipt_size = [], 0
        while chunk := os.read(receipt_fd, 65536):
            _deadline(deadline)
            receipt_size += len(chunk)
            _require(receipt_size <= config.preparation_size, "artifact-size-or-type")
            chunks.append(chunk)
        plan = _json(b"".join(chunks))
        source = stack.enter_context(snapshot._SnapshotIO(vm.safe_path(config.snapshot)))
        info = os.fstat(source.fd)
        _require((info.st_dev, info.st_ino) == config.snapshot_identity, "snapshot-root-identity-mismatch")
        manifest = snapshot._verify_snapshot(source)
        _require(manifest["fixture"]["selectionStage"] == "minimal"
                 and manifest["fixture"]["secrets"] == "guest-generated-only", "snapshot-not-minimal")
        archive, archive_fd = stack.enter_context(cache.archive_transport(work))
        nar_hash, nar_size = _source_archive(source, manifest, archive, deadline)
        origin, objects, artifact, eligible = _validate_plan(
            plan, manifest, nar_hash, nar_size, Path(config.export_artifact).name, config)
        lock = _json(source.read("flake.lock")[0])
        locked_hashes = {node["locked"]["narHash"] for node in lock["nodes"].values() if "locked" in node}
        _require(plan["lockedInputs"]["status"] == "complete"
                 and type(plan["lockedInputs"]["count"]) is int
                 and plan["lockedInputs"]["count"] == len(locked_hashes), "locked-inputs-incomplete")
        for row in eligible.values():
            if row["status"] == "nativeavailable":
                _require(row["narHash"] in locked_hashes | {nar_hash}, "unverified-source")
        _require(locked_hashes <= {row["narHash"] for row in eligible.values()}, "locked-inputs-incomplete")
        binding = artifact["binding"]
        artifact_fd, artifact_bound = stack.enter_context(_verified_file(
            config.export_artifact, artifact["size"], artifact["sha256"], deadline,
            expected_identity=tuple(binding[key] for key in ("device", "inode", "size", "modifiedNs", "changedNs")),
            expected_mode=binding["mode"], parent_identity=config.artifact_parent_identity))
        exported, export_fd = stack.enter_context(cache.archive_transport(work))
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(artifact_fd, 1024 * 1024):
            _deadline(deadline)
            size += len(chunk)
            _require(size <= artifact["size"], "artifact-size-or-type")
            digest.update(chunk)
            exported.write(chunk)
        _require(size == artifact["size"] and digest.hexdigest() == artifact["sha256"], "artifact-hash-mismatch")
        artifact_bound()
        archive.flush()
        archive.seek(0)
        archive_digest = hashlib.sha256()
        while chunk := archive.read(1024 * 1024):
            _deadline(deadline)
            archive_digest.update(chunk)
        metadata = {"schemaVersion": 1, "source": {**origin,
                    "manifestSha256": hashlib.sha256(source.read(snapshot.MANIFEST_NAME)[0]).hexdigest()},
                    "snapshot": {"size": archive.tell(), "sha256": archive_digest.hexdigest()},
                    "preparation": {"format": "nix-export", "objects": objects,
                                    "artifact": {key: artifact[key] for key in ("size", "sha256")},
                                    "signatureScope": SIGNATURE_SCOPE}}
        info, info_fd = stack.enter_context(cache.archive_transport(work))
        info.write((json.dumps(metadata, sort_keys=True) + "\n").encode())
        for stream in (archive, exported, info):
            stream.flush()
            stream.seek(0)
        for fd in (archive_fd, export_fd, info_fd):
            os.lseek(fd, 0, os.SEEK_SET)
        receipt_bound()
        source.check()
        yield {"grafts": [f"{name}=/dev/fd/{fd}" for name, fd in
                          (("source.tar", archive_fd), ("dependencies.export", export_fd), ("acceptance.json", info_fd))],
               "pass_fds": (archive_fd, export_fd, info_fd), "metadata": metadata}
        source.check()
        receipt_bound()
        artifact_bound()


@contextmanager
def approved_network_payloads(config: NetworkConfig, work: Path, deadline: float):
    """Yield source.tar and metadata.json as anonymous readonly regular FDs.

    The caller supplies the independently frozen source store path as an expected
    string only. work is an untrusted staging namespace; archive_transport binds
    exclusive file handles and unlinks their names before any payload write.
    No store path is opened and no native process is invoked.
    """
    _validate_network_config(config)
    _deadline(deadline)
    with ExitStack() as stack:
        source = stack.enter_context(snapshot._SnapshotIO(vm.safe_path(config.snapshot)))
        root = os.fstat(source.fd)
        _require((root.st_dev, root.st_ino) == config.snapshot_identity, "snapshot-root-identity-mismatch")
        manifest = snapshot._verify_snapshot(source)
        _require(manifest["fixture"]["selectionStage"] == "minimal"
                 and manifest["fixture"]["secrets"] == "guest-generated-only", "snapshot-not-minimal")
        _deadline(deadline)
        archive, archive_fd = stack.enter_context(cache.archive_transport(work))
        nar_hash, nar_size = _source_archive(source, manifest, archive, deadline)
        _require(type(nar_size) is int and 0 < nar_size <= MAX_SOURCE * 2, "source-budget-exceeded")
        archive.flush()
        size = archive.tell()
        _require(0 < size <= MAX_SOURCE * 2, "source-budget-exceeded")
        archive.seek(0)
        digest, observed = hashlib.sha256(), 0
        while chunk := archive.read(1024 * 1024):
            _deadline(deadline)
            observed += len(chunk)
            _require(observed <= size, "source-budget-exceeded")
            digest.update(chunk)
        _require(observed == size, "source-size-mismatch")
        metadata = {"schemaVersion": 1, "transport": "guest-fetch",
                    "source": {"path": config.source_store_path, "narHash": nar_hash,
                               "lockSha256": manifest["lockSha256"],
                               "manifestSha256": hashlib.sha256(source.read(snapshot.MANIFEST_NAME)[0]).hexdigest()},
                    "snapshot": {"size": size, "sha256": digest.hexdigest()}, "sourceNarSize": nar_size}
        info, info_fd = stack.enter_context(cache.archive_transport(work))
        info.write((json.dumps(metadata, sort_keys=True) + "\n").encode())
        info.flush()
        for fd in (archive_fd, info_fd):
            os.lseek(fd, 0, os.SEEK_SET)
        source.check()
        _deadline(deadline)
        yield {"grafts": [f"{name}=/dev/fd/{fd}" for name, fd in
                          (("source.tar", archive_fd), ("metadata.json", info_fd))],
               "pass_fds": (archive_fd, info_fd), "metadata": metadata}
        source.check()
        _deadline(deadline)


def _exit(value, *, failed=False):
    _require(type(value) is int and (1 <= value <= 255 if failed else value == 0), "invalid-actual-exit")


def _secret_facts(row):
    _keys(row, ("ageKeyPresent", "ageKeyMode0600", "decrypted"))
    _require(row["ageKeyPresent"] is True and row["ageKeyMode0600"] is True, "secret-permissions-unverified")
    _keys(row["decrypted"], cache.SECRETS)
    for secret in row["decrypted"].values():
        _keys(secret, ("exists", "mode0600"))
        _require(secret["exists"] is True and secret["mode0600"] is True, "secret-permissions-unverified")


def _journal(row, target, *, failed=False):
    _keys(row, ("operationId", "operation", "result", "terminal", "journalPersisted", "sourceVerified",
                "sourceFrozenPath", "sourceOutputPath", "bindingTarget", "activateStatus", "verifyStatus", "conflictPartial"))
    _require(isinstance(row["operationId"], str) and re.fullmatch(r"[0-9a-f-]{36}", row["operationId"]) is not None)
    _require(row["operation"] == "apply" and row["journalPersisted"] is True
             and row["sourceVerified"] is True and _path(row["sourceFrozenPath"])
             and row["sourceOutputPath"] == target, "journal-source-unbound")
    if failed:
        _require(row["result"] == row["terminal"] == "partial" and row["activateStatus"] == "partial"
                 and row["verifyStatus"] is None and row["bindingTarget"] is None
                 and row["conflictPartial"] is True, "failure-not-preserved")
    else:
        _require(row["result"] == row["terminal"] == "ok" and row["activateStatus"] == row["verifyStatus"] == "ok"
                 and row["bindingTarget"] == target and row["conflictPartial"] is False, "journal-generation-unbound")


def receipt_from(raw: bytes, token: str, source: dict, *, transport: str = "nix-export") -> dict:
    """Strict future lifecycle receipt consumer; never infer success from exit 0."""
    _require(type(transport) is str and transport in ("nix-export", "guest-fetch"), "invalid-acceptance-transport")
    networked = transport == "guest-fetch"
    rows, installs = [], []
    for line in raw.splitlines():
        for prefix, destination in ((RECEIPT_PREFIX, rows), (install.RECEIPT_PREFIX, installs)):
            if line.startswith(prefix.encode()):
                data = line[len(prefix):]
                _require(len(data) <= MAX_RECEIPT, "oversized-acceptance-receipt")
                destination.append(_json(data))
    _require(len(rows) == len(installs) == 1, "missing-or-duplicate-acceptance-receipt")
    row, installed = rows[0], installs[0]
    keys = ("schemaVersion", "evidence", "arch", "token", "source", "install", "stages", "error")
    _keys(row, (*keys, "transport") if networked else keys)
    if networked:
        _require(row["transport"] == transport, "mismatched-acceptance-transport")
    _require(type(row["schemaVersion"]) is int and row["schemaVersion"] == 1
             and row["evidence"] == (NETWORK_EVIDENCE if networked else EVIDENCE) and row["arch"] == "x86_64"
             and row["token"] == token and row["source"] == source and row["error"] is None,
             "mismatched-acceptance-receipt")
    _keys(source, ("path", "narHash", "lockSha256", "manifestSha256"))
    _require(_path(source["path"]) and _nar_hash(source["narHash"])
             and _hash(source["lockSha256"]) and _hash(source["manifestSha256"]))
    _keys(installed, ("token", "arch", "version", "initialNoNix", "daemon", "daemonService", "buildUsers", "profile",
                      "installerExitZero", "bootstrapTools", "stages", "error"))
    _require(_same(row["install"], installed), "mismatched-install-receipt")
    install.receipt_from(raw, token)
    stages = row["stages"]
    _keys(stages, STAGES)
    setup = stages["setup"]
    _keys(setup, ("exitCode", "interactive", "saved", "commitDeclined", "applyDeclined", "existingNixBranch"))
    _exit(setup["exitCode"])
    _require(all(setup[key] is True for key in setup if key != "exitCode"), "setup-not-completed")
    for name in ("firstApply", "repeatApply", "recoveryApply"):
        stage = stages[name]
        _keys(stage, ("exitCode", "target", "generation", "journal", "managedSha256", "permissionsSha256", "secrets", "tuiBuildTests"))
        _exit(stage["exitCode"])
        _require(_path(stage["target"]) and type(stage["generation"]) is int and stage["generation"] > 0)
        _require(_hash(stage["managedSha256"]) and _hash(stage["permissionsSha256"]) and stage["tuiBuildTests"] is True)
        _secret_facts(stage["secrets"])
        _journal(stage["journal"], stage["target"])
    first = stages["firstApply"]
    for name in ("repeatApply", "recoveryApply"):
        _require(all(stages[name][key] == first[key] for key in ("target", "managedSha256", "permissionsSha256")),
                 "repeat-or-recovery-drift")
    fault = stages["faultApply"]
    _keys(fault, ("exitCode", "target", "observedGeneration", "journal", "injectedAfter", "retryAllowed", "originalMachineRestored", "partialReadableAfterRecovery"))
    _exit(fault["exitCode"], failed=True)
    observed = fault["observedGeneration"]
    if observed is not None:
        _keys(observed, ("number", "target"))
        _require(type(observed["number"]) is int and observed["number"] > 0 and _path(observed["target"]))
    _require(_path(fault["target"]) and fault["target"] != first["target"] and fault["injectedAfter"] == "linkGeneration"
             and fault["retryAllowed"] is False and fault["originalMachineRestored"] is True
             and fault["partialReadableAfterRecovery"] is True, "failure-not-preserved")
    _journal(fault["journal"], fault["target"], failed=True)
    ids = [stages[name]["journal"]["operationId"] for name in STAGES if name != "setup"]
    _require(len(ids) == len(set(ids)), "reused-journal-operation")
    return row


def full_probe(config: FullConfig) -> dict:
    if isinstance(config, NetworkConfig):
        from full import full_probe as run_network_probe
        return run_network_probe(config)
    """Fail closed at the milestone; a later reviewed orchestrator owns launch.

    Do not add this to an executable flake wrapper before implementing the real
    guest PTY/login, import verification and durable lifecycle observations.
    """
    report = {"schemaVersion": 1, "command": "full-probe", "status": "blocked", "ok": False,
              "evidence": None, "fullReady": False, "milestone": "acceptance-boundary",
              "commands": [], "receipt": None, "error": "guest-lifecycle-not-implemented"}
    try:
        _validate_config(config)
    except (vm.ProbeError, TypeError, ValueError) as exc:
        report["error"] = str(exc) if isinstance(exc, vm.ProbeError) else "invalid-acceptance-input"
    return report

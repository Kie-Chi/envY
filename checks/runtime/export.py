#!/usr/bin/env python3
"""Prepare a bounded Nix export from the existing public dependency preflight."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time

import cache

CHUNK = 65536
STDERR_CAP = 65536
# Legacy export framing: magic, path, references, deriver, signature marker and
# padding. Charge a generous fixed allowance plus every encoded metadata string.
FRAMING_BYTES = 4096
PROVENANCE = {"verified-snapshot", "locked-input", "verified-snapshot-or-locked-input"}


def _identity(value):
    return value.st_dev, value.st_ino


def _selection(plan: dict, byte_budget: int, object_byte_budget: int) -> tuple[dict, dict]:
    """Consume preflight evidence only; never query or authorize another source."""
    if (type(byte_budget) is not int or byte_budget <= 0
            or type(object_byte_budget) is not int or object_byte_budget <= 0):
        raise cache.CacheError("Explicit positive export byte budgets are required")
    if plan.get("publicCache") != cache.PUBLIC_CACHE:
        raise cache.CacheError("Unexpected public cache policy")
    objects = {}
    for row in plan["objects"]:
        path = cache.store_path(row["path"])
        if path != row["path"] or path in objects:
            raise cache.CacheError("Noncanonical or duplicate plan object")
        objects[path] = row
    validated, failures = {}, {}
    for path, row in objects.items():
        if row["status"] == "needsguestbuild":
            continue
        try:
            public = row["status"] == "cached"
            if not public and not (row["status"] == "nativeavailable"
                                   and row.get("kind") == "source"
                                   and row.get("provenance") in PROVENANCE):
                raise cache.CacheError("Unapproved object")
            if path.endswith(".drv"):
                raise cache.CacheError("Derivations are not transport inputs")
            normalized = cache.metadata({path: row}, path, public=public)
            if normalized["references"] != row["references"]:
                raise cache.CacheError("Noncanonical references")
            if normalized["narSize"] > object_byte_budget:
                raise cache.CacheError("Object NAR budget exceeded")
            validated[path] = row
        except (cache.CacheError, KeyError, TypeError):
            failures[path] = "unapproved-or-over-budget-object"
    eligible = {}
    for path in validated:
        seen, pending = set(), [path]
        while pending:
            target = pending.pop()
            if target in seen:
                continue
            seen.add(target)
            if target not in validated:
                failures[path] = "incomplete-approved-closure"
                break
            pending.extend(validated[target]["references"])
        else:
            eligible[path] = validated[path]
    nar_bytes = sum(row["narSize"] for row in eligible.values())
    download_bytes = sum(row["fileSize"] for row in eligible.values() if row["status"] == "cached")
    overhead = 8 + sum(FRAMING_BYTES + len(path.encode()) + sum(len(ref.encode()) + 16 for ref in row["references"])
                       for path, row in eligible.items())
    capacity = {"byteBudget": byte_budget, "objectNarByteBudget": object_byte_budget,
                "knownNarBytes": nar_bytes, "knownPublicDownloadBytes": download_bytes,
                "transportMetadataOverheadBytes": overhead,
                "exportByteCap": nar_bytes + overhead,
                "complete": len(eligible) == sum(row["status"] != "needsguestbuild" for row in objects.values()),
                "fits": nar_bytes + overhead <= byte_budget and download_bytes <= byte_budget,
                "objects": [{"path": path, "narSize": row["narSize"],
                             "fileSize": row.get("fileSize") if row["status"] == "cached" else None}
                            for path, row in sorted(eligible.items())]}
    return eligible, {"capacity": capacity, "failures": [{"path": p, "kind": kind} for p, kind in sorted(failures.items())]}


def export_capacity(plan: dict, *, byte_budget: int, object_byte_budget: int) -> dict:
    """Allow a caller to review capacity before authorizing actual preparation."""
    return _selection(plan, byte_budget, object_byte_budget)[1]


def _legacy_tool(nix: cache.Nix) -> str:
    # Resolve the trusted Nix package, then retain the sibling's invocation name:
    # resolving nix-store itself would turn a multicall symlink back into nix.
    parent = Path(nix.executable).resolve(strict=True).parent
    sibling = parent / "nix-store"
    if (not sibling.is_file() or not os.access(sibling, os.X_OK)
            or sibling.resolve(strict=True).parent != parent):
        raise cache.CacheError("Trusted sibling nix-store unavailable")
    return str(sibling)


def _stream_export(nix: cache.Nix, paths: list[str], fd: int, artifact: dict,
                   cap: int, deadline: float) -> None:
    """Closed legacy command; bounded buffers, caller-owned output FD, no URI."""
    if not paths or paths != sorted(set(paths)) or any(cache.store_path(p) != p for p in paths):
        raise cache.CacheError("Canonical nonempty export path list required")
    options = ["--option", "substituters", "", "--option", "extra-substituters", "",
               "--option", "builders", "", "--option", "max-jobs", "0",
               "--option", "substitute", "false", "--option", "netrc-file", ""]
    argv = [_legacy_tool(nix), *options, "--export", *paths]
    artifact["command"] = {"argv": argv, "exitCode": None}
    digest, size, stderr_size = hashlib.sha256(), 0, 0
    process = subprocess.Popen(argv, cwd=nix.work, env=nix.env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        with selectors.DefaultSelector() as selector:
            for stream, label in ((process.stdout, "out"), (process.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise cache.CacheError("Export timed out")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), CHUNK)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "err":
                        stderr_size += len(chunk)
                        if stderr_size > STDERR_CAP:
                            raise cache.CacheError("Export stderr cap exceeded")
                        continue
                    if size + len(chunk) > cap:
                        raise cache.CacheError("Export stdout cap exceeded")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise cache.CacheError("Export write made no progress")
                        digest.update(view[:written])
                        size += written
                        artifact.update(size=size, sha256=digest.hexdigest())
                        view = view[written:]
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise cache.CacheError("Export timed out") from exc
        artifact["command"]["exitCode"] = process.returncode
        if process.returncode:
            raise cache.CacheError("Export command unavailable")
    finally:
        failed, cleanup_error = sys.exc_info()[0] is not None, None
        try:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    cleanup_error = exc
            try:
                process.wait(timeout=cache.REAP_TIMEOUT)
            except (subprocess.TimeoutExpired, OSError) as exc:
                cleanup_error = exc
        finally:
            artifact["command"]["exitCode"] = process.returncode
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError as exc:
                    cleanup_error = exc
        if cleanup_error is not None and not failed:
            raise cache.CacheError("Export child cleanup unconfirmed") from cleanup_error


@contextmanager
def _artifact(path: Path, parent_identity: tuple[int, int]):
    """Atomically create one owned inode under a caller-prevalidated parent.

    The caller must retain its prevalidated parent identity from before this
    operation, rather than treating our first observation as ownership proof.
    """
    directory = cache.snapshot._open_directory_path(path.parent)
    fd = None
    try:
        def parent_bound():
            if _identity(os.fstat(directory)) != parent_identity:
                raise cache.CacheError("Prevalidated parent binding changed")
            observed = cache.snapshot._open_directory_path(path.parent)
            try:
                if _identity(os.fstat(observed)) != parent_identity:
                    raise cache.CacheError("Export parent binding changed")
            finally:
                os.close(observed)
        parent_bound()
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        owned = os.fstat(fd)
        if not stat.S_ISREG(owned.st_mode):
            raise cache.CacheError("Export inode is not regular")
        def bound():
            parent_bound()
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            held = os.fstat(fd)
            if (_identity(current) != _identity(owned) or not stat.S_ISREG(current.st_mode)
                    or held.st_nlink != 1 or stat.S_IMODE(held.st_mode) != 0o600):
                raise cache.CacheError("Export artifact binding changed")
        bound()
        yield fd, bound
        bound()
    finally:
        failed, cleanup_error = sys.exc_info()[0] is not None, None
        for held_fd in (fd, directory):
            if held_fd is not None:
                try:
                    os.close(held_fd)
                except OSError as exc:
                    cleanup_error = exc
        if cleanup_error is not None and not failed:
            raise cache.CacheError("Export descriptor cleanup unconfirmed") from cleanup_error


def _verify_artifact(fd: int, artifact: dict, deadline: float):
    os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    digest, size = hashlib.sha256(), 0
    while True:
        if time.monotonic() >= deadline:
            raise cache.CacheError("Artifact verification timed out")
        chunk = os.read(fd, CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > artifact["size"]:
            raise cache.CacheError("Artifact size changed")
        digest.update(chunk)
    if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise cache.CacheError("Artifact verification failed")


def prepare_export(snapshot: Path, new_artifact_path: Path, nix: cache.Nix, *, byte_budget: int,
                   object_byte_budget: int, parent_identity: tuple[int, int], timeout: float = 300) -> dict:
    """Recompute policy, verify host objects, then stream an independently checked export.

    Public substitution retains Nix signature checks. Local content verification
    uses --no-trust only for that verify invocation, since approved source paths
    need not be signed. The export is a transport, not a narinfo signature archive.
    Preflight retains its own command/node/request budgets; timeout bounds the
    subsequent preparation commands. parent_identity comes from the caller's
    prevalidated parent, before preparation or any possible namespace mutation.
    """
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise cache.CacheError("Invalid preparation deadline")
    if (not isinstance(parent_identity, tuple) or len(parent_identity) != 2
            or any(type(v) is not int for v in parent_identity)):
        raise cache.CacheError("A caller-prevalidated parent identity is required")
    destination = Path(os.path.abspath(new_artifact_path))
    source_root = snapshot.resolve(strict=True)
    checkout = Path(__file__).absolute().parents[2]
    if any(destination == p or p in destination.parents for p in (source_root, checkout)):
        raise cache.CacheError("Export must be outside snapshot and checkout")
    if destination.name in {"", ".", ".."}:
        raise cache.CacheError("A new export file is required")
    plan = cache.preflight(snapshot, nix)
    eligible, progress = _selection(plan, byte_budget, object_byte_budget)
    deadline = time.monotonic() + timeout
    progress["limits"] = {"timeoutSecondsAfterPreflight": timeout,
                          "stdoutByteCap": progress["capacity"]["exportByteCap"],
                          "stderrByteCap": STDERR_CAP, "streamChunkBytes": CHUNK,
                          "downloadByteScope": "known public metadata estimate, not observed network traffic"}
    progress.update(format="nix-export", completed=[], objects=[], artifact=None, complete=False,
                    signatureScope="public-copy-require-sigs; local-content-verify-no-trust; export-is-not-narinfo")
    plan.update(ready=False, preparation=progress)
    verified = set()

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise cache.CacheError("Preparation timed out")
        return value

    @contextmanager
    def command_budget():
        previous = nix.budget
        nix.budget = replace(previous, timeout=min(previous.timeout, remaining()))
        try:
            yield
        finally:
            nix.budget = previous

    def verify(path):
        remaining()
        row = eligible[path]
        with command_budget():
            actual = nix.info(path)
        if any(actual[key] != row[key] for key in ("narHash", "narSize", "references")):
            raise cache.CacheError("Native object metadata differs from plan")
        with command_budget():
            nix.run(["store", "verify", "--no-trust", "--no-substitute", path], json_output=False)
        remaining()
        verified.add(path)
        progress["completed"].append(path)
        progress["objects"].append({key: row[key] for key in ("path", "narHash", "narSize", "references")})

    def finish():
        progress["complete"] = plan["ready"]
        plan["status"] = "ready" if plan["ready"] else "partial" if verified else "blocked"
        return plan

    if not eligible:
        return finish()
    if not progress["capacity"]["fits"]:
        progress["failures"].append({"kind": "capacity-exceeded"})
        return finish()
    try:
        remaining()
        artifact = {"relativePath": destination.name, "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(), "verified": False, "complete": False}
        with _artifact(destination, parent_identity) as (fd, check):
            progress["artifact"] = artifact
            for path, row in sorted(eligible.items()):
                if path in verified:
                    continue
                check()
                remaining()
                if row["status"] == "cached":
                    try:
                        with command_budget():
                            nix.run(["copy", "--option", "require-sigs", "true", "--from", cache.PUBLIC_CACHE, path],
                                    network=True, json_output=False)
                    except cache.CacheError:
                        # Copy is not atomic: verify every approved member that
                        # may already have landed before the failed operation.
                        progress["failures"].append({"path": path, "kind": "public-copy-unavailable"})
                try:
                    check()
                    verify(path)
                except (cache.CacheError, KeyError, OSError):
                    progress["failures"].append({"path": path, "kind": "local-verification-unavailable"})
                check()
            # A failed root cannot authorize a partial closure in the stream.
            exportable = set(verified)
            while True:
                incomplete = {p for p in exportable if not set(eligible[p]["references"]) <= exportable}
                if not incomplete:
                    break
                exportable -= incomplete
            progress["exportedPaths"] = sorted(exportable)
            if exportable:
                check()
                _stream_export(nix, sorted(exportable), fd, artifact,
                               progress["capacity"]["exportByteCap"],
                               time.monotonic() + min(remaining(), nix.budget.timeout))
                check()
                _verify_artifact(fd, artifact, deadline)
                check()
                observed = os.fstat(fd)
                # A later consumer must check this identity before reading bytes.
                artifact["binding"] = {
                    "device": observed.st_dev, "inode": observed.st_ino,
                    "size": observed.st_size, "modifiedNs": observed.st_mtime_ns,
                    "changedNs": observed.st_ctime_ns, "mode": stat.S_IMODE(observed.st_mode),
                }
        if exportable:
            artifact.update(verified=True, complete=True)
        plan["ready"] = bool(eligible) and not plan["gaps"] and not progress["failures"] and (
            len(verified) == len(eligible) and progress["capacity"]["complete"]
            and progress["artifact"] is not None and progress["artifact"]["verified"])
    except (cache.CommandCancelled, KeyboardInterrupt) as exc:
        plan.update(status="cancelled", ready=False)
        exc.result = plan
        raise
    except (cache.CacheError, cache.snapshot.SnapshotError, OSError) as exc:
        progress["failures"].append({"kind": "export-or-binding-unavailable", "error": type(exc).__name__})
    return finish()

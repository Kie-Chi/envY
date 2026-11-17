"""Synthetic export tests: no native store, network, host config or builds."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("runtime_export", Path(__file__).with_name("export.py"))
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)
cache = export.cache


def path(name):
    return "/nix/store/" + "a" * 32 + "-" + name


def row(name, refs=(), *, public=True):
    result = {"path": path(name), "narHash": "sha256-PUBLIC-TEST-ONLY=", "narSize": 128,
              "references": sorted(refs), "status": "cached" if public else "nativeavailable"}
    if public:
        result["fileSize"] = 32
    else:
        result.update(kind="source", provenance="verified-snapshot-or-locked-input")
    return result


def plan(*rows, gaps=()):
    return {"schemaVersion": 1, "objects": list(rows), "gaps": list(gaps),
            "publicCache": cache.PUBLIC_CACHE, "ready": False, "status": "planned"}


class FakeNix:
    def __init__(self, rows):
        self.budget = cache.Budget(requests=100)
        self.native = {r["path"]: copy.deepcopy(r) for r in rows if "narSize" in r}
        self.calls = []
        self.fail = set()
        self.hook = lambda: None

    def info(self, target):
        self.calls.append(("info", target))
        self.hook()
        if target in self.fail or target not in self.native:
            raise cache.CacheError("synthetic missing object")
        return copy.deepcopy(self.native[target])

    def run(self, args, **kwargs):
        self.calls.append(("run", args, kwargs))
        self.hook()
        if args[-1] in self.fail:
            raise cache.CacheError("synthetic unavailable")
        return ""


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="envy-export-unit-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.destination = self.root / "payload"
        self.rows = [row("runtime"), row("safe-source", public=False)]
        self.nix = FakeNix(self.rows)
        self.stream_calls = []
        self.stream_hook = lambda fd: None
        self.payload = b"PUBLIC-SYNTHETIC-EXPORT"

    def stream(self, nix, paths, fd, artifact, cap, deadline):
        self.stream_calls.append(paths)
        self.assertTrue(stat_regular(fd))
        self.stream_hook(fd)
        os.write(fd, self.payload)
        artifact.update(size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest(),
                        command={"exitCode": 0})

    def prepare(self, *, gaps=(), byte_budget=100000, object_byte_budget=10000):
        with patch.object(cache, "preflight", return_value=plan(*self.rows, gaps=gaps)) as preflight, \
                patch.object(export, "_stream_export", side_effect=self.stream):
            result = export.prepare_export(self.snapshot, self.destination, self.nix,
                                           byte_budget=byte_budget, object_byte_budget=object_byte_budget,
                                           parent_identity=export._identity(self.destination.parent.stat()))
            preflight.assert_called_once_with(self.snapshot, self.nix)
            return result

    def test_success_needs_content_verify_metadata_export_and_hash(self):
        result = self.prepare()
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ready")
        progress = result["preparation"]
        self.assertEqual(progress["format"], "nix-export")
        self.assertTrue(progress["artifact"]["verified"])
        self.assertEqual((self.destination).read_bytes(), self.payload)
        self.assertEqual(progress["artifact"]["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(progress["artifact"]["relativePath"], self.destination.name)
        observed = self.destination.stat()
        self.assertEqual(progress["artifact"]["binding"], {
            "device": observed.st_dev, "inode": observed.st_ino, "size": observed.st_size,
            "modifiedNs": observed.st_mtime_ns, "changedNs": observed.st_ctime_ns,
            "mode": 0o600,
        })
        self.assertEqual(len(progress["completed"]), 2)
        copies = [c for c in self.nix.calls if c[0] == "run" and c[1][0] == "copy"]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][1], ["copy", "--option", "require-sigs", "true", "--from", cache.PUBLIC_CACHE, path("runtime")])
        self.assertEqual(copies[0][2], {"network": True, "json_output": False})
        self.assertFalse(any("--to" in c[1] for c in self.nix.calls if c[0] == "run"))
        verifies = [c for c in self.nix.calls if c[0] == "run" and c[1][0] == "store"]
        self.assertEqual(len(verifies), 2)
        self.assertTrue(all(c[1][:4] == ["store", "verify", "--no-trust", "--no-substitute"] for c in verifies))
        self.assertFalse(any("build" in c[1] for c in self.nix.calls if c[0] == "run"))

    def test_unknown_private_sources_never_read_or_export(self):
        for bad in (row("private", public=False), {"path": path("unknown"), "status": "unknown"}):
            bad.pop("provenance", None)
            self.rows.append(bad)
        result = self.prepare(gaps=[{"kind": "unverified-source"}])
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.stream_calls, [[path("runtime"), path("safe-source")]])
        self.assertFalse(any(c[0] == "info" and c[1] in {path("private"), path("unknown")} for c in self.nix.calls))

    def test_incomplete_closure_blocks_ancestor_but_preserves_independent_object(self):
        self.rows[0]["references"] = [path("missing")]
        self.rows.append({"path": path("missing"), "status": "missing", "kind": "FOD"})
        result = self.prepare(gaps=[{"kind": "FOD-cache-missing"}])
        self.assertFalse(result["ready"])
        self.assertEqual(self.stream_calls, [[path("safe-source")]])
        self.assertFalse(any(c[0] == "info" and c[1] != path("safe-source") for c in self.nix.calls))

    def test_guest_build_outputs_are_not_exported(self):
        self.rows.append({"path": path("envy"), "status": "needsguestbuild"})
        result = self.prepare()
        self.assertTrue(result["ready"])
        self.assertNotIn(path("envy"), result["preparation"]["completed"])

    def test_native_provenance_exact_allowlist_and_drv_exclusion(self):
        self.rows += [row("bad-provenance", public=False), row("unneeded.drv")]
        self.rows[-2]["provenance"] = "host-file-exists"
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["preparation"]["completed"]), 2)
        self.assertTrue(all(not p.endswith(".drv") for p in self.stream_calls[0]))

    def test_wrong_metadata_and_failed_content_verify_block_export(self):
        for key, value in (("narHash", "sha256-WRONG="), ("narSize", 129), ("references", [path("foreign")])):
            with self.subTest(key=key):
                self.destination = self.root / key
                self.nix.native[path("runtime")][key] = value
                result = self.prepare()
                self.assertFalse(result["ready"])
                self.assertNotIn(path("runtime"), self.stream_calls[-1])
                self.nix.native[path("runtime")] = copy.deepcopy(self.rows[0])
        self.destination = self.root / "content-failure"
        original = self.nix.run
        def run(args, **kwargs):
            if args[0] == "store" and args[-1] == path("runtime"):
                raise cache.CacheError("content corrupt")
            return original(args, **kwargs)
        self.nix.run = run
        self.assertFalse(self.prepare()["ready"])
        self.assertEqual(self.stream_calls[-1], [path("safe-source")])

    def test_copy_failure_retains_verified_partial_and_never_claims_ready(self):
        self.rows[0]["references"] = [path("dependency")]
        self.nix.native[path("runtime")] = copy.deepcopy(self.rows[0])
        self.rows.append(row("dependency"))
        self.nix.native[path("dependency")] = copy.deepcopy(self.rows[-1])
        original = self.nix.run
        def run(args, **kwargs):
            if args[0] == "copy" and args[-1] == path("runtime"):
                raise cache.CacheError("partial recursive copy")
            return original(args, **kwargs)
        self.nix.run = run
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["preparation"]["completed"]), 3)
        self.assertTrue(result["preparation"]["artifact"]["verified"])

    def test_failed_member_prevents_export_of_verified_ancestor(self):
        self.rows[0]["references"] = [path("dependency")]
        self.rows.append(row("dependency"))
        self.nix.native[path("runtime")] = copy.deepcopy(self.rows[0])
        self.nix.fail.add(path("dependency"))
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertIn(path("runtime"), result["preparation"]["completed"])
        self.assertEqual(self.stream_calls, [[path("safe-source")]])

    def test_capacity_precedes_copy_and_directory_creation(self):
        result = self.prepare(byte_budget=100)
        self.assertFalse(result["ready"])
        self.assertEqual(self.nix.calls, [])
        self.assertFalse(self.destination.exists())
        capacity = result["preparation"]["capacity"]
        self.assertEqual(capacity["knownPublicDownloadBytes"], 32)
        self.assertEqual(capacity["knownNarBytes"], 256)
        self.assertGreater(capacity["transportMetadataOverheadBytes"], 0)
        result = self.prepare(object_byte_budget=127)
        self.assertFalse(result["ready"])
        self.assertEqual(self.nix.calls, [])

    def test_unknown_or_invalid_sizes_are_not_zero(self):
        for key, value in (("fileSize", None), ("fileSize", False), ("narSize", None), ("narSize", "128")):
            with self.subTest(key=key, value=value):
                bad = row("bad")
                bad[key] = value
                report = export.export_capacity(plan(bad), byte_budget=100000, object_byte_budget=10000)
                self.assertFalse(report["capacity"]["complete"])
                self.assertEqual(report["capacity"]["objects"], [])
                self.assertTrue(report["failures"])

    def test_artifact_hash_or_export_exit_failure_never_ready(self):
        original = self.stream
        def corrupt(*args):
            original(*args)
            os.write(args[2], b"TAMPERED")
        self.stream = corrupt
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertFalse(result["preparation"]["artifact"]["verified"])
        self.destination = self.root / "exit-failure"
        def fail(*args):
            original(*args)
            raise cache.CacheError("export exit 1")
        self.stream = fail
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["preparation"]["completed"]), 2)

    def test_cancel_preserves_progress_and_closes_owned_fd(self):
        captured = []
        def cancel(fd):
            captured.append(fd)
            raise cache.CommandCancelled(signal.SIGTERM)
        self.stream_hook = cancel
        with self.assertRaises(cache.CommandCancelled) as caught:
            self.prepare()
        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertEqual(caught.exception.result["status"], "cancelled")
        self.assertEqual(len(caught.exception.result["preparation"]["completed"]), 2)
        with self.assertRaises(OSError):
            os.fstat(captured[0])

    def test_existing_destination_and_symlink_are_untouched(self):
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "canary").write_bytes(b"DO-NOT-CHANGE")
        self.destination.symlink_to(foreign, target_is_directory=True)
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(list(foreign.iterdir()), [foreign / "canary"])
        self.assertEqual(self.nix.calls, [])

    def test_destination_and_ancestor_swaps_before_or_during_export(self):
        for ancestor in (False, True):
            for during in (False, True):
                with self.subTest(ancestor=ancestor, during=during):
                    parent = self.root / ("parent-" + str(ancestor) + str(during))
                    parent.mkdir()
                    self.destination = parent / "payload"
                    foreign = self.root / ("foreign-" + parent.name)
                    foreign.mkdir()
                    (foreign / "canary").write_bytes(b"DO-NOT-CHANGE")
                    attacked = False
                    def attack(fd=None):
                        nonlocal attacked
                        if attacked:
                            return
                        attacked = True
                        target = parent if ancestor else self.destination
                        target.rename(target.with_name(target.name + "-owned"))
                        target.symlink_to(foreign, target_is_directory=True)
                    self.stream_hook = attack if during else lambda fd: None
                    self.nix.hook = (lambda: None) if during else attack
                    result = self.prepare()
                    self.assertFalse(result["ready"])
                    self.assertEqual(list(foreign.iterdir()), [foreign / "canary"])
                    self.assertEqual((foreign / "canary").read_bytes(), b"DO-NOT-CHANGE")
                    self.nix.hook = lambda: None

    def test_leaf_replacement_during_and_after_write_never_touches_foreign(self):
        for after in (False, True):
            with self.subTest(after=after):
                self.destination = self.root / ("leaf-" + str(after))
                canary = self.root / ("canary-" + str(after))
                canary.write_bytes(b"FOREIGN")
                def replace(fd):
                    leaf = self.destination
                    leaf.rename(self.destination.with_name(self.destination.name + "-owned"))
                    leaf.symlink_to(canary)
                self.stream_hook = (lambda fd: None) if after else replace
                original = self.stream
                if after:
                    def after_stream(*args):
                        original(*args)
                        replace(args[2])
                    self.stream = after_stream
                result = self.prepare()
                self.assertFalse(result["ready"])
                self.assertEqual(canary.read_bytes(), b"FOREIGN")
                self.stream = original

    def test_leaf_existing_before_create_is_not_opened(self):
        canary = self.root / "canary"
        canary.write_bytes(b"FOREIGN")
        original = export.os.open
        def plant(name, flags, *args, **kwargs):
            if name == self.destination.name and flags & os.O_CREAT:
                self.destination.symlink_to(canary)
            return original(name, flags, *args, **kwargs)
        with patch.object(export.os, "open", side_effect=plant):
            result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(canary.read_bytes(), b"FOREIGN")
        self.assertEqual(self.stream_calls, [])

    def test_parent_realdir_replacement_before_binding_is_rejected(self):
        parent = self.root / "prevalidated"
        parent.mkdir()
        identity = export._identity(parent.stat())
        parent.rename(self.root / "owned-parent")
        parent.mkdir()
        self.destination = parent / "payload"
        with patch.object(cache, "preflight", return_value=plan(*self.rows)):
            result = export.prepare_export(self.snapshot, self.destination, self.nix,
                                           byte_budget=100000, object_byte_budget=10000,
                                           parent_identity=identity)
        self.assertFalse(result["ready"])
        self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual(self.nix.calls, [])

    def test_snapshot_or_checkout_destination_is_rejected(self):
        for destination in (self.snapshot / "export", Path(export.__file__).parent / "export-output"):
            self.destination = destination
            with self.assertRaises(cache.CacheError):
                self.prepare()
            self.assertFalse(destination.exists())

    def test_replaced_parent_immediately_after_atomic_create_has_zero_writes(self):
        parent = self.root / "pinned-parent"
        parent.mkdir()
        self.destination = parent / "payload"
        foreign = self.root / "foreign-parent"
        foreign.mkdir()
        original = export.os.open
        def attack(name, flags, *args, **kwargs):
            fd = original(name, flags, *args, **kwargs)
            if name == "payload" and flags & os.O_CREAT:
                parent.rename(self.root / "original-parent")
                parent.symlink_to(foreign, target_is_directory=True)
            return fd
        with patch.object(export.os, "open", side_effect=attack):
            result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(self.nix.calls, [])
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertEqual((self.root / "original-parent" / "payload").stat().st_size, 0)

    def test_existing_regular_artifact_is_never_overwritten(self):
        self.destination.write_bytes(b"FOREIGN")
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(self.destination.read_bytes(), b"FOREIGN")
        self.assertEqual(self.nix.calls, [])

    def test_added_hardlink_prevents_ready(self):
        def attack(fd):
            os.link(self.destination, self.root / "alias")
        self.stream_hook = attack
        result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertFalse(result["preparation"]["artifact"]["complete"])

    def test_fsync_failure_preserves_verified_objects(self):
        with patch.object(export.os, "fsync", side_effect=OSError("synthetic fsync failure")):
            result = self.prepare()
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["preparation"]["completed"]), 2)
        self.assertFalse(result["preparation"]["artifact"]["complete"])

    def test_preflight_cannot_promote_duplicate_noncanonical_or_other_cache(self):
        bad_plans = [plan(row("duplicate"), row("duplicate")), plan(row("path"))]
        bad_plans[1]["objects"][0]["path"] = Path(path("path")).name
        other = plan(row("path"))
        other["publicCache"] = "https://unapproved.invalid"
        bad_plans.append(other)
        for bad in bad_plans:
            with self.assertRaises(cache.CacheError):
                export.export_capacity(bad, byte_budget=10000, object_byte_budget=1000)
        for budget in (0, -1, True, 1.0):
            with self.assertRaises(cache.CacheError):
                export.export_capacity(plan(*self.rows), byte_budget=budget, object_byte_budget=1000)

    def test_reuses_cached_stop_policy_without_accessing_build_inputs(self):
        # Exercise the real planner with synthetic derivations; the exporter
        # receives only that planner's output, never another dependency catalog.
        class PlannerNix(FakeNix):
            request_count = 0
            def info(inner, target, *, public=False):
                self.assertNotIn("private", target)
                self.assertNotIn("never", target)
                return copy.deepcopy(inner.native[target])
            def derivation(inner, target):
                return {"outputs": {"out": {"path": target.removesuffix(".drv")}},
                        "system": "x86_64-linux", "inputDrvs": {path("never.drv"): ["out"]},
                        "inputSrcs": [path("private-source")]}
            def run(inner, args, **kwargs):
                if args[:2] == ["flake", "archive"]:
                    return {"path": path("safe-source"), "inputs": {}}
                if args[0] == "eval":
                    return {"activation": path("activation.drv"), "setup": path("setup.drv"),
                            "extensionAvailable": True, "secretNames": list(cache.SECRETS)}
                return super().run(args, **kwargs)
        rows = [row("activation"), row("setup"), row("safe-source", public=False)]
        self.nix = PlannerNix(rows)
        source = {"path": path("safe-source"), "narHash": rows[-1]["narHash"],
                  "lockSha256": "0" * 64, "lock": {"nodes": {"root": {}}}}
        with patch.object(cache, "freeze_source", return_value=source), \
                patch.object(export, "_stream_export", side_effect=self.stream):
            result = export.prepare_export(self.snapshot, self.destination, self.nix,
                                           byte_budget=100000, object_byte_budget=10000,
                                           parent_identity=export._identity(self.destination.parent.stat()))
        self.assertTrue(result["ready"])
        self.assertEqual(self.stream_calls, [[path("activation"), path("safe-source"), path("setup")]])


def stat_regular(fd):
    import stat
    return stat.S_ISREG(os.fstat(fd).st_mode)


class StreamTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="envy-export-stream-unit-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.tool = self.bin / "nix"
        self.tool.write_text("#!/bin/sh\nexit 99\n")
        self.tool.chmod(0o700)
        self.legacy = self.bin / "nix-store"
        self.work = self.root / "work"
        self.work.mkdir()
        self.nix = cache.Nix(str(self.tool), self.work, cache.Budget(timeout=1))
        self.fd = os.open(self.root / "owned", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        self.addCleanup(os.close, self.fd)
        self.artifact = {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}

    def configure(self, code):
        self.legacy.write_text("#!" + sys.executable + "\nimport os, sys, time, json\n" + code)
        self.legacy.chmod(0o700)

    def stream(self, cap=100000, timeout=1):
        export._stream_export(self.nix, [path("tiny")], self.fd, self.artifact,
                              cap, time.monotonic() + timeout)

    def test_success_fixed_argv_empty_config_and_small_chunks(self):
        self.configure("open('../calls', 'w').write(json.dumps({'argv':sys.argv,'home':os.environ['HOME']}))\n"
                       "os.write(1, b'PUBLIC-ONLY')\n")
        self.stream()
        call = json.loads((self.root / "calls").read_text())
        self.assertEqual(call["argv"][0], str(self.legacy))
        self.assertEqual(call["home"], str(self.work / "home"))
        self.assertNotIn("--offline", call["argv"])
        self.assertNotIn("--to", call["argv"])
        self.assertNotIn("file://", " ".join(call["argv"]))
        self.assertEqual(call["argv"][-2:], ["--export", path("tiny")])
        self.assertEqual(self.artifact["command"]["exitCode"], 0)
        export._verify_artifact(self.fd, self.artifact, time.monotonic() + 1)

    def test_multicall_sibling_keeps_name_and_external_target_rejected(self):
        self.legacy.symlink_to(self.tool)
        self.assertEqual(export._legacy_tool(self.nix), str(self.legacy))
        self.legacy.unlink()
        foreign = self.root / "foreign"
        foreign.write_text("PUBLIC")
        foreign.chmod(0o700)
        self.legacy.symlink_to(foreign)
        with self.assertRaises(cache.CacheError):
            export._legacy_tool(self.nix)

    def test_stdout_stderr_timeout_and_nonzero_exit_are_bounded(self):
        for code, cap, timeout, pattern in (
                ("os.write(1, b'x'*20000)\n", 1024, 1, "stdout cap"),
                ("os.write(2, b'x'*100000)\n", 100000, 1, "stderr cap"),
                ("time.sleep(5)\n", 100000, 0.05, "timed out"),
                ("os.write(1,b'partial');sys.exit(3)\n", 100000, 1, "unavailable")):
            with self.subTest(pattern=pattern):
                self.configure(code)
                before = time.monotonic()
                with self.assertRaisesRegex(cache.CacheError, pattern):
                    self.stream(cap, timeout)
                self.assertLess(time.monotonic() - before, 2)
                self.assertLessEqual(self.artifact["size"], cap)

    def test_large_stream_does_not_accumulate_stdout(self):
        self.configure("for i in range(256): os.write(1, b'x'*65536)\n")
        import tracemalloc
        tracemalloc.start()
        try:
            self.stream(cap=20 * 1024 * 1024, timeout=5)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(self.artifact["size"], 16 * 1024 * 1024)
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_short_writes_hash_only_written_bytes(self):
        self.configure("os.write(1, b'PUBLIC-ONLY'*20)\n")
        original = os.write
        def short(fd, data):
            return original(fd, data[:7])
        with patch.object(export.os, "write", side_effect=short):
            self.stream()
        self.assertEqual(self.artifact["size"], len(b"PUBLIC-ONLY" * 20))
        export._verify_artifact(self.fd, self.artifact, time.monotonic() + 1)

    def test_invalid_export_paths_do_not_start_process(self):
        with patch.object(export.subprocess, "Popen") as popen:
            for paths in ([], ["/etc/passwd"], [path("x"), path("x")], [Path(path("x")).name]):
                with self.assertRaises(cache.CacheError):
                    export._stream_export(self.nix, paths, self.fd, self.artifact, 1000, time.monotonic() + 1)
            popen.assert_not_called()

    def test_sigterm_unwinds_stream_owned_group_and_retains_cancel(self):
        self.configure("open('../pid','w').write(str(os.getpid()))\ntime.sleep(10)\n")
        original_read = export.selectors.DefaultSelector
        class CancelSelector:
            def __enter__(inner):
                inner.real = original_read()
                inner.real.__enter__()
                return inner
            def __exit__(inner, *args):
                return inner.real.__exit__(*args)
            def register(inner, *args):
                return inner.real.register(*args)
            def get_map(inner):
                return inner.real.get_map()
            def select(inner, timeout):
                deadline = time.monotonic() + 2
                while not (self.root / "pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGTERM)
        with cache.cli_cancellation(), patch.object(export.selectors, "DefaultSelector", CancelSelector):
            with self.assertRaises(cache.CommandCancelled) as caught:
                self.stream()
        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_bounded_reap_and_original_failure_are_preserved(self):
        self.configure("pass\n")
        for cancelled in (False, True):
            process = SimpleNamespace(pid=12345678, returncode=None, stdout=Mock(), stderr=Mock(),
                                      wait=Mock(side_effect=subprocess.TimeoutExpired("fake", 1)))
            selector = Mock()
            selector.__enter__ = Mock(return_value=selector)
            selector.__exit__ = Mock(return_value=False)
            selector.get_map.return_value = True
            original = cache.CommandCancelled(signal.SIGTERM) if cancelled else cache.CacheError("original timeout")
            selector.select.side_effect = original
            with patch.object(export.subprocess, "Popen", return_value=process), \
                    patch.object(export.selectors, "DefaultSelector", return_value=selector), \
                    patch.object(export.os, "set_blocking"), patch.object(export.os, "killpg") as kill:
                with self.assertRaises(type(original)) as caught:
                    self.stream()
                self.assertIs(caught.exception, original)
                kill.assert_called_once_with(process.pid, signal.SIGKILL)
                process.wait.assert_called_once_with(timeout=cache.REAP_TIMEOUT)
                process.stdout.close.assert_called_once()
                process.stderr.close.assert_called_once()

    def test_reaped_pid_never_signalled(self):
        self.configure("sys.exit(3)\n")
        with patch.object(export.os, "killpg") as kill:
            with self.assertRaises(cache.CacheError):
                self.stream()
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()

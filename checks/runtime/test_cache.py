"""Offline unit tests for public-cache planning; no downloads or Linux builds."""
import copy
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("runtime_cache", Path(__file__).with_name("cache.py"))
cache = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cache
SPEC.loader.exec_module(cache)


def path(name):
    return "/nix/store/" + "a" * 32 + "-" + name


def info(name, refs=(), public=False):
    result = {"path": path(name), "narHash": "sha256-PUBLIC-TEST-ONLY=", "narSize": 123,
              "references": list(refs), "status": "cached" if public else "nativeavailable"}
    if public:
        result["fileSize"] = 17
    return result


def drv(name, dependencies=None, sources=(), fixed=False, modern=False):
    output = {"path": Path(path(name)).name if modern else path(name)}
    if fixed:
        output["hash"] = "sha256-PUBLIC-TEST-ONLY="
    row = {"outputs": {"out": output}, "system": "x86_64-linux"}
    deps = dependencies or {}
    if modern:
        row["inputs"] = {"drvs": {Path(key).name: {"outputs": value, "dynamicOutputs": {}}
                                   for key, value in deps.items()}, "srcs": list(sources)}
    else:
        row.update(inputDrvs=deps, inputSrcs=list(sources))
    return row


SOURCE = {"path": path("safe-source"), "narHash": "sha256-PUBLIC-TEST-ONLY=",
          "lockSha256": "0" * 64, "lock": {"nodes": {"root": {}}}}


class FakeNix:
    def __init__(self):
        self.budget = cache.Budget(requests=256)
        self.request_count = 0
        self.rows = {}
        self.public = {}
        self.native = {SOURCE["path"]: info("safe-source")}
        self.visits = []
        self.commands = []
        self.fail_copies = set()
        self.bad_copies = set()
        self.archive = {"path": SOURCE["path"], "inputs": {}}
        self.roots = {"activation": path("activation.drv"), "setup": path("setup.drv"),
                      "extensionAvailable": True, "secretNames": list(cache.SECRETS)}

    def info(self, target, *, public=False, destination=None, pass_fds=()):
        if destination:
            fd, = pass_fds
            if destination != "file:///dev/fd/" + str(fd):
                raise AssertionError("Destination must use the inherited directory FD")
            os.fstat(fd)
        self.visits.append((target, public, destination))
        if public:
            self.request_count += 1
            if self.request_count > self.budget.requests:
                raise cache.CacheError("budget")
        rows = self.public if public else self.native
        if destination:
            rows = {**self.native, **self.public}
        row = rows.get(target)
        if row is None or isinstance(row, Exception):
            raise cache.CacheError("unavailable")
        result = copy.deepcopy(row)
        if destination and target in self.bad_copies:
            result["narSize"] += 1
        return result

    def derivation(self, target):
        self.commands.append(["derivation", "show", target])
        if target not in self.rows:
            raise cache.CacheError("unavailable")
        return self.rows[target]

    def run(self, args, **kwargs):
        self.commands.append(args)
        if args[:2] == ["flake", "archive"]:
            if isinstance(self.archive, Exception):
                raise self.archive
            return self.archive
        if args[0] == "eval":
            return copy.deepcopy(self.roots)
        if args[0] == "copy":
            fd, = kwargs["pass_fds"]
            if args[args.index("--to") + 1] != "file:///dev/fd/" + str(fd):
                raise AssertionError("Copy must use the inherited directory FD")
            os.fstat(fd)
            if args[-1] in self.fail_copies:
                raise cache.CacheError("copy unavailable")
            return ""
        raise AssertionError(args)

    def add(self, name, row, *, cached=False):
        self.rows[path(name + ".drv")] = row
        self.native[path(name + ".drv")] = info(name + ".drv",
            [*cache.derivation_inputs(row)[0], *cache.derivation_inputs(row)[1]])
        self.public[path(name)] = info(name, public=True) if cached else {"path": path(name), "status": "missing"}


def complete_fake():
    nix = FakeNix()
    nix.add("activation", drv("activation"), cached=True)
    nix.add("setup", drv("setup"), cached=True)
    return nix


class ShapeTests(unittest.TestCase):
    def test_public_sizes_never_use_uncompressed_nar_size_as_file_size(self):
        target = path("object")
        row = {"narHash": "sha256-test=", "narSize": 900, "references": []}
        with self.assertRaises(cache.CacheError):
            cache.metadata({target: row}, target, public=True)
        for extra in ({"fileSize": 27}, {"downloadSize": 27}, {"download": {"fileSize": 27}}):
            result = cache.metadata({target: {**row, **extra}}, target, public=True)
            self.assertEqual(result["fileSize"], 27)
            self.assertEqual(result["narSize"], 900)

    def test_path_info_legacy_list_and_versioned_shapes(self):
        target = path("object")
        row = {"path": target, "narHash": "sha256-test=", "narSize": 42,
               "references": [Path(path("ref")).name]}
        for value in ({target: row}, [row], {"version": 2, "info": {Path(target).name: row}}):
            self.assertEqual(cache.metadata(value, target, public=False)["references"], [path("ref")])

    def test_derivation_modern_and_legacy_shapes(self):
        for modern in (False, True):
            row = drv("out", {path("input.drv"): ["out"]}, [path("source")], modern=modern)
            self.assertEqual(cache.derivation_inputs(row), ({path("input.drv"): ["out"]}, [path("source")]))
            self.assertEqual(cache.output_path(row, "out"), path("out"))

    def test_reject_unknown_dynamic_inputs_and_bad_paths(self):
        for value in ("/etc/passwd", "https://user:secret@example.org/private", "../source", path("x") + "/file"):
            with self.assertRaises(cache.CacheError):
                cache.store_path(value)
        row = drv("out", modern=True)
        row["inputs"]["drvs"][path("child.drv")] = {"outputs": ["out"], "dynamicOutputs": {"x": {}}}
        with self.assertRaises(cache.CacheError):
            cache.derivation_inputs(row)
        with self.assertRaises(cache.CacheError):
            cache.derivation_inputs({"inputs": []})

    def test_overlay_is_only_three_secrets_and_real_hm_extension(self):
        expr = cache.root_expression(SOURCE)
        self.assertIn("original.extendModules", expr)
        self.assertIn("packages.x86_64-linux.setup.drvPath", expr)
        self.assertEqual(expr.count("sopsFile = lib.mkForce placeholder"), 3)
        self.assertNotIn("/Users/", expr)
        self.assertNotIn("secrets/secrets.yaml", expr)
        self.assertNotIn("homeManagerConfiguration", expr)


class GraphTests(unittest.TestCase):
    def test_cached_output_follows_refs_and_stops_build_dependencies(self):
        nix = FakeNix()
        nix.add("activation", drv("activation", {path("must-not-visit.drv"): ["out"]}, [path("private-source")]), cached=True)
        nix.add("setup", drv("setup"), cached=True)
        nix.public[path("activation")]["references"] = [path("runtime"), path("runtime")]
        nix.public[path("runtime")] = info("runtime", public=True)
        nix.public[path("runtime")]["references"] = [path("activation")]
        plan = cache.Planner(nix, SOURCE).plan()
        self.assertEqual(plan["status"], "planned")
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["totals"]["publicObjectCount"], 3)
        self.assertEqual(plan["totals"]["fileSize"], 51)
        self.assertNotIn(["derivation", "show", path("must-not-visit.drv")], nix.commands)
        self.assertFalse(any(visit[0] == path("private-source") for visit in nix.visits))

    def test_missing_local_envy_tui_continues_build_inputs(self):
        nix = FakeNix()
        nix.add("activation", drv("activation", {path("envy.drv"): ["out"]}))
        nix.add("envy", drv("envy", {path("envy-tui.drv"): ["out"]}, modern=True))
        nix.add("envy-tui", drv("envy-tui", {path("rust.drv"): ["out"]}))
        nix.add("rust", drv("rust", {path("never.drv"): ["out"]}), cached=True)
        nix.add("setup", drv("setup"), cached=True)
        plan = cache.Planner(nix, SOURCE).plan()
        rows = {row["path"]: row for row in plan["objects"]}
        self.assertEqual(rows[path("envy-tui")]["status"], "needsguestbuild")
        self.assertEqual(rows[path("rust")]["status"], "cached")
        self.assertEqual(plan["gaps"], [])
        self.assertNotIn(["derivation", "show", path("never.drv")], nix.commands)

    def test_unavailable_is_not_missing_or_guest_build(self):
        nix = complete_fake()
        nix.public[path("activation")] = cache.CacheError("transport failed")
        plan = cache.Planner(nix, SOURCE).plan()
        row = next(row for row in plan["objects"] if row["path"] == path("activation"))
        self.assertEqual(row["status"], "unavailable")
        self.assertNotIn("drv", row)
        self.assertFalse(plan["totals"]["complete"])
        self.assertIsNone(plan["totals"]["fileSize"])
        self.assertIsNone(plan["totals"]["narSize"])

    def test_fod_missing_is_explicit_gap(self):
        nix = complete_fake()
        nix.add("activation", drv("activation", fixed=True))
        plan = cache.Planner(nix, SOURCE).plan()
        self.assertIn({"kind": "FOD-cache-missing", "path": path("activation")}, plan["gaps"])
        self.assertTrue(any(row.get("kind") == "FOD" for row in plan["objects"]))

    def test_unknown_input_source_is_never_probed_or_export_authorized(self):
        nix = complete_fake()
        nix.add("activation", drv("activation", sources=[path("host-user-config")]))
        nix.native[path("host-user-config")] = info("host-user-config")
        plan = cache.Planner(nix, SOURCE).plan()
        self.assertIn({"kind": "unverified-source", "path": path("host-user-config")}, plan["gaps"])
        self.assertFalse(any(visit[0] == path("host-user-config") for visit in nix.visits))

    def test_locked_inputs_must_cover_all_expected_nar_hashes(self):
        nix = complete_fake()
        source = copy.deepcopy(SOURCE)
        source["lock"]["nodes"]["public-input"] = {"locked": {"narHash": "sha256-locked="}}
        plan = cache.Planner(nix, source).plan()
        self.assertEqual(plan["lockedInputs"]["status"], "unavailable")
        nix.archive["inputs"]["public-input"] = {"path": path("input"), "inputs": {}}
        nix.native[path("input")] = {**info("input"), "narHash": "sha256-locked="}
        plan = cache.Planner(nix, source).plan()
        self.assertEqual(plan["lockedInputs"]["status"], "complete")
        nix.native[path("input")]["narHash"] = "sha256-wrong="
        self.assertEqual(cache.Planner(nix, source).plan()["lockedInputs"]["status"], "unavailable")

    def test_locked_source_unknown_refs_prevent_complete_plan(self):
        nix = complete_fake()
        source = copy.deepcopy(SOURCE)
        source["lock"]["nodes"]["public-input"] = {"locked": {"narHash": "sha256-locked="}}
        nix.archive["inputs"]["public-input"] = {"path": path("input"), "inputs": {}}
        nix.native[path("input")] = {**info("input", [path("unverified-ref")]), "narHash": "sha256-locked="}
        plan = cache.Planner(nix, source).plan()
        self.assertFalse(plan["totals"]["complete"])
        self.assertIn({"kind": "unverified-source", "path": path("unverified-ref")}, plan["gaps"])

    def test_budgets_bound_metadata_and_graph(self):
        nix = complete_fake()
        nix.budget = cache.Budget(requests=1)
        plan = cache.Planner(nix, SOURCE).plan()
        self.assertFalse(plan["totals"]["complete"])
        nix = complete_fake()
        nix.budget = cache.Budget(nodes=1)
        plan = cache.Planner(nix, SOURCE).plan()
        self.assertTrue(any(g["kind"] == "root-or-graph-unavailable" for g in plan["gaps"]))

    def test_cache_ref_missing_blocks_complete_closure(self):
        nix = complete_fake()
        nix.public[path("activation")]["references"] = [path("absent")]
        nix.public[path("absent")] = {"path": path("absent"), "status": "missing"}
        self.assertIn({"kind": "incomplete-public-closure", "path": path("absent")}, cache.Planner(nix, SOURCE).plan()["gaps"])


class NativeRegressionTests(unittest.TestCase):
    def test_v4_fixed_output_without_path_uses_bound_environment_output(self):
        row = {"version": 4, "outputs": {"out": {"hash": "sha256-PUBLIC-TEST-ONLY=", "method": "flat"}},
               "env": {"out": path("crate-zmij-1.0.23.tar.gz")}}
        self.assertEqual(cache.output_path(row, "out"), path("crate-zmij-1.0.23.tar.gz"))
        for change in ({"version": 99}, {"version": 3}, {"env": {"out": "/private/canary"}},
                       {"outputs": {"out": {"method": "flat"}}},
                       {"outputs": {"out": {"hash": "sha256-test=", "method": "unknown"}}}):
            with self.subTest(change=change), self.assertRaises(cache.CacheError):
                cache.output_path({**row, **change}, "out")

    def test_v4_fod_is_missing_only_with_explicit_public_miss(self):
        for status in ("missing", "cached", "unavailable"):
            nix = complete_fake()
            row = drv("activation", fixed=True, modern=True)
            row["version"] = 4
            row["env"] = {"out": path("activation")}
            row["outputs"]["out"] = {"hash": "sha256-test=", "method": "flat"}
            nix.add("activation", row, cached=status == "cached")
            if status == "unavailable":
                nix.public[path("activation")] = cache.CacheError("PRIVATE-MARKER")
            plan = cache.Planner(nix, SOURCE).plan()
            obj = next(r for r in plan["objects"] if r["path"] == path("activation"))
            self.assertEqual(obj["status"], status)
            self.assertEqual(any(g["kind"] == "FOD-cache-missing" for g in plan["gaps"]), status == "missing")
            self.assertFalse(any(g["kind"] == "root-or-graph-unavailable" for g in plan["gaps"]))

    def test_graph_failure_has_closed_context_without_raw_diagnostics(self):
        nix = complete_fake()
        nix.rows[path("activation.drv")]["outputs"] = {"out": {"method": "flat"}}
        nix.rows[path("activation.drv")]["env"] = {"out": "PRIVATE-MARKER"}
        plan = cache.Planner(nix, SOURCE).plan()
        gap = next(g for g in plan["gaps"] if g["kind"] == "root-or-graph-unavailable")
        self.assertEqual(gap["step"], "output-path")
        self.assertEqual(gap["drv"], path("activation.drv"))
        self.assertEqual(gap["reason"], "evidence-unavailable")
        self.assertNotIn("PRIVATE-MARKER", json.dumps(plan))


class SourceProofTests(unittest.TestCase):
    def fixture(self, *, foreign=False):
        nix = complete_fake()
        source = copy.deepcopy(SOURCE)
        source["lock"] = {"root": "root", "nodes": {
            "root": {"inputs": {"nixpkgs": "nixpkgs"}},
            "nixpkgs": {"locked": {"narHash": "sha256-locked="}}}}
        owner = {"path": path("nixpkgs"), "narHash": "sha256-locked="}
        nix.archive["inputs"]["nixpkgs"] = {"path": owner["path"], "inputs": {}}
        nix.native[owner["path"]] = {**info("nixpkgs"), "narHash": owner["narHash"]}
        sources = {"tui": path("envy-tui"), "cargoLock": path("Cargo.lock"),
                   "sourceStdenv": path("source-stdenv.sh"), "defaultBuilder": path("default-builder.sh")}
        for target in sources.values():
            nix.native[target] = info(Path(target).name[33:])
        value = {"binding": {k: source[k] for k in ("path", "narHash", "lockSha256")},
                 "input": owner, "sources": sources}
        original_run = nix.run
        def run(args, **kwargs):
            if args[0] == "eval" and "inherit (args) src cargoLock" in args[-1]:
                nix.commands.append(args)
                return copy.deepcopy(value)
            return original_run(args, **kwargs)
        nix.run = run
        targets = list(sources.values())
        if foreign:
            targets.append("/nix/store/" + "b" * 32 + "-default-builder.sh")
        nix.add("activation", drv("activation", sources=targets))
        return nix, source, value

    def test_owner_expected_paths_have_bound_nar_and_existing_provenance(self):
        nix, source, value = self.fixture()
        plan = cache.Planner(nix, source).plan()
        self.assertEqual(plan["gaps"], [])
        rows = {r["path"]: r for r in plan["objects"]}
        for target in value["sources"].values():
            row = rows[target]
            self.assertEqual(row["status"], "nativeavailable")
            self.assertEqual(row["provenance"], "verified-snapshot-or-locked-input")
            self.assertEqual(row["origin"]["expectedPath"], target)
            self.assertEqual(row["origin"]["narHash"], row["narHash"])
            self.assertEqual(row["origin"]["binding"], value["binding"])
            self.assertEqual(row["origin"]["lockedInput"], value["input"])
        expr = cache.source_expression(source)
        self.assertIn("pkgs.callPackage", expr)
        self.assertIn("package.cargoLock.lockFile", expr)
        self.assertIn("package.src", expr)
        self.assertNotIn("cleanSource", expr)

    def test_foreign_same_basename_stays_unknown_and_is_never_probed(self):
        nix, source, value = self.fixture(foreign=True)
        plan = cache.Planner(nix, source).plan()
        foreign = "/nix/store/" + "b" * 32 + "-default-builder.sh"
        self.assertIn({"kind": "unverified-source", "path": foreign}, plan["gaps"])
        self.assertFalse(any(v[0] == foreign for v in nix.visits))
        self.assertEqual(sum("origin" in r for r in plan["objects"]), 4)

    def test_snapshot_lock_and_locked_owner_drift_reject_all_proofs(self):
        for field in ("path", "narHash", "lockSha256", "inputNarHash", "inputPath"):
            with self.subTest(field=field):
                nix, source, value = self.fixture()
                if field in ("path", "narHash", "lockSha256"):
                    value["binding"][field] = "FOREIGN-BINDING"
                elif field == "inputNarHash":
                    value["input"]["narHash"] = "sha256-foreign="
                else:
                    value["input"]["path"] = path("foreign-nixpkgs")
                plan = cache.Planner(nix, source).plan()
                self.assertEqual(sum(g["kind"] == "unverified-source" for g in plan["gaps"]), 4)
                self.assertFalse(any("origin" in r for r in plan["objects"]))
                self.assertFalse(any(v[0] in value["sources"].values() for v in nix.visits))

    def test_nar_drift_after_expected_path_reconstruction_blocks_source(self):
        nix, source, value = self.fixture()
        original_info = nix.info
        counts = {}
        target = value["sources"]["tui"]
        def changing_info(path, **kwargs):
            result = original_info(path, **kwargs)
            counts[path] = counts.get(path, 0) + 1
            if path == target and counts[path] > 1:
                result["narHash"] = "sha256-foreign="
            return result
        nix.info = changing_info
        plan = cache.Planner(nix, source).plan()
        self.assertIn({"kind": "source-unavailable", "path": target}, plan["gaps"])
        self.assertFalse(any(r["path"] == target and r.get("origin") for r in plan["objects"]))


FAKE_TOOL = '''
import json, os, pathlib, sys, time
settings = json.loads(pathlib.Path(__file__).with_name('settings.json').read_text())
args = sys.argv[1:]
mode = settings.get('mode', 'json')
pathlib.Path(__file__).with_name('calls.json').write_text(json.dumps({'args': args, 'env': dict(os.environ), 'pid': os.getpid(), 'pgid': os.getpgrp()}))
if mode == 'flood':
    while True: os.write(2, b'x' * 8192)
if mode == 'timeout': time.sleep(5)
if mode == 'invalid': print('not json'); sys.exit(0)
if mode == 'missing':
    print(json.dumps({args[-1]: None}))
    print("error: path '" + args[-1] + "' is not valid", file=sys.stderr)
    sys.exit(1)
if mode == 'unavailable':
    print(json.dumps({args[-1]: None}))
    print('error: HTTP transport failure PRIVATE-MARKER', file=sys.stderr)
    sys.exit(1)
print(json.dumps(settings.get('value', {})))
'''


class ToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-cache-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tool = self.root / "nix"
        self.tool.write_text("#!" + sys.executable + "\n" + FAKE_TOOL)
        self.tool.chmod(0o700)
        self.settings = self.root / "settings.json"
        self.configure()
        self.work = self.root / "owned"
        self.work.mkdir()

    def configure(self, **settings):
        self.settings.write_text(json.dumps(settings))

    def nix(self, **budget):
        return cache.Nix(str(self.tool), self.work, cache.Budget(**budget))

    def test_clean_home_fixed_argv_no_builder_or_personal_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "PRIVATE-MARKER", "NIX_CONFIG": "builders = private", "HOME": "/private"}):
            nix = self.nix()
            nix.run(["eval", "--json", "--expr", "{}"])
        call = json.loads((self.root / "calls.json").read_text())
        self.assertNotIn("OPENAI_API_KEY", call["env"])
        self.assertNotIn("NIX_CONFIG", call["env"])
        self.assertEqual(call["env"]["HOME"], str(self.work / "home"))
        args = call["args"]
        self.assertIn("--offline", args)
        for key, value in (("builders", ""), ("max-jobs", "0"), ("allow-import-from-derivation", "false"),
                           ("substituters", ""), ("extra-substituters", ""), ("flake-registry", "")):
            self.assertEqual(args[args.index(key) + 1], value)
        self.assertNotIn("build", args)
        self.assertNotIn("--impure", args)

    def test_zero_budget_performs_no_remote_command(self):
        nix = self.nix()
        with self.assertRaises(cache.CacheError):
            nix.info(path("object"), public=True)
        self.assertEqual(nix.command_count, 0)
        self.assertEqual(nix.request_count, 0)
        self.assertFalse((self.root / "calls.json").exists())

    def test_exact_public_store_and_query_budget(self):
        target = path("object")
        self.configure(value={target: {**info("object", public=True)}})
        nix = self.nix(requests=1)
        self.assertEqual(nix.info(target, public=True)["fileSize"], 17)
        call = json.loads((self.root / "calls.json").read_text())
        self.assertEqual(call["args"][call["args"].index("--store") + 1], cache.PUBLIC_CACHE)
        self.assertNotIn("--offline", call["args"])
        with self.assertRaises(cache.CacheError):
            nix.info(target, public=True)
        self.assertEqual(nix.command_count, 1)

    def test_404_evidence_and_transport_error_are_distinct(self):
        nix = self.nix(requests=4)
        self.configure(mode="missing")
        self.assertEqual(nix.info(path("missing"), public=True)["status"], "missing")
        self.configure(mode="unavailable")
        with self.assertRaises(cache.CacheError) as caught:
            nix.info(path("unavailable"), public=True)
        self.assertNotIn("PRIVATE-MARKER", str(caught.exception))
        self.assertEqual(nix.command_count, 2)
        self.assertEqual(nix.request_count, 2)
        self.configure(mode="missing")
        self.assertEqual(nix.info(path("unavailable"), public=True)["status"], "missing")
        self.assertEqual(nix.command_count, 3)

    def test_stdout_stderr_caps_and_outer_timeout(self):
        self.configure(mode="flood")
        nix = self.nix(byte_cap=1024)
        with self.assertRaisesRegex(cache.CacheError, "byte cap"):
            nix.run(["eval", "--json", "--expr", "{}"])
        self.configure(mode="timeout")
        nix.budget = cache.Budget(timeout=0.1)
        with self.assertRaisesRegex(cache.CacheError, "timed out"):
            nix.run(["eval", "--json", "--expr", "{}"])
        self.configure(mode="invalid")
        with self.assertRaisesRegex(cache.CacheError, "invalid JSON"):
            nix.run(["eval", "--json", "--expr", "{}"])

    def test_bounded_reap_preserves_timeout_cap_and_cancellation(self):
        for mode in ("timeout", "cap", "interrupt", "unreapable", "success-unreapable"):
            with self.subTest(mode=mode):
                process = Mock(pid=123456, returncode=None)
                process.stdout.fileno.return_value = 10
                process.stderr.fileno.return_value = 11
                process.wait.side_effect = subprocess.TimeoutExpired("owned", 1)
                selector = Mock()
                selector.__enter__ = Mock(return_value=selector)
                selector.__exit__ = Mock(return_value=False)
                selector.get_map.return_value = {10: True}
                selector.select.return_value = [(SimpleNamespace(fileobj=process.stdout, data="out"), 1)]
                if mode == "interrupt":
                    selector.select.side_effect = KeyboardInterrupt
                if mode in {"unreapable", "success-unreapable"}:
                    selector.get_map.return_value = {}
                if mode == "success-unreapable":
                    process.returncode = 0
                    process.wait.side_effect = [0, subprocess.TimeoutExpired("owned", 1)]
                nix = self.nix(byte_cap=1024)
                # Each subcase gets an empty synthetic HOME.
                (self.work / "home").rmdir()
                with patch.object(cache.subprocess, "Popen", return_value=process), \
                        patch.object(cache.selectors, "DefaultSelector", return_value=selector), \
                        patch.object(cache.os, "set_blocking"), \
                        patch.object(cache.os, "read", return_value=b"x" * 1025), \
                        patch.object(cache.os, "killpg") as killpg, \
                        patch.object(cache.time, "monotonic", side_effect=[0, 31] if mode == "timeout" else None,
                                     return_value=0):
                    expected = KeyboardInterrupt if mode == "interrupt" else cache.CacheError
                    pattern = {"timeout": "timed out", "cap": "byte cap", "unreapable": "timed out",
                               "success-unreapable": "cleanup unconfirmed"}.get(mode)
                    if pattern:
                        with self.assertRaisesRegex(expected, pattern):
                            nix.run(["eval"], json_output=False)
                    else:
                        with self.assertRaises(expected):
                            nix.run(["eval"], json_output=False)
                    if process.returncode is None:
                        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
                    else:
                        killpg.assert_not_called()
                self.assertLessEqual(process.wait.call_count, 2)
                for call in process.wait.call_args_list:
                    self.assertGreater(call.kwargs["timeout"], 0)
                    self.assertLessEqual(call.kwargs["timeout"], nix.budget.timeout)
                process.stdout.close.assert_called_once()
                process.stderr.close.assert_called_once()

    def test_sigterm_cli_unwinds_owned_session_and_preserves_exit_status(self):
        self.configure(mode="timeout")
        command = [sys.executable, str(Path(cache.__file__)), "preflight", str(self.root / "unused"),
                   "--nix", str(self.tool)]
        # Use a child CLI with synthetic preflight so no snapshot or native Nix
        # is touched. The real Nix runner starts our sleeping fake executable.
        script = ("import sys; sys.path.insert(0, sys.argv.pop(1)); import cache; "
                  "cache.preflight = lambda root,nix: nix.run(['eval']); cache.main()")
        command = [sys.executable, "-c", script, str(Path(cache.__file__).parent), *command[2:]]
        process = subprocess.Popen(command, env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            calls = self.root / "calls.json"
            while not calls.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(calls.exists(), "Fake child did not start")
            call = json.loads(calls.read_text())
            self.assertEqual(call["pid"], call["pgid"])
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, (stdout, stderr))
            with self.assertRaises(ProcessLookupError):
                os.kill(call["pid"], 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=2)

    def public_rows(self, prefix="object"):
        return {path(f"{prefix}-{i}"): info(f"{prefix}-{i}",
                    [path(f"{prefix}-{i}"), path(f"{prefix}-{(i + 1) % 7}")], public=True)
                for i in range(7)}

    def public_queries(self, nix, rows, recursive):
        native_run = nix.run
        def run(args, **kwargs):
            self.configure(**(recursive if "--recursive" in args else {"value": {args[-1]: rows[args[-1]]}}))
            return native_run(args, **kwargs)
        return patch.object(nix, "run", side_effect=run)

    def test_recursive_public_closure_reuses_only_reachable_metadata_in_all_shapes(self):
        nix = self.nix(requests=9)
        for shape in ("map", "legacy", "versioned"):
            with self.subTest(shape=shape):
                rows = self.public_rows(shape)
                root = path(f"{shape}-0")
                extra = path(f"{shape}-unrelated")
                payload = {**rows, extra: info(f"{shape}-unrelated", public=True)}
                if shape == "legacy":
                    payload = list(payload.values())
                elif shape == "versioned":
                    payload = {"version": 2, "info": {Path(k).name: {**v,
                        "references": [Path(ref).name for ref in v["references"]]}
                        for k, v in payload.items()}}
                before = nix.command_count
                with self.public_queries(nix, rows, {"value": payload}) as query:
                    planner = cache.Planner(nix, SOURCE)
                    self.assertEqual(planner.cached(root), "cached")
                    self.assertEqual(set(planner.records), set(rows))
                    self.assertEqual(nix.command_count - before, 2)
                    for target in rows:
                        self.assertEqual(nix.info(target, public=True), cache.metadata(rows, target, public=True)
                                         | {"status": "cached"})
                    self.assertEqual(query.call_count, 2)
                    self.assertNotIn("--recursive", query.call_args_list[0].args[0])
                    self.assertEqual(query.call_args_list[1].args[0],
                        ["path-info", "--json", "--store", cache.PUBLIC_CACHE, "--recursive", root])
                self.configure(mode="unavailable")
                with self.assertRaises(cache.CommandUnavailable):
                    nix.info(extra, public=True)
                self.assertEqual(nix.command_count - before, 3)
        self.assertEqual(nix.request_count, 9)

    def test_recursive_failure_never_publishes_partial_metadata(self):
        nix = self.nix(requests=100)
        for failure in ("incomplete", "null", "references", "path", "hash", "size", "fileSize",
                        "shape", "changed-root", "changed-refs", "duplicate"):
            with self.subTest(failure=failure):
                rows = self.public_rows(failure)
                root, child = list(rows)[:2]
                payload = copy.deepcopy(rows)
                if failure == "incomplete":
                    del payload[child]
                elif failure == "null":
                    payload[child] = None
                elif failure == "references":
                    payload[child]["references"] = None
                elif failure == "path":
                    payload[child]["references"] = ["/private/not-a-store-path"]
                elif failure == "hash":
                    payload[child]["narHash"] = None
                elif failure == "size":
                    payload[child]["narSize"] = True
                elif failure == "fileSize":
                    del payload[child]["fileSize"]
                elif failure == "shape":
                    payload = {"info": []}
                elif failure == "changed-root":
                    payload[root]["narSize"] += 1
                elif failure == "changed-refs":
                    payload[root]["references"] = []
                else:
                    payload = [*payload.values(), payload[child]]
                before = nix.command_count
                with self.public_queries(nix, rows, {"value": payload}):
                    self.assertEqual(nix.info(root, public=True), rows[root])
                self.assertEqual(nix.command_count - before, 2)
                # Even a valid row visited before a later failure grants no trust.
                self.configure(mode="unavailable")
                for target in rows:
                    with self.assertRaises(cache.CommandUnavailable):
                        nix.info(target, public=True)
                self.assertEqual(nix.command_count - before, 9)

    def test_recursive_node_limit_preserves_existing_planner_guard(self):
        rows = self.public_rows()
        root = next(iter(rows))
        nix = self.nix(requests=20, nodes=6)
        with self.public_queries(nix, rows, {"value": rows}):
            self.assertEqual(nix.info(root, public=True), rows[root])
        self.configure(mode="unavailable")
        for target in rows:
            with self.assertRaises(cache.CommandUnavailable):
                nix.info(target, public=True)
        with self.public_queries(nix, rows, {"value": rows}):
            with self.assertRaisesRegex(cache.CacheError, "Graph node budget"):
                cache.Planner(nix, SOURCE).cached(root)
        self.assertLessEqual(nix.request_count, 20)

    def test_recursive_memo_union_and_conflicting_prior_rows_are_rejected(self):
        rows = self.public_rows("prior")
        root = next(iter(rows))
        nix = self.nix(requests=10, nodes=7)
        with self.public_queries(nix, rows, {"value": rows}):
            nix.info(root, public=True)
        for case in ("union", "conflict"):
            with self.subTest(case=case):
                new = self.public_rows(case)
                target = next(iter(new))
                payload = copy.deepcopy(new)
                if case == "conflict":
                    nix.budget = cache.Budget(requests=10, nodes=20)
                    # A two-object closure fits by itself, but conflicts with a
                    # previously validated row without changing this new root.
                    child = list(new)[1]
                    new = {target: info(f"{case}-0", [child, root], public=True),
                           child: info(f"{case}-1", public=True)}
                    payload = {**new, root: {**rows[root], "references": []}}
                before = nix.command_count
                with self.public_queries(nix, new, {"value": payload}):
                    self.assertEqual(nix.info(target, public=True), new[target])
                self.assertEqual(nix.command_count - before, 2)
                self.assertEqual(nix.info(root, public=True), rows[root])
                self.configure(mode="unavailable")
                with self.assertRaises(cache.CommandUnavailable):
                    nix.info(target, public=True)
                self.assertEqual(nix.command_count - before, 3)

    def test_recursive_memo_never_satisfies_native_or_destination_reverification(self):
        rows = self.public_rows()
        root = next(iter(rows))
        nix = self.nix(requests=2)
        with self.public_queries(nix, rows, {"value": rows}):
            result = nix.info(root, public=True)
        result["references"].clear()
        self.assertEqual(nix.info(root, public=True), rows[root])
        changed = {**rows[root], "narSize": 999, "references": []}
        self.configure(value={root: changed})
        self.assertEqual(nix.info(root)["narSize"], 999)
        directory_fd = os.open(self.work, os.O_RDONLY)
        try:
            destination = "file:///dev/fd/" + str(directory_fd)
            self.assertEqual(nix.info(root, destination=destination,
                                     pass_fds=(directory_fd,))["narSize"], 999)
        finally:
            os.close(directory_fd)
        call = json.loads((self.root / "calls.json").read_text())
        self.assertIn("--offline", call["args"])
        self.assertIn(destination, call["args"])
        self.assertNotIn("--recursive", call["args"])
        self.assertEqual(nix.request_count, 2)
        self.assertEqual(nix.command_count, 4)

    def test_remaining_single_request_skips_recursive_and_keeps_fallback_bounded(self):
        rows = self.public_rows()
        root, child = list(rows)[:2]
        nix = self.nix(requests=1)
        with self.public_queries(nix, rows, {"value": rows}) as query:
            self.assertEqual(nix.info(root, public=True), rows[root])
            self.assertEqual(query.call_count, 1)
            self.assertNotIn("--recursive", query.call_args.args[0])
            with self.assertRaisesRegex(cache.CacheError, "request budget"):
                nix.info(child, public=True)
        self.assertEqual(nix.command_count, 1)
        self.assertEqual(nix.request_count, 1)

    def test_recursive_skipped_when_references_are_self_or_already_memoized(self):
        rows = self.public_rows()
        root = next(iter(rows))
        nix = self.nix(requests=4)
        with self.public_queries(nix, rows, {"value": rows}):
            nix.info(root, public=True)
        for name, refs in (("self", [path("self")]), ("known", [root])):
            row = info(name, refs, public=True)
            self.configure(value={path(name): row})
            self.assertEqual(nix.info(path(name), public=True), row)
            self.assertNotIn("--recursive", json.loads((self.root / "calls.json").read_text())["args"])
        self.assertEqual(nix.request_count, 4)

    def test_recursive_runner_caps_timeout_and_cancellation_preserve_bounds(self):
        nix = self.nix(requests=20, byte_cap=4096, timeout=1)
        for mode in ("flood", "timeout", "invalid", "unavailable"):
            with self.subTest(mode=mode):
                rows = self.public_rows(mode)
                root, child = list(rows)[:2]
                before = nix.command_count
                with self.public_queries(nix, rows, {"mode": mode}):
                    self.assertEqual(nix.info(root, public=True), rows[root])
                self.assertEqual(nix.command_count - before, 2)
                self.configure(mode="unavailable")
                with self.assertRaises(cache.CommandUnavailable):
                    nix.info(child, public=True)
                self.assertEqual(nix.command_count - before, 3)
        rows = self.public_rows("cancel")
        root, child = list(rows)[:2]
        with patch.object(nix, "run", side_effect=[{root: rows[root]}, cache.CommandCancelled(signal.SIGTERM)]):
            with self.assertRaises(cache.CommandCancelled):
                nix.info(root, public=True)
        self.configure(mode="unavailable")
        with self.assertRaises(cache.CommandUnavailable):
            nix.info(child, public=True)

    def test_derivation_json_wrapper(self):
        target = path("object.drv")
        row = drv("object", modern=True)
        self.configure(value={"version": 4, "derivations": {Path(target).name: row}})
        self.assertEqual(self.nix().derivation(target), row)


class FreezeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-cache-freeze-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "synthetic-source"
        self.source.mkdir()
        self.data = b'{"nodes":{"root":{}}}'
        (self.source / "flake.lock").write_bytes(self.data)
        self.manifest = {"schemaVersion": 1,
                         "files": [cache.snapshot._entry("flake.lock", self.data, 0o644, "working-tree")],
                         "lockSha256": cache.hashlib.sha256(self.data).hexdigest(),
                         "fixture": {"selectionStage": "minimal"}}
        (self.source / cache.snapshot.MANIFEST_NAME).write_text(json.dumps(self.manifest))
        self.nix = FakeNix()
        self.nix.work = self.root / "transport"
        self.nix.work.mkdir()
        self.frozen = self.root / "frozen"
        self.frozen.mkdir()
        self.commands = []
        self.attack = False
        original_io = cache.snapshot._SnapshotIO
        def pinned_io(root):
            return original_io(self.frozen if str(root) == SOURCE["path"] else root)
        patcher = patch.object(cache.snapshot, "_SnapshotIO", side_effect=pinned_io)
        patcher.start()
        self.addCleanup(patcher.stop)

        def run(args, **kwargs):
            self.commands.append(args)
            self.assertEqual(args[:3], ["store", "prefetch-file", "--unpack"])
            fd, = kwargs["pass_fds"]
            self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
            self.assertEqual(args[-1], "file:///dev/fd/" + str(fd))
            self.assertEqual(list(self.nix.work.iterdir()), [])
            if self.attack:
                canary = self.root / "foreign"
                canary.mkdir()
                (canary / "private-canary").write_text("SYNTHETIC-PRIVATE-DO-NOT-COPY")
                self.nix.work.rename(self.root / "original-transport")
                self.nix.work.symlink_to(canary, target_is_directory=True)
            with os.fdopen(os.dup(fd), "rb") as stream:
                with tarfile.open(fileobj=stream, mode="r:") as archive:
                    members = archive.getmembers()
                    self.assertEqual({m.name for m in members},
                                     {"source/flake.lock", "source/" + cache.snapshot.MANIFEST_NAME})
                    for member in members:
                        target = self.frozen / member.name.removeprefix("source/")
                        if member.isdir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            self.assertTrue(member.isfile())
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.extractfile(member).read())
                            target.chmod(0o555 if member.mode & 0o111 else 0o444)
            return {"storePath": SOURCE["path"], "hash": SOURCE["narHash"]}
        self.nix.run = run

    def test_freeze_only_approved_regular_fd_bytes(self):
        result = cache.freeze_source(self.source, self.nix)
        self.assertEqual(result["lockSha256"], self.manifest["lockSha256"])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(result["manifest"], self.manifest)
        self.assertEqual((self.frozen / "flake.lock").read_bytes(), self.data)

    def test_before_nativecall_parent_retarget_cannot_read_or_copy_canary(self):
        self.attack = True
        result = cache.freeze_source(self.source, self.nix)
        self.assertEqual(result["manifest"], self.manifest)
        self.assertFalse((self.frozen / "private-canary").exists())
        self.assertEqual({p.name for p in self.frozen.iterdir()}, {"flake.lock", cache.snapshot.MANIFEST_NAME})

    def test_added_private_namespace_fails_before_nix_or_private_file_read(self):
        (self.source / "secrets").mkdir()
        (self.source / "secrets/private").write_text("SYNTHETIC-PRIVATE-MARKER")
        reads = []
        original = cache.snapshot._read_source
        def spy(fd, name, **kwargs):
            reads.append(name)
            return original(fd, name, **kwargs)
        with patch.object(cache.snapshot, "_read_source", side_effect=spy):
            with self.assertRaises(cache.snapshot.SnapshotError):
                cache.freeze_source(self.source, self.nix)
        self.assertEqual(self.commands, [])
        self.assertFalse(any(name.startswith("secrets/") for name in reads))

    def test_modified_allowed_bytes_and_symlink_are_rejected_before_nix(self):
        (self.source / "flake.lock").write_text("changed-public-bytes")
        with self.assertRaises(cache.snapshot.SnapshotError):
            cache.freeze_source(self.source, self.nix)
        (self.source / "flake.lock").unlink()
        (self.source / "flake.lock").symlink_to(self.root / "must-not-open")
        with self.assertRaises(cache.snapshot.SnapshotError):
            cache.freeze_source(self.source, self.nix)
        self.assertEqual(self.commands, [])


class ExportGraphTests(unittest.TestCase):
    def test_missing_parent_drv_with_cached_child_plans_outputs_and_sources_only(self):
        nix = complete_fake()
        nix.add("activation", drv("activation", {path("cached-child.drv"): ["out"]}, [SOURCE["path"]]))
        nix.add("cached-child", drv("cached-child", {path("must-not-visit.drv"): ["out"]},
                                   [path("unverified-unused-source")]), cached=True)
        self.assertEqual(nix.native[path("activation.drv")]["references"],
                         [path("cached-child.drv"), SOURCE["path"]])
        self.assertTrue(nix.native[path("cached-child.drv")]["references"])
        plan = cache.Planner(nix, SOURCE).plan()
        self.assertEqual(plan["gaps"], [])
        objects = {row["path"] for row in plan["objects"]
                   if row["status"] in {"cached", "nativeavailable"}}
        self.assertEqual(objects, {SOURCE["path"], path("setup"), path("cached-child")})
        self.assertFalse(any(value.endswith(".drv") for value in objects))
        self.assertNotIn(["derivation", "show", path("must-not-visit.drv")], nix.commands)
        self.assertFalse(any(visit[0].endswith(".drv") for visit in nix.visits))
        self.assertFalse(any(visit[0] == path("unverified-unused-source") for visit in nix.visits))

    def test_removed_directory_backend_rejects_before_any_source_or_filesystem_work(self):
        with patch.object(sys, "argv", ["cache.py", "prepare", "/synthetic-source", "--nix", "/synthetic-nix"]), \
                patch.object(cache, "preflight") as preflight, \
                patch.object(cache.tempfile, "TemporaryDirectory") as temporary, \
                patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                cache.main()
        self.assertEqual(failure.exception.code, 2)
        preflight.assert_not_called()
        temporary.assert_not_called()


if __name__ == "__main__":
    unittest.main()

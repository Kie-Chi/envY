"""Runtime snapshot boundaries, using only disposable synthetic repositories."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


_TEST_ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
_SOURCE = _TEST_ROOT / "checks/runtime/snapshot.py"
_SPEC = importlib.util.spec_from_file_location("runtime_snapshot", _SOURCE)
snapshot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(snapshot)


class RuntimeSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-snapshot-test-")
        self.addCleanup(temporary.cleanup)
        self.temp = temporary.name
        self.base = Path(self.temp)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.output = self.base / "export"
        self.git_home = self.base / "git-home"
        self.git_home.mkdir()
        self.git_env = {
            "PATH": os.defpath, "HOME": str(self.git_home), "XDG_CONFIG_HOME": str(self.git_home),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "LC_ALL": "C",
        }
        self.git("init", "--quiet")
        for name in snapshot.ROOT_SOURCES:
            self.source(name, b"synthetic source\n")
        self.source("flake.lock", b'{"nodes": {"synthetic": {}}}\n')
        self.source("checks/runtime/fixture.nix", b"{ exclusions ? {} }: { ... }: {}\n")
        self.source("hosts/default.nix", b"{ ... }: { # Empty shared defaults\n}\n")
        self.source("modules/example.nix", b"initial indexed source\n")
        self.source("resources/scripts/example", b"#!/bin/sh\nexit 0\n", mode=0o755)
        self.git("add", "--all")

    def git(self, *args):
        result = subprocess.run([shutil.which("git"), "-c", "core.hooksPath=" + os.devnull, *args], cwd=self.repo, env=self.git_env, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, "Synthetic Git command failed")
        return result.stdout

    def source(self, name, data=b"synthetic-private-canary\n", mode=0o644):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        return target

    def export(self):
        return snapshot.export_snapshot(self.repo, self.output)

    def evaluated(self):
        groups = {}
        for name, option in snapshot.GROUP_OPTIONS.items():
            ids = sorted(snapshot.REQUIRED_NIX_IDS | {"fzf", "codegraph", "vscode"}) if name == "nix.user.package" else ["synthetic-tool"]
            groups[name] = {"optionPath": option, "selection": {
                "include": [{"id": item, "ref": "local:synthetic/" + item} for item in ids],
                "effective": [{"id": item, "ref": "local:synthetic/" + item} for item in ids], "exclude": [],
            }}
        return {"schemaVersion": 3, "id": "runtime-min", "platform": "linux", "settings": {
            "envy.user.name": "envy-runtime", "envy.user.home": "/home/envy-runtime",
            "envy.repository.path": "/home/envy-runtime/.envy", "envy.linux.option": "server",
            "envy.linux.desktop": "none", "envy.mirrors.mode": "upstream", "envy.storage.mode": "manual",
            "envy.storage.archive.repository": "",
        }, "software": {"groups": groups}}

    def test_current_dirty_and_intent_to_add_bytes_without_mutating_parent_index(self):
        self.source("modules/example.nix", b"dirty current source\n")
        self.source("checks/intent.nix", b"intent-to-add source\n")
        self.git("add", "-N", "checks/intent.nix")
        self.source("resources/scripts/tests/test_neovim_terminal.py", b"untracked canary\n")
        index_before = (self.repo / ".git/index").read_bytes()
        manifest = self.export()
        self.assertEqual((self.output / "modules/example.nix").read_bytes(), b"dirty current source\n")
        self.assertEqual((self.output / "checks/intent.nix").read_bytes(), b"intent-to-add source\n")
        self.assertFalse((self.output / "resources/scripts/tests/test_neovim_terminal.py").exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)
        self.assertEqual(manifest, snapshot.verify_snapshot(self.output))
        self.assertFalse((self.output / ".git").exists())

    def test_sensitive_candidates_never_reach_stat_or_source_reader(self):
        private = [".pi/session", ".device-label", ".sops.yaml", "secrets/secrets.yaml", "secrets/README", ".nas_cred", "hosts/linux/personal.nix", "hosts/darwin/personal.nix", "modules/secrets/private", "resources/.nas_cred"]
        for name in private:
            self.source(name)
        self.git("add", "--all")
        original_read = snapshot._read_source
        original_stat = os.stat

        def poison_read(fd, name, **kwargs):
            self.assertNotIn(name, private)
            return original_read(fd, name, **kwargs)

        private_paths = {self.repo / name for name in private}

        def poison_stat(path, *args, **kwargs):
            if isinstance(path, (str, Path)):
                self.assertNotIn(Path(path), private_paths)
            return original_stat(path, *args, **kwargs)

        with patch.object(snapshot, "_read_source", side_effect=poison_read), patch.object(snapshot.os, "stat", side_effect=poison_stat):
            manifest = self.export()
        serialized = json.dumps(manifest)
        self.assertNotIn("synthetic-private-canary", serialized)
        self.assertNotIn("personal.nix", serialized)
        self.assertNotIn("private", " ".join(entry["relativePath"] for entry in manifest["files"]))
        for name in private:
            self.assertFalse((self.output / name).exists())
        self.assertEqual(manifest["excludedCategories"]["machine-hosts"], 2)
        self.assertEqual(manifest["excludedCategories"]["private-namespace"], 8)

    def test_cache_backup_result_and_metadata_namespaces_are_excluded(self):
        excluded = ["resources/cache/entry", "resources/envy-tui/target/debug/binary", "modules/result", "checks/result-vm/link", "files/node_modules/x", "files/backup/x", "files/draft.bak", "docs/source.md", "README.md", ".github/workflows/test.yml"]
        for name in excluded:
            self.source(name)
        self.git("add", "--all")
        manifest = self.export()
        names = {entry["relativePath"] for entry in manifest["files"]}
        self.assertFalse(names.intersection(excluded))
        self.assertEqual(manifest["excludedCategories"]["cache-or-backup"], 7)
        self.assertEqual(manifest["excludedCategories"]["non-source-metadata"], 3)

    def test_unknown_new_namespace_fails_without_fallback_or_output(self):
        self.source("new-source/dependency.nix")
        self.git("add", "--all")
        with self.assertRaises(snapshot.SnapshotError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_output_must_be_new_and_outside_checkout(self):
        for target in (self.repo / "export", self.repo, self.base):
            with self.subTest(target=target), self.assertRaises(snapshot.SnapshotError):
                snapshot.export_snapshot(self.repo, target)
        self.output.mkdir()
        (self.output / "keep").write_bytes(b"existing")
        with self.assertRaises(snapshot.SnapshotError):
            self.export()
        self.assertEqual((self.output / "keep").read_bytes(), b"existing")

    def test_existing_destination_symlink_is_not_followed(self):
        self.output.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(snapshot.SnapshotError):
            self.export()
        self.assertTrue(self.output.is_symlink())

    def test_git_environment_is_isolated_from_host_variables_and_global_hooks(self):
        original_run = subprocess.run
        calls = []
        poison = {"GIT_DIR": "/private/synthetic", "GIT_WORK_TREE": "/private/synthetic", "GIT_INDEX_FILE": "/private/index", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false", "GIT_TRACE": "1", "SYNTHETIC_SECRET": "env-canary"}

        def capture(*args, **kwargs):
            calls.append((args, kwargs))
            return original_run(*args, **kwargs)

        with patch.dict(os.environ, poison), patch.object(snapshot.subprocess, "run", side_effect=capture):
            manifest = self.export()
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        for key in poison:
            self.assertNotIn(key, kwargs["env"])
        self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertIn("core.hooksPath=" + os.devnull, args[0])
        self.assertNotIn("env-canary", json.dumps(manifest))

    def test_git_failure_does_not_echo_private_diagnostics(self):
        result = subprocess.CompletedProcess([], 7, b"", b"synthetic-secret-canary")
        with patch.object(snapshot.subprocess, "run", return_value=result), self.assertRaises(snapshot.SnapshotError) as raised:
            self.export()
        self.assertNotIn("canary", str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_staged_symlink_is_rejected_without_reading_its_target(self):
        outside = self.base / "personal"
        outside.write_bytes(b"synthetic-outside-canary")
        self.source("modules/link").unlink()
        (self.repo / "modules/link").symlink_to(outside)
        self.git("add", "--all")
        with patch.object(snapshot, "_read_source", side_effect=AssertionError("Must reject before reading")), self.assertRaises(snapshot.SnapshotError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_dirty_symlink_replacing_regular_file_is_rejected(self):
        target = self.repo / "modules/example.nix"
        target.unlink()
        target.symlink_to(self.base / "personal")
        with self.assertRaises(snapshot.SnapshotError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_symlink_directory_replacement_is_rejected(self):
        original = self.repo / "modules"
        moved = self.base / "private"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(snapshot.SnapshotError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_source_replacement_during_read_is_rejected(self):
        target = self.repo / "modules/example.nix"
        root_fd = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)
        real_read = os.read
        replaced = False

        def replacing_read(fd, count):
            nonlocal replaced
            content = real_read(fd, count)
            if not replaced:
                replaced = True
                target.unlink()
                target.write_bytes(b"replacement canary")
            return content

        with patch.object(snapshot.os, "read", side_effect=replacing_read), self.assertRaises(snapshot.SnapshotError):
            snapshot._read_source(root_fd, "modules/example.nix")

    def test_deleted_tracked_source_stays_deleted(self):
        (self.repo / "modules/example.nix").unlink()
        manifest = self.export()
        self.assertFalse((self.output / "modules/example.nix").exists())
        self.assertEqual(manifest["excludedCategories"]["deleted-source"], 1)

    def test_missing_required_root_fails_and_removes_partial_export(self):
        (self.repo / "home.nix").unlink()
        with self.assertRaises(snapshot.SnapshotError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_shared_defaults_must_remain_empty(self):
        self.source("hosts/default.nix", b'{ ... }: { envy.user.name = "private-canary"; }')
        with self.assertRaises(snapshot.SnapshotError) as raised:
            self.export()
        self.assertNotIn("private-canary", str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_manifest_inventory_hashes_modes_and_lock_are_complete(self):
        manifest = self.export()
        files = {p.relative_to(self.output).as_posix() for p in self.output.rglob("*") if p.is_file()}
        self.assertEqual(files, {entry["relativePath"] for entry in manifest["files"]} | {snapshot.MANIFEST_NAME})
        for entry in manifest["files"]:
            target = self.output / entry["relativePath"]
            self.assertEqual(entry["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(entry["mode"], f"{stat.S_IMODE(target.stat().st_mode):04o}")
        self.assertEqual(manifest["lockSha256"], hashlib.sha256((self.output / "flake.lock").read_bytes()).hexdigest())
        self.assertEqual((self.output / snapshot.MANIFEST_NAME).read_bytes(), (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        self.assertEqual((self.output / "resources/scripts/example").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.output / "hosts/darwin").is_dir(), True)
        self.assertFalse((self.output / "secrets").exists())
        self.assertNotIn(str(self.repo), json.dumps(manifest))

    def test_verification_rejects_tamper_and_private_injection(self):
        self.export()
        for name in (".git/history", "secrets/secrets.yaml", "hosts/linux/personal.nix", "modules/.pi/session", "resources/cache/blob", "unexpected/file"):
            with self.subTest(name=name):
                target = self.output / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"synthetic-canary")
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot.verify_snapshot(self.output)
                target.unlink()
                # Remove only directories this test added.
                parent = target.parent
                while parent != self.output and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        (self.output / "modules/example.nix").write_bytes(b"tampered")
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.verify_snapshot(self.output)

    def test_verification_rejects_manifest_symlink_before_reading_target(self):
        self.export()
        target = self.output / snapshot.MANIFEST_NAME
        target.unlink()
        outside = self.base / "private-manifest"
        outside.write_bytes(b"synthetic-secret-canary")
        target.symlink_to(outside)
        with patch.object(snapshot.os, "read", side_effect=AssertionError("Must not read linked manifest")), self.assertRaises(snapshot.SnapshotError):
            snapshot.verify_snapshot(self.output)

    def test_verification_rejects_injected_symlink(self):
        self.export()
        (self.output / "modules/link").symlink_to(self.repo)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.verify_snapshot(self.output)

    def test_two_stage_selection_only_changes_minimal_host_and_manifest(self):
        initial = self.export()
        before = {entry["relativePath"]: (self.output / entry["relativePath"]).read_bytes() for entry in initial["files"]}
        result = snapshot.apply_minimal_selection(self.output, self.evaluated())
        self.assertEqual(result["fixture"]["selectionStage"], "minimal")
        self.assertEqual(result["lockSha256"], initial["lockSha256"])
        for name, data in before.items():
            if name != snapshot.MIN_HOST:
                self.assertEqual((self.output / name).read_bytes(), data)
        selected = (self.output / snapshot.MIN_HOST).read_text()
        self.assertIn('"nix.user.package" = [ "codegraph" "fzf" "vscode" ];', selected)
        self.assertNotIn('"envy"', selected)
        self.assertNotIn("effective", selected)
        self.assertEqual((self.repo / "modules/example.nix").read_bytes(), b"initial indexed source\n")
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.apply_minimal_selection(self.output, self.evaluated())

    def test_export_rejects_generated_parent_alias_without_external_writes(self):
        for use_repo in (True, False):
            with self.subTest(use_repo=use_repo):
                self.output = self.base / ("export-repo" if use_repo else "export-outside")
                outside = self.repo if use_repo else self.base / "outside"
                outside.mkdir(exist_ok=True)
                before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
                real_host_text = snapshot._host_text
                attacked = False

                def replace_parent(*args):
                    nonlocal attacked
                    if not attacked:
                        attacked = True
                        parent = self.output / "hosts/linux"
                        parent.rmdir()
                        parent.symlink_to(outside, target_is_directory=True)
                    return real_host_text(*args)

                with patch.object(snapshot, "_host_text", side_effect=replace_parent):
                    with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                        self.export()
                self.assertTrue(attacked)
                self.assertTrue((self.output / "hosts/linux").is_symlink())
                self.assertEqual(before, {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()})
                shutil.rmtree(self.output)

    def test_selection_rejects_file_and_parent_swaps_without_canary_io(self):
        for phase in ("verification-read", "verified", "before-open", "compared", "before-replace"):
            for alias in ("file", "same-content-file", "parent", "root", "manifest"):
                with self.subTest(phase=phase, alias=alias):
                    self.output = self.base / ("export-" + phase + "-" + alias)
                    displaced = self.base / ("displaced-" + phase + "-" + alias)
                    initial = self.export()
                    outside = self.base / ("outside-" + phase + "-" + alias)
                    outside.mkdir(exist_ok=True)
                    canary = outside / (snapshot.MIN_HOST if alias == "root" else "runtime-min.nix")
                    canary.parent.mkdir(parents=True, exist_ok=True)
                    original = snapshot._host_text()
                    canary.write_bytes(original)
                    manifest_canary = outside / snapshot.MANIFEST_NAME
                    manifest_bytes = (self.output / snapshot.MANIFEST_NAME).read_bytes()
                    manifest_canary.write_bytes(manifest_bytes)
                    protected = {canary.stat().st_ino, manifest_canary.stat().st_ino}
                    reads = []
                    writes = []
                    attacked = False
                    real_read = snapshot._read_source
                    real_verify = snapshot._verify_snapshot
                    real_check = snapshot._SnapshotIO.check
                    real_host_text = snapshot._host_text
                    real_os_read = os.read
                    real_open = Path.open
                    real_fdopen = os.fdopen
                    host_reads = 0

                    def attack():
                        nonlocal attacked
                        if attacked:
                            return
                        attacked = True
                        if alias in {"file", "same-content-file"}:
                            target = self.output / snapshot.MIN_HOST
                            target.rename(target.with_suffix(".saved"))
                            if alias == "file":
                                target.symlink_to(canary)
                            else:
                                target.write_bytes(original)
                        elif alias == "manifest":
                            target = self.output / snapshot.MANIFEST_NAME
                            target.rename(self.output / "manifest.saved")
                            target.symlink_to(manifest_canary)
                        else:
                            target = self.output / "hosts/linux" if alias == "parent" else self.output
                            target.rename(displaced)
                            target.symlink_to(outside, target_is_directory=True)

                    def after_read(fd, name, **kwargs):
                        nonlocal host_reads
                        if name == snapshot.MIN_HOST:
                            host_reads += 1
                            if phase == "before-open" and host_reads == 2:
                                attack()
                        result = real_read(fd, name, **kwargs)
                        if phase == "verification-read" and name == initial["files"][-1]["relativePath"]:
                            attack()
                        return result

                    def after_verify(source):
                        result = real_verify(source)
                        if phase == "verified":
                            attack()
                        return result

                    def before_replace(source):
                        result = real_check(source)
                        if phase == "before-replace" and list((self.output / "hosts/linux").glob(".runtime-min.nix.*.tmp")):
                            attack()
                        return result

                    def after_compare(*args):
                        result = real_host_text(*args)
                        if phase == "compared" and not args:
                            attack()
                        return result

                    def observe_read(fd, count):
                        if os.fstat(fd).st_ino in protected:
                            reads.append(fd)
                        return real_os_read(fd, count)

                    def observe_fdopen(fd, mode="r", *args, **kwargs):
                        if os.fstat(fd).st_ino in protected:
                            (writes if any(flag in mode for flag in "wax+") else reads).append(fd)
                        return real_fdopen(fd, mode, *args, **kwargs)

                    def observe_open(path, mode="r", *args, **kwargs):
                        stream = real_open(path, mode, *args, **kwargs)
                        if os.fstat(stream.fileno()).st_ino in protected:
                            (writes if any(flag in mode for flag in "wax+") else reads).append(str(path))
                        return stream

                    with patch.object(snapshot, "_read_source", side_effect=after_read), patch.object(snapshot, "_verify_snapshot", side_effect=after_verify), patch.object(snapshot._SnapshotIO, "check", new=before_replace), patch.object(snapshot, "_host_text", side_effect=after_compare), patch.object(snapshot.os, "read", side_effect=observe_read), patch.object(snapshot.os, "fdopen", side_effect=observe_fdopen), patch.object(Path, "open", new=observe_open):
                        with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                            snapshot.apply_minimal_selection(self.output, self.evaluated())
                    self.assertTrue(attacked)
                    self.assertEqual(reads, [])
                    self.assertEqual(writes, [])
                    self.assertEqual(canary.read_bytes(), original)
                    self.assertEqual(manifest_canary.read_bytes(), manifest_bytes)
                    if self.output.is_symlink():
                        self.output.unlink()
                    else:
                        shutil.rmtree(self.output)
                    if displaced.exists():
                        shutil.rmtree(displaced)
                    shutil.rmtree(outside)

    def test_export_manifest_alias_is_exclusive_and_preserved_on_conflict(self):
        canary = self.base / "manifest-canary"
        canary.write_bytes(b"synthetic external manifest")
        canary.chmod(0o400)
        before = canary.stat()
        real_write_manifest = snapshot._write_manifest

        def inject_alias(root, manifest):
            (self.output / snapshot.MANIFEST_NAME).symlink_to(canary)
            return real_write_manifest(root, manifest)

        with patch.object(snapshot, "_write_manifest", side_effect=inject_alias):
            with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                self.export()
        self.assertTrue((self.output / snapshot.MANIFEST_NAME).is_symlink())
        self.assertEqual(canary.read_bytes(), b"synthetic external manifest")
        self.assertEqual(canary.stat().st_mode, before.st_mode)

    def test_export_fchmod_uses_created_fd_after_file_alias_swap(self):
        canary = self.base / "host-canary"
        canary.write_bytes(b"synthetic external host")
        canary.chmod(0o400)
        before = canary.stat()
        real_fchmod = os.fchmod
        attacked = False

        def swap_before_chmod(fd, mode):
            nonlocal attacked
            target = self.output / snapshot.MIN_HOST
            if not attacked and target.exists() and os.fstat(fd).st_ino == target.stat().st_ino:
                attacked = True
                target.rename(target.with_suffix(".saved"))
                target.symlink_to(canary)
            return real_fchmod(fd, mode)

        with patch.object(snapshot.os, "fchmod", side_effect=swap_before_chmod):
            with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                self.export()
        self.assertTrue(attacked)
        self.assertTrue((self.output / snapshot.MIN_HOST).is_symlink())
        self.assertEqual(canary.read_bytes(), b"synthetic external host")
        self.assertEqual(canary.stat().st_mode, before.st_mode)

    def test_selection_temp_creation_is_exclusive_and_keeps_collision(self):
        self.export()
        canary = self.base / "temp-canary"
        canary.write_bytes(b"synthetic temporary canary")
        manifest_before = (self.output / snapshot.MANIFEST_NAME).read_bytes()
        host_before = (self.output / snapshot.MIN_HOST).read_bytes()
        temporary = self.output / "hosts/linux" / (".runtime-min.nix." + "00" * 16 + ".tmp")
        real_host_text = snapshot._host_text

        def collide(*args):
            if not args and not temporary.is_symlink():
                temporary.symlink_to(canary)
            return real_host_text(*args)

        with patch.object(snapshot.os, "urandom", return_value=bytes(16)), patch.object(snapshot, "_host_text", side_effect=collide):
            with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                snapshot.apply_minimal_selection(self.output, self.evaluated())
        self.assertTrue(temporary.is_symlink())
        self.assertEqual(canary.read_bytes(), b"synthetic temporary canary")
        self.assertEqual((self.output / snapshot.MIN_HOST).read_bytes(), host_before)
        self.assertEqual((self.output / snapshot.MANIFEST_NAME).read_bytes(), manifest_before)

    def test_selection_rechecks_staged_temp_identity_before_replace(self):
        self.export()
        canary = self.base / "temp-canary"
        canary.write_bytes(b"synthetic temporary canary")
        host_before = (self.output / snapshot.MIN_HOST).read_bytes()
        manifest_before = (self.output / snapshot.MANIFEST_NAME).read_bytes()
        real_check = snapshot._SnapshotIO.check
        attacked = False

        def swap_staged(source):
            nonlocal attacked
            if not attacked:
                for temporary in (self.output / "hosts/linux").glob(".runtime-min.nix.*.tmp"):
                    attacked = True
                    temporary.rename(temporary.with_suffix(".saved"))
                    temporary.symlink_to(canary)
            return real_check(source)

        with patch.object(snapshot._SnapshotIO, "check", new=swap_staged):
            with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                snapshot.apply_minimal_selection(self.output, self.evaluated())
        self.assertTrue(attacked)
        self.assertEqual(canary.read_bytes(), b"synthetic temporary canary")
        self.assertEqual((self.output / snapshot.MIN_HOST).read_bytes(), host_before)
        self.assertEqual((self.output / snapshot.MANIFEST_NAME).read_bytes(), manifest_before)

    def test_selection_rejects_root_parent_alias_after_verification(self):
        container = self.base / "container"
        container.mkdir()
        self.output = container / "export"
        self.export()
        outside = self.base / "outside"
        canary = outside / "export" / snapshot.MIN_HOST
        canary.parent.mkdir(parents=True)
        canary.write_bytes(snapshot._host_text())
        original = canary.read_bytes()
        moved = self.base / "moved-container"
        real_verify = snapshot._verify_snapshot
        real_read = os.read
        canary_reads = []
        canary_inode = canary.stat().st_ino

        def after_verify(source):
            result = real_verify(source)
            container.rename(moved)
            container.symlink_to(outside, target_is_directory=True)
            return result

        def observe_read(fd, count):
            if os.fstat(fd).st_ino == canary_inode:
                canary_reads.append(fd)
            return real_read(fd, count)

        with patch.object(snapshot, "_verify_snapshot", side_effect=after_verify), patch.object(snapshot.os, "read", side_effect=observe_read):
            with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                snapshot.apply_minimal_selection(self.output, self.evaluated())
        self.assertTrue(container.is_symlink())
        self.assertEqual(canary_reads, [])
        self.assertEqual(canary.read_bytes(), original)
        self.assertEqual((moved / "export" / snapshot.MIN_HOST).read_bytes(), original)
        self.assertEqual(snapshot.verify_snapshot(moved / "export")["fixture"]["selectionStage"], "initial")

    def test_selection_uses_only_pinned_dirfds_for_both_replacements(self):
        self.export()
        real_replace = os.replace
        calls = []

        def observe_replace(source, target, *, src_dir_fd=None, dst_dir_fd=None):
            self.assertIsInstance(src_dir_fd, int)
            self.assertEqual(src_dir_fd, dst_dir_fd)
            self.assertNotIn("/", source)
            self.assertNotIn("/", target)
            calls.append(target)
            return real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with patch.object(snapshot.os, "replace", side_effect=observe_replace):
            result = snapshot.apply_minimal_selection(self.output, self.evaluated())
        self.assertEqual(calls, ["runtime-min.nix", snapshot.MANIFEST_NAME])
        self.assertEqual(result["fixture"]["selectionStage"], "minimal")

    def test_export_does_not_resolve_parent_alias_again_after_destination_check(self):
        container = self.base / "container"
        container.mkdir()
        self.output = container / "export"
        before = {p.relative_to(self.repo): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        real_candidates = snapshot._git_candidates

        def replace_parent(repo):
            result = real_candidates(repo)
            container.rename(self.base / "moved-container")
            container.symlink_to(self.repo, target_is_directory=True)
            return result

        with patch.object(snapshot, "_git_candidates", side_effect=replace_parent):
            with self.assertRaisesRegex(snapshot.SnapshotError, "[Cc]onflict"):
                self.export()
        self.assertTrue(container.is_symlink())
        self.assertEqual(before, {p.relative_to(self.repo): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()})

    def test_selection_excludes_all_non_nix_effective_ids(self):
        result = snapshot.minimal_exclusions(self.evaluated())
        for group in snapshot.GROUP_OPTIONS:
            self.assertEqual(result[group], ["codegraph", "fzf", "vscode"] if group == "nix.user.package" else ["synthetic-tool"])

    def test_selection_excludes_dynamic_native_package_without_inventing_reference(self):
        value = self.evaluated()
        selection = value["software"]["groups"]["native.system.package"]["selection"]
        item = {"id": "linux-kernel-headers", "name": "Current kernel headers", "ref": None}
        selection["include"] = [item]
        selection["effective"] = [item]
        self.assertEqual(snapshot.minimal_exclusions(value)["native.system.package"], ["linux-kernel-headers"])
        self.assertIsNone(item["ref"])

    def test_selection_rejects_unknown_groups_missing_tools_and_wrong_identity(self):
        for mutation in ("identity", "group", "tool", "reference", "interpolation", "preexcluded", "option"):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(self.evaluated())
                group = value["software"]["groups"]["nix.user.package"]
                if mutation == "identity":
                    value["settings"]["envy.user.home"] = "/home/private-canary"
                elif mutation == "group":
                    value["software"]["groups"]["unknown.group"] = {}
                elif mutation == "tool":
                    group["selection"]["effective"] = []
                elif mutation == "reference":
                    group["selection"]["effective"][0]["ref"] = None
                elif mutation == "interpolation":
                    group["selection"]["effective"][0]["id"] = '${builtins.readFile /private}'
                elif mutation == "preexcluded":
                    group["selection"]["exclude"] = ["tool"]
                else:
                    group["optionPath"] = "unknown.option"
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot.minimal_exclusions(value)

    def test_selection_accepts_duplicate_shared_include_contributions(self):
        value = self.evaluated()
        group = value["software"]["groups"]["nix.user.package"]
        group["selection"]["include"].append(copy.deepcopy(group["selection"]["include"][0]))
        self.assertEqual(snapshot.minimal_exclusions(value)["nix.user.package"], ["codegraph", "fzf", "vscode"])

    def test_selection_rejects_incomplete_effective_inventory(self):
        value = self.evaluated()
        value["software"]["groups"]["npm.user.tool"]["selection"]["effective"] = []
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.minimal_exclusions(value)

    def test_failed_selection_validation_leaves_export_unchanged(self):
        self.export()
        before = (self.output / snapshot.MANIFEST_NAME).read_bytes()
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.apply_minimal_selection(self.output, {})
        self.assertEqual((self.output / snapshot.MANIFEST_NAME).read_bytes(), before)
        snapshot.verify_snapshot(self.output)

    def test_generated_guest_seed_is_inert_and_has_valid_bash_syntax(self):
        self.export()
        result = subprocess.run(["bash", "-n", str(self.output / snapshot.SEED_SCRIPT)], capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        script = (self.output / snapshot.SEED_SCRIPT).read_text()
        self.assertIn("age-keygen -o", script)
        self.assertIn("sops --encrypt", script)
        self.assertIn('"$(id -un)" != envy-runtime', script)
        self.assertNotIn("sudo", script)
        self.assertFalse((self.output / "secrets").exists())

    def test_invalid_git_paths_and_unmerged_index_fail_closed(self):
        for raw in (b"100644 abc 0\t../private\0", b"100644 abc 1\tmodules/x\0", b"100644 abc 0\tmodules//x\0", b"160000 abc 0\tmodules/submodule\0"):
            with self.subTest(raw=raw), patch.object(snapshot.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, raw, b"")), self.assertRaises(snapshot.SnapshotError):
                self.export()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()

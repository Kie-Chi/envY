from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from envy import secure_io, structured_config
from envy.structured_config import ConflictError, contains, prepare_update, update_file


class StructuredConfigTests(unittest.TestCase):
    def test_contains_ignores_object_key_order_but_preserves_list_order(self):
        self.assertTrue(contains({"a": {"x": 1}}, {"a": {"x": 1, "y": 2}}))
        self.assertTrue(contains({"items": [{"x": 1, "y": 2}]}, {"items": [{"y": 2, "x": 1}]}))
        self.assertFalse(contains({"items": [1, 2]}, {"items": [2, 1]}))
        self.assertFalse(contains({"items": [1]}, {"items": [1, 2]}))

    def test_json_merge_preserves_runtime_fields_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            target.write_text(json.dumps({"runtime": True, "nested": {"keep": 1, "set": "old"}}))
            target.chmod(0o640)
            self.assertTrue(update_file(target, {"nested": {"set": "new"}}))
            self.assertEqual(json.loads(target.read_text()), {"runtime": True, "nested": {"keep": 1, "set": "new"}})
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_toml_merge_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.toml"
            target.write_text('[runtime]\nkeep = true\n\n[model]\nname = "old"\n')
            update_file(target, {"model": {"name": "new"}, "tools": ["a", "b"]}, format="toml")
            self.assertEqual(
                tomllib.loads(target.read_text()),
                {"runtime": {"keep": True}, "model": {"name": "new"}, "tools": ["a", "b"]},
            )

    def test_managed_scalars_remove_stale_values_and_preserve_user_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.toml"
            target.write_text('model_catalog_json = "old.json"\nmodel_context_window = 100\ncustom = true\n')
            update_file(target, {"model_context_window": 200}, format="toml",
                        managed_keys=["model_catalog_json", "model_context_window"])
            data = __import__("tomllib").loads(target.read_text())
            self.assertNotIn("model_catalog_json", data)
            self.assertEqual(data["model_context_window"], 200)
            self.assertTrue(data["custom"])

    def test_parse_failure_keeps_original(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            target.write_text("{broken")
            with self.assertRaises(ValueError):
                update_file(target, {"new": True})
            self.assertEqual(target.read_text(), "{broken")

    def test_store_link_is_replaced_with_writable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text('{"existing": 1}')
            target = root / "settings.json"
            target.symlink_to(source)
            update_file(target, {"declared": 2})
            self.assertFalse(target.is_symlink())
            self.assertEqual(json.loads(target.read_text()), {"existing": 1, "declared": 2})
            self.assertTrue(os.access(target, os.W_OK))


class PreparedUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-merge-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "config.json"

    def test_symlink_parent_then_dotdot_uses_filesystem_semantics(self):
        for relative in (False, True):
            with self.subTest(relative=relative):
                root = self.root / str(relative)
                child = root / "physical" / "child"
                child.mkdir(parents=True)
                (root / "alias").symlink_to(child)
                wrong = root / "config.json"
                wrong.write_text('{"unrelated": true}')
                wrong.chmod(0o440)
                original = (wrong.read_bytes(), wrong.stat())
                actual = child.parent / "config.json"
                source = root / "source"
                source.write_text('{"managed": "old", "user": true}')
                if relative:
                    actual.symlink_to(source)
                else:
                    actual.write_bytes(source.read_bytes())
                requested = Path("alias") / ".." / "config.json"
                with chdir(root):
                    prepared = prepare_update(requested if relative else root / requested,
                                              {"managed": "new"})
                self.assertTrue(prepared.apply())
                self.assertEqual(json.loads(actual.read_text()), {"managed": "new", "user": True})
                self.assertFalse(actual.is_symlink())
                self.assertEqual(source.read_text(), '{"managed": "old", "user": true}')
                self.assertEqual((wrong.read_bytes(), wrong.stat()), original)

    def test_parent_alias_replacement_is_detected_even_with_the_same_destination(self):
        child = self.root / "physical" / "child"
        child.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(child)
        actual = child.parent / "config.json"
        actual.write_text('{"managed": "old"}')
        self.target = alias / ".." / "config.json"
        prepared = prepare_update(self.target, {"managed": "new"})

        def change():
            alias.unlink()
            alias.symlink_to(child)

        self._race_after_fsync(prepared, change)
        self.assertEqual(actual.read_text(), '{"managed": "old"}')

    def test_symlink_dotdot_with_missing_parent_is_read_only_until_apply(self):
        child = self.root / "physical" / "child"
        child.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(child)
        requested = alias / ".." / "new" / "nested" / "config.json"
        prepared = prepare_update(requested, {"managed": True})
        self.assertFalse((child.parent / "new").exists())
        self.assertFalse((self.root / "new").exists())
        self.assertTrue(prepared.apply())
        self.assertEqual(json.loads((child.parent / "new" / "nested" / "config.json").read_text()),
                         {"managed": True})
        self.assertFalse((self.root / "new").exists())

    def test_yaml_unmanaged_mixed_keys_and_lists_survive_preview_merge_and_noop(self):
        data = {"user": {1: "EXISTING_SECRET", "label": "EXISTING_SECRET"},
                "items": [{1: "EXISTING_SECRET", "label": "EXISTING_SECRET"}],
                "managed": "old"}
        self.target.write_text(yaml.safe_dump(data, sort_keys=False))
        before = self.target.read_bytes()
        initial = prepare_update(self.target, {"managed": "old"}, format="yaml")
        self.assertEqual(initial.preview()["changes"], [])
        self.assertFalse(initial.apply())
        self.assertEqual(self.target.read_bytes(), before)
        prepared = prepare_update(self.target, {"managed": "new"}, format="yaml")
        self.assertEqual(prepared.preview()["changes"], [{"path": ["managed"], "operation": "change"}])
        self.assertNotIn("SECRET", json.dumps(prepared.preview()))
        self.assertNotIn("label", json.dumps(prepared.preview()))
        self.assertTrue(prepared.apply())
        self.assertEqual(yaml.safe_load(self.target.read_text()), {**data, "managed": "new"})
        again = prepare_update(self.target, {"managed": "new"}, format="yaml")
        self.assertEqual(again.preview(), {"format": "yaml", "changed": False, "changes": [], "operations": []})
        self.assertFalse(again.apply())

    def test_yaml_namespace_preserves_non_string_user_members(self):
        data = {"providers": {7: "EXISTING_SECRET", False: "EXISTING_SECRET",
                              "envy": {"same": 1}, "envy-old": {"secret": "EXISTING_SECRET"},
                              "envyish": {"user": True}}}
        self.target.write_text(yaml.safe_dump(data, sort_keys=False))
        declared = {"providers": {"envy": {"same": 1}}}
        prepared = prepare_update(self.target, declared, format="yaml",
                                  managed_key_prefixes={"providers": "envy"})
        self.assertEqual(prepared.preview()["changes"],
                         [{"path": ["providers", "envy-old"], "operation": "remove"}])
        self.assertNotIn("SECRET", json.dumps(prepared.preview()))
        self.assertTrue(prepared.apply())
        expected = {"providers": {key: value for key, value in data["providers"].items() if key != "envy-old"}}
        self.assertEqual(yaml.safe_load(self.target.read_text()), expected)
        again = prepare_update(self.target, declared, format="yaml",
                               managed_key_prefixes={"providers": "envy"})
        self.assertEqual(again.preview()["changes"], [])
        self.assertFalse(again.apply())
        self.assertEqual(yaml.safe_load(self.target.read_text()), expected)

    def test_preview_final_paths_and_lists_without_values(self):
        self.target.write_text(json.dumps({
            "user": "EXISTING_SECRET", "same": 1, "drop": "EXISTING_SECRET",
            "nested": {"set": "EXISTING_SECRET", "keep": 42},
            "items": [{"secret": "EXISTING_SECRET"}],
        }))
        original = self.target.stat()
        prepared = prepare_update(self.target, {
            "same": 1, "nested": {"set": "DECLARED_SECRET"},
            "items": [{"secret": "DECLARED_SECRET"}], "added": True,
        }, managed_keys=["same", "drop"])
        preview = prepared.preview()
        self.assertEqual(preview["changes"], [
            {"path": ["added"], "operation": "add"},
            {"path": ["drop"], "operation": "remove"},
            {"path": ["items"], "operation": "change"},
            {"path": ["nested", "set"], "operation": "change"},
        ])
        self.assertEqual(preview["operations"], ["write"])
        self.assertNotIn("SECRET", json.dumps(preview))
        self.assertNotIn("SECRET", repr(prepared))
        self.assertEqual(self.target.stat(), original)
        prepared.apply()
        self.assertEqual(json.loads(self.target.read_text())["user"], "EXISTING_SECRET")
        self.assertFalse(prepare_update(self.target, {"same": 1}).preview()["changed"])

    def test_namespace_boundary_and_rebuilt_unchanged_values(self):
        data = {"providers": {"envy": {"same": 1}, "envy-old": {"secret": "old"},
                              "envyish": {"custom": True}, "user-envy": 2}}
        self.target.write_text(json.dumps(data))
        prepared = prepare_update(self.target, {"providers": {"envy": {"same": 1}}},
                                  managed_key_prefixes={"providers": "envy"})
        self.assertEqual(prepared.preview()["changes"], [
            {"path": ["providers", "envy-old"], "operation": "remove"},
        ])
        self.assertTrue(prepared.apply())
        actual = json.loads(self.target.read_text())
        self.assertEqual(actual["providers"]["envyish"], {"custom": True})
        self.assertEqual(actual["providers"]["user-envy"], 2)
        again = prepare_update(self.target, {"providers": {"envy": {"same": 1}}},
                               managed_key_prefixes={"providers": "envy"})
        self.assertEqual(again.preview()["changes"], [])
        self.assertFalse(again.apply())

    def test_prepare_is_read_only_including_missing_parents_and_modes(self):
        missing = self.root / "one" / "two" / "config.json"
        with patch.object(secure_io.tempfile, "mkstemp", side_effect=AssertionError("write")), \
                patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")), \
                patch.object(Path, "chmod", side_effect=AssertionError("chmod")):
            prepared = prepare_update(missing, {"new": True})
            self.assertEqual(prepared.preview()["operations"], ["create"])
        self.assertFalse(missing.parent.exists())
        self.assertTrue(prepared.apply())
        self.assertEqual(missing.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(missing.read_text()), {"new": True})
        self.target.write_text('{"same": true}')
        self.target.chmod(0o400)
        before = self.target.stat()
        prepared = prepare_update(self.target, {"same": True})
        self.assertEqual(prepared.preview()["operations"], ["chmod"])
        self.assertEqual(self.target.stat(), before)
        self.assertFalse(prepared.apply())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_modes_remain_private_and_never_gain_group_or_world_permissions(self):
        for mode in [0o000, 0o400, 0o600, 0o640, 0o644, 0o750]:
            with self.subTest(mode=oct(mode)):
                self.target.write_text('{}')
                self.target.chmod(mode)
                # Tests can be run by a user unable to read a 000 file.
                if mode == 0 and os.geteuid() != 0:
                    with self.assertRaises(PermissionError):
                        prepare_update(self.target, {"new": True})
                    self.target.chmod(0o600)
                    continue
                update_file(self.target, {"new": True})
                self.assertEqual(self.target.stat().st_mode & 0o777, mode | 0o600)

    def test_symlink_preview_and_apply_leave_source_unchanged(self):
        source = self.root / "source.json"
        source.write_text('{"same": 1}')
        source.chmod(0o440)
        self.target.symlink_to(source)
        prepared = prepare_update(self.target, {"same": 1})
        self.assertEqual(prepared.preview()["changes"], [])
        self.assertEqual(prepared.preview()["operations"], ["write", "replace_symlink", "chmod"])
        self.assertTrue(self.target.is_symlink())
        before = source.stat()
        self.assertTrue(prepared.apply())
        self.assertEqual(source.stat(), before)
        self.assertEqual(source.read_text(), '{"same": 1}')
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)

    def test_dangling_directory_and_special_files_are_rejected(self):
        self.target.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(ValueError, "dangling"):
            prepare_update(self.target, {})
        self.target.unlink()
        self.target.mkdir()
        with self.assertRaises(ValueError):
            prepare_update(self.target, {})
        self.target.rmdir()
        os.mkfifo(self.target)
        with self.assertRaises(ValueError):
            prepare_update(self.target, {})

    def test_invalid_formats_and_roots_do_not_echo_content_or_modify_target(self):
        for format, text in [("json", '{"secret":"EXISTING_SECRET", broken'),
                             ("yaml", 'secret: EXISTING_SECRET\nbad: [unterminated'),
                             ("toml", 'secret = "EXISTING_SECRET"\nbad = [unterminated'),
                             ("json", '["EXISTING_SECRET"]')]:
            with self.subTest(format=format, text=text):
                self.target.write_text(text)
                with self.assertRaises(ValueError) as raised:
                    prepare_update(self.target, {"secret": "DECLARED_SECRET"}, format=format)
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertEqual(self.target.read_text(), text)

    def test_yaml_preserves_user_fields_and_is_idempotent(self):
        self.target.write_text('user: true\nmanaged:\n  old: true\n  same: 1\n')
        declared = {"managed": {"same": 1}, "list": ["a", "b"]}
        self.assertTrue(update_file(self.target, declared, format="yaml"))
        before = self.target.read_bytes()
        self.assertFalse(update_file(self.target, declared, format="yaml"))
        self.assertEqual(self.target.read_bytes(), before)
        self.assertIn('user: true', self.target.read_text())
        self.assertIn('old: true', self.target.read_text())

    def test_noop_rejects_content_inode_and_mode_changes(self):
        for mutation in ["content", "inode", "mode"]:
            with self.subTest(mutation=mutation):
                self.target.write_text('{"same": 1}')
                self.target.chmod(0o600)
                prepared = prepare_update(self.target, {"same": 1})
                if mutation == "content":
                    self.target.write_text('{"competitor": true}')
                elif mutation == "inode":
                    other = self.root / "other"
                    other.write_bytes(self.target.read_bytes())
                    other.replace(self.target)
                else:
                    self.target.chmod(0o640)
                before, mode = self.target.read_bytes(), self.target.stat().st_mode
                with self.assertRaises(ConflictError):
                    prepared.apply()
                self.assertEqual((self.target.read_bytes(), self.target.stat().st_mode), (before, mode))

    def test_chmod_checks_the_open_descriptor_identity(self):
        self.target.write_text('{"same": 1}')
        self.target.chmod(0o400)
        prepared = prepare_update(self.target, {"same": 1})
        original_open = os.open

        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if path == self.target and flags & os.O_NOFOLLOW:
                competitor = self.root / "competitor"
                competitor.write_text('{"competitor": true}')
                competitor.chmod(0o440)
                competitor.replace(self.target)
            return descriptor

        with patch.object(structured_config.os, "open", side_effect=replace_after_open):
            with self.assertRaises(ConflictError):
                prepared.apply()
        self.assertEqual(self.target.read_text(), '{"competitor": true}')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o440)

    def _race_after_fsync(self, prepared, mutation):
        ready, done = threading.Event(), threading.Event()
        errors = []
        real_fsync = os.fsync

        def competitor():
            try:
                if not ready.wait(5):
                    raise AssertionError("writer never reached fsync")
                mutation()
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        def fsync(descriptor):
            real_fsync(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                ready.set()
                if not done.wait(5):
                    raise AssertionError("competitor did not finish")

        thread = threading.Thread(target=competitor, daemon=True)
        thread.start()
        try:
            with patch.object(secure_io.os, "fsync", side_effect=fsync):
                with self.assertRaisesRegex(ConflictError, "Close the application and retry"):
                    prepared.apply()
        finally:
            ready.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([p for p in self.root.rglob("*") if ".envy-" in p.name], [])

    def test_fsync_guard_rejects_content_inode_permission_and_missing_target_races(self):
        for mutation in ["content", "inode", "permissions", "missing"]:
            with self.subTest(mutation=mutation):
                if self.target.exists():
                    self.target.unlink()
                if mutation != "missing":
                    self.target.write_text('{"same": 1}')
                prepared = prepare_update(self.target, {"declared": "DECLARED_SECRET"})
                saved = []

                def change():
                    if mutation == "inode":
                        other = self.root / "other"
                        other.write_bytes(self.target.read_bytes())
                        other.replace(self.target)
                    elif mutation == "permissions":
                        self.target.chmod(0o440)
                    else:
                        self.target.write_text('{"competitor": "EXISTING_SECRET"}')
                    saved.append((self.target.read_bytes(), self.target.stat().st_mode))

                self._race_after_fsync(prepared, change)
                self.assertEqual((self.target.read_bytes(), self.target.stat().st_mode), saved[0])

    def test_fsync_guard_rejects_link_and_source_races(self):
        source = self.root / "source"
        alternate = self.root / "alternate"
        for mutation in ["link", "source", "source_inode"]:
            with self.subTest(mutation=mutation):
                if self.target.is_symlink():
                    self.target.unlink()
                if source.exists():
                    source.chmod(0o600)
                source.write_text('{"same": 1}')
                alternate.write_text('{"same": 1}')
                self.target.symlink_to(source)
                prepared = prepare_update(self.target, {"declared": True})
                saved = []

                def change():
                    if mutation == "link":
                        self.target.unlink()
                        self.target.symlink_to(alternate)
                    elif mutation == "source":
                        source.write_text('{"competitor": true}')
                        source.chmod(0o440)
                    else:
                        alternate.replace(source)
                    saved.append((os.readlink(self.target), source.read_bytes(), source.stat().st_mode))

                self._race_after_fsync(prepared, change)
                self.assertEqual((os.readlink(self.target), source.read_bytes(), source.stat().st_mode), saved[0])

    def test_fsync_guard_rejects_parent_replacement_and_cleans_moved_temporary(self):
        parent = self.root / "parent"
        parent.mkdir()
        self.target = parent / "config.json"
        self.target.write_text('{"same": 1}')
        prepared = prepare_update(self.target, {"declared": True})

        def change():
            parent.rename(self.root / "moved")
            parent.mkdir()
            self.target.write_text('{"competitor": true}')
            self.target.chmod(0o440)

        self._race_after_fsync(prepared, change)
        self.assertEqual(self.target.read_text(), '{"competitor": true}')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o440)
        self.assertEqual((self.root / "moved" / "config.json").read_text(), '{"same": 1}')

    def test_new_parent_baseline_keeps_original_ancestors_protected(self):
        ancestor = self.root / "ancestor"
        ancestor.mkdir()
        self.target = ancestor / "new" / "nested" / "config.json"
        prepared = prepare_update(self.target, {"declared": True})

        def change():
            ancestor.rename(self.root / "old-ancestor")
            self.target.parent.mkdir(parents=True)
            self.target.write_text('{"competitor": true}')

        self._race_after_fsync(prepared, change)
        self.assertEqual(self.target.read_text(), '{"competitor": true}')

    def test_parent_timestamp_changes_are_allowed_but_existing_identity_is_not(self):
        self.target.write_text('{}')
        prepared = prepare_update(self.target, {"new": True})
        (self.root / "unrelated").write_text('touch parent mtime')
        self.assertTrue(prepared.apply())
        prepared = prepare_update(self.target, {"new": True})
        self.root.chmod(0o750)
        with self.assertRaises(ConflictError):
            prepared.apply()

    def test_initial_read_detects_concurrent_write(self):
        self.target.write_text('{"same": 1}')
        original_open = os.open
        ready, done = threading.Event(), threading.Event()

        def competitor():
            if ready.wait(5):
                self.target.write_text('{"competitor": true}')
                done.set()

        def open_then_wait(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if path == self.target:
                ready.set()
                if not done.wait(5):
                    os.close(descriptor)
                    raise AssertionError("competitor timed out")
            return descriptor

        thread = threading.Thread(target=competitor, daemon=True)
        thread.start()
        try:
            with patch.object(structured_config.os, "open", side_effect=open_then_wait):
                with self.assertRaises(ConflictError):
                    prepare_update(self.target, {"new": True})
        finally:
            ready.set()
            thread.join(5)
        self.assertEqual(self.target.read_text(), '{"competitor": true}')


class StructuredConfigCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-merge-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "target"
        self.source = self.root / "source"
        self.target.write_text('{"managed": "EXISTING_SECRET", "user": true}')
        self.source.write_text('{"managed": "DECLARED_SECRET"}')

    def run_cli(self, *arguments):
        result = subprocess.run([
            sys.executable, "-m", "envy.structured_config", "--source", str(self.source),
            "--target", str(self.target), *arguments,
        ], capture_output=True, text=True, timeout=10)
        self.assertNotIn("EXISTING_SECRET", result.stdout + result.stderr)
        self.assertNotIn("DECLARED_SECRET", result.stdout + result.stderr)
        return result

    def test_cli_symlink_parent_dotdot_updates_the_real_target_only(self):
        child = self.root / "physical" / "child"
        child.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(child)
        wrong = self.target
        before = (wrong.read_bytes(), wrong.stat())
        actual = child.parent / "target"
        actual.write_text('{"managed": "EXISTING_SECRET", "physical": true}')
        self.target = alias / ".." / "target"
        result = self.run_cli("--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["data"]["changes"],
                         [{"path": ["managed"], "operation": "change"}])
        result = self.run_cli()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(json.loads(actual.read_text()), {"managed": "DECLARED_SECRET", "physical": True})
        self.assertEqual((wrong.read_bytes(), wrong.stat()), before)

    def test_preview_json_envelope_and_plain_preview_are_value_free_and_read_only(self):
        before = self.target.stat()
        for arguments in [("--dry-run", "--json"), ("--dry-run",)]:
            result = self.run_cli(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            if "--json" in arguments:
                self.assertEqual(payload["schemaVersion"], 1)
                self.assertEqual(payload["command"], "structured-config.merge")
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["warnings"])
                payload = payload["data"]
            self.assertEqual(payload["changes"], [{"path": ["managed"], "operation": "change"}])
            self.assertEqual(result.stderr, "")
            self.assertEqual(self.target.stat(), before)

    def test_dry_run_never_chmods_or_creates_missing_parents(self):
        self.target.chmod(0o400)
        result = self.run_cli("--dry-run", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertIn("chmod", json.loads(result.stdout)["data"]["operations"])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o400)
        self.target = self.root / "missing" / "nested" / "target"
        result = self.run_cli("--dry-run", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["data"]["operations"], ["create"])
        self.assertFalse(self.target.parent.exists())

    def test_json_apply_and_io_errors_keep_the_envelope_value_free(self):
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "structured-config.merge")
        self.assertEqual(json.loads(self.target.read_text())["managed"], "DECLARED_SECRET")
        self.source.unlink()
        result = self.run_cli("--json", "--dry-run")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "io_error")
        self.assertEqual(result.stderr, "")

    def test_legacy_activation_success_is_silent_and_idempotent(self):
        result = self.run_cli()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(json.loads(self.target.read_text()), {"managed": "DECLARED_SECRET", "user": True})
        before = self.target.stat()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.target.stat(), before)

    def test_errors_are_sanitized_in_stdout_stderr_and_json(self):
        for format, invalid in [("json", '{"secret":"EXISTING_SECRET", bad'),
                                ("yaml", 'secret: EXISTING_SECRET\nbad: ['),
                                ("toml", 'secret = "EXISTING_SECRET"\nbad = [')]:
            self.target.write_text(invalid)
            for extra in [("--dry-run",), ("--dry-run", "--json")]:
                result = self.run_cli("--format", format, *extra)
                self.assertEqual(result.returncode, 1)
                if "--json" in extra:
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["error"]["code"], "invalid_configuration")
                    self.assertEqual(payload["command"], "structured-config.merge")
                    self.assertEqual(result.stderr, "")
                self.assertEqual(self.target.read_text(), invalid)
        for extra in [("--managed-keys", 'DECLARED_SECRET'),
                      ("--managed-key-prefixes", 'DECLARED_SECRET'),
                      ("--format", 'DECLARED_SECRET'), ("--DECLARED_SECRET",)]:
            result = self.run_cli("--json", *extra)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_arguments")
            self.assertEqual(result.stderr, "")
        self.source.write_text('DECLARED_SECRET')
        result = self.run_cli("--dry-run", "--json")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_configuration")

    def test_conflict_error_has_stable_code_and_close_app_hint(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(structured_config.PreparedUpdate, "apply", side_effect=ConflictError()):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = structured_config.main(["--source", str(self.source), "--target", str(self.target), "--json"])
        self.assertEqual(result, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "conflict")
        self.assertIn("Close the application and retry", payload["error"]["message"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("SECRET", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

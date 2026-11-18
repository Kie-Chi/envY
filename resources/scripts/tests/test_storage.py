"""Storage lifecycle regressions use isolated files and fake remote commands."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from envy.storage import backend, cli, lifecycle, planner
from envy.storage.model import Rule, StorageError, policy_from_manifest, rule_details
from envy.storage.state import locked, read_record, records, write_record


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "Downloads"
        self.root.mkdir()
        self.state = self.home / "state"
        self.manifest = {"settings": {"envy.user.home": str(self.home)}, "storage": {
            "mode": "manual", "quarantineDays": 1, "archive": {}, "rules": [{
                "id": "old-downloads", "path": str(self.root), "action": "quarantine",
                "minAgeDays": 30, "extensions": ["dmg"], "minSizeBytes": 0}]}}
        self.file = self.root / "old [installer]\nname.dmg"
        self.file.write_bytes(b"valuable test content")
        old = time.time() - 40 * 86400
        os.utime(self.file, (old, old))
        self.policy = self.load_policy()
        self.inactive = patch.object(lifecycle, "assert_inactive")
        self.inactive.start()
        self.addCleanup(self.inactive.stop)

    def load_policy(self):
        return policy_from_manifest(self.manifest, self.state, self.home / "checkout")

    def scan_output(self, *, path=None, errors=0):
        events = [{"type": "MSG", "path": str(path or self.file), "msg": "envy-storage-candidate"},
                  {"type": "REPORT", "error_count": errors, "success_count": 1}]
        return subprocess.CompletedProcess([], 0, "\n".join(json.dumps(e) for e in events), "")

    def plan(self):
        with patch.object(planner, "run_process", return_value=self.scan_output()):
            return planner.plan(self.policy, self.state)

    def archive_policy(self):
        self.manifest["storage"]["archive"] = {
            "repository": "sftp:test@nas:/archives", "passwordFile": str(self.home / "password")}
        self.manifest["storage"]["rules"][0]["action"] = "archive"
        self.policy = self.load_policy()

    def archive_mock(self, name, **kwargs):
        context = patch.object(backend.Archive, name, **kwargs)
        mocked = context.start()
        self.addCleanup(context.stop)
        return mocked

    def test_plan_uses_simulation_and_does_not_move_files(self):
        with patch.object(planner, "run_process", return_value=self.scan_output()) as run:
            plan = planner.plan(self.policy, self.state)
        self.assertEqual(run.call_args.args[0][1:4], ["sim", "--format", "jsonl"])
        self.assertEqual(plan["candidates"][0]["path"], str(self.file))
        self.assertTrue(self.file.exists())
        self.assertEqual(read_record(self.state, "plans", plan["id"]), plan)
        self.assertEqual((self.state / "plans" / f"{plan['id']}.json").stat().st_mode & 0o777, 0o600)

    def test_incomplete_or_invalid_scan_never_saves_plan(self):
        for output in [self.scan_output(errors=1), subprocess.CompletedProcess([], 0, "{}", ""),
                       subprocess.CompletedProcess([], 0, "invalid-json", "")]:
            with self.subTest(output=output.stdout), patch.object(planner, "run_process", return_value=output):
                with self.assertRaises(StorageError):
                    planner.plan(self.policy, self.state)
        self.assertEqual(records(self.state, "plans"), [])
        self.assertTrue(self.file.exists())

    def test_protected_symlink_and_hardlink_are_skipped(self):
        protected = self.file.parent / "protected.dmg"
        protected.symlink_to(self.file)
        for path in [protected, self.file]:
            if path == self.file:
                os.link(self.file, self.root / "other.dmg")
            with patch.object(planner, "run_process", return_value=self.scan_output(path=path)):
                result = planner.plan(self.policy, self.state)
            self.assertEqual(result["candidates"], [])
            self.assertTrue(result["skipped"])

    def test_policy_rejects_unsafe_roots_and_secret_urls(self):
        for path in [str(self.home), "/", "relative", str(self.home / ".." / "other")]:
            with self.subTest(path=path):
                self.manifest["storage"]["rules"][0]["path"] = path
                with self.assertRaises(StorageError):
                    self.load_policy()
        self.manifest["storage"]["rules"][0]["path"] = str(self.root)
        self.manifest["storage"]["archive"] = {"repository": "sftp://user:password@nas/path"}
        with self.assertRaises(StorageError):
            self.load_policy()

    def test_modified_candidate_is_retained(self):
        plan = self.plan()
        self.file.write_text("changed")
        result = lifecycle.execute(self.policy, self.state, plan["id"])
        self.assertTrue(result["errors"])
        self.assertTrue(self.file.exists())

    def test_changed_policy_or_expired_plan_cannot_execute(self):
        plan = self.plan()
        plan["createdAt"] -= 2 * 86400
        write_record(self.state, "plans", plan)
        with self.assertRaises(StorageError):
            lifecycle.execute(self.policy, self.state, plan["id"])
        self.assertTrue(self.file.exists())

    def test_quarantine_restore_and_expiry(self):
        result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        self.assertEqual(result["errors"], [])
        self.assertFalse(self.file.exists())
        held = Path(result["items"][0]["heldPath"])
        self.assertTrue(held.exists())
        target = self.home / "recovered"
        recovered = lifecycle.restore(self.policy, self.state, result["id"], target)
        self.assertEqual(recovered["restored"], [str(self.file)])
        self.assertEqual((target / self.file.relative_to("/")).read_bytes(), held.read_bytes())
        result["items"][0]["expiresAt"] = 0
        write_record(self.state, "runs", result)
        self.assertEqual(len(lifecycle.purge(self.policy, self.state)["candidates"]), 1)
        self.assertTrue(held.exists())
        lifecycle.purge(self.policy, self.state, execute=True)
        self.assertFalse(held.exists())
        self.assertEqual(read_record(self.state, "runs", result["id"])["items"][0]["status"], "expired")

    def test_replaying_plan_is_rejected(self):
        plan = self.plan()
        lifecycle.execute(self.policy, self.state, plan["id"])
        with self.assertRaises(StorageError):
            lifecycle.execute(self.policy, self.state, plan["id"])

    def test_changed_quarantine_is_never_purged(self):
        result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        item = result["items"][0]
        Path(item["heldPath"]).write_text("replacement")
        item["expiresAt"] = 0
        write_record(self.state, "runs", result)
        expiry = lifecycle.purge(self.policy, self.state, execute=True)
        self.assertTrue(expiry["errors"])
        self.assertTrue(Path(item["heldPath"]).exists())

    def test_archive_backup_failure_keeps_local_file(self):
        self.archive_policy()
        self.archive_mock("identity", return_value="a" * 64)
        self.archive_mock("backup", side_effect=StorageError("incomplete backup"))
        with self.assertRaises(StorageError):
            lifecycle.execute(self.policy, self.state, self.plan()["id"])
        self.assertTrue(self.file.exists())

    def test_archive_hash_failure_keeps_local_and_snapshot_record(self):
        self.archive_policy()
        self.archive_mock("identity", return_value="a" * 64)
        self.archive_mock("backup", return_value="b" * 64)
        self.archive_mock("content_hash", return_value="wrong")
        result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        self.assertTrue(result["errors"])
        self.assertTrue(self.file.exists())
        self.assertEqual(read_record(self.state, "runs", result["id"])["snapshot"], "b" * 64)

    def test_verified_archive_is_held_then_released_without_expiring_snapshot(self):
        self.archive_policy()
        digest = planner.file_hash(self.file)
        self.archive_mock("identity", return_value="a" * 64)
        self.archive_mock("backup", return_value="b" * 64)
        self.archive_mock("content_hash", return_value=digest)
        result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        self.assertEqual(result["errors"], [])
        self.assertFalse(self.file.exists())
        self.assertEqual(result["items"][0]["status"], "archived-held")
        held = Path(result["items"][0]["heldPath"])
        self.assertTrue(held.exists())
        self.assertEqual(lifecycle.purge(self.policy, self.state, execute=True)["candidates"], [])
        result["items"][0]["expiresAt"] = 0
        write_record(self.state, "runs", result)
        self.assertEqual(lifecycle.purge(self.policy, self.state, execute=True)["errors"], [])
        self.assertFalse(held.exists())
        self.assertEqual(read_record(self.state, "runs", result["id"])["items"][0]["status"], "archived")
        self.archive_mock("identity", return_value="c" * 64)
        with self.assertRaises(StorageError):
            lifecycle.restore(self.policy, self.state, result["id"], self.home / "recovery")

    def test_interrupted_move_can_be_recovered(self):
        plan = self.plan()
        identifier = uuid.uuid4().hex
        held = self.state / "quarantine" / identifier / "0"
        held.parent.mkdir(parents=True)
        item = dict(plan["candidates"][0], status="prepared", heldPath=str(held))
        os.rename(self.file, held)
        write_record(self.state, "runs", {"id": identifier, "snapshot": None, "items": [item]})
        result = lifecycle.restore(self.policy, self.state, identifier, self.home / "recovery")
        self.assertEqual(result["restored"], [str(self.file)])

    def test_mounted_archive_policy_requires_non_secret_source_identity(self):
        repository = str(self.root / "archive")
        self.manifest["storage"]["archive"] = {"repository": repository}
        with self.assertRaisesRegex(StorageError, "requires mountSource"):
            self.load_policy()
        for invalid in (None, 3, "//user:fixture-secret@nas/share", "//nas/share\n", " "):
            with self.subTest(source=invalid):
                self.manifest["storage"]["archive"]["mountSource"] = invalid
                with self.assertRaises(StorageError):
                    self.load_policy()
        self.manifest["storage"]["archive"]["mountSource"] = "//fixture@nas.invalid/archive"
        first = self.load_policy()
        self.assertEqual(first.mount_source, "//fixture@nas.invalid/archive")
        self.manifest["storage"]["archive"]["mountSource"] = "//other.invalid/archive"
        self.assertNotEqual(first.fingerprint, self.load_policy().fingerprint)

    def test_sftp_archive_rejects_stray_mount_source(self):
        self.manifest["storage"]["archive"] = {
            "repository": "sftp:fixture@nas.invalid:/archive", "mountSource": "//nas/share"}
        with self.assertRaises(StorageError):
            self.load_policy()
        self.manifest["storage"]["archive"]["mountSource"] = ""
        self.assertEqual(self.load_policy().mount_source, "")

    def test_local_archive_repository_is_protected_from_rules(self):
        repository = self.root / "repository"
        repository.mkdir()
        pack = repository / "pack.dmg"
        pack.write_text("archive data")
        old = time.time() - 40 * 86400
        os.utime(pack, (old, old))
        self.manifest["storage"]["archive"] = {"repository": str(repository),
                                               "mountSource": "//fixture@nas.invalid/archive"}
        self.policy = self.load_policy()
        with patch.object(planner, "run_process", return_value=self.scan_output(path=pack)):
            result = planner.plan(self.policy, self.state)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(pack.exists())

    def test_release_recovers_after_unlink_before_record_commit(self):
        result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        item = result["items"][0]
        item.update(expiresAt=0, status="releasing")
        write_record(self.state, "runs", result)
        Path(item["heldPath"]).unlink()
        self.assertEqual(lifecycle.purge(self.policy, self.state, execute=True)["errors"], [])
        self.assertEqual(read_record(self.state, "runs", result["id"])["items"][0]["status"], "expired")

    def test_late_writer_does_not_lose_data_after_archive_move(self):
        self.archive_policy()
        digest = planner.file_hash(self.file)
        self.archive_mock("identity", return_value="a" * 64)
        self.archive_mock("backup", return_value="b" * 64)
        self.archive_mock("content_hash", return_value=digest)
        original_stage = lifecycle._stage
        handles = []
        def late_open(candidate, destination):
            handles.append(self.file.open("ab"))
            original_stage(candidate, destination)
        with patch.object(lifecycle, "_stage", side_effect=late_open):
            result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        handle = handles.pop()
        handle.write(b"late writes must survive")
        handle.close()
        item = result["items"][0]
        held = Path(item["heldPath"])
        self.assertTrue(held.read_bytes().endswith(b"late writes must survive"))
        item["expiresAt"] = 0
        write_record(self.state, "runs", result)
        self.assertTrue(lifecycle.purge(self.policy, self.state, execute=True)["errors"])
        self.assertTrue(held.exists())

    def test_held_archive_restores_without_accessing_offline_nas(self):
        self.archive_policy()
        digest = planner.file_hash(self.file)
        self.archive_mock("identity", return_value="a" * 64)
        self.archive_mock("backup", return_value="b" * 64)
        self.archive_mock("content_hash", return_value=digest)
        result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        self.archive_mock("identity", side_effect=StorageError("NAS offline"))
        recovered = lifecycle.restore(self.policy, self.state, result["id"], self.home / "offline-recovery")
        self.assertEqual(recovered["restored"], [str(self.file)])

    @unittest.skipUnless(shutil.which("lsof") or Path("/usr/sbin/lsof").exists(), "lsof unavailable")
    def test_real_activity_check_does_not_match_our_state_lock(self):
        result = lifecycle.execute(self.policy, self.state, self.plan()["id"])
        result["items"][0]["expiresAt"] = 0
        write_record(self.state, "runs", result)
        self.inactive.stop()
        with patch.object(Path, "home", return_value=self.home), locked(self.state):
            expired = lifecycle.purge(self.policy, self.state, execute=True)
        self.assertEqual(expired["errors"], [])
        self.assertFalse(Path(result["items"][0]["heldPath"]).exists())

    def test_activity_unknown_or_active_blocks_candidate(self):
        for result in [subprocess.CompletedProcess([], 0, "123", ""),
                       subprocess.CompletedProcess([], 1, "", "warning"),
                       subprocess.CompletedProcess([], 124, "", "timeout")]:
            with self.subTest(result=result), patch.object(planner, "run_process", return_value=result):
                with self.assertRaises(StorageError):
                    planner.assert_inactive(self.file)

    def test_lock_excludes_concurrent_operations(self):
        with locked(self.state):
            with self.assertRaises(StorageError):
                with locked(self.state):
                    self.fail("A concurrent lock was accepted")

    def test_restic_incomplete_exit_is_not_a_success(self):
        with patch.object(backend.Archive, "environment", return_value={}), patch.object(
            backend, "run_process", return_value=subprocess.CompletedProcess([], 3, "", "partial")
        ):
            with self.assertRaises(StorageError):
                backend.Archive(self.policy, self.state).command(["backup"])

    def test_restic_file_list_preserves_special_filenames(self):
        seen = []
        def command(args):
            seen.append(Path(args[-1]).read_bytes())
            return json.dumps({"message_type": "summary", "snapshot_id": "a" * 64})
        with patch.object(backend.Archive, "command", side_effect=command):
            snapshot = backend.Archive(self.policy, self.state).backup([str(self.file)], "batch")
        self.assertEqual(snapshot, "a" * 64)
        self.assertEqual(seen, [os.fsencode(self.file) + b"\x00"])

    def test_cli_preview_and_report_schedule_do_not_execute(self):
        runner = CliRunner()
        for args in [["run", "--json"], ["run", "--scheduled", "--json"]]:
            self.manifest["storage"]["mode"] = "report"
            self.policy = self.load_policy()
            with patch.object(cli, "_context", return_value=(self.policy, self.state)), patch.object(
                planner, "run_process", return_value=self.scan_output()
            ), patch.object(lifecycle, "execute") as execute:
                output = io.StringIO()
                with patch.object(cli.log, "console", Console(file=output, force_terminal=False)):
                    result = runner.invoke(cli.app, args)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(json.loads(output.getvalue())["ok"])
            execute.assert_not_called()
        self.assertTrue(self.file.exists())

    @unittest.skipUnless(shutil.which("organize") and shutil.which("restic"), "real storage tools not on PATH")
    def test_real_organize_restic_archive_release_and_restore(self):
        self.archive_policy()
        password = self.home / "password"
        password.write_text("isolated test repository password")
        password.chmod(0o600)
        environment = dict(os.environ, HOME=str(self.home),
                           XDG_CONFIG_HOME=str(self.home / ".config"),
                           RESTIC_REPOSITORY=str(self.home / "repository"),
                           RESTIC_PASSWORD_FILE=str(password),
                           RESTIC_CACHE_DIR=str(self.home / "restic-cache"))
        with patch.object(backend.Archive, "environment", return_value=environment):
            archive = backend.Archive(self.policy, self.state)
            archive.command(["init", "--repository-version", "2"])
            expected_source = self.file.read_bytes()
            password.write_text("incorrect isolated repository password")
            with self.assertRaises(StorageError):
                archive.identity()
            self.assertEqual(self.file.read_bytes(), expected_source)
            password.write_text("isolated test repository password")
            with patch.dict(environment, RESTIC_REPOSITORY=str(self.home / "missing-repository")):
                with self.assertRaises(StorageError):
                    archive.identity()
                self.assertEqual(self.file.read_bytes(), expected_source)
            self.assertEqual(len(archive.identity()), 64)
            real_run = planner.run_process
            def checked_scan(*args, **kwargs):
                scan = real_run(*args, **kwargs, env=environment)
                self.assertEqual(scan.returncode, 0, f"{scan.stdout}\n{scan.stderr}")
                return scan
            with patch.object(planner, "run_process", side_effect=checked_scan):
                plan = planner.plan(self.policy, self.state)
            self.assertEqual(len(plan["candidates"]), 1)
            expected = self.file.read_bytes()
            result = lifecycle.execute(self.policy, self.state, plan["id"])
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["items"][0]["status"], "archived-held")
            result["items"][0]["expiresAt"] = 0
            write_record(self.state, "runs", result)
            self.inactive.stop()
            with locked(self.state):
                self.assertEqual(lifecycle.purge(self.policy, self.state, execute=True)["errors"], [])
            self.assertFalse(Path(result["items"][0]["heldPath"]).exists())
            target = self.home / "restored-real"
            lifecycle.restore(self.policy, self.state, result["id"], target)
            self.assertEqual((target / self.file.relative_to("/")).read_bytes(), expected)
            archive.command(["check", "--read-data"])

    def test_cli_error_is_json_and_returns_failure(self):
        output = io.StringIO()
        with patch.object(cli, "_context", side_effect=StorageError("missing manifest")), patch.object(
            cli.log, "console", Console(file=output, force_terminal=False)
        ):
            result = CliRunner().invoke(cli.app, ["status", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])


class StorageDirectoryTests(unittest.TestCase):
    """Directory rules, protected overrides, and native maintenance gating."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.state = self.home / "state"
        self.data = self.home / "Library" / "Application Support" / "Demo" / "Cache"
        self.bucket = self.data / "bucket"
        (self.bucket / "nested").mkdir(parents=True)
        (self.bucket / "nested" / "blob.bin").write_bytes(b"x" * 4096)
        old = time.time() - 40 * 86400
        for entry in [*self.bucket.rglob("*"), self.bucket]:
            os.utime(entry, (old, old))
        self.manifest = {"settings": {"envy.user.home": str(self.home)}, "storage": {
            "mode": "report", "quarantineDays": 1, "archive": {}, "protectedOverrides": [
                str(self.data)], "rules": [{
                    "id": "demo-cache", "path": str(self.data), "action": "quarantine",
                    "targets": "dirs", "depth": 1, "recursiveAge": True,
                    "minAgeDays": 30, "names": ["bucket"]}]}}

    def policy(self):
        return policy_from_manifest(self.manifest, self.state, self.home / "isolation")

    def scan(self, path):
        events = [{"type": "MSG", "msg": "envy-storage-candidate", "path": str(path)},
                  {"type": "REPORT", "error_count": 0, "success_count": 1}]
        return subprocess.CompletedProcess([], 0, "\n".join(json.dumps(e) for e in events), "")

    def plan(self, policy, path=None):
        with patch.object(planner, "run_process", return_value=self.scan(path or self.bucket)):
            return planner.plan(policy, self.state)

    def test_directory_candidate_is_frozen_and_restored(self):
        policy = self.policy()
        plan = self.plan(policy)
        self.assertEqual(len(plan["candidates"]), 1)
        candidate = plan["candidates"][0]
        self.assertTrue(candidate["path"].endswith("bucket"))
        self.assertIn("treeHash", candidate)
        self.assertGreater(candidate["size"], 0)
        with patch.object(lifecycle, "assert_inactive"):
            lifecycle.execute(policy, self.state, plan["id"])
            self.assertFalse(self.bucket.exists())
            run = records(self.state, "runs")[0]
            target = self.home / "restored"
            result = lifecycle.restore(policy, self.state, run["id"], target)
        self.assertEqual(result["restored"], [str(self.bucket)])
        restored = target / self.bucket.relative_to("/") / "nested" / "blob.bin"
        self.assertEqual(restored.read_bytes(), b"x" * 4096)

    def test_changed_directory_is_never_moved(self):
        policy = self.policy()
        plan = self.plan(policy)
        (self.bucket / "late.bin").write_bytes(b"late")
        with patch.object(lifecycle, "assert_inactive"):
            batch = lifecycle.execute(policy, self.state, plan["id"])
        self.assertTrue(self.bucket.exists())
        batch_errors = batch["errors"]
        self.assertTrue(batch_errors)

    def test_protected_override_is_scoped_to_its_directory(self):
        from envy.storage.model import is_protected
        policy = self.policy()
        self.assertFalse(is_protected(self.bucket, policy))
        sibling = self.home / "Library" / "Application Support" / "Demo" / "Vault" / "secret"
        sibling.mkdir(parents=True)
        self.assertTrue(is_protected(sibling, policy))

    def test_override_must_name_a_nested_directory(self):
        self.manifest["storage"]["protectedOverrides"] = [
            str(self.home / "Library" / "Application Support" / "Demo")]
        with self.assertRaises(StorageError):
            self.policy()

    def test_directory_archive_is_rejected(self):
        self.manifest["storage"]["archive"] = {"repository": "sftp:test@nas:/a",
                                               "passwordFile": str(self.home / "pw")}
        self.manifest["storage"]["rules"][0]["action"] = "archive"
        with self.assertRaises(StorageError):
            self.policy()

    def test_running_process_marks_candidates_blocked(self):
        self.manifest["storage"]["rules"][0]["processes"] = ["python3"]
        policy = self.policy()
        with patch.object(planner, "active_processes", return_value=["python3"]):
            plan = self.plan(policy)
        self.assertTrue(plan["candidates"][0]["blocked"])
        with patch.object(lifecycle, "active_processes", return_value=["python3"]):
            with patch.object(lifecycle, "assert_inactive"):
                batch = lifecycle.execute(policy, self.state, plan["id"])
        self.assertTrue(self.bucket.exists())
        self.assertTrue(batch["errors"])

    def test_unmatched_glob_root_is_reported(self):
        self.manifest["storage"]["rules"][0]["path"] = str(self.data / "*" / "missing")
        self.manifest["storage"]["rules"][0]["optionalRoot"] = True
        plan = self.plan(self.policy())
        self.assertEqual(plan["candidates"], [])
        self.assertTrue(any("matched no roots" in entry["reason"] for entry in plan["skipped"]))

    def test_expand_resolves_wildcards(self):
        from envy.storage.model import expand
        first = self.data / "one"
        second = self.data / "two"
        first.mkdir()
        second.mkdir()
        self.assertLessEqual({first, second}, set(expand(str(self.data / "*"))))

    def test_maintenance_actions_require_policy_enablement(self):
        from envy.storage import maintenance
        policy = self.policy()
        self.assertEqual(policy.maintenance, ())
        with self.assertRaises(StorageError):
            maintenance.preview(policy, ["docker"])


class StorageAgeBoundaryTests(unittest.TestCase):
    """Freeze both clocks and exercise organize selection and envY revalidation."""

    NOW = 1_700_000_000
    MIN_AGE_DAYS = 30

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "Downloads"
        self.root.mkdir()
        self.state = self.home / "state"
        self.cutoff = self.NOW - self.MIN_AGE_DAYS * 86400

    def policy(self, *, directories=False):
        rule = {"id": "age-boundary", "path": str(self.root), "action": "quarantine",
                "minAgeDays": self.MIN_AGE_DAYS, "depth": 1}
        if directories:
            rule.update(targets="dirs", recursiveAge=True)
        return policy_from_manifest(
            {"settings": {"envy.user.home": str(self.home)}, "storage": {"rules": [rule]}},
            self.state, self.home / "checkout")

    def file_at(self, name, mtime):
        path = self.root / name
        path.write_bytes(b"isolated age fixture")
        os.utime(path, (mtime, mtime))
        return path

    def tree_at(self, name, mtime):
        bucket = self.root / name
        nested = bucket / "nested"
        nested.mkdir(parents=True)
        blob = nested / "blob.bin"
        blob.write_bytes(b"isolated subtree fixture")
        for path in (blob, nested, bucket):
            os.utime(path, (mtime, mtime))
        return bucket, nested, blob

    def age_cases(self, *, directories=False):
        paths = []
        for name, offset in (("too-young", 1), ("at-boundary", 0), ("old-enough", -1)):
            mtime = self.cutoff + offset
            path = self.tree_at(name, mtime)[0] if directories else self.file_at(name, mtime)
            self.assertEqual(path.stat().st_mtime, mtime)
            paths.append(path)
        return paths

    def trees_with_recent_descendants(self):
        buckets = []
        for name, recent_index in (("recent-file", 2), ("recent-directory", 1)):
            paths = self.tree_at(name, self.cutoff - 60)
            recent = paths[recent_index]
            os.utime(recent, (self.NOW - 3600, self.NOW - 3600))
            # The parent must remain old: otherwise its own age could mask the bug.
            self.assertLess(paths[0].stat().st_mtime, self.cutoff)
            self.assertGreater(recent.stat().st_mtime, self.cutoff)
            buckets.append(paths[0])
        cold = self.tree_at("entirely-old", self.cutoff - 60)[0]
        return [*buckets, cold]

    def plan_reported_paths(self, paths, *, directories=False):
        # Pretend organize reports everything so the independent envY age gate runs.
        events = [{"type": "MSG", "msg": "envy-storage-candidate", "path": str(path)}
                  for path in paths]
        events.append({"type": "REPORT", "error_count": 0})
        scan = subprocess.CompletedProcess([], 0, "\n".join(json.dumps(e) for e in events), "")
        with patch.object(planner.time, "time", return_value=self.NOW), patch.object(
            planner, "run_process", return_value=scan
        ):
            return planner.plan(self.policy(directories=directories), self.state)

    def plan_with_real_organize(self, *, directories=False):
        clock = self.home / "clock"
        clock.mkdir()
        # A child-only import hook freezes organize's time and Arrow comparison clock.
        (clock / "sitecustomize.py").write_text(
            "import builtins, time\n"
            f"time.time = lambda: {self.NOW!r}\n"
            "original_import = builtins.__import__\n"
            "def frozen_import(name, *args, **kwargs):\n"
            "    module = original_import(name, *args, **kwargs)\n"
            "    if name == 'arrow':\n"
            f"        module.now = lambda *a, **kw: module.get({self.NOW!r})\n"
            "    return module\n"
            "builtins.__import__ = frozen_import\n"
        )
        environment = dict(os.environ, HOME=str(self.home), TZ="UTC",
                           XDG_CONFIG_HOME=str(self.home / ".config"),
                           PYTHONPATH=str(clock) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        real_run = planner.run_process
        reported = []

        def scan(argv, **kwargs):
            result = real_run(argv, env=environment, **kwargs)
            self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
            self.assertNotIn("Error in sitecustomize", result.stderr or "")
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            reported.extend(event["path"] for event in events
                            if event.get("type") == "MSG"
                            and event.get("msg") == "envy-storage-candidate")
            return result

        with patch.object(planner.time, "time", return_value=self.NOW), patch.object(
            planner, "run_process", side_effect=scan
        ):
            plan = planner.plan(self.policy(directories=directories), self.state)
        return plan, reported

    def test_file_age_revalidation_before_at_and_after_threshold(self):
        paths = self.age_cases()
        plan = self.plan_reported_paths(paths)
        self.assertEqual({entry["path"] for entry in plan["candidates"]},
                         {str(path) for path in paths[1:]})

    @unittest.skipUnless(shutil.which("organize"), "real organize not on PATH")
    def test_real_organize_file_age_before_at_and_after_threshold(self):
        paths = self.age_cases()
        plan, reported = self.plan_with_real_organize()
        # organize's native "older" is strict; equality is conservatively excluded.
        expected = {str(paths[2])}
        self.assertEqual(set(reported), expected)
        self.assertEqual({entry["path"] for entry in plan["candidates"]}, expected)

    def test_recursive_age_revalidation_before_at_and_after_threshold(self):
        paths = self.age_cases(directories=True)
        plan = self.plan_reported_paths(paths, directories=True)
        self.assertEqual({entry["path"] for entry in plan["candidates"]},
                         {str(path) for path in paths[1:]})

    @unittest.skipUnless(shutil.which("organize"), "real organize not on PATH")
    def test_real_organize_recursive_age_before_at_and_after_threshold(self):
        paths = self.age_cases(directories=True)
        plan, reported = self.plan_with_real_organize(directories=True)
        expected = {str(path) for path in paths[1:]}
        self.assertEqual(set(reported), expected)
        self.assertEqual({entry["path"] for entry in plan["candidates"]}, expected)

    def test_old_parents_with_recent_descendants_fail_candidate_revalidation(self):
        paths = self.trees_with_recent_descendants()
        plan = self.plan_reported_paths(paths, directories=True)
        self.assertEqual([entry["path"] for entry in plan["candidates"]], [str(paths[2])])
        self.assertTrue(all(path.exists() for path in paths))

    @unittest.skipUnless(shutil.which("organize"), "real organize not on PATH")
    def test_real_organize_rejects_old_parents_with_recent_descendants_before_plan(self):
        paths = self.trees_with_recent_descendants()
        plan, reported = self.plan_with_real_organize(directories=True)
        self.assertEqual(reported, [str(paths[2])])
        self.assertEqual([entry["path"] for entry in plan["candidates"]], [str(paths[2])])
        self.assertTrue(all(path.exists() for path in paths))


class StorageSelectionTests(unittest.TestCase):
    """The engine consumes evaluated selections without merging machine policy."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.state = self.home / "state"
        self.root = self.home / "Downloads"
        self.root.mkdir()
        self.file = self.root / "old.bin"
        self.file.write_bytes(b"fixture")
        old = time.time() - 100 * 86400
        os.utime(self.file, (old, old))
        self.rule = {"id": "downloads", "path": str(self.root), "action": "quarantine",
                     "minAgeDays": 60, "source": "default:common", "optionalRoot": True,
                     "overriddenFields": ["minAgeDays"]}
        self.selection = {"include": {"downloads": dict(self.rule)}, "exclude": ["disabled"],
                          "effective": [self.rule]}
        self.manifest = {"settings": {"envy.user.home": str(self.home)},
                         "storage": {"rules": self.selection}}

    def policy(self):
        return policy_from_manifest(self.manifest, self.state, self.home / "checkout")

    def output(self, *paths):
        events = [{"type": "MSG", "msg": "envy-storage-candidate", "path": str(path)}
                  for path in paths]
        events.append({"type": "REPORT", "error_count": 0})
        return subprocess.CompletedProcess([], 0, "\n".join(json.dumps(e) for e in events), "")

    def invoke(self, arguments):
        output = io.StringIO()
        with patch.object(cli, "_context", return_value=(self.policy(), self.state)), patch.object(
            cli.log, "console", Console(file=output, force_terminal=False)
        ):
            result = CliRunner().invoke(cli.app, arguments)
        return result, json.loads(output.getvalue())

    def test_prune_defaults_preserve_legacy_and_positional_rules(self):
        legacy = Rule("legacy", str(self.root), "quarantine", "files", 1, (), False,
                      (), (), 60, 0, (), True, "default:common", ("minAgeDays",))
        self.assertFalse(legacy.prune)
        self.assertEqual(legacy.overridden_fields, ("minAgeDays",))
        self.assertFalse(self.policy().rules[0].prune)
        self.assertFalse(rule_details(self.policy().rules[0])["prune"])
        self.manifest["storage"]["rules"] = [self.rule]
        self.assertFalse(self.policy().rules[0].prune)
        self.rule["prune"] = True
        self.assertTrue(self.policy().rules[0].prune)

    def test_prune_boolean_changes_details_and_policy_fingerprint(self):
        default = self.policy()
        self.rule["prune"] = False
        self.assertEqual(default.fingerprint, self.policy().fingerprint)
        self.rule.update(prune=True, overriddenFields=["minAgeDays", "prune"])
        enabled = self.policy()
        self.assertTrue(enabled.rules[0].prune)
        self.assertTrue(rule_details(enabled.rules[0])["prune"])
        self.assertEqual(enabled.rules[0].action, "quarantine")
        self.rule["prune"] = False
        self.assertNotEqual(enabled.fingerprint, self.policy().fingerprint)

    def test_prune_rejects_non_boolean_values(self):
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                self.rule["prune"] = value
                with self.assertRaisesRegex(StorageError, "prune must be a boolean"):
                    self.policy()

    def test_single_field_override_is_consumed_from_effective(self):
        policy = self.policy()
        self.assertEqual(policy.rules[0].min_age_days, 60)
        self.assertEqual(policy.excluded, ("disabled",))
        with patch.object(planner, "run_process", return_value=self.output(self.file)):
            plan = planner.plan(policy, self.state)
        self.assertEqual(plan["excluded"], ["disabled"])
        self.assertEqual(plan["rules"][0]["path"], str(self.root))
        self.assertEqual(plan["rules"][0]["source"], "default:common")
        self.assertEqual(plan["candidates"][0]["overriddenFields"], ["minAgeDays"])
        with patch.object(planner, "run_process", return_value=self.output()):
            result = planner.validate(policy, self.state)
        self.assertEqual(result["rules"][0]["overriddenFields"], ["minAgeDays"])

    def test_only_effective_rules_grant_protected_overrides(self):
        from envy.storage.model import is_protected
        cache = self.home / "Library/Application Support/Demo/Cache"
        cache.mkdir(parents=True)
        self.selection["include"]["disabled"] = {"protectedOverrides": [str(cache)]}
        self.assertTrue(is_protected(cache, self.policy()))
        self.rule["protectedOverrides"] = [str(cache)]
        self.assertFalse(is_protected(cache, self.policy()))
        self.selection["effective"] = []
        self.assertTrue(is_protected(cache, self.policy()))

    def test_source_does_not_grant_permission_or_optional_roots(self):
        from envy.storage.model import is_protected
        del self.rule["optionalRoot"]
        self.assertFalse(self.policy().rules[0].optional_root)
        self.assertTrue(is_protected(self.home / "Library/Application Support/Demo/Cache", self.policy()))

    def test_invalid_fields_are_rejected(self):
        for field, value in [("optionalRoot", 1), ("minAgeDays", True), ("depth", False),
                             ("source", []), ("overriddenFields", [1]),
                             ("protectedOverrides", "all"), ("unknown", True),
                             ("extensions", ["*.bin"]), ("action", [])]:
            with self.subTest(field=field):
                original = dict(self.rule)
                self.rule[field] = value
                with self.assertRaises(StorageError):
                    self.policy()
                self.rule.clear()
                self.rule.update(original)
        self.selection["exclude"] = ["downloads"]
        with self.assertRaises(StorageError):
            self.policy()

    def test_optional_missing_and_unmatched_roots_skip_validation(self):
        for suffix in ["missing", "*/missing"]:
            self.rule["path"] = str(self.root / suffix)
            with patch.object(planner, "run_process") as run:
                result = planner.validate(self.policy(), self.state)
                plan = planner.plan(self.policy(), self.state)
            self.assertTrue(result["ok"])
            self.assertTrue(result["rules"][0]["skipped"])
            self.assertTrue(result["rules"][0]["reason"])
            self.assertTrue(plan["skipped"])
            run.assert_not_called()

    def test_custom_missing_roots_fail_validation_and_plan(self):
        self.rule.update(source="machine", optionalRoot=False)
        for suffix in ["missing", "*/missing"]:
            self.rule["path"] = str(self.root / suffix)
            self.assertFalse(planner.validate(self.policy(), self.state)["ok"])
            with self.assertRaisesRegex(StorageError, "downloads"):
                planner.plan(self.policy(), self.state)

    def test_permission_failures_are_not_optional_skips(self):
        policy = self.policy()
        for target in ["expand", "no_symlinks"]:
            with self.subTest(target=target), patch.object(
                planner, target, side_effect=PermissionError("fixture inspection denied")
            ):
                result = planner.validate(policy, self.state)
                self.assertFalse(result["ok"])
                self.assertFalse(result["rules"][0].get("skipped", False))
                with self.assertRaises(StorageError):
                    planner.plan(policy, self.state)
        with patch("envy.storage.model.os.scandir", side_effect=PermissionError("denied")):
            self.rule["path"] = str(self.root / "*")
            self.assertFalse(planner.validate(self.policy(), self.state)["ok"])
            with self.assertRaises(StorageError):
                planner.plan(self.policy(), self.state)

    def test_same_path_from_different_rules_is_rejected(self):
        self.selection["effective"].append(dict(self.rule, id="second"))
        with patch.object(planner, "run_process", return_value=self.output(self.file)):
            with self.assertRaisesRegex(StorageError, "downloads and second"):
                planner.plan(self.policy(), self.state)
        self.assertEqual(records(self.state, "plans"), [])

    def test_parent_child_candidates_and_stored_plans_are_rejected(self):
        directory = self.root / "cache"
        directory.mkdir()
        nested = directory / "nested.bin"
        nested.write_bytes(b"fixture")
        old = time.time() - 100 * 86400
        os.utime(directory, (old, old))
        os.utime(nested, (old, old))
        self.rule.update(targets="dirs")
        self.selection["effective"].append(dict(self.rule, id="child", path=str(directory), targets="files"))
        policy = self.policy()
        with patch.object(planner, "run_process", side_effect=[self.output(directory), self.output(nested)]):
            with self.assertRaisesRegex(StorageError, "downloads and child"):
                planner.plan(policy, self.state)
        from envy.storage.model import MODEL_VERSION
        record = {"id": uuid.uuid4().hex, "modelVersion": MODEL_VERSION,
                  "createdAt": time.time(), "policy": policy.fingerprint,
                  "candidates": [{"path": str(directory), "rule": "downloads"},
                                 {"path": str(nested), "rule": "child"}]}
        write_record(self.state, "plans", record)
        with self.assertRaisesRegex(StorageError, "downloads and child"):
            lifecycle.execute(policy, self.state, record["id"])
        self.assertTrue(nested.exists())
        self.assertEqual(records(self.state, "runs"), [])

    def test_old_plan_model_is_invalid_even_with_current_policy_hash(self):
        with patch.object(planner, "run_process", return_value=self.output(self.file)):
            record = planner.plan(self.policy(), self.state)
        del record["modelVersion"]
        write_record(self.state, "plans", record)
        with self.assertRaises(StorageError):
            lifecycle.execute(self.policy(), self.state, record["id"])
        self.assertTrue(self.file.exists())

    def test_validate_json_failure_sets_envelope_and_exit_status(self):
        self.rule.update(path=str(self.root / "missing"), optionalRoot=False)
        result, envelope = self.invoke(["validate", "--json"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertFalse(envelope["ok"])
        self.assertFalse(envelope["data"]["ok"])
        self.assertEqual(envelope["schemaVersion"], 1)
        self.assertEqual(envelope["command"], "storage validate")
        self.assertIn("error", envelope)

    def test_status_reports_full_rule_and_local_archive_readiness_without_remote_calls(self):
        password = self.home / "password"
        self.manifest["storage"]["archive"] = {"repository": "sftp:fixture@nas:/archive",
                                                "passwordFile": str(password)}
        with patch.object(backend.Archive, "command", side_effect=AssertionError("remote access")):
            result, envelope = self.invoke(["status", "--json"])
            self.assertEqual(result.exit_code, 0, result.output)
            data = envelope["data"]
            self.assertTrue(data["archiveConfigured"])
            self.assertFalse(data["archive"]["repositoryChecked"])
            self.assertFalse(data["archive"]["passwordFile"]["exists"])
            self.assertEqual(data["excluded"], ["disabled"])
            self.assertEqual(set(data["ruleDetails"][0]), {
                "id", "path", "action", "targets", "depth", "names", "recursiveAge", "processes",
                "extensions", "minAgeDays", "minSizeBytes", "protectedOverrides", "optionalRoot",
                "source", "overriddenFields", "prune"})
            password.write_bytes(b"fixture")
            for mode, private in [(0o600, True), (0o644, False)]:
                password.chmod(mode)
                result, envelope = self.invoke(["status", "--json"])
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(envelope["data"]["archive"]["passwordFile"]["private"], private)

    def test_native_maintenance_flags_are_forwarded_without_execution(self):
        with patch.object(cli.maintenance, "preview", return_value={"items": [], "execute": False}) as preview:
            result, envelope = self.invoke(["maintain", "--uv", "--npm", "--pip", "--brew", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(envelope["ok"])
        self.assertEqual(preview.call_args.args[1], ["uv", "npm", "pip", "brew"])

    def test_subtree_permission_failure_rejects_entire_plan(self):
        directory = self.root / "cache"
        directory.mkdir()
        old = time.time() - 100 * 86400
        os.utime(directory, (old, old))
        self.rule.update(targets="dirs")
        with patch.object(planner, "run_process", return_value=self.output(directory)), patch.object(
            planner, "tree_digest", side_effect=PermissionError("nested subtree denied")
        ):
            with self.assertRaisesRegex(StorageError, "downloads"):
                planner.plan(self.policy(), self.state)
        self.assertEqual(records(self.state, "plans"), [])

    def test_directory_activity_probe_covers_descendants(self):
        with patch.object(planner, "run_process", return_value=subprocess.CompletedProcess([], 0, "123", "")) as run:
            with self.assertRaises(StorageError):
                planner.assert_inactive(self.root, ancestors=False)
        self.assertEqual(run.call_args.args[0][1:], ["-t", "+D", str(self.root)])

    def test_subtree_digest_rejects_hard_links_and_walk_failures(self):
        from envy.storage.model import tree_digest
        os.link(self.file, self.root / "duplicate.bin")
        with self.assertRaisesRegex(StorageError, "hard links"):
            tree_digest(self.root)
        def failed_walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("nested directory denied"))
            return iter(())
        with patch("envy.storage.model.os.walk", side_effect=failed_walk):
            with self.assertRaises(PermissionError):
                tree_digest(self.root)

    def test_directory_containing_protected_descendant_is_skipped(self):
        self.rule.update(targets="dirs")
        directory = self.root / "cache"
        directory.mkdir()
        protected = directory / "vault"
        protected.mkdir()
        self.manifest["storage"]["protectedPaths"] = [str(protected)]
        old = time.time() - 100 * 86400
        os.utime(directory, (old, old))
        with patch.object(planner, "run_process", return_value=self.output(directory)):
            result = planner.plan(self.policy(), self.state)
        self.assertEqual(result["candidates"], [])
        self.assertIn("protected", result["skipped"][0]["reason"].lower())

    def test_validate_organize_failure_is_json_failure(self):
        with patch.object(planner, "run_process", return_value=subprocess.CompletedProcess([], 1, "", "invalid config")):
            result, envelope = self.invoke(["validate", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["data"]["rules"][0]["error"], "invalid config")

    def test_explicit_yes_semantics_are_preserved_in_report_mode(self):
        self.manifest["storage"]["mode"] = "report"
        with patch.object(planner, "plan", return_value={"id": "fixture"}), patch.object(
            lifecycle, "execute", return_value={"errors": []}
        ) as execute:
            result, envelope = self.invoke(["run", "--yes", "--json"])
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(envelope["ok"])
        execute.assert_called_once()

    def test_catalog_internal_glob_overrides_expand_for_effective_and_legacy_rules(self):
        from envy.storage.model import is_protected
        application = self.home / "Library/Application Support"
        patterns = [application / "Lark/users/*/Service Worker/CacheStorage",
                    application / "WeChat/wechatprofiles/*/Cache"]
        caches = [application / "Lark/users/alice/Service Worker/CacheStorage",
                  application / "Lark/users/bob/Service Worker/CacheStorage",
                  application / "WeChat/wechatprofiles/profile-one/Cache"]
        for cache in caches:
            cache.mkdir(parents=True)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.rule.pop("protectedOverrides", None)
                self.manifest["storage"].pop("protectedOverrides", None)
                if legacy:
                    self.manifest["storage"]["rules"] = [self.rule]
                    self.manifest["storage"]["protectedOverrides"] = list(map(str, patterns))
                else:
                    self.manifest["storage"]["rules"] = self.selection
                    self.rule["protectedOverrides"] = list(map(str, patterns))
                policy = self.policy()
                self.assertEqual(set(policy.overrides), set(caches))
                for cache in caches:
                    self.assertFalse(is_protected(cache / "entry", policy))
                    self.assertTrue(is_protected(cache.parent, policy))
                self.assertTrue(is_protected(application / "Lark/users/alice/Vault", policy))
        self.manifest["storage"].pop("protectedOverrides")
        self.manifest["storage"]["rules"] = self.selection
        self.selection["include"]["disabled"] = {"protectedOverrides": list(map(str, patterns))}
        self.selection["effective"] = []
        self.assertEqual(self.policy().overrides, ())

    def test_glob_overrides_reject_broad_patterns_and_propagate_permissions(self):
        application = self.home / "Library/Application Support"
        for pattern in [application, application / "Lark", application / "Lark/users/*",
                        application / "Lark/users/*/Cache?", application / "Lark/users/*/[Cc]ache"]:
            with self.subTest(pattern=pattern):
                self.rule["protectedOverrides"] = [str(pattern)]
                with self.assertRaises(StorageError):
                    self.policy()
        pattern = application / "Lark/users/*/Service Worker/CacheStorage"
        for legacy in (False, True):
            self.rule.pop("protectedOverrides", None)
            self.manifest["storage"].pop("protectedOverrides", None)
            if legacy:
                self.manifest["storage"]["rules"] = [self.rule]
                self.manifest["storage"]["protectedOverrides"] = [str(pattern)]
            else:
                self.manifest["storage"]["rules"] = self.selection
                self.rule["protectedOverrides"] = [str(pattern)]
            (application / "Lark/users").mkdir(parents=True, exist_ok=True)
            with self.subTest(legacy=legacy), patch(
                "envy.storage.model.os.scandir", side_effect=PermissionError("fixture override denied")
            ):
                with self.assertRaises(PermissionError):
                    self.policy()

    def test_unmatched_internal_glob_grants_no_override(self):
        self.rule["protectedOverrides"] = [str(self.home / "Library/Application Support/Lark/users/*/Cache")]
        self.assertEqual(self.policy().overrides, ())

    def test_ancestors_of_state_checkout_and_secrets_are_protected(self):
        from envy.storage.model import is_protected
        for protected in (self.state, self.home / "checkout", self.home / ".config/sops",
                          self.home / ".ssh", self.home / "Library/Keychains"):
            with self.subTest(protected=protected):
                policy = self.policy()
                self.assertTrue(is_protected(protected, policy))
                self.assertTrue(is_protected(protected.parent, policy))
                self.assertTrue(is_protected(protected / "child", policy))
        parent = self.root / "parent"
        protected = parent / "vault"
        protected.mkdir(parents=True)
        self.manifest["storage"]["protectedPaths"] = [str(protected)]
        self.assertTrue(is_protected(parent, self.policy()))
        self.assertTrue(is_protected(protected / "child", self.policy()))
        self.assertFalse(is_protected(parent / "sibling", self.policy()))
        self.rule.update(targets="dirs")
        old = time.time() - 100 * 86400
        os.utime(parent, (old, old))
        with patch.object(planner, "run_process", return_value=self.output(parent)):
            result = planner.plan(self.policy(), self.state)
        self.assertEqual(result["candidates"], [])
        self.assertIn("Protected", result["skipped"][0]["reason"])

    def test_cache_override_does_not_bypass_nested_state_or_explicit_protection(self):
        from envy.storage.model import is_protected
        cache = self.home / "Library/Application Support/Lark/Cache"
        cache.mkdir(parents=True)
        self.rule["protectedOverrides"] = [str(cache)]
        self.manifest["storage"]["protectedPaths"] = [str(cache / "private/vault")]
        policy = policy_from_manifest(self.manifest, cache / "state", cache / "checkout")
        for path in [cache, cache.parent, cache / "private", cache / "private/vault/secret",
                     cache / "state", cache / "state/record", cache / "checkout/file"]:
            with self.subTest(path=path):
                self.assertTrue(is_protected(path, policy))
        self.assertFalse(is_protected(cache / "unprotected", policy))

    def test_status_accepts_private_sops_symlink_without_reading_secret_or_nas(self):
        password = self.home / "secrets-generation/password"
        password.parent.mkdir()
        password.write_bytes(b"fixture")
        password.chmod(0o600)
        link = self.home / "sops-password"
        link.symlink_to(password)
        self.manifest["storage"]["archive"] = {"repository": "sftp:fixture@nas:/archive",
                                                "passwordFile": str(link)}
        with patch.object(backend.Archive, "command", side_effect=AssertionError("remote access")), patch.object(
            Path, "read_bytes", side_effect=AssertionError("secret read")
        ), patch.object(Path, "read_text", side_effect=AssertionError("secret read")):
            result, envelope = self.invoke(["status", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(envelope["data"]["archive"]["passwordFile"],
                         {"configured": True, "exists": True, "private": True})
        self.assertFalse(envelope["data"]["archive"]["repositoryChecked"])
        password.chmod(0o644)
        self.assertFalse(cli._password_status(str(link))["private"])

    def test_status_password_owner_matches_backend_and_resolution_fails_closed(self):
        from types import SimpleNamespace
        import stat
        password = self.home / "password"
        password.write_bytes(b"fixture")
        for owner, private in [(0, True), (os.getuid(), True), (os.getuid() + 10000, False)]:
            with self.subTest(owner=owner), patch.object(Path, "resolve", return_value=password), patch.object(
                Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=owner)
            ):
                self.assertEqual(cli._password_status(str(password))["private"], private)
        for error in [PermissionError("fixture denied"), RuntimeError("symlink loop")]:
            with self.subTest(error=error), patch.object(Path, "resolve", side_effect=error):
                status = cli._password_status(str(password))
                self.assertIsNone(status["exists"])
                self.assertFalse(status["private"])
                self.assertIn("error", status)

    def test_darwin_sops_age_directory_cannot_be_exempted(self):
        from envy.storage.model import is_protected
        sops = self.home / "Library/Application Support/sops"
        age = sops / "age"
        age.mkdir(parents=True)
        self.rule["protectedOverrides"] = [str(age)]
        policy = self.policy()
        self.assertIn(sops, policy.mandatory)
        self.assertIn(age, policy.overrides)
        for path in [sops.parent, sops, age, age / "keys.txt"]:
            with self.subTest(path=path):
                self.assertTrue(is_protected(path, policy))
        self.manifest["storage"]["protectedOverrides"] = [str(age)]
        self.rule.pop("protectedOverrides")
        self.assertTrue(is_protected(age / "keys.txt", self.policy()))


if __name__ == "__main__":
    unittest.main()

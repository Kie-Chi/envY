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
from envy.storage.model import StorageError, policy_from_manifest
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

    def test_local_archive_repository_is_protected_from_rules(self):
        repository = self.root / "repository"
        repository.mkdir()
        pack = repository / "pack.dmg"
        pack.write_text("archive data")
        old = time.time() - 40 * 86400
        os.utime(pack, (old, old))
        self.manifest["storage"]["archive"] = {"repository": str(repository)}
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


if __name__ == "__main__":
    unittest.main()

"""Prune regressions exercise isolated files and mocked external boundaries."""

import copy
import io
import json
import os
import pty
import re
import shutil
import subprocess
import tempfile
import termios
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, patch

from rich.console import Console
from typer.testing import CliRunner

from envy import log
from envy.storage import backend, cli, lifecycle, maintenance, planner, pruning
from envy.storage.model import StorageError, policy_from_manifest
from envy.storage.state import read_record, records, write_record


class PruneFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / "cache"
        self.root.mkdir()
        self.state = self.home / "state"
        self.manifest = {"id": "fixture", "platform": "darwin",
                         "settings": {"envy.user.home": str(self.home)}, "storage": {
            "mode": "manual", "quarantineDays": 30, "archive": {}, "rules": [{
                "id": "cache", "path": str(self.root), "action": "quarantine",
                "prune": True, "minAgeDays": 30}]}}
        self.file = self.old_file("old cache.bin")
        self.policy = self.load_policy()
        # Only external activity probes are replaced; file validation stays real.
        for owner, name in ((lifecycle, "assert_inactive"), (planner, "assert_inactive"),
                            (lifecycle, "active_processes"), (planner, "active_processes")):
            context = patch.object(owner, name, return_value=[])
            context.start()
            self.addCleanup(context.stop)
        external = patch.object(backend.Archive, "command",
                                side_effect=AssertionError("Unexpected real NAS command"))
        external.start()
        self.addCleanup(external.stop)

    def load_policy(self):
        return policy_from_manifest(self.manifest, self.state, self.home / "checkout")

    def old_file(self, name, content=b"disposable fixture"):
        path = self.root / name
        path.write_bytes(content)
        old = time.time() - 40 * 86400
        os.utime(path, (old, old))
        return path

    def prepare(self, paths=None):
        paths = [self.file] if paths is None else paths
        events = [{"type": "MSG", "path": str(path), "msg": "envy-storage-candidate"}
                  for path in paths]
        events.append({"type": "REPORT", "error_count": 0})
        output = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with patch.object(planner, "run_process", return_value=output):
            return pruning.prepare(self.policy, self.state)

    def execute(self, prepared):
        return pruning.execute(self.policy, self.state, prepared)

    def archive_policy(self):
        self.manifest["storage"]["archive"] = {
            "repository": "sftp:fixture@nas.invalid:/archives",
            "passwordFile": str(self.home / "password")}
        self.manifest["storage"]["rules"][0].update(action="archive", prune=False)
        self.policy = self.load_policy()

    def test_prepare_leaves_sources_and_deletion_records_untouched(self):
        before = self.file.read_bytes()
        prepared = self.prepare()
        self.assertEqual(prepared["summary"]["eligibleItems"], 1)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(records(self.state, "prunes"), [])
        self.assertFalse((self.state / "prune-staging").exists())

    def test_cache_is_deleted_immediately_with_separate_durable_record(self):
        result = self.execute(self.prepare())
        self.assertFalse(self.file.exists())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["deletedItems"], 1)
        self.assertEqual(result["deletedBytesEstimate"], len(b"disposable fixture"))
        self.assertFalse(Path(result["items"][0]["heldPath"]).exists())
        self.assertEqual(read_record(self.state, "prunes", result["id"]), result)
        self.assertEqual(records(self.state, "runs"), [])
        record = self.state / "prunes" / (result["id"] + ".json")
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

    def test_unqualified_rules_are_never_scanned_or_deleted(self):
        self.policy = replace(self.policy, rules=tuple(replace(rule, prune=False)
                                                      for rule in self.policy.rules))
        with patch.object(planner, "run_process") as scan:
            prepared = pruning.prepare(self.policy, self.state)
        scan.assert_not_called()
        self.assertEqual(prepared["candidates"], [])
        result = self.execute(prepared)
        self.assertEqual(result["summary"]["deletedItems"], 0)
        self.assertTrue(self.file.exists())

    def test_young_cache_is_deleted_but_undersized_files_are_retained(self):
        young = self.root / "young.bin"
        young.write_bytes(b"new rebuildable cache")
        small = self.old_file("small.bin", b"x")
        self.policy = replace(self.policy, rules=tuple(replace(rule, min_size_bytes=5)
                                                      for rule in self.policy.rules))
        result = self.execute(self.prepare([young, small, self.file]))
        self.assertFalse(young.exists())
        self.assertTrue(small.exists())
        self.assertFalse(self.file.exists())
        self.assertEqual(result["summary"]["deletedItems"], 2)

    def test_prune_ignores_future_timestamps_without_changing_run_age(self):
        future = time.time() + 86400
        os.utime(self.file, (future, future))
        prepared = self.prepare()
        self.assertEqual(prepared["summary"]["eligibleItems"], 1)
        self.assertEqual(self.policy.rules[0].min_age_days, 30)
        with patch.object(planner, "run_process", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"type": "MSG", "path": str(self.file),
                                   "msg": "envy-storage-candidate"}) + "\n" +
                json.dumps({"type": "REPORT", "error_count": 0}), "")):
            normal = planner.plan(self.policy, self.state)
        self.assertEqual(normal["candidates"], [])
        with self.assertRaisesRegex(StorageError, "policy changed"):
            lifecycle.execute(self.policy, self.state, prepared["planId"])
        self.assertEqual(self.execute(prepared)["summary"]["deletedItems"], 1)

    @unittest.skipUnless(shutil.which("organize"), "Requires organize")
    def test_real_organize_selects_recent_files_without_name_or_age_filters(self):
        os.utime(self.file, None)
        environment = dict(os.environ, HOME=str(self.home),
                           XDG_CONFIG_HOME=str(self.home / ".config"))
        real_run = planner.run_process

        def scan(argv, **kwargs):
            result = real_run(argv, env=environment, **kwargs)
            self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
            return result

        with patch.object(planner, "run_process", side_effect=scan):
            prepared = pruning.prepare(self.policy, self.state)
        self.assertEqual([item["path"] for item in prepared["candidates"]], [str(self.file)])
        self.assertEqual(self.execute(prepared)["summary"]["deletedItems"], 1)
        self.assertFalse(self.file.exists())

    def test_archive_keeps_its_age_cutoff_even_with_prune_enabled(self):
        self.archive_policy()
        self.manifest["storage"]["rules"][0]["prune"] = True
        self.policy = self.load_policy()
        os.utime(self.file, None)
        prepared = self.prepare()
        self.assertEqual(prepared["candidates"], [])
        self.assertEqual(self.execute(prepared)["summary"]["deletedItems"], 0)
        self.assertTrue(self.file.exists())

    def test_symlinks_and_hardlinks_are_refused(self):
        symbolic = self.root / "symbolic.bin"
        symbolic.symlink_to(self.file)
        hard = self.root / "hard.bin"
        os.link(self.file, hard)
        result = self.execute(self.prepare([symbolic, hard, self.file]))
        self.assertEqual(result["summary"]["deletedItems"], 0)
        self.assertTrue(symbolic.is_symlink())
        self.assertTrue(hard.exists())
        self.assertTrue(self.file.exists())

    def test_changed_source_is_skipped_without_an_error(self):
        prepared = self.prepare()
        self.file.write_bytes(b"replacement must survive")
        result = self.execute(prepared)
        self.assertEqual(self.file.read_bytes(), b"replacement must survive")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["skippedItems"], 1)

    def test_disappeared_source_is_skipped(self):
        prepared = self.prepare()
        self.file.unlink()
        result = self.execute(prepared)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["skippedItems"], 1)

    def test_process_starting_after_prepare_skips_source(self):
        prepared = self.prepare()
        with patch.object(lifecycle, "active_processes", return_value=["fixture"]):
            result = self.execute(prepared)
        self.assertTrue(self.file.exists())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["skippedItems"], 1)

    def test_open_file_is_not_deleted(self):
        prepared = self.prepare()
        with patch.object(lifecycle, "assert_inactive",
                          side_effect=StorageError("File is active", code="busy")):
            result = self.execute(prepared)
        self.assertTrue(self.file.exists())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["skippedItems"], 1)

    def test_activity_after_staging_preserves_held_data(self):
        prepared = self.prepare()
        with patch.object(planner, "active_processes", return_value=["fixture"]):
            result = self.execute(prepared)
        self.assertFalse(self.file.exists())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["items"][0]["status"], "held")
        self.assertEqual(Path(result["items"][0]["heldPath"]).read_bytes(), b"disposable fixture")
        self.assertEqual(result["summary"]["retainedItems"], 1)

    def test_recreated_source_after_staging_is_not_chased(self):
        prepared = self.prepare()
        stage = lifecycle._stage

        def recreate(candidate, destination):
            stage(candidate, destination)
            self.file.write_bytes(b"new application cache")

        with patch.object(lifecycle, "_stage", side_effect=recreate):
            result = self.execute(prepared)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.file.read_bytes(), b"new application cache")
        self.assertFalse(Path(result["items"][0]["heldPath"]).exists())

    def test_directory_contents_are_deleted_but_siblings_survive(self):
        directory = self.root / "old-directory"
        directory.mkdir()
        (directory / "nested").mkdir()
        (directory / "nested" / "data").write_bytes(b"directory fixture")
        old = time.time() - 40 * 86400
        os.utime(directory, (old, old))
        self.policy = replace(self.policy, rules=tuple(replace(rule, targets="dirs")
                                                      for rule in self.policy.rules))
        result = self.execute(self.prepare([directory]))
        self.assertEqual(result["errors"], [])
        self.assertFalse(directory.exists())
        self.assertTrue(self.file.exists())

    def test_directory_containing_symlink_preserves_external_target(self):
        directory = self.root / "unsafe-directory"
        directory.mkdir()
        (directory / "escape").symlink_to(self.file)
        old = time.time() - 40 * 86400
        os.utime(directory, (old, old))
        self.policy = replace(self.policy, rules=tuple(replace(rule, targets="dirs")
                                                      for rule in self.policy.rules))
        result = self.execute(self.prepare([directory]))
        self.assertEqual(result["summary"]["deletedItems"], 0)
        self.assertTrue(directory.exists())
        self.assertTrue(self.file.exists())

    def test_verified_nas_batch_is_backed_up_once_and_released_immediately(self):
        self.archive_policy()
        second = self.old_file("second.bin", b"second archive")
        digests = {str(path): planner.file_hash(path) for path in (self.file, second)}
        prepared = self.prepare([self.file, second])

        def read_back(snapshot, source):
            self.assertEqual(snapshot, "snapshot-fixture")
            self.assertTrue(Path(source).exists())
            return digests[source]

        with patch.object(backend.Archive, "identity", return_value="repository-fixture"), \
                patch.object(backend.Archive, "backup", return_value="snapshot-fixture") as backup, \
                patch.object(backend.Archive, "content_hash", side_effect=read_back) as verify:
            result = self.execute(prepared)
        backup.assert_called_once()
        self.assertCountEqual(backup.call_args.args[0], list(digests))
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["archivedItems"], 2)
        self.assertFalse(self.file.exists())
        self.assertFalse(second.exists())
        self.assertTrue(all(not Path(item["heldPath"]).exists() for item in result["items"]))
        self.assertEqual(records(self.state, "runs"), [])

    def test_nas_backup_failure_preserves_source(self):
        self.archive_policy()
        with patch.object(backend.Archive, "identity", return_value="repository-fixture"), \
                patch.object(backend.Archive, "backup", side_effect=StorageError("NAS unavailable")):
            result = self.execute(self.prepare())
        self.assertTrue(result["errors"])
        self.assertEqual(result["state"], "partial")
        self.assertTrue(self.file.exists())

    def test_nas_readback_mismatch_preserves_source_and_snapshot_record(self):
        self.archive_policy()
        with patch.object(backend.Archive, "identity", return_value="repository-fixture"), \
                patch.object(backend.Archive, "backup", return_value="snapshot-fixture"), \
                patch.object(backend.Archive, "content_hash", return_value="incorrect-digest"):
            result = self.execute(self.prepare())
        self.assertTrue(result["errors"])
        self.assertTrue(self.file.exists())
        self.assertEqual(read_record(self.state, "prunes", result["id"])["snapshot"], "snapshot-fixture")

    def test_prune_does_not_purge_old_quarantine_even_when_expired(self):
        identifier = "a" * 32
        held = self.state / "quarantine" / identifier / "0"
        held.parent.mkdir(parents=True)
        held.write_bytes(b"previous quarantine")
        old_run = {"id": identifier, "items": [{"status": "quarantined", "heldPath": str(held),
                                             "expiresAt": 0}], "errors": []}
        write_record(self.state, "runs", old_run)
        self.execute(self.prepare())
        self.assertEqual(held.read_bytes(), b"previous quarantine")
        self.assertEqual(read_record(self.state, "runs", identifier), old_run)

    def test_public_candidate_edits_do_not_expand_saved_scope(self):
        prepared = self.prepare()
        survivor = self.old_file("not-in-approved-plan.bin")
        injected = copy.deepcopy(prepared["candidates"][0])
        injected["path"] = str(survivor)
        prepared["candidates"].append(injected)
        result = self.execute(prepared)
        self.assertFalse(self.file.exists())
        self.assertTrue(survivor.exists())
        self.assertEqual(result["summary"]["deletedItems"], 1)
        with self.assertRaises(StorageError):
            self.execute(prepared)

    def test_saved_plan_tampering_is_rejected_before_deletion(self):
        prepared = self.prepare()
        saved = read_record(self.state, "plans", prepared["planId"])
        saved["candidates"][0]["size"] += 1
        write_record(self.state, "plans", saved)
        with self.assertRaises(StorageError):
            self.execute(prepared)
        self.assertTrue(self.file.exists())
        self.assertEqual(records(self.state, "prunes"), [])

    def test_policy_change_is_rejected_before_deletion(self):
        prepared = self.prepare()
        self.policy = replace(self.policy, quarantine_days=31)
        with self.assertRaises(StorageError):
            self.execute(prepared)
        self.assertTrue(self.file.exists())

    def test_directory_child_change_after_prepare_is_retained(self):
        directory = self.root / "changed-directory"
        directory.mkdir()
        child = directory / "data"
        child.write_bytes(b"old child")
        old = time.time() - 40 * 86400
        os.utime(directory, (old, old))
        self.policy = replace(self.policy, rules=tuple(replace(rule, targets="dirs")
                                                      for rule in self.policy.rules))
        prepared = self.prepare([directory])
        child.write_bytes(b"new child must survive")
        result = self.execute(prepared)
        self.assertEqual(child.read_bytes(), b"new child must survive")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["skippedItems"], 1)

    def test_archive_becoming_busy_is_skipped_before_nas_backup(self):
        self.archive_policy()
        prepared = self.prepare()
        with patch.object(lifecycle, "active_processes", return_value=["fixture"]), \
                patch.object(backend.Archive, "backup") as backup:
            result = self.execute(prepared)
        backup.assert_not_called()
        self.assertTrue(self.file.exists())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["skippedItems"], 1)

    def test_native_failure_is_reported_as_partial(self):
        self.policy = replace(self.policy, maintenance=("npm",))
        with patch.object(maintenance, "preview", return_value={"execute": False, "items": []}), \
                patch.object(maintenance, "maintain", side_effect=StorageError("Native cleanup failed")):
            result = self.execute(self.prepare())
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["errors"])
        self.assertFalse(self.file.exists())

    def test_native_error_uses_short_reason_instead_of_policy_note(self):
        self.policy = replace(self.policy, maintenance=("npm",))
        native = {"execute": True, "items": [{"action": "npm", "status": "failed",
                  "reason": "fixture failure", "note": "Long policy description"}], "errors": ["npm"]}
        with patch.object(maintenance, "preview", return_value={"items": []}), \
                patch.object(maintenance, "maintain", return_value=native):
            prepared = self.prepare()
            self.assertEqual(prepared["warnings"], [])
            result = self.execute(prepared)
        self.assertEqual(result["errors"], [{"action": "npm", "reason": "fixture failure"}])

    def test_native_cleanup_only_executes_with_bound_preview(self):
        self.policy = replace(self.policy, maintenance=("npm",))
        bound = {"execute": False, "items": [{"action": "npm", "available": True,
                                             "binding": {"fixture": "approved"}}]}
        with patch.object(maintenance, "preview", return_value=bound) as preview, \
                patch.object(maintenance, "maintain", return_value={"execute": True, "items": []}) as native:
            prepared = self.prepare()
            native.assert_not_called()
            self.assertTrue(self.file.exists())
            preview.assert_called_once_with(self.policy, ["npm"], bind_targets=True)
            result = self.execute(prepared)
            native.assert_called_once_with(self.policy, ["npm"], approved_preview=bound,
                                           report_reclaimed=True, on_progress=ANY,
                                           output_dir=self.state / "prune-output" / result["id"], on_output=None)


    def test_native_interruption_keeps_bound_target_and_step_progress(self):
        self.policy = replace(self.policy, maintenance=("npm",))
        bound = {"execute": False, "items": [{"action": "npm", "available": True,
                                             "binding": {"target": {"path": str(self.root)}}}]}
        progress = {"execute": True, "items": [{**bound["items"][0], "output": [
            {"command": "fixture cleanup", "state": "running", "returncode": None}]}]}

        def interrupted(*args, on_progress, **kwargs):
            saved = records(self.state, "prunes")[0]
            self.assertEqual(saved["maintenance"], bound)
            self.assertEqual(saved["maintenanceState"], "running")
            on_progress(progress)
            raise KeyboardInterrupt

        with patch.object(maintenance, "preview", return_value=bound), \
                patch.object(maintenance, "maintain", side_effect=interrupted):
            prepared = self.prepare()
            with self.assertRaises(KeyboardInterrupt):
                self.execute(prepared)
        saved = records(self.state, "prunes")[0]
        self.assertEqual(saved["state"], "running")
        self.assertEqual(saved["maintenance"], progress)
        self.assertIsNone(saved["maintenance"]["items"][0]["output"][0]["returncode"])


class PruneCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve() / "state"
        self.runner = CliRunner()
        self.prepared = {"execute": False, "candidates": [], "maintenance": {"items": []},
                         "skipped": [], "warnings": [],
                         "summary": {"eligibleItems": 0, "estimatedBytes": 0, "nativeActions": []}}
        self.completed = {"execute": True, "errors": [], "state": "completed", "items": [],
                          "deletedBytesEstimate": 0, "maintenance": {"items": []},
                          "summary": {"deletedItems": 0, "archivedItems": 0,
                                      "skippedItems": 0, "retainedItems": 0}}
        self.policy = object()
        for name, value in (("_context", (self.policy, self.state)), ("_prune_interactive", False)):
            context = patch.object(cli, name, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(pruning, "prepare", return_value=self.prepared)
        self.prepare = context.start()
        self.addCleanup(context.stop)
        context = patch.object(pruning, "execute", return_value=self.completed)
        self.execute = context.start()
        self.addCleanup(context.stop)

    def invoke(self, *args):
        return self.runner.invoke(cli.app, ["prune", *args])

    def test_yes_aliases_execute_without_prompt_and_emit_standard_json(self):
        for flag in ("--yes", "-f", "--force"):
            with self.subTest(flag=flag), patch.object(cli.typer, "confirm") as confirm:
                result = self.invoke(flag, "--json")
                self.assertEqual(result.exit_code, 0, result.output)
                envelope = json.loads(result.stdout)
                self.assertEqual(envelope["schemaVersion"], 1)
                self.assertEqual(envelope["command"], "storage prune")
                self.assertTrue(envelope["ok"])
                self.assertEqual(envelope["data"], self.completed)
                confirm.assert_not_called()
        self.assertEqual(self.execute.call_count, 3)
        self.execute.assert_called_with(self.policy, self.state, self.prepared, on_output=ANY)

    def test_json_without_execution_consent_is_rejected_before_scan(self):
        result = self.invoke("--json")
        self.assertNotEqual(result.exit_code, 0)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["command"], "storage prune")
        self.assertFalse(envelope["ok"])
        self.prepare.assert_not_called()
        self.execute.assert_not_called()

    def test_noninteractive_without_yes_is_rejected_before_scan(self):
        result = self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()
        self.execute.assert_not_called()

    def test_dry_run_emits_preview_without_deletion_or_prompt(self):
        with patch.object(cli.typer, "confirm") as confirm:
            result = self.invoke("--dry-run", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["data"], self.prepared)
        self.execute.assert_not_called()
        confirm.assert_not_called()

    def test_dry_run_conflicts_with_every_yes_alias(self):
        for flag in ("--yes", "-f", "--force"):
            with self.subTest(flag=flag):
                result = self.invoke("--dry-run", flag, "--json")
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(json.loads(result.stdout)["ok"])
        self.prepare.assert_not_called()
        self.execute.assert_not_called()

    def test_interactive_confirmation_occurs_once_after_summary(self):
        stream = io.StringIO()

        def confirm(*args, **kwargs):
            self.assertIn("Eligible files/directories", stream.getvalue())
            self.assertIn("native maintenance", args[0])
            self.assertIn("delete", args[0].lower())
            self.execute.assert_not_called()
            return True

        with patch.object(cli, "_prune_interactive", return_value=True), \
                patch.object(log, "console", Console(file=stream, width=120)), \
                patch.object(cli.typer, "confirm", side_effect=confirm) as prompt:
            result = self.invoke()
        self.assertEqual(result.exit_code, 0, str(result.exception))
        prompt.assert_called_once()
        self.prepare.assert_called_once()
        self.execute.assert_called_once_with(self.policy, self.state, self.prepared, on_output=ANY)

    def test_cancelled_confirmation_never_executes(self):
        with patch.object(cli, "_prune_interactive", return_value=True), \
                patch.object(cli.typer, "confirm", return_value=False) as prompt:
            result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        prompt.assert_called_once()
        self.execute.assert_not_called()

    def render(self, data):
        stream = io.StringIO()
        with patch.object(log, "console", Console(file=stream, width=180, color_system=None)):
            cli._show_prune(data)
        return stream.getvalue()

    def test_native_preview_has_concise_targets_and_separate_counts(self):
        self.prepared["maintenance"]["items"] = [{
            "action": "pip", "available": True, "status": "pending",
            "target": {"path": "/fixture/pip", "metadata": {"private": "hidden"}},
            "note": "Read-only preview; no cleanup command was run."}]
        stream = io.StringIO()
        with patch.object(log, "console", Console(file=stream, width=180, color_system=None)):
            result = self.invoke("--dry-run")
        self.assertEqual(result.exit_code, 0, result.output)
        output = stream.getvalue()
        self.assertIn("Eligible files/directories: 0", output)
        self.assertIn("Native maintenance actions: 1", output)
        self.assertIn("/fixture/pip", output)
        self.assertIn("all", output.lower())
        for noise in ("hidden", "Read-only", "unknown", "Storage plan"):
            self.assertNotIn(noise, output)

    def test_native_result_status_reason_once_and_only_known_bytes(self):
        self.completed.update(maintenance={"items": [
            {"action": "uv", "available": True, "status": "completed",
             "reportedReclaimedBytes": 2**20},
            {"action": "npm", "available": True, "status": "failed",
             "reason": "fixture command failed", "note": "Verbose policy description"},
            {"action": "pip", "available": False, "status": "unavailable",
             "reason": "fixture tool missing"}], "errors": ["npm", "pip"]},
            errors=[{"action": "npm", "reason": "fixture command failed"},
                    {"action": "pip", "reason": "fixture tool missing"}],
            availableBytesChange=-65536)
        output = self.render(self.completed)
        for status in ("uv: completed", "npm: failed", "pip: unavailable", "1.00 MiB"):
            self.assertIn(status, output)
        self.assertEqual(output.count("fixture command failed"), 1)
        self.assertEqual(output.count("fixture tool missing"), 1)
        for noise in ("not reported", "Verbose", "free-space", "-0.06", "Deleted size"):
            self.assertNotIn(noise, output)

    def test_incomplete_native_result_preserves_completed_unknown_and_unexecuted_states(self):
        self.completed.update(maintenanceState="unknown", maintenance={"items": [
            {"action": "go", "status": "completed"},
            {"action": "docker", "status": "running"},
            {"action": "uv", "status": "pending"}]})
        output = self.render(self.completed)
        for state in ("go: completed", "docker: unknown", "uv: skipped"):
            self.assertIn(state, output)
        self.assertNotIn("failed", output)

    def test_noop_result_hides_journal_disk_usage(self):
        self.completed["availableBytesChange"] = -65536
        output = self.render(self.completed)
        self.assertIn("no files changed", output)
        self.assertNotIn("MiB", output)

    def test_native_json_stdout_contains_only_envelope(self):
        self.completed["maintenance"] = {"items": [{
            "action": "nix", "status": "completed", "reportedReclaimedBytes": 2**20}]}
        result = self.invoke("--yes", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["data"], self.completed)
        self.assertNotIn("MiB", result.stdout)

    def test_native_streams_are_live_plain_text_on_stderr(self):
        for json_output in (False, True):
            with self.subTest(json_output=json_output):
                stream = io.StringIO()
                console = Console(file=stream, width=180, color_system=None)

                def execute(policy, state, prepared, *, on_output):
                    self.assertEqual((policy, state, prepared),
                                     (self.policy, self.state, self.prepared))
                    on_output("nix", "stdout", "[bold]native stdout[/bold]\n")
                    self.assertEqual(stream.getvalue(), "nix:\n[bold]native stdout[/bold]\n")
                    on_output("nix", "stderr", "native stderr 123\n")
                    self.assertIn("native stderr 123\n", stream.getvalue())
                    on_output("pip", "stdout", "next action\n")
                    return self.completed

                self.execute.side_effect = execute
                with patch.object(log, "diagnostic_console", console):
                    result = self.invoke("--yes", *(["--json"] if json_output else []))
                self.assertEqual(result.exit_code, 0, str(result.exception))
                output = stream.getvalue()
                self.assertEqual(output.count("nix:\n"), 1)
                self.assertLess(output.index("pip:\n"), output.index("next action"))
                self.assertNotIn("native stdout", result.stdout)
                self.assertNotIn("native stderr", result.stdout)
                if json_output:
                    self.assertEqual(json.loads(result.stdout)["data"], self.completed)

    def test_native_callback_uses_stderr_and_keeps_json_stdout_pure_on_failure(self):
        self.completed.update(state="partial", errors=[{"action": "npm", "reason": "native failed"}])

        def execute(policy, state, prepared, *, on_output):
            on_output("npm", "stdout", "native stdout\n")
            on_output("npm", "stderr", "native failed\n")
            return self.completed

        self.execute.side_effect = execute
        result = self.invoke("--yes", "--json")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(result.stderr, "npm:\nnative stdout\nnative failed\n")

    def test_native_terminal_output_returns_to_column_zero_without_rich_wrapping(self):
        lines = ["deleting '/nix/store/" + "a" * 120 + ".drv'\n",
                 "deleting '/nix/store/second.drv'\n"]

        def execute(policy, state, prepared, *, on_output):
            on_output("nix", "start", "Collecting Nix store")
            for line in lines:
                on_output("nix", "stderr", line)
            return self.completed

        self.execute.side_effect = execute
        for processed in (False, True):
            with self.subTest(output_processing=processed):
                master, slave = pty.openpty()
                try:
                    attrs = termios.tcgetattr(slave)
                    attrs[1] |= termios.ONLCR
                    if processed:
                        attrs[1] |= termios.OPOST
                    else:
                        attrs[1] &= ~termios.OPOST
                    termios.tcsetattr(slave, termios.TCSANOW, attrs)
                    with os.fdopen(os.dup(slave), "w") as stream, \
                            patch.object(log, "diagnostic_console", Console(file=stream, width=20)):
                        result = self.invoke("--yes", "--json")
                    self.assertEqual(result.exit_code, 0, str(result.exception))
                    self.assertTrue(json.loads(result.stdout)["ok"])
                    os.set_blocking(master, False)
                    output = os.read(master, 4096).replace(b"\r\r\n", b"\r\n")
                    expected = ("Collecting Nix store:\n" + "".join(lines)).replace("\n", "\r\n")
                    self.assertEqual(output, expected.encode())
                    self.assertEqual(termios.tcgetattr(slave), attrs)
                finally:
                    os.close(slave)
                    os.close(master)

    def test_native_summary_reports_counts_sizes_and_unique_log_paths(self):
        logs = {"stdoutPath": "/fixture/stdout.log", "stderrPath": "/fixture/stderr.log",
                "truncatedStreams": ["stderr"]}
        self.completed["maintenance"]["items"] = [
            {"action": "nix", "status": "completed", "reportedRemovedItems": 3,
             "reportedReclaimedBytes": 2**21, "reportedSummary": "tool summary [bold]literal[/bold]",
             "output": [{"logs": logs}, {"logs": logs}]},
            {"action": "pip", "status": "completed", "reportedRemovedItems": 0, "reportedReclaimedBytes": 123},
            {"action": "go", "status": "completed"}]
        output = self.render(self.completed)
        self.assertIn("nix: completed; 3 removed; 2.00 MiB reported", output)
        self.assertIn("tool summary [bold]literal[/bold]", output)
        self.assertIn("pip: completed; 0 removed; 123 bytes reported", output)
        self.assertIn("go: completed\n", output)
        self.assertEqual(output.count("Logs: /fixture"), 1)
        self.assertIn("Truncated logs: stderr.log", output)

    def test_native_targets_select_human_fields_and_all_cache_effects(self):
        for key in ("path", "resolvedPath", "storeDir", "endpoint", "cache"):
            with self.subTest(key=key):
                self.assertEqual(cli._native_target({"target": {key: "/fixture/target",
                                                                "metadata": {"private": "hidden"}}}),
                                 "/fixture/target")
        for action in ("go", "go-modcache", "pip"):
            self.assertIn("all", cli._native_effect(action))
            self.assertIn("no age cutoff", cli._native_effect(action))

    def test_file_errors_and_retained_paths_remain_visible(self):
        self.completed.update(errors=[{"path": "/fixture/source", "reason": "file failed",
                                       "heldPath": "/fixture/held"}],
                              retained=[{"heldPath": "/fixture/retained"}])
        output = self.render(self.completed)
        for text in ("/fixture/source", "/fixture/held", "/fixture/retained", "file failed"):
            self.assertIn(text, output)

    def test_execution_errors_have_nonzero_exit_and_json_error_envelope(self):
        self.execute.return_value = {**self.completed, "state": "partial",
                                     "errors": [{"reason": "NAS unavailable"}]}
        result = self.invoke("--yes", "--json")
        self.assertNotEqual(result.exit_code, 0)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["command"], "storage prune")
        self.assertFalse(envelope["ok"])
        self.assertTrue(envelope["error"])


@unittest.skipUnless(os.environ.get("ENVY_STORAGE_RULES_FIXTURE") and shutil.which("organize"),
                     "Requires the Nix catalog fixture and organize")
class PruneCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state = self.home / "state"
        fixture = json.loads(Path(os.environ["ENVY_STORAGE_RULES_FIXTURE"]).read_text())
        rules = json.loads(json.dumps(fixture["rules"]).replace(fixture["home"], str(self.home)))
        self.targets = []
        self.controls = []
        for rule in rules.values():
            if not rule.get("prune"):
                continue
            root = Path(rule["path"])
            for name in rule["names"]:
                target = root / name
                target.mkdir(parents=True)
                (target / "data").write_bytes(b"rebuildable GPU cache")
                self.targets.append(target)
            control = root / "profile-state.bin"
            control.write_bytes(b"application data: retain")
            self.controls.append(control)
        for relative in (".cache/nix", ".cache/codex-runtimes", ".cache/uv", ".cache/node",
                         ".cache/npm", ".cache/envy/devshell", ".cache/envy/toolchains",
                         ".cache/envy/storage-archive", ".cache/envy/project-artifacts"):
            retained = self.home / relative / "retain.bin"
            retained.parent.mkdir(parents=True, exist_ok=True)
            retained.write_bytes(b"application data: retain")
            self.controls.append(retained)
        self.assertTrue(self.targets)
        old = time.time() - 40 * 86400
        for path in sorted(self.home.rglob("*"), key=lambda entry: len(entry.parts), reverse=True):
            os.utime(path, (old, old))
        manifest = {"settings": {"envy.user.home": str(self.home)}, "storage": {
            "mode": "manual", "rules": [dict(rule, id=identifier) for identifier, rule in rules.items()]}}
        self.policy = policy_from_manifest(manifest, self.state, self.home / "checkout")

    def commands(self, active=()):
        real_run = planner.run_process

        def run(argv, **kwargs):
            if Path(argv[0]).name == "pgrep":
                self.assertIn(argv[1], ("-x", "-f"))
                re.compile(argv[2])
                return subprocess.CompletedProcess(argv, 0 if argv[2] in active else 1, "", "")
            self.assertEqual(Path(argv[0]).name, "organize")
            environment = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"))
            return real_run(argv, env=environment, **kwargs)

        return patch.object(planner, "run_process", side_effect=run)

    def test_shipped_cache_rules_delete_only_named_old_subdirectories(self):
        with self.commands(), patch.object(lifecycle, "assert_inactive"), patch.object(planner, "assert_inactive"):
            prepared = pruning.prepare(self.policy, self.state)
            self.assertCountEqual([item["path"] for item in prepared["candidates"]],
                                  [str(path) for path in self.targets])
            result = pruning.execute(self.policy, self.state, prepared)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["deletedItems"], len(self.targets))
        self.assertTrue(all(not path.exists() for path in self.targets))
        self.assertTrue(all(path.read_bytes() == b"application data: retain" for path in self.controls))

    def test_running_tools_keep_their_catalog_caches(self):
        blocked_rules = {"envy-build-cache", "envy-swift-module-cache", "nvim-lua-cache"}
        with self.commands(active=("cargo", "swiftc", "nvim")), \
                patch.object(lifecycle, "assert_inactive"), patch.object(planner, "assert_inactive"):
            prepared = pruning.prepare(self.policy, self.state)
            blocked = [item for item in prepared["candidates"] if item.get("blocked")]
            self.assertEqual({item["rule"] for item in blocked}, blocked_rules)
            result = pruning.execute(self.policy, self.state, prepared)
        self.assertEqual(result["errors"], [])
        self.assertTrue(all(Path(item["path"]).exists() for item in blocked))
        self.assertEqual(result["summary"]["skippedItems"], len(blocked))
        self.assertEqual(result["summary"]["deletedItems"], len(self.targets) - len(blocked))
        self.assertTrue(all(path.exists() for path in self.controls))

    def test_prune_deletes_recent_and_future_catalog_caches_but_run_keeps_them(self):
        for index, target in enumerate(self.targets):
            stamp = time.time() + (86400 if index % 2 else 0)
            os.utime(target / "data", (stamp, stamp))
        with self.commands(), patch.object(lifecycle, "assert_inactive"), patch.object(planner, "assert_inactive"):
            normal = planner.plan(self.policy, self.state)
            self.assertEqual(normal["candidates"], [])
            prepared = pruning.prepare(self.policy, self.state)
            self.assertCountEqual([item["path"] for item in prepared["candidates"]],
                                  [str(path) for path in self.targets])
            result = pruning.execute(self.policy, self.state, prepared)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["deletedItems"], len(self.targets))
        self.assertTrue(all(not path.exists() for path in self.targets))
        self.assertTrue(all(path.exists() for path in self.controls))


if __name__ == "__main__":
    unittest.main()

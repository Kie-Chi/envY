"""Upgrade evidence tests use synthetic sources, profiles, state and app files only."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer
from rich.console import Console
from typer.testing import CliRunner

from envy import behavior, journal, log, provenance, utils
from envy.main import cli
from envy.structured_config import ConflictError
from envy.workflows import generations, plan, system, update


def manifest():
    return {"id": "fixture", "platform": "linux", "schemaVersion": 3,
            "settings": {"envy.vscode.mode": "local"},
            "software": {"groups": {"npm.user.tool": {"selection": {
                "include": [{"id": "tool", "ref": "npm:tool", "sourceId": "npm:npmjs"}],
                "exclude": [], "effective": [{"id": "tool", "version": "1"}]}}},
                "sources": {"npm:npmjs": {"ecosystem": "npm", "type": "registry",
                            "parameters": {"registry": "https://registry.npmjs.org"}}}},
            "behavior": {"coverage": "partial", "unknowns": ["External effects are not simulated"],
                         "actions": []}}


def source():
    return {"frozenPath": "/nix/store/fixture-source", "narHash": "sha256-fixture",
            "gitRevision": "0" * 40, "dirty": True, "dirtyFingerprint": "1" * 64,
            "lockFingerprint": "2" * 64, "coverage": "path flake archive, including ignored files",
            "gitCoverage": "tracked content and untracked names; frozen NAR identifies all archived content",
            "reason": None, "verified": True,
            "drvPath": "/nix/store/fixture.drv", "outputPath": "/nix/store/fixture-output"}


def shared_plan_manifest():
    value = manifest()
    value["behavior"]["actions"] = [{
        "id": "agents.fixture-settings", "owner": "agents", "manager": "writable-merge",
        "summary": "Merge declared fixture settings", "effects": ["managed application keys"],
        "verification": "Preview managed keys only", "repeatability": "Revalidate concurrent writes before merging",
        "rollbackBoundary": "Writable application state is outside the generation rollback boundary",
        "merge": {"target": "/fixture/home/.config/fixture/settings.json", "format": "json",
                  "source": "/nix/store/fixture-settings.json", "managedKeys": ["managed"], "managedKeyPrefixes": {}}}]
    return value


def produce_shared_plan():
    output = io.StringIO()
    current = generations.Generation(1, Path("/fixture/profiles/home-manager-1-link"), Path("/nix/store/fixture-old"), "2000-01-01T00:00:00+00:00", True)
    with patch.object(log, "console", Console(file=output, width=20, force_terminal=True)), \
         patch.object(plan, "current_machine_id", return_value="fixture"), \
         patch.object(plan, "platform_name", return_value="linux"), \
         patch.object(plan, "build_snapshot", return_value=(source(), shared_plan_manifest(), Path("/nix/store/fixture-output"))), \
         patch.object(plan, "current_generation", return_value=current), \
         patch.object(plan, "baseline_for_generation", return_value=None), \
         patch.object(plan, "closure_diff", return_value="fixture: 1 → 2"):
        plan.plan_configuration(json_output=True)
    return json.loads(output.getvalue())


class FixtureCase(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch.object(journal, "state_dir", return_value=self.root / "state"))
        self.enterContext(patch.object(journal, "current_machine_id", return_value="fixture"))
        self.enterContext(patch.object(journal, "platform_name", return_value="linux"))
        self.enterContext(patch.object(system, "current_machine_id", return_value="fixture"))
        self.enterContext(patch.object(system, "platform_name", return_value="linux"))
        self.enterContext(patch.object(system, "PLATFORM", "linux"))
        self.output = io.StringIO()
        self.enterContext(patch.object(log, "console", Console(file=self.output, width=20, force_terminal=True)))

    def generation(self, target="/nix/store/fixture-output", number=2):
        return generations.Generation(number, self.root / f"profiles/home-manager-{number}-link", Path(target), "fixture", True)

    def apply(self, *, activation_error=None, after=None, frozen=None):
        after = after or self.generation()
        with patch.object(system, "refine_before_apply"), patch.object(system, "require_current_machine_file"), \
             patch("envy.provenance.build_snapshot", return_value=(frozen or source(), manifest(), Path("/nix/store/fixture-output"))), \
             patch.object(generations, "current_generation", side_effect=[self.generation("/nix/store/old", 1), after]), \
             patch.object(system, "run_process", side_effect=activation_error) as activate:
            system.apply_configuration()
        return activate


class SourceFreezeTests(FixtureCase):
    def freeze(self, *, paths=None, git=None, locks=None, nar="sha256-fixture"):
        with patch.object(provenance, "_git_evidence", side_effect=git or [{"gitRevision": "abc"}] * 2), \
             patch.object(provenance, "_lock_digest", side_effect=locks or ["lock"] * 3), \
             patch.object(provenance, "nar_hash", return_value=nar), \
             patch.object(provenance, "_json_command", side_effect=[{"path": path} for path in (paths or ["/nix/store/frozen"] * 2)]) as run:
            result = provenance.freeze_source(self.root)
        return result, run

    def test_metadata_checks_ignored_content_without_archiving_inputs(self):
        result, run = self.freeze()
        self.assertTrue(result["verified"])
        self.assertEqual(result["frozenPath"], "/nix/store/frozen")
        self.assertIn("ignored", result["coverage"])
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args, (["nix", "flake", "metadata", "--json",
                                          "--no-update-lock-file", f"path:{self.root}"], self.root))

    def test_ignored_content_race_rejects_even_when_git_is_unchanged(self):
        with self.assertRaisesRegex(provenance.SnapshotError, "Source changed") as error:
            self.freeze(paths=["/nix/store/one", "/nix/store/two"])
        self.assertEqual(error.exception.partial["source"]["frozenPath"], "/nix/store/one")

    def test_git_and_lock_races_reject_snapshot(self):
        for kwargs in ({"git": [{"gitRevision": "one"}, {"gitRevision": "two"}]},
                       {"locks": ["old", "new", "new"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(provenance.SnapshotError):
                self.freeze(**kwargs)

    def test_missing_nar_evidence_cannot_claim_verified(self):
        result, _ = self.freeze(nar=None)
        self.assertFalse(result["verified"])
        self.assertIn("NAR", result["reason"])

    def test_build_and_manifest_use_only_the_same_frozen_source(self):
        calls = []
        working_directories = []
        def command(arguments, root):
            calls.append(arguments)
            working_directories.append(root)
            if arguments[1] == "build":
                return [{"drvPath": "/nix/store/fixture.drv", "outputs": {"out": {"path": "/nix/store/fixture-output"}}}]
            if arguments[1] == "derivation":
                return {"version": 3, "derivations": {"fixture.drv": {"outputs": {"out": {"path": "fixture-output"}}}}}
            return manifest()
        with patch.object(provenance, "freeze_source", return_value=source()), \
             patch.object(provenance, "_json_command", side_effect=command), \
             patch("envy.utils.platform_name", return_value="linux"):
            evidence, evaluated, output = provenance.build_snapshot("fixture", root=self.root)
        self.assertEqual(output, Path("/nix/store/fixture-output"))
        self.assertEqual(evaluated, manifest())
        self.assertTrue(evidence["verified"])
        self.assertIn("path:/nix/store/fixture-source#homeConfigurations.fixture.activationPackage", calls[0])
        self.assertIn("path:/nix/store/fixture-source#homeConfigurations.fixture.config.envy.machine.manifest", calls[2])
        self.assertFalse(any("path:.#" in arg for args in calls for arg in args))
        # A store cwd can make Nix treat the frozen flake target as a store path.
        for index in (0, 2):
            with self.subTest(command=calls[index][1]):
                self.assertEqual(working_directories[index], self.root)

    def test_derivation_json_shapes_and_missing_output(self):
        for value in ({"/nix/store/fixture.drv": {"outputs": {"out": {"path": "/nix/store/output"}}}},
                      {"version": 3, "derivations": {"fixture.drv": {"outputs": {"out": {"path": "output"}}}}}):
            self.assertEqual(provenance.derivation_output(value, "/nix/store/fixture.drv"), "/nix/store/output")
        self.assertIsNone(provenance.derivation_output({"version": 3}, "/nix/store/fixture.drv"))

    def test_missing_build_derivation_evidence_keeps_output_but_unverified(self):
        with patch.object(provenance, "freeze_source", return_value=source()), \
             patch.object(provenance, "_json_command", side_effect=[
                 [{"outputs": {"out": "/nix/store/fixture-output"}}], manifest()]):
            evidence, _, output = provenance.build_snapshot("fixture", root=self.root)
        self.assertEqual(output, Path("/nix/store/fixture-output"))
        self.assertFalse(evidence["verified"])

    def test_missing_build_output_keeps_partial_source(self):
        with patch.object(provenance, "freeze_source", return_value=source()), \
             patch.object(provenance, "_json_command", return_value=[{"outputs": {}}]), \
             self.assertRaises(provenance.SnapshotError) as error:
            provenance.build_snapshot("fixture", root=self.root)
        self.assertEqual(error.exception.partial["source"]["frozenPath"], source()["frozenPath"])

    def test_reverify_source_checks_lock_and_derivation_target(self):
        with patch.object(provenance, "nar_hash", return_value="sha256-fixture"), \
             patch.object(provenance, "_lock_digest", return_value="2" * 64), \
             patch.object(provenance, "_json_command", return_value={"/nix/store/fixture.drv": {
                 "outputs": {"out": {"path": "/nix/store/other"}}}}):
            self.assertFalse(provenance.reverify_source(source(), "/nix/store/fixture-output"))
        with patch.object(provenance, "nar_hash", return_value="sha256-fixture"), \
             patch.object(provenance, "_lock_digest", return_value="changed"), \
             patch.object(provenance, "_json_command") as command:
            self.assertFalse(provenance.reverify_source(source(), "/nix/store/fixture-output"))
        command.assert_not_called()

    def test_nar_json_old_and_new_shapes(self):
        for value in ({"/nix/store/source": {"narHash": "sha256-fixture"}},
                      [{"path": "/nix/store/source", "narHash": "sha256-fixture"}],
                      {"version": 2, "storeDir": "/nix/store", "info": {"source": {"narHash": "sha256-fixture"}}}):
            with patch.object(provenance, "_json_command", return_value=value):
                self.assertEqual(provenance.nar_hash("/nix/store/source", root=self.root), "sha256-fixture")


class SemanticPreviewTests(FixtureCase):
    def test_shared_fixture_matches_the_real_backend_producer(self):
        fixture = utils.ENVY_ROOT / "resources/scripts/envy-tui/src/workflow/fixtures/plan-v1.json"
        self.assertEqual(json.loads(fixture.read_text()), produce_shared_plan())

    def test_semantics_keep_selection_identity_and_exclude_bodies(self):
        value = manifest()
        value["settings"]["envy.secret.token"] = "DO-NOT-PRINT"
        value["environment"] = {"API_KEY": "DO-NOT-PRINT"}
        value["agents"] = {"private": "DO-NOT-PRINT"}
        value["software"]["groups"]["npm.user.tool"]["selection"]["include"][0]["parameters"] = {"token": "DO-NOT-PRINT"}
        result = behavior.semantic_snapshot(value)
        self.assertNotIn("DO-NOT-PRINT", json.dumps(result))
        self.assertEqual(result["software"]["npm.user.tool"]["include"][0]["sourceId"], "npm:npmjs")
        self.assertIn("npm:npmjs", result["sources"])

    def test_unknown_legacy_generation_is_not_reconstructed(self):
        result = behavior.comparison_report(None, behavior.semantic_snapshot(manifest()))
        self.assertTrue(all(item["status"] == "unknown" for item in result.values()))
        self.assertTrue(all("before" not in item for item in result.values()))

    def test_each_section_detects_change(self):
        old = behavior.semantic_snapshot(manifest())
        new = behavior.semantic_snapshot(manifest())
        new["settings"]["envy.vscode.mode"] = "remote"
        new["software"]["npm.user.tool"]["exclude"] = ["tool"]
        new["sources"]["npm:npmjs"]["owner"] = "host"
        new["behavior"]["actions"] = [{"id": "extra"}]
        result = behavior.comparison_report(old, new)
        self.assertTrue(all(item["status"] == "compared" and item["changed"] for item in result.values()))

    def declaration(self):
        artifact = self.root / "artifact.json"
        target = self.root / "app.json"
        artifact.write_text(json.dumps({"managed": {"token": "DECLARED-SECRET"}}))
        target.write_text(json.dumps({"managed": {"token": "OLD-SECRET"}, "custom": "PERSONAL-BODY"}))
        return {"source": str(artifact), "target": str(target), "format": "json", "managedKeys": [], "managedKeyPrefixes": {}}

    def test_fixture_merge_preview_emits_only_operations_and_managed_paths(self):
        declaration = self.declaration()
        original = Path(declaration["target"]).read_bytes()
        preview = behavior.preview_merge(declaration, source_root=self.root)
        self.assertEqual(preview["status"], "compared")
        self.assertEqual(preview["changes"], [{"path": ["managed", "token"], "operation": "change"}])
        self.assertEqual(preview["operations"], ["write"])
        self.assertEqual(Path(declaration["target"]).read_bytes(), original)
        self.assertNotIn("SECRET", json.dumps(preview))
        self.assertNotIn("PERSONAL-BODY", json.dumps(preview))
        self.assertNotIn("hash", json.dumps(preview).lower())

    def test_merge_permissions_parse_and_conflicts_are_explicit_without_error_body(self):
        declaration = self.declaration()
        for error, status in ((PermissionError("PERSONAL-BODY"), "unavailable"),
                              (ValueError("DECLARED-SECRET"), "unavailable"), (ConflictError(), "unknown")):
            with self.subTest(error=type(error)), patch.object(behavior, "prepare_update", side_effect=error):
                result = behavior.preview_merge(declaration, source_root=self.root)
            self.assertEqual(result["status"], status)
            self.assertNotIn("BODY", json.dumps(result))
            self.assertNotIn("SECRET", json.dumps(result))

    def test_plan_does_not_read_real_runtime_content(self):
        value = manifest()
        value["behavior"]["actions"] = [{"id": "merge", "owner": "agent", "manager": "merge",
             "summary": "Merge managed keys", "effects": ["configuration"], "merge": self.declaration()}]
        with patch.object(plan, "current_machine_id", return_value="fixture"), \
             patch.object(plan, "build_snapshot", return_value=(source(), value, Path("/nix/store/fixture-output"))), \
             patch.object(plan, "current_generation", return_value=None), \
             patch.object(behavior, "preview_merge") as inspect:
            plan.plan_configuration(json_output=True)
        inspect.assert_not_called()
        payload = json.loads(self.output.getvalue())
        self.assertEqual(payload["data"]["activation"]["actions"][0]["mergePreview"]["status"], "unknown")
        self.assertNotIn("SECRET", self.output.getvalue())
        self.assertNotIn("\x1b", self.output.getvalue())

    def test_empty_software_selection_does_not_suppress_declared_actions(self):
        value = manifest()
        value["software"]["groups"] = {}
        value["behavior"]["actions"] = [{"id": "brew", "owner": "homebrew", "manager": "homebrew",
                                            "summary": "Run declared cleanup", "effects": ["cleanup"]}]
        result = behavior.activation_preview(behavior.semantic_snapshot(value))
        self.assertEqual(result["actions"][0]["id"], "brew")
        self.assertEqual(result["actions"][0]["realized"], "unknown")

    def test_human_plan_cannot_report_no_differences_after_a_diff_failure(self):
        output = io.StringIO()
        with patch.object(log, "console", Console(file=output, width=240, force_terminal=False)), \
             patch.object(plan, "current_machine_id", return_value="fixture"), \
             patch.object(plan, "build_snapshot", return_value=(source(), manifest(), Path("/nix/store/fixture-output"))), \
             patch.object(plan, "current_generation", return_value=self.generation()), \
             patch.object(plan, "closure_diff", side_effect=RuntimeError("diff failed")):
            plan.plan_configuration()
        self.assertIn("unavailable", output.getvalue())
        self.assertNotIn("No Nix closure differences.", output.getvalue())

    def test_missing_history_generation_still_emits_all_section_statuses(self):
        with patch.object(generations, "require_generation", side_effect=typer.BadParameter("generation does not exist")):
            result = CliRunner().invoke(cli, ["history", "diff", "1", "2", "--json"])
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(set(payload["data"]["sections"]), {"closure", "settings", "software", "sources", "behavior"})
        self.assertIsNone(payload["data"]["before"])

    def test_history_failure_keeps_partial_semantic_report_and_json(self):
        left, right = self.generation(number=1), self.generation(number=2)
        with patch.object(generations, "require_generation", side_effect=[left, right]), \
             patch.object(generations, "closure_diff", side_effect=RuntimeError("diff failed")):
            result = CliRunner().invoke(cli, ["history", "diff", "1", "2", "--json"])
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "history.diff")
        self.assertEqual(payload["data"]["before"]["number"], 1)
        self.assertIsNone(payload["closureDiff"])
        self.assertEqual(payload["data"]["sections"]["settings"]["status"], "unknown")


class ReceiptTests(FixtureCase):
    def fsync_failure(self, event):
        """Fail after the complete selected event has been flushed to a real file."""
        original = journal.os.fsync
        failed = []

        def fsync(descriptor):
            records = [json.loads(line) for line in journal.journal_path().read_text().splitlines()]
            last = records[-1]
            if last.get("event") == event and not failed:
                failed.append(last)
                raise OSError("injected journal fsync failure")
            return original(descriptor)

        return patch.object(journal.os, "fsync", side_effect=fsync), failed

    def assert_retry_denied(self, entry, reason):
        with patch.object(provenance, "reverify_source") as reverify, \
             patch.object(generations, "current_generation") as current:
            self.assertFalse(entry["retry"]["allowed"])
            self.assertIn(reason, entry["retry"]["reason"])
            with self.assertRaisesRegex(ValueError, reason):
                journal.verify_retry(entry["operationId"], "verify-generation")
            result = CliRunner().invoke(cli, ["log", "--retry", entry["operationId"], "--json"])
        reverify.assert_not_called()
        current.assert_not_called()
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertEqual(payload["command"], "log.retry")
        self.assertFalse(payload["data"]["retry"]["allowed"])
        self.assertIn(reason, payload["error"]["message"])

    def fresh_process_receipt(self):
        script = '''
import json, sys
from pathlib import Path
from unittest.mock import patch
from envy import journal, provenance
from envy.workflows import generations
value = json.loads(sys.argv[2])
generation = generations.Generation(value["number"], Path(value["path"]), Path(value["target"]), value["createdAt"], value["current"])
with patch.object(journal, "state_dir", return_value=Path(sys.argv[1])), patch.object(journal, "current_machine_id", return_value="fixture"), patch.object(journal, "platform_name", return_value="linux"), patch.object(provenance, "reverify_source", return_value=True), patch.object(generations, "current_generation", return_value=generation):
    entry = journal.read(operation="apply")[0]
    try:
        journal.verify_retry(entry["operationId"], "verify-generation")
        retry_denied = False
    except ValueError:
        retry_denied = True
    print(json.dumps({"bound": journal.baseline_for_generation(generation) is not None, "persisted": entry.get("journalPersisted"), "allowed": entry["retry"]["allowed"], "retryDenied": retry_denied}))
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.root / "state"),
                                 json.dumps(self.generation().to_dict())], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_terminal_fsync_failure_cannot_bind_a_readable_terminal_in_a_new_process(self):
        fault, failed = self.fsync_failure("terminal")
        with fault:
            self.apply()
        self.assertEqual(len(failed), 1)
        records = [json.loads(line) for line in journal.journal_path().read_text().splitlines()]
        self.assertEqual(records[-1]["event"], "terminal")
        self.assertEqual(records[-1]["result"], "ok")
        self.assertIn("binding", records[-1])
        self.assertTrue(journal.append({"operation": "unrelated", "result": "ok"}))
        self.assertEqual(journal.read(operation="unrelated")[0]["result"], "ok")
        self.assertIsNone(journal.baseline_for_generation(self.generation()))
        entry = journal.read(operation="apply")[0]
        self.assertFalse(entry["journalPersisted"])
        self.assert_retry_denied(entry, "not fully persisted")
        self.assertEqual(self.fresh_process_receipt(), {"bound": False, "persisted": False, "allowed": False, "retryDenied": True})

    def test_started_fsync_failure_with_readable_bytes_cannot_enable_retry(self):
        fault, failed = self.fsync_failure("started")
        with fault:
            self.apply()
        self.assertEqual(len(failed), 1)
        records = [json.loads(line) for line in journal.journal_path().read_text().splitlines()]
        self.assertEqual(records[0]["event"], "started")
        self.assertTrue(any(record["event"] == "terminal" for record in records))
        entry = journal.read()[0]
        self.assertFalse(entry["journalPersisted"])
        self.assertIsNone(journal.baseline_for_generation(self.generation()))
        self.assert_retry_denied(entry, "not fully persisted")
        self.assertEqual(self.fresh_process_receipt(), {"bound": False, "persisted": False, "allowed": False, "retryDenied": True})

    def test_verified_stage_without_terminal_is_interrupted_and_cannot_retry(self):
        generation = self.generation().to_dict()
        receipt = journal._Receipt("apply", {})
        self.assertTrue(journal.append(receipt.record("started")))
        token = journal._active_receipt.set(receipt)
        try:
            self.assertTrue(journal.operation_stage("verify", "ok", source=source(), after=generation,
                binding={**generation, "host": "fixture", "platform": "linux", "profile": str(self.root / "profiles/home-manager")},
                semanticBaseline=behavior.semantic_snapshot(manifest())))
        finally:
            journal._active_receipt.reset(token)
        entry = journal.read()[0]
        self.assertEqual(entry["result"], "interrupted")
        self.assertIsNone(entry["terminal"])
        self.assertIn("binding", entry)
        self.assert_retry_denied(entry, "successful terminal")
        self.assertIsNone(journal.baseline_for_generation(self.generation()))
        self.assertEqual(self.fresh_process_receipt(), {"bound": False, "persisted": False, "allowed": False, "retryDenied": True})

    def test_readable_confirmation_proves_prior_terminal_sync_even_if_confirmation_sync_fails(self):
        fault, failed = self.fsync_failure("terminal-confirmed")
        with fault:
            self.apply()
        self.assertEqual(len(failed), 1)
        entry = journal.read()[0]
        self.assertTrue(entry["journalPersisted"])
        self.assertIsNotNone(journal.baseline_for_generation(self.generation()))
        self.assertNotIn("terminalId", entry)
        self.assertNotIn("priorEventsPersisted", entry)
        text = " ".join(self.output.getvalue().split())
        self.assertIn("confirmation durability is unknown", text)
        self.assertNotIn("baseline is unbound", text)
        self.assertEqual(self.fresh_process_receipt(), {"bound": True, "persisted": True, "allowed": True, "retryDenied": False})

    def test_interruption_after_terminal_sync_but_before_confirmation_stays_unbound(self):
        original = journal.os.fsync

        def interrupt(descriptor):
            original(descriptor)
            last = json.loads(journal.journal_path().read_text().splitlines()[-1])
            if last.get("event") == "terminal":
                raise KeyboardInterrupt

        with patch.object(journal.os, "fsync", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.apply()
        entry = journal.read()[0]
        self.assertEqual(entry["terminal"], "ok")
        self.assertFalse(entry["journalPersisted"])
        self.assert_retry_denied(entry, "not fully persisted")
        self.assertEqual(self.fresh_process_receipt(), {"bound": False, "persisted": False, "allowed": False, "retryDenied": True})

    def test_partial_terminal_with_verified_binding_is_not_retryable(self):
        generation = self.generation().to_dict()

        @journal.record_operation("sync")
        def operation():
            journal.operation_stage("activate", "started")
            journal.operation_stage("verify", "ok", source=source(), after=generation,
                binding={**generation, "host": "fixture", "platform": "linux", "profile": str(self.root / "profiles/home-manager")},
                semanticBaseline=behavior.semantic_snapshot(manifest()))
            raise RuntimeError("later synthetic workflow failure")

        with self.assertRaisesRegex(RuntimeError, "later synthetic"):
            operation()
        entry = journal.read()[0]
        self.assertTrue(entry["journalPersisted"])
        self.assertEqual(entry["terminal"], "partial")
        self.assertIn("binding", entry)
        self.assert_retry_denied(entry, "successful terminal")
        self.assertIsNone(journal.baseline_for_generation(self.generation()))

    def test_started_and_stages_without_terminal_are_interrupted(self):
        journal.append({"operationId": "interrupted", "event": "started", "operation": "apply"})
        journal.append({"operationId": "interrupted", "event": "stage", "source": source(),
                        "stage": {"name": "activate", "status": "started"}})
        entry = journal.read()[0]
        self.assertEqual(entry["result"], "interrupted")
        self.assertIsNone(entry["terminal"])
        self.assertEqual(entry["stages"], [{"name": "activate", "status": "started"}])
        self.assertEqual(entry["source"], source())
        self.assertFalse(entry["retry"]["allowed"])

    def test_verified_apply_binds_exact_generation_and_safe_baseline(self):
        activate = self.apply()
        activate.assert_called_once_with(["/nix/store/fixture-output/activate"], check=True,
                                         activity="activate frozen Home Manager generation")
        entry = journal.read()[0]
        self.assertEqual(entry["result"], "ok")
        self.assertEqual([stage["name"] for stage in entry["stages"]], ["build", "build", "activate", "activate", "verify", "verify"])
        self.assertEqual(entry["binding"]["profile"], str(self.root / "profiles/home-manager"))
        self.assertEqual(journal.baseline_for_generation(self.generation()), behavior.semantic_snapshot(manifest()))
        self.assertIsNone(journal.baseline_for_generation(self.generation(number=3)))
        self.assertIsNone(journal.baseline_for_generation(self.generation("/nix/store/other")))
        with patch.object(journal, "current_machine_id", return_value="other"):
            self.assertIsNone(journal.baseline_for_generation(self.generation()))

    def test_host_change_before_activation_is_blocked(self):
        with patch.object(system, "current_machine_id", side_effect=["fixture", "other"]), \
             patch.object(system, "refine_before_apply"), patch.object(system, "require_current_machine_file"), \
             patch("envy.provenance.build_snapshot", return_value=(source(), manifest(), Path("/nix/store/fixture-output"))), \
             patch.object(generations, "current_generation", return_value=self.generation()), \
             patch.object(system, "run_process") as activate, self.assertRaises(typer.Exit):
            system.apply_configuration()
        activate.assert_not_called()
        self.assertEqual(journal.read()[0]["stages"][-1], {"name": "activate", "status": "blocked"})
        self.assertEqual(journal.read()[0]["result"], "fail")

    def test_build_failure_preserves_partial_source_and_exit_code(self):
        with patch.object(system, "refine_before_apply"), patch.object(system, "require_current_machine_file"), \
             patch("envy.provenance.build_snapshot", side_effect=provenance.SnapshotError("Source changed", code=7, partial={"source": source()})), \
             patch.object(generations, "current_generation", return_value=self.generation()), \
             patch.object(system, "run_process") as activate, self.assertRaises(typer.Exit) as error:
            system.apply_configuration()
        activate.assert_not_called()
        self.assertEqual(error.exception.exit_code, 7)
        self.assertEqual(journal.read()[0]["source"], source())
        self.assertEqual(journal.read()[0]["result"], "fail")

    def test_partial_activation_never_binds_or_offers_replay(self):
        with self.assertRaisesRegex(RuntimeError, "activation failed"):
            self.apply(activation_error=RuntimeError("activation failed"))
        entry = journal.read()[0]
        self.assertEqual(entry["result"], "partial")
        self.assertNotIn("binding", entry)
        self.assertFalse(entry["retry"]["allowed"])
        self.assertIsNone(journal.baseline_for_generation(self.generation()))

    def test_target_mismatch_and_missing_source_evidence_leave_baseline_unbound(self):
        with self.assertRaises(typer.Exit) as error:
            self.apply(after=self.generation("/nix/store/unexpected"))
        self.assertEqual(error.exception.exit_code, 1)
        self.assertEqual(journal.read()[0]["result"], "partial")
        self.apply(frozen={**source(), "verified": False})
        self.assertIsNone(journal.baseline_for_generation(self.generation()))
        self.assertEqual(journal.read()[0]["stages"][-1]["status"], "unavailable")

    def test_journal_failure_does_not_fail_business_or_claim_bound_baseline(self):
        real_append = journal.append
        def append(record):
            if record["event"] == "started":
                return False
            return real_append(record)
        with patch.object(journal, "append", side_effect=append):
            self.apply()
        self.assertFalse(journal.read()[0]["journalPersisted"])
        self.assertIsNone(journal.baseline_for_generation(self.generation()))
        self.assertIn("not fully persisted", " ".join(self.output.getvalue().split()))

    def test_real_journal_io_failure_does_not_fail_activation(self):
        (self.root / "state").write_text("not a directory")
        self.apply()
        self.assertEqual(journal.read(), [])
        self.assertIn("not fully persisted", " ".join(self.output.getvalue().split()))

    def test_terminal_write_failure_is_interrupted_and_baseline_unbound(self):
        real_append = journal.append
        with patch.object(journal, "append", side_effect=lambda row: False if row["event"] == "terminal" else real_append(row)):
            self.apply()
        self.assertEqual(journal.read()[0]["result"], "interrupted")
        self.assertIsNone(journal.baseline_for_generation(self.generation()))

    def test_retry_allowlist_and_revalidation_never_execute_journal_commands(self):
        self.enterContext(patch.object(provenance, "_lock_digest", return_value="2" * 64))
        self.enterContext(patch.object(provenance, "_json_command", return_value={
            "/nix/store/fixture.drv": {"outputs": {"out": {"path": "/nix/store/fixture-output"}}}}))
        self.apply()
        entry = journal.read()[0]
        operation_id = entry["operationId"]
        with patch.object(provenance, "nar_hash", return_value="sha256-fixture"), \
             patch.object(generations, "current_generation", return_value=self.generation()), \
             patch.object(system, "run_process") as execute:
            self.assertEqual(journal.verify_retry(operation_id, "verify-generation")["status"], "verified")
            for step in ("apply", "brew update", "sh -c true", "verify-generation; exit"):
                with self.subTest(step=step), self.assertRaisesRegex(ValueError, "Unsafe"):
                    journal.verify_retry(operation_id, step)
        execute.assert_not_called()
        with patch.object(provenance, "nar_hash", return_value="sha256-other"), self.assertRaisesRegex(ValueError, "source"):
            journal.verify_retry(operation_id, "verify-generation")
        with patch.object(journal, "current_machine_id", return_value="other"), self.assertRaisesRegex(ValueError, "host"):
            journal.verify_retry(operation_id, "verify-generation")
        with patch.object(provenance, "nar_hash", return_value="sha256-fixture"), \
             patch.object(generations, "current_generation", return_value=self.generation("/nix/store/changed")), \
             self.assertRaisesRegex(ValueError, "target changed"):
            journal.verify_retry(operation_id, "verify-generation")

    def test_unsafe_retry_cli_is_json_and_denies_without_replaying(self):
        result = CliRunner().invoke(cli, ["log", "--retry", "fake", "--step", "apply", "--json"])
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertEqual(payload["command"], "log.retry")
        self.assertFalse(payload["data"]["retry"]["allowed"])
        self.assertIn("Unsafe retry", payload["error"]["message"])

    def test_log_read_failure_is_json_and_not_an_empty_success(self):
        with patch.object(journal, "snapshot", side_effect=RuntimeError("Cannot read operation journal")):
            result = CliRunner().invoke(cli, ["log", "--json"])
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["data"]["status"], "unavailable")

    def test_lock_validation_failure_restores_existing_transaction_boundary(self):
        lock = self.root / "flake.lock"
        lock.write_text("before")
        def mutate(*args, **kwargs):
            lock.write_text("after")
            return subprocess.CompletedProcess(args[0], 0, "", "")
        with patch.object(update, "ENVY_ROOT", self.root), patch.object(update, "run_process", side_effect=mutate), \
             patch.object(update, "prime_flake_sources"), patch.object(update, "check_or_exit", side_effect=typer.Exit(1)):
            with self.assertRaises(typer.Exit):
                update.update_inputs()
        self.assertEqual(lock.read_text(), "before")
        self.assertEqual(journal.read()[0]["stages"][-1], {"name": "restore-lock", "status": "ok"})


if __name__ == "__main__":
    unittest.main()

"""Operation integration uses private temporary state and synthetic providers only."""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from envy import utils
from envy.storage import cli, contracts, lifecycle, maintenance, planner, receipts, source_identity
from envy.storage.model import MODEL_VERSION, Policy, StorageError
from envy.storage.state import locked, read_record, write_record


def fake_preview(policy, actions, *, bind_targets=False):
    return {"execute": False, "items": [{"action": action, "available": True,
            "commands": [["fake-native", action]], "note": "Synthetic provider only.",
            "target": {"path": str(policy.home / action), "identity": "fixed"}}
            for action in actions]}


def _child_confirm(policy, state, token, queue, crash=False, barrier=None):
    def execute(*args, **kwargs):
        with (state / "native-calls").open("a") as handle:
            handle.write("called\n")
        if crash:
            raise SystemExit(91)
        return {"execute": True, "items": [], "errors": []}
    if barrier is not None:
        barrier.wait(10)
    try:
        with patch.object(maintenance, "preview", side_effect=fake_preview), \
                patch.object(maintenance, "maintain", side_effect=execute), locked(state):
            result = receipts.confirm("maintain", policy, state, {"actions": ["go"]}, token)
        queue.put(result["state"])
    except StorageError as exc:
        queue.put(str(exc))


def produce_operations_fixture() -> dict:
    """Build shared consumer examples through real producers with fake native boundaries."""
    from types import SimpleNamespace
    from envy.storage import native_context
    from envy.storage.model import signature

    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary).resolve()
        state = home / "state"
        policy = Policy(home, "manual", 30, (), (), (), (), "", "", ("go", "npm"),
                        machine_id="fixture", platform="linux")
        manifest = {"id": "fixture", "platform": "linux", "storage": {
            "maintenance": {"go": True, "npm": True},
            "archive": {"repository": "https://user:secret@example.invalid/archive", "passwordFile": "private"}}}

        def capture(action, policy, *, platform):
            return {"version": 1, "action": action,
                    "executable": {"path": "/fixture/bin/" + action, "resolvedPath": "/fixture/bin/" + action,
                                   "exists": True, "identity": [1, 2, 33261, 10, 0, 0]},
                    "cwd": {"path": str(home), "identity": [1, 3, 16832]},
                    "scope": {"selectorsDigest": "0" * 64, "files": []},
                    "target": {"path": str(home / ".cache" / action)}, "roots": []}

        with patch.object(Policy, "fingerprint", new=property(lambda self: "d" * 64)), \
                patch.object(source_identity, "fingerprint", return_value="c" * 64), \
                patch.object(receipts.secrets, "token_hex", side_effect=["a" * 32, "b" * 32]), \
                patch.object(receipts.uuid, "uuid4", side_effect=[SimpleNamespace(hex="1" * 32), SimpleNamespace(hex="2" * 32)]), \
                patch.object(receipts.time, "time", return_value=1700000000), \
                patch.object(maintenance, "PLATFORM", "linux"), \
                patch.object(native_context, "capture", side_effect=capture), \
                patch.object(native_context, "verify", side_effect=lambda action, binding, policy, *, platform:
                             capture(action, policy, platform=platform)), \
                patch.object(native_context, "execution_environment", return_value={}), locked(state):
            capabilities = contracts.capabilities(manifest)
            selected = receipts.request("maintain", actions=["go", "npm"])
            maintenance_preview = receipts.preview("maintain", policy, state, selected)
            run_id = "3" * 32
            held = state / "quarantine" / run_id / "0"
            held.parent.mkdir(parents=True)
            held.write_bytes(b"synthetic fixture content")
            proof = signature(held.stat())
            write_record(state, "runs", {"id": run_id, "createdAt": 1699999900,
                "snapshot": None, "repositoryId": None,
                "items": [{"path": str(home / "Downloads" / "report.bin"), "action": "quarantine",
                           "targets": "files", "status": "quarantined", "size": held.stat().st_size,
                           "signature": proof, "heldSignature": proof, "heldPath": str(held), "expiresAt": 1700100000}], "errors": []})
            with patch.object(lifecycle, "_target_binding", return_value={
                    "parents": [{"path": "/", "device": 1, "inode": 1},
                                {"path": str(home), "device": 1, "inode": 2}], "missingParents": []}):
                restore_preview = receipts.preview("restore", policy, state, receipts.request(
                    "restore", run_id=run_id, target=home / "recovered"))
            results = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 5, "", "")]
            with patch.object(maintenance, "run_process", side_effect=results):
                partial = receipts.confirm("maintain", policy, state, selected, maintenance_preview["previewToken"])

        def normalize(value, key=""):
            if key in {"signature", "heldSignature"}:
                return [1, 2, 24, 0, 0]
            if key == "manifestDigest":
                return "e" * 64
            if isinstance(value, str):
                return value.replace(str(home), "/fixture/home")
            if isinstance(value, dict):
                return {name: normalize(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        return normalize({"contractVersion": 1, "capabilities": capabilities,
                          "maintenancePreview": maintenance_preview, "restorePreview": restore_preview,
                          "partialResult": partial})


class StorageReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.state = self.home / "state"
        self.policy = Policy(self.home, "manual", 30, (), (), (), (), "", "",
                             ("go", "npm"), machine_id="fixture", platform="linux")
        self.selected = receipts.request("maintain", actions=["go"])
        self.real_preview = maintenance.preview
        self.fake = patch.object(maintenance, "preview", side_effect=fake_preview)
        self.fake.start()
        self.addCleanup(self.fake.stop)

    def preview(self):
        with locked(self.state):
            return receipts.preview("maintain", self.policy, self.state, self.selected)

    def confirm(self, token, *, policy=None, selected=None):
        with locked(self.state):
            return receipts.confirm("maintain", policy or self.policy, self.state,
                                    self.selected if selected is None else selected, token)

    def invoke(self, argv, *, width=80, color=False):
        output = io.StringIO()
        with patch.object(cli, "_context", return_value=(self.policy, self.state)), \
                patch.object(cli.log, "console", Console(file=output, width=width, force_terminal=color)):
            result = CliRunner().invoke(cli.app, argv)
        self.assertEqual(result.output, "", result.output)
        return result, output.getvalue()

    def save_plan(self):
        # The actual planner producer runs an empty synthetic policy: no scan/probe.
        with locked(self.state):
            return planner.plan(self.policy, self.state)

    def test_shared_fixture_matches_real_producers(self):
        # Nix copies tests separately; the repository root belongs to utils.
        fixture = utils.ENVY_ROOT / "resources/scripts/envy-tui/src/storage/fixtures/operations-v1.json"
        with patch.object(maintenance, "preview", self.real_preview):
            produced = produce_operations_fixture()
        self.assertEqual(json.loads(fixture.read_text()), produced)
        self.assertEqual(produced["partialResult"]["state"], "partial")
        self.assertEqual(produced["maintenancePreview"]["kind"], "preview")
        self.assertEqual(produced["restorePreview"]["preview"]["items"][0]["status"], "held")
        self.assertNotIn("secret@example", json.dumps(produced))

    def test_status_unknown_summary_hides_nonce_and_binding(self):
        token = self.preview()["previewToken"]
        record = read_record(self.state, "previews", token)
        record["state"] = "claimed"
        write_record(self.state, "previews", record)
        result, text = self.invoke(["status", "--json"])
        self.assertEqual(result.exit_code, 0, result.exception)
        summary = json.loads(text)["data"]["operations"][0]
        self.assertEqual(summary["state"], "unknown")
        self.assertEqual(summary["operationId"], record["operationId"])
        self.assertNotIn(token, json.dumps(summary))
        self.assertNotIn("binding", summary)

    def test_preview_cancellation_does_not_execute_and_keeps_private_receipt(self):
        with patch.object(maintenance, "maintain", side_effect=AssertionError("native")):
            preview = self.preview()
        self.assertEqual(preview["kind"], "preview")
        self.assertEqual(preview["state"], "ready")
        self.assertIsNone(preview["result"])
        record = self.state / "previews" / (preview["previewToken"] + ".json")
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertNotIn("binding", preview)
        self.assertNotIn("host", preview)

    def test_complete_and_duplicate_confirmation_calls_engine_once(self):
        token = self.preview()["previewToken"]
        raw = {"execute": True, "items": [{"action": "go", "output": [{"returncode": 0}]}], "errors": []}
        with patch.object(maintenance, "maintain", return_value=raw) as native:
            result = self.confirm(token)
            repeated = self.confirm(token)
        self.assertEqual(result, repeated)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["result"], raw)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(native.call_args.kwargs["approved_preview"], result["preview"])

    def test_consumed_receipts_ignore_source_policy_expiry_and_native_drift(self):
        for outcome in ("complete", "partial", "claimed", "unknown"):
            for drift in ("source", "policy", "expired", "opt-out", "native-context"):
                with self.subTest(outcome=outcome, drift=drift):
                    token = self.preview()["previewToken"]
                    if outcome == "claimed":
                        record = read_record(self.state, "previews", token)
                        record["state"] = "claimed"
                        write_record(self.state, "previews", record)
                        expected = None
                    else:
                        raw = {"items": [{"action": "go", "verified": True}],
                               "errors": ["fake failure"] if outcome == "partial" else [],
                               "unknown": [{"reason": "fake unknown"}] if outcome == "unknown" else []}
                        with patch.object(maintenance, "maintain", return_value=raw):
                            expected = self.confirm(token)
                    policy = (replace(self.policy, quarantine_days=99) if drift == "policy" else
                              replace(self.policy, maintenance=()) if drift == "opt-out" else self.policy)
                    context = (patch.object(source_identity, "fingerprint", return_value="changed-source")
                               if drift == "source" else
                               patch.object(receipts.time, "time", return_value=time.time() + receipts.PREVIEW_TTL + 1)
                               if drift == "expired" else
                               patch.object(maintenance, "preview", return_value={"changed": True}))
                    with context, patch.object(receipts, "_metadata", side_effect=AssertionError("probe")) as probe, \
                            patch.object(maintenance, "maintain", side_effect=AssertionError("leaf")) as leaf:
                        replay = self.confirm(token, policy=policy)
                    probe.assert_not_called()
                    leaf.assert_not_called()
                    if expected is None:
                        self.assertEqual(replay["state"], "unknown")
                        self.assertEqual(replay["unknown"][0]["code"], "unknown_operation")
                    else:
                        self.assertEqual(replay, expected)

    def test_consumed_receipts_reject_wrong_request_machine_host_and_physical_state(self):
        for outcome in ("complete", "partial", "claimed"):
            for drift in ("request", "operation", "machine", "platform", "hostname", "home", "physical-state"):
                with self.subTest(outcome=outcome, drift=drift):
                    token = self.preview()["previewToken"]
                    if outcome == "claimed":
                        record = read_record(self.state, "previews", token)
                        record["state"] = "claimed"
                        write_record(self.state, "previews", record)
                    else:
                        with patch.object(maintenance, "maintain", return_value={
                                "errors": ["fake failure"] if outcome == "partial" else []}):
                            self.confirm(token)
                    policy = self.policy
                    selected = self.selected
                    operation = "maintain"
                    if drift == "request":
                        selected = {"actions": ["npm"]}
                    elif drift == "operation":
                        operation = "purge"
                    elif drift == "machine":
                        policy = replace(policy, machine_id="other-machine")
                    elif drift == "platform":
                        policy = replace(policy, platform="darwin")
                    elif drift == "home":
                        home = self.home / ("other-home-" + token)
                        home.mkdir()
                        policy = replace(policy, home=home)
                    elif drift == "physical-state":
                        old = self.home / ("old-state-" + token)
                        self.state.rename(old)
                        shutil.copytree(old, self.state)
                    context = (patch.object(receipts.os, "uname", return_value=type("Host", (), {"nodename": "other-host"})())
                               if drift == "hostname" else patch.object(receipts, "PREVIEW_TTL", receipts.PREVIEW_TTL))
                    with context, locked(self.state), \
                            patch.object(receipts, "_metadata", side_effect=AssertionError("probe")) as probe, \
                            patch.object(maintenance, "maintain", side_effect=AssertionError("leaf")) as leaf:
                        with self.assertRaises(StorageError) as error:
                            receipts.confirm(operation, policy, self.state, selected, token)
                    self.assertEqual(error.exception.code, "stale_preview")
                    probe.assert_not_called()
                    leaf.assert_not_called()

    def test_consumed_query_never_reads_current_source_policy_or_writes_a_receipt(self):
        token = self.preview()["previewToken"]
        with patch.object(maintenance, "maintain", return_value={"errors": []}):
            expected = self.confirm(token)
        def unavailable_policy(policy):
            raise AssertionError("current policy fingerprint")
        with patch.object(source_identity, "fingerprint", side_effect=AssertionError("source read")) as source, \
                patch.object(Policy, "fingerprint", new=property(unavailable_policy)), \
                patch.object(receipts, "_persist", side_effect=AssertionError("receipt write")) as persist, \
                patch.object(maintenance, "maintain", side_effect=AssertionError("leaf")) as leaf:
            self.assertEqual(self.confirm(token), expected)
        source.assert_not_called()
        persist.assert_not_called()
        leaf.assert_not_called()

    def test_consumed_cli_receipt_survives_policy_opt_out(self):
        token = self.preview()["previewToken"]
        with patch.object(maintenance, "maintain", return_value={"errors": []}):
            expected = self.confirm(token)
        with patch.object(self, "policy", replace(self.policy, maintenance=())), \
                patch.object(maintenance, "preview", side_effect=AssertionError("probe")) as probe, \
                patch.object(maintenance, "maintain", side_effect=AssertionError("leaf")) as leaf:
            result, text = self.invoke(["maintain", "--go", "--preview-token", token, "--yes", "--json"])
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertEqual(json.loads(text)["data"], expected)
        probe.assert_not_called()
        leaf.assert_not_called()

    def test_partial_preserves_every_engine_step(self):
        token = self.preview()["previewToken"]
        raw = {"execute": True, "items": [{"action": "go", "output": [
            {"command": "fake first", "returncode": 0},
            {"command": "fake second", "returncode": 5}], "note": "second failed"}], "errors": ["go"]}
        with patch.object(maintenance, "maintain", return_value=raw):
            result = self.confirm(token)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["result"], raw)

    def test_policy_code_host_selection_target_ttl_drift_reject_before_native(self):
        with patch.object(maintenance, "maintain", side_effect=AssertionError("native")) as native:
            for drift in ("policy", "machine", "platform", "code", "host", "selection", "target", "ttl"):
                with self.subTest(drift=drift):
                    token = self.preview()["previewToken"]
                    kwargs = {}
                    if drift == "policy":
                        kwargs["policy"] = replace(self.policy, quarantine_days=99)
                    elif drift == "machine":
                        kwargs["policy"] = replace(self.policy, machine_id="other-machine")
                    elif drift == "platform":
                        kwargs["policy"] = replace(self.policy, platform="darwin")
                    elif drift == "selection":
                        kwargs["selected"] = {"actions": ["npm"]}
                    if drift == "host":
                        old = self.home / ("old-state-" + token)
                        self.state.rename(old)
                        shutil.copytree(old, self.state)
                    context = (patch.object(source_identity, "fingerprint", return_value="different") if drift == "code"
                               else patch.object(receipts.time, "time", return_value=time.time() + 301) if drift == "ttl"
                               else patch.object(maintenance, "preview", return_value={"execute": False, "items": []}) if drift == "target"
                               else patch.object(receipts, "PREVIEW_TTL", receipts.PREVIEW_TTL))
                    with context, self.assertRaises(StorageError) as error:
                        self.confirm(token, **kwargs)
                    self.assertEqual(error.exception.code, "expired_preview" if drift == "ttl" else "stale_preview")
        native.assert_not_called()

    def test_missing_manifest_identity_is_rejected_without_provider_probe(self):
        for policy in (replace(self.policy, machine_id=""), replace(self.policy, platform="")):
            with locked(self.state), patch.object(maintenance, "preview") as probe:
                with self.assertRaises(StorageError):
                    receipts.preview("maintain", policy, self.state, self.selected)
                probe.assert_not_called()

    def test_empty_and_policy_disabled_selection_never_calls_provider(self):
        for args in (["maintain", "--preview", "--json"], ["maintain", "--preview-token", "a" * 32, "--yes", "--json"]):
            with patch.object(maintenance, "preview") as probe, patch.object(maintenance, "maintain") as native:
                result, text = self.invoke(args)
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(json.loads(text)["error"]["code"], "invalid_preview")
            probe.assert_not_called()
            native.assert_not_called()
        with locked(self.state), patch.object(maintenance, "preview") as probe:
            with self.assertRaises(StorageError):
                receipts.preview("maintain", replace(self.policy, maintenance=()), self.state, self.selected)
            probe.assert_not_called()

    def test_flag_conflicts_no_context_no_implicit_plan(self):
        for command in ("run", "purge", "maintain", "restore"):
            base = [command, *(["a" * 32, "--target", str(self.home / "recovery")] if command == "restore" else [])]
            for flags in (["--preview", "--yes"], ["--preview", "--preview-token", "a" * 32], ["--preview-token", "a" * 32]):
                with patch.object(cli, "_context", side_effect=AssertionError("context")):
                    result, text = self.invoke([*base, *flags, "--json"])
                self.assertEqual(result.exit_code, 1, result.exception)
                self.assertEqual(json.loads(text)["error"]["code"], "invalid_preview")
        with patch.object(planner, "plan", side_effect=AssertionError("implicit plan")):
            result, text = self.invoke(["run", "--preview", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(json.loads(text)["error"]["code"], "invalid_preview")

    def test_cli_operation_envelopes_use_exact_command_names(self):
        plan_id = self.save_plan()["id"]
        for operation, arguments in (("run", [plan_id]), ("maintain", ["--go"]),
                                     ("purge", []), ("restore", ["a" * 32, "--target", str(self.home / "recovery")])):
            with self.subTest(operation=operation), \
                    patch.object(lifecycle, "preview_restore", return_value={"items": []}), \
                    patch.object(lifecycle, "preview_purge", return_value={"approvedRuns": []}), \
                    patch.object(lifecycle, "execute", return_value={"errors": []}), \
                    patch.object(maintenance, "maintain", return_value={"errors": []}), \
                    patch.object(lifecycle, "restore", return_value={"errors": []}), \
                    patch.object(lifecycle, "purge", return_value={"errors": []}):
                result, text = self.invoke([operation, *arguments, "--preview", "--json"])
                self.assertEqual(result.exit_code, 0, result.exception)
                payload = json.loads(text)
                self.assertEqual(payload["command"], "storage " + operation)
                token = payload["data"]["previewToken"]
                result, text = self.invoke([operation, *arguments, "--preview-token", token, "--yes", "--json"])
                self.assertEqual(result.exit_code, 0, result.exception)
                self.assertEqual(json.loads(text)["command"], "storage " + operation)
                result, text = self.invoke([operation, *arguments, "--preview-token", token, "--json"])
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(json.loads(text)["command"], "storage " + operation)

    def test_json_color_narrow_and_partial_exit(self):
        result, text = self.invoke(["maintain", "--go", "--preview", "--json"], width=15, color=True)
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertNotIn("\x1b", text)
        payload = json.loads(text)
        self.assertEqual(payload["command"], "storage maintain")
        token = payload["data"]["previewToken"]
        with patch.object(maintenance, "maintain", return_value={"items": [], "errors": ["go"]}):
            result, text = self.invoke(["maintain", "--go", "--preview-token", token, "--yes", "--json"], width=10, color=True)
        payload = json.loads(text)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(payload["command"], "storage maintain")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["data"]["state"], "partial")
        self.assertEqual(payload["error"]["code"], "partial_failure")

    def test_human_preview_shows_real_target_effect_and_token(self):
        result, text = self.invoke(["maintain", "--go", "--preview"])
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertIn(str(self.home / "go"), text)
        self.assertIn("Synthetic provider only", text)
        self.assertIn("Preview token", text)

    def test_new_plan_origin_is_producer_owned_and_full_content_bound(self):
        plan = self.save_plan()
        selected = receipts.request("run", plan_id=plan["id"])
        with locked(self.state):
            preview = receipts.preview("run", self.policy, self.state, selected)
        self.assertEqual(preview["preview"], plan)
        plan["candidates"].append({"path": "/synthetic-not-approved"})
        write_record(self.state, "plans", plan)
        with locked(self.state), patch.object(lifecycle, "execute") as execute:
            with self.assertRaises(StorageError) as error:
                receipts.confirm("run", self.policy, self.state, selected, preview["previewToken"])
        self.assertEqual(error.exception.code, "stale_preview")
        execute.assert_not_called()

    def test_legacy_unproven_and_expired_plans_need_regeneration(self):
        for kind in ("unproven", "expired", "model", "rules"):
            plan = self.save_plan()
            if kind == "unproven":
                (self.state / "plan-origins" / (plan["id"] + ".json")).unlink()
            elif kind == "expired":
                plan["createdAt"] -= 86401
            elif kind == "model":
                plan["modelVersion"] = MODEL_VERSION - 1
            else:
                plan["rules"].append({"id": "unapproved"})
            write_record(self.state, "plans", plan)
            with locked(self.state), self.assertRaises(StorageError):
                receipts.preview("run", self.policy, self.state, receipts.request("run", plan_id=plan["id"]))

    def test_restore_preview_dispatch_never_calls_restore_or_creates_target(self):
        target = self.home / "new-recovery"
        with patch.object(lifecycle, "preview_restore", return_value={"items": [], "metadata": {"archiveReadRequired": False}}) as preview, \
                patch.object(lifecycle, "restore", side_effect=AssertionError("restore")):
            result, text = self.invoke(["restore", "a" * 32, "--target", str(target), "--preview", "--json"])
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertFalse(target.exists())
        preview.assert_called_once_with(self.policy, self.state, "a" * 32, target)
        token = json.loads(text)["data"]["previewToken"]
        selected = receipts.request("restore", run_id="a" * 32, target=self.home / "other")
        with locked(self.state), self.assertRaises(StorageError):
            receipts.confirm("restore", self.policy, self.state, selected, token)

    def test_remote_restore_preview_contract_describes_read_only_archive_verification(self):
        run_id = "a" * 32
        original = str(self.home / "original.bin")
        snapshot, repository_id, content_hash = "b" * 64, "c" * 64, "d" * 64
        write_record(self.state, "runs", {"id": run_id, "snapshot": snapshot,
                     "repositoryId": repository_id, "items": [{"path": original, "action": "archive",
                     "targets": "files", "status": "archived", "sha256": content_hash}]})
        target = self.home / "missing" / "recovery"
        with patch.object(lifecycle, "Archive", autospec=True) as archive, \
                patch.object(lifecycle, "restore", side_effect=AssertionError("restore")) as restore:
            archive.return_value.identity.return_value = repository_id
            archive.return_value.content_hash.return_value = content_hash
            result, text = self.invoke(["restore", run_id, "--target", str(target), "--preview", "--json"])
        self.assertEqual(result.exit_code, 0, result.exception)
        preview = json.loads(text)["data"]["preview"]
        self.assertEqual(preview["items"][0]["status"], "remote")
        self.assertTrue(preview["metadata"]["archiveReadRequired"])
        archive.return_value.identity.assert_called_once_with()
        archive.return_value.content_hash.assert_called_once_with(snapshot, original)
        archive.return_value.restore.assert_not_called()
        archive.return_value.restore_file.assert_not_called()
        restore.assert_not_called()
        self.assertFalse(target.parent.exists())
        command = next(item for item in contracts.capabilities({})["commands"] if item["id"] == "restore")
        effect = next(item["effect"] for item in command["flags"] if item["flag"] == "--preview")
        for phrase in ("read-only archive", "credentials", "time", "without creating the target"):
            self.assertIn(phrase, effect)
        self.assertIn("read-only archive", command["sideEffects"])
        self.assertNotIn("reads metadata only", command["sideEffects"])

    def test_purge_passes_exact_approved_item_selection(self):
        metadata = {"candidates": [], "errors": [], "approvedRuns": ["a" * 32],
                    "eligible": [{"run": "a" * 32, "items": [{"path": "/fixture/item"}]}],
                    "metadata": {"archiveReadRequired": False}}
        with patch.object(lifecycle, "preview_purge", return_value=metadata), locked(self.state):
            preview = receipts.preview("purge", self.policy, self.state, receipts.request("purge"))
            with patch.object(lifecycle, "purge", return_value={"errors": []}) as purge:
                receipts.confirm("purge", self.policy, self.state, receipts.request("purge"), preview["previewToken"])
        purge.assert_called_once_with(self.policy, self.state, execute=True,
                                      approved_runs=metadata["approvedRuns"], approved_preview=metadata)

    def test_actual_manifest_supplies_policy_and_machine_in_one_read(self):
        manifest = {"id": "one-manifest", "platform": "linux", "settings": {
            "envy.user.home": str(self.home)}, "storage": {"maintenance": {"go": True}, "rules": []}}
        output = io.StringIO()
        with patch.object(cli, "machine_manifest", return_value=manifest) as read, \
                patch.object(cli, "state_dir", return_value=self.home / "new-state"), \
                patch.object(cli.log, "console", Console(file=output, force_terminal=False)), \
                patch.object(maintenance, "preview", side_effect=fake_preview) as producer:
            result = CliRunner().invoke(cli.app, ["maintain", "--go", "--preview", "--json"])
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertEqual(read.call_count, 1)
        policy = producer.call_args.args[0]
        self.assertEqual(policy.machine_id, "one-manifest")
        self.assertEqual(policy.platform, "linux")
        self.assertEqual(policy.maintenance, ("go",))

    def test_legacy_restore_and_scheduled_path_keep_dispatch(self):
        with patch.object(lifecycle, "restore", return_value={"restored": []}) as restore, \
                patch.object(lifecycle, "preview_restore", side_effect=AssertionError("preview")):
            result, _ = self.invoke(["restore", "a" * 32, "--target", str(self.home / "recovery"), "--json"])
        self.assertEqual(result.exit_code, 0, result.exception)
        restore.assert_called_once_with(self.policy, self.state, "a" * 32, self.home / "recovery")
        with patch.object(planner, "plan", side_effect=AssertionError("scheduled flags must reject before plan")):
            result, text = self.invoke(["run", "--scheduled", "--preview", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(json.loads(text)["error"]["code"], "invalid_preview")

    def test_claim_after_atomic_replace_still_requires_strict_file_fsync(self):
        token = self.preview()["previewToken"]
        real_sync = os.fsync
        count = 0
        def fsync(fd):
            nonlocal count
            if stat.S_ISREG(os.fstat(fd).st_mode):
                count += 1
                if count == 2:
                    raise OSError("strict claim file fsync failed")
            return real_sync(fd)
        with patch("os.fsync", side_effect=fsync), patch.object(maintenance, "maintain") as native:
            with self.assertRaises(OSError):
                self.confirm(token)
        native.assert_not_called()
        self.assertEqual(read_record(self.state, "previews", token)["state"], "claimed")
        with patch.object(maintenance, "maintain") as native:
            self.assertEqual(self.confirm(token)["state"], "unknown")
        native.assert_not_called()

    def test_actual_result_fsync_failure_keeps_success_and_prevents_replay(self):
        token = self.preview()["previewToken"]
        real_sync = os.fsync
        count = 0
        def fsync(fd):
            nonlocal count
            if stat.S_ISREG(os.fstat(fd).st_mode):
                count += 1
                if count == 3:
                    raise OSError("result file fsync failed")
            return real_sync(fd)
        raw = {"items": [{"action": "go", "success": True}], "errors": []}
        with patch("os.fsync", side_effect=fsync), patch.object(maintenance, "maintain", return_value=raw) as native:
            result = self.confirm(token)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["result"], raw)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(read_record(self.state, "previews", token)["state"], "claimed")
        with patch.object(maintenance, "maintain") as native:
            self.assertEqual(self.confirm(token)["state"], "unknown")
        native.assert_not_called()

    def test_claim_write_without_replace_is_detected_before_native(self):
        token = self.preview()["previewToken"]
        with patch.object(receipts, "write_record", return_value=False), \
                patch.object(maintenance, "maintain") as native, self.assertRaises(StorageError) as error:
            self.confirm(token)
        self.assertEqual(error.exception.code, "unknown_operation")
        native.assert_not_called()
        self.assertEqual(read_record(self.state, "previews", token)["state"], "ready")

    def test_claim_write_failure_never_executes(self):
        token = self.preview()["previewToken"]
        with patch.object(receipts, "write_record", side_effect=OSError("disk full")), \
                patch.object(maintenance, "maintain") as native, self.assertRaises(OSError):
            self.confirm(token)
        native.assert_not_called()

    def test_real_file_and_directory_fsync_failures_never_execute(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                token = self.preview()["previewToken"]
                real_sync = os.fsync
                def fsync(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode) == directory:
                        raise OSError("injected fsync failure")
                    return real_sync(fd)
                with patch("os.fsync", side_effect=fsync), patch.object(maintenance, "maintain") as native:
                    with self.assertRaises(OSError):
                        self.confirm(token)
                native.assert_not_called()
                # Re-open from an independent interpreter, not a process-local cache.
                script = "from pathlib import Path; from envy.storage.state import read_record; import sys; print(read_record(Path(sys.argv[1]), 'previews', sys.argv[2])['state'])"
                loaded = subprocess.run([sys.executable, "-c", script, str(self.state), token],
                                        capture_output=True, text=True, check=True)
                self.assertEqual(loaded.stdout.strip(), "claimed" if directory else "ready")
                if directory:
                    with patch.object(maintenance, "maintain") as native:
                        self.assertEqual(self.confirm(token)["state"], "unknown")
                    native.assert_not_called()

    def test_result_write_failure_keeps_result_and_durable_claim(self):
        token = self.preview()["previewToken"]
        real_write = receipts.write_record
        def write(root, kind, record):
            if record["state"] != "claimed":
                raise OSError("outcome storage unavailable")
            return real_write(root, kind, record)
        raw = {"items": [{"action": "go", "success": True}], "errors": []}
        with patch.object(receipts, "write_record", side_effect=write), patch.object(maintenance, "maintain", return_value=raw) as native:
            result = self.confirm(token)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["result"], raw)
        self.assertEqual(native.call_count, 1)
        with patch.object(maintenance, "maintain") as native:
            replay = self.confirm(token)
        native.assert_not_called()
        self.assertEqual(replay["state"], "unknown")

    def test_refresh_failure_preserves_completed_execution(self):
        token = self.preview()["previewToken"]
        with patch.object(receipts, "_refresh", side_effect=OSError("refresh unavailable")), \
                patch.object(maintenance, "maintain", return_value={"errors": []}):
            result = self.confirm(token)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["refreshedStatus"]["status"], "unavailable")
        self.assertEqual(result["result"], {"errors": []})

    def test_recovery_partial_error_keeps_known_success_and_durable_cli_result(self):
        for has_unknown in (False, True):
            with self.subTest(has_unknown=has_unknown):
                run_id = "a" * 32
                target = self.home / "recovery"
                arguments = ["restore", run_id, "--target", str(target)]
                with patch.object(lifecycle, "preview_restore", return_value={"items": []}):
                    result, text = self.invoke([*arguments, "--preview", "--json"])
                    self.assertEqual(result.exit_code, 0, result.exception)
                    token = json.loads(text)["data"]["previewToken"]
                    raw = {"run": run_id, "target": str(target),
                           "restored": [str(self.home / "verified.bin")],
                           "errors": [{"path": str(self.home / "failed.bin"), "reason": "Synthetic write failure"}],
                           "unknown": ([{"path": str(self.home / "failed.bin"), "code": "unknown_operation",
                                         "reason": "Synthetic unverified output"}] if has_unknown else []),
                           "unavailable": [{"path": str(self.home / "missing.bin"), "status": "unavailable"}],
                           "notAttempted": [{"path": str(self.home / "later.bin"), "reason": "Recovery stopped"}]}
                    failure = lifecycle.RecoveryPartialError("Synthetic recovery stopped", raw)
                    with patch.object(lifecycle, "restore", side_effect=failure) as leaf:
                        result, text = self.invoke([*arguments, "--preview-token", token, "--yes", "--json"])
                    leaf.assert_called_once()
                self.assertEqual(result.exit_code, 1)
                payload = json.loads(text)
                self.assertEqual(payload["command"], "storage restore")
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], "unknown_operation" if has_unknown else "partial_failure")
                data = payload["data"]
                self.assertEqual(data["state"], "unknown" if has_unknown else "partial")
                self.assertEqual(data["result"], raw)
                self.assertEqual(data["errors"], raw["errors"])
                self.assertEqual(data["unknown"], raw["unknown"])
                durable = read_record(self.state, "previews", token)
                self.assertEqual(durable["result"], raw)
                self.assertEqual(durable["state"], data["state"])
                with patch.object(source_identity, "fingerprint", return_value="changed-after-partial"), \
                        patch.object(self, "policy", replace(self.policy, quarantine_days=99)), \
                        patch.object(receipts.time, "time", return_value=time.time() + receipts.PREVIEW_TTL + 1), \
                        patch.object(receipts, "_metadata", side_effect=AssertionError("probe")) as probe, \
                        patch.object(lifecycle, "restore", side_effect=AssertionError("leaf")) as leaf:
                    result, text = self.invoke([*arguments, "--preview-token", token, "--yes", "--json"])
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(json.loads(text)["data"], data)
                probe.assert_not_called()
                leaf.assert_not_called()
                self.assertFalse(target.exists())

    def test_engine_exception_is_unknown_and_not_replayed(self):
        token = self.preview()["previewToken"]
        with patch.object(maintenance, "maintain", side_effect=RuntimeError("injected after effect")) as native:
            result = self.confirm(token)
            replay = self.confirm(token)
        self.assertEqual(result, replay)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(native.call_count, 1)

    def test_real_multiprocess_claim_executes_once(self):
        token = self.preview()["previewToken"]
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        barrier = context.Barrier(2)
        processes = [context.Process(target=_child_confirm, args=(self.policy, self.state, token, queue, False, barrier)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        states = [queue.get(timeout=5) for _ in processes]
        self.assertIn("complete", states)
        self.assertEqual((self.state / "native-calls").read_text(), "called\n")
        with patch.object(maintenance, "maintain") as native:
            self.assertEqual(self.confirm(token)["state"], "complete")
        native.assert_not_called()

    def test_claim_crash_fresh_process_returns_unknown_without_replay(self):
        token = self.preview()["previewToken"]
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=_child_confirm, args=(self.policy, self.state, token, queue, True))
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 91)
        self.assertEqual(read_record(self.state, "previews", token)["state"], "claimed")
        retry = context.Process(target=_child_confirm, args=(self.policy, self.state, token, queue))
        retry.start()
        retry.join(20)
        self.assertEqual(retry.exitcode, 0)
        self.assertEqual(queue.get(timeout=5), "unknown")
        self.assertEqual((self.state / "native-calls").read_text(), "called\n")
        with patch.object(source_identity, "fingerprint", return_value="changed-after-crash"), \
                patch.object(receipts.time, "time", return_value=time.time() + receipts.PREVIEW_TTL + 1), \
                patch.object(maintenance, "preview", side_effect=AssertionError("probe")) as probe, \
                patch.object(maintenance, "maintain", side_effect=AssertionError("leaf")) as leaf:
            replay = self.confirm(token, policy=replace(self.policy, maintenance=(), quarantine_days=99))
        self.assertEqual(replay["state"], "unknown")
        probe.assert_not_called()
        leaf.assert_not_called()


if __name__ == "__main__":
    unittest.main()

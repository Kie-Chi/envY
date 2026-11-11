import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from envy import agent, error, evaluation, host, process
from envy.workflows import check, plan
from envy.agent_models import model_bindings
from envy.config import machine_nix
from envy.errno import ErrorCode
from envy.evaluation import informative_nix_error
from envy.main import cli


class AgentDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "envy.agents.pi.adapter": "pi",
            "envy.llm.endpoints.demo.baseUrl": "https://example.invalid/v1",
            "envy.llm.credentials.demo.secretPath": "llm/demo/key",
            "envy.agentProfiles.fast.model": "test-model",
            "envy.agentProfiles.fast.endpoint": "demo",
            "envy.agentProfiles.fast.credential": "demo",
        }

    def diagnostic(self, models, values=None):
        with self.assertRaises(ValueError) as raised:
            model_bindings(self.values if values is None else values, {
                "adapter": "pi", "model": None, "models": models,
            })
        return str(raised.exception)

    def test_inline_unknown_credential_names_the_entry_and_declaration(self):
        text = self.diagnostic([{"profile": "fast"}, {
            "model": "test-model", "endpoint": "demo", "credential": "missing",
        }])
        self.assertIn("envy.agents.pi.models[1].credential", text)
        self.assertIn("credential 'missing' is not declared", text)
        self.assertIn("envy.llm.credentials.missing.secretPath", text)
        self.assertNotIn("or null", text)

    def test_inherited_reference_names_the_profile_and_override_removes_it(self):
        values = self.values | {"envy.agentProfiles.fast.credential": "missing"}
        text = self.diagnostic([{"profile": "fast"}], values)
        self.assertIn("envy.agents.pi.models[0].credential", text)
        self.assertIn("inherited from envy.agentProfiles.fast.credential", text)
        bindings = model_bindings(values, {"adapter": "pi", "model": None,
            "models": [{"profile": "fast", "credential": "demo"}]})
        self.assertEqual(bindings[0]["selection"]["credential"], "demo")

    def test_missing_model_endpoint_and_profile_are_distinct(self):
        for models, phrase, location, fix in [
            ([{"endpoint": "demo"}], "model is not set", ".models[0].model", ".models[0].model"),
            ([{"model": "m", "endpoint": "missing"}], "endpoint 'missing' is not declared", ".models[0].endpoint", "envy.llm.endpoints.missing.baseUrl"),
            ([{"profile": "missing"}], "profile 'missing' is not declared", ".models[0].profile", "envy.agentProfiles.missing"),
        ]:
            with self.subTest(phrase=phrase):
                text = self.diagnostic(models)
                self.assertIn(phrase, text)
                self.assertIn(location, text)
                self.assertIn(fix, text)

    def test_declared_credential_missing_metadata_is_not_an_unknown_reference(self):
        values = self.values | {"envy.llm.credentials.partial.kind": "api-key"}
        text = self.diagnostic([{"profile": "fast", "credential": "partial"}], values)
        self.assertIn("envy.llm.credentials.partial.secretPath", text)
        self.assertIn("secretPath is not set", text)
        self.assertNotIn("is not declared", text)

    def test_conflict_names_both_entries_and_differing_fields(self):
        text = self.diagnostic([{"profile": "fast"}, {"profile": "fast", "maxTokens": 123}])
        self.assertIn("conflicting metadata", text)
        self.assertIn("envy.agents.pi.models[0]", text)
        self.assertIn("envy.agents.pi.models[1]", text)
        self.assertIn("maxTokens", text)
        self.assertLess(text.index("conflicting metadata"), text.index("Location:"))
        self.assertLess(text.index("Location:"), text.index("Next:"))

    def test_validation_preserves_reference_error_code_and_path(self):
        values = self.values | {"envy.agents.pi.models": [{"profile": "fast", "credential": "missing"}]}
        issues = agent._validate_issues(values)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, ErrorCode.UNKNOWN_REFERENCE)
        self.assertEqual(issues[0].path, "envy.agents.pi.models[0].credential")
        self.assertIn("envy.llm.credentials.missing.secretPath", issues[0].hint)

    def test_legacy_agent_and_unused_profile_references_are_precise(self):
        for path in ("envy.agents.codex.credential", "envy.agentProfiles.fast.credential"):
            with self.subTest(path=path):
                values = self.values | {"envy.agents.codex.adapter": "codex", path: "missing"}
                issues = agent._validate_issues(values)
                item = next(item for item in issues if item.path == path)
                self.assertIn("credential 'missing' is not declared", item.message)
                self.assertIn("envy.llm.credentials.missing.secretPath", item.hint)

    def test_unknown_primary_profile_is_reported_once(self):
        issues = agent._validate_issues(self.values | {"envy.agents.pi.profile": "missing", "envy.agents.pi.models": []})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].path, "envy.agents.pi.profile")

    def test_refresh_rejects_invalid_configuration_before_decryption(self):
        values = self.values | {"envy.agents.pi.models": [{"profile": "fast", "credential": "missing"}]}
        with patch.object(agent, "_read_values", return_value=values), \
             patch.object(agent, "read_secrets_data") as decrypt, \
             patch.object(agent, "atomic_write_text") as write:
            for structured in (False, True):
                result = CliRunner().invoke(cli, ["agent", "refresh"] + (["--json"] if structured else []))
                self.assertNotEqual(result.exit_code, 0)
                if structured:
                    payload = json.loads(result.output)
                    self.assertEqual(payload["error"]["code"], "unknown-reference")
                    self.assertIn("credential 'missing' is not declared", payload["error"]["message"])
                else:
                    self.assertIn("credential 'missing' is not declared", result.output)
            decrypt.assert_not_called()
            write.assert_not_called()

    def test_cli_and_json_errors_do_not_write_policy_or_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = Path(directory) / "machine.nix"
            machine.write_text("{ ... }: {\n}\n")
            with patch.object(agent, "machine_config_file", return_value=machine), \
                 patch.object(machine_nix, "machine_config_file", return_value=machine), \
                 patch.object(agent, "refresh_runtime") as refresh, \
                 patch.object(agent, "offer_mutation_commit") as commit:
                agent._write_values(self.values)
                before = machine.read_bytes()
                models = json.dumps([{"profile": "fast", "credential": "missing"}])
                for command in ("agent", "config"):
                    for structured in (False, True):
                        with self.subTest(command=command, structured=structured):
                            args = [command, "set", "envy.agents.pi.models", models, "--yes"]
                            result = CliRunner().invoke(cli, args + (["--json"] if structured else []))
                            self.assertNotEqual(result.exit_code, 0, result.output)
                            text = result.output
                            if structured:
                                payload = json.loads(text)
                                self.assertEqual(payload["schemaVersion"], 1)
                                self.assertFalse(payload["ok"])
                                self.assertEqual(payload["error"]["code"], "invalid-value")
                                text = payload["error"]["message"]
                            text = " ".join(text.replace("│", " ").split())
                            self.assertIn("credential 'missing' is not declared", text)
                            self.assertIn("envy.agents.pi.models[0].credential", text)
                            self.assertIn("envy.llm.credentials.missing.secretPath", text)
                            self.assertEqual(machine.read_bytes(), before)
                refresh.assert_not_called()
                commit.assert_not_called()


class ExternalDiagnosticTests(unittest.TestCase):
    def encoded(self, **overrides):
        issue = {"code": "unknown-reference", "path": "envy.agents.pi.models[0].credential",
            "message": "credential 'missing' is not declared",
            "hint": "Declare envy.llm.credentials.missing.secretPath first."} | overrides
        return "error: [envy-config] " + json.dumps(issue)

    def render(self, stderr, debug=False):
        output = io.StringIO()
        with patch.object(process.log, "console", Console(file=output, width=240, force_terminal=False)), \
             patch.object(process.log, "is_debug", return_value=debug):
            process.render_command_error(process.CommandError(subprocess.CompletedProcess(["nix", "eval"], 1, "", stderr)))
        return output.getvalue()

    def test_recognized_nix_error_is_presented_before_command_details(self):
        stderr = "error: while evaluating models\n" + self.encoded() + "\n at /test/model.nix:1"
        text = self.render(stderr)
        self.assertIn("credential 'missing' is not declared", text)
        self.assertIn("Location: envy.agents.pi.models[0].credential", text)
        self.assertIn("Next: Declare envy.llm.credentials.missing.secretPath first.", text)
        self.assertLess(text.index("credential 'missing'"), text.index("Location:"))
        self.assertLess(text.index("Location:"), text.index("Next:"))
        self.assertNotIn("[envy-config]", text)
        self.assertNotIn("while evaluating", text)
        summary = informative_nix_error(stderr)
        self.assertIn("credential 'missing' is not declared", summary)
        self.assertIn("envy.agents.pi.models[0].credential", summary)
        self.assertIn("envy.llm.credentials.missing.secretPath", summary)

    def test_check_plan_and_host_use_the_common_diagnostic(self):
        stderr = self.encoded() + "\n at /test/model.nix:1"
        failed = subprocess.CompletedProcess(["nix", "eval"], 7, "", stderr)
        output = io.StringIO()
        with patch.object(process.log, "console", Console(file=output, width=240, force_terminal=False)):
            with patch.object(check, "run_process", return_value=failed):
                self.assertEqual(check.run_machine_checks([("linux", "test")]), [("linux", "test", False)])
            self.assertIn("credential 'missing' is not declared", output.getvalue())
            self.assertNotIn("[envy-config]", output.getvalue())
            output.seek(0)
            output.truncate()
            with patch.object(plan, "run_process", return_value=failed), \
                 patch.object(plan, "current_machine_id", return_value="test"):
                result = CliRunner().invoke(cli, ["plan", "--json"])
                self.assertEqual(result.exit_code, 7)
                payload = json.loads(result.output)
                self.assertEqual(payload["schemaVersion"], 1)
                self.assertFalse(payload["ok"])
                self.assertIn("envy.llm.credentials.missing.secretPath", payload["error"])
            with tempfile.TemporaryDirectory() as directory:
                machine = Path(directory) / "test.nix"
                machine.touch()
                with patch.object(host, "machine_file", return_value=machine), \
                     patch.object(host, "run_process", return_value=failed) as run:
                    result = CliRunner().invoke(cli, ["host", "check", "test"])
                    self.assertEqual(result.exit_code, 7)
                    self.assertTrue(run.call_args.kwargs["capture"])
                    self.assertIn("Location: envy.agents.pi.models[0].credential", result.output)
                    self.assertNotIn("[envy-config]", result.output)

    def test_unknown_command_error_retains_all_original_diagnostic_lines(self):
        stderr = "unclassified prefix\n" + "x" * 600 + "\n  error: unfamiliar failure\noriginal context\noriginal suffix"
        output = io.StringIO()
        with patch.object(process.log, "console", Console(file=output, width=1000, force_terminal=False)), \
             patch.object(process.log, "is_debug", return_value=False):
            process.render_command_error(process.CommandError(subprocess.CompletedProcess(["nix", "eval"], 1, "", stderr)))
        for line in stderr.splitlines():
            self.assertIn(line, output.getvalue())
        self.assertNotIn("Location:", output.getvalue())
        self.assertNotIn("Next:", output.getvalue())

    def test_unknown_nix_diagnostics_survive_plan_check_and_manifest_entry_points(self):
        context = "\n  at /example/host.nix:42:7\noriginal multiline context\n" + "x" * 600 + "\noriginal suffix\n"
        for prefix in ("error: unfamiliar failure", "unclassified diagnostic",
                       "error: [envy-config] not-json", self.encoded(code="unknown-future-code"),
                       self.encoded(path=17)):
            stderr = prefix + context
            failed = subprocess.CompletedProcess(["nix", "eval"], 7, "", stderr)
            with self.subTest(prefix=prefix):
                self.assertEqual(informative_nix_error(stderr), stderr)
                with patch.object(check, "run_process", return_value=failed), \
                     patch.object(process.log, "hint") as hint:
                    self.assertEqual(check.run_machine_checks([("linux", "test")]), [("linux", "test", False)])
                    hint.assert_called_once_with(stderr)
                with patch.object(plan, "run_process", return_value=failed), \
                     patch.object(plan, "current_machine_id", return_value="test"):
                    result = CliRunner().invoke(cli, ["plan", "--json"])
                    self.assertEqual(result.exit_code, 7)
                    self.assertEqual(json.loads(result.output), {
                        "schemaVersion": 1, "machine": "test", "ok": False, "error": stderr})
                    with patch.object(process.log, "hint") as hint:
                        result = CliRunner().invoke(cli, ["plan"])
                        self.assertEqual(result.exit_code, 7)
                        hint.assert_called_once_with(stderr)
                with patch.object(evaluation, "run_process", return_value=failed), \
                     patch.object(evaluation, "_last_manifest_error", None):
                    self.assertIsNone(evaluation._evaluate_machine_manifest("test"))
                    self.assertEqual(evaluation.last_manifest_error(), stderr)
                with patch.object(host, "run_process", return_value=failed):
                    with self.assertRaises(RuntimeError) as raised:
                        host.evaluate_machine_manifest("linux", "test")
                    self.assertEqual(str(raised.exception), "cannot evaluate linux/test: " + stderr)

    def test_empty_nix_diagnostics_keep_the_existing_fallback(self):
        for stderr in ("", "\n \t\n"):
            with self.subTest(stderr=stderr):
                self.assertEqual(informative_nix_error(stderr), "nix eval exited without stderr")
                self.assertEqual(informative_nix_error(stderr, fallback="fixture fallback"), "fixture fallback")

    def test_host_check_keeps_successful_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = Path(directory) / "test.nix"
            machine.touch()
            result = subprocess.CompletedProcess(["nix", "eval"], 0, "/nix/store/test.drv", "")
            with patch.object(host, "machine_file", return_value=machine), \
                 patch.object(host, "run_process", return_value=result):
                invoked = CliRunner().invoke(cli, ["host", "check", "test"])
                self.assertEqual(invoked.exit_code, 0)
                self.assertIn("/nix/store/test.drv", invoked.output)

    def test_unknown_errors_and_malformed_payloads_remain_raw(self):
        for text in ("error: mysterious failure at provider", "error: [envy-config] not-json",
                     self.encoded(code="unknown-future-code"), self.encoded(path=17)):
            with self.subTest(text=text):
                self.assertIn(text, self.render(text))
                self.assertNotIn("Location:", self.render(text))

    def test_trace_text_and_secret_shaped_fields_cannot_become_diagnostics(self):
        self.assertIsNone(error.parse_nix_error("evaluating expression: " + self.encoded()))
        self.assertIsNone(error.parse_nix_error(self.encoded(apiKey="never-print-this")))


if __name__ == "__main__":
    unittest.main()

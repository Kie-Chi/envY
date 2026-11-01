"""Dynamic terminal text must not be parsed as Rich markup."""
import io
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from envy import host, log, process
from envy.config import cli, secret_store
from envy.doctor import runner
from envy.doctor.model import CheckResult, SECTION_SYSTEM


class LiteralRenderingTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.diagnostics = io.StringIO()
        self.enterContext(patch.object(log, "console", Console(file=self.output, width=240, color_system=None)))
        self.enterContext(patch.object(log, "diagnostic_console", Console(file=self.diagnostics, width=240, color_system=None)))

    def test_log_messages_scopes_and_fields_are_literal(self):
        for method in (log.step, log.info, log.ok, log.fix, log.warn, log.error):
            with self.subTest(method=method.__name__):
                self.output.seek(0)
                self.output.truncate()
                method("scope[bot]", "value[bold] [/tmp/x]", **{"field[red]": "[rejected]"})
                rendered = self.output.getvalue()
                for text in ("scope[bot]", "value[bold] [/tmp/x]", "field[red]=[rejected]"):
                    self.assertIn(text, rendered)

    def test_hint_debug_and_activity_preserve_brackets(self):
        log.hint("[/tmp/x] [bold] [rejected]")
        self.assertIn("[/tmp/x] [bold] [rejected]", self.output.getvalue())
        with patch.dict("os.environ", {"ENVY_DEBUG": "1"}), patch.object(log, "_activity_is_allowed", return_value=True):
            log.debug("scope[bot]", "[/tmp/x]", value="[rejected]")
            log.activity("scope[bot]", "[/tmp/x]", value="[rejected]")
        self.assertEqual(self.diagnostics.getvalue().count("scope[bot]: [/tmp/x] value=[rejected]"), 2)

    def test_log_status_keeps_its_color(self):
        output = io.StringIO()
        with patch.object(log, "console", Console(file=output, force_terminal=True, color_system="standard", highlight=False)):
            log.error("scope", "literal[bot]")
        self.assertIn("\x1b[31mERR", output.getvalue())
        self.assertIn("literal[bot]", output.getvalue())

    def test_external_command_errors_remain_readable(self):
        for debug in ("0", "1"):
            with self.subTest(debug=debug), patch.dict("os.environ", {"ENVY_DEBUG": debug}):
                self.output.seek(0)
                self.output.truncate()
                error = process.CommandError(subprocess.CompletedProcess(
                    ["git", "push", "path[bot]"], 1, "",
                    "error: failed to push [/tmp/x]\n! [rejected] master (non-fast-forward)\n",
                ))
                process.render_command_error(error)
                rendered = self.output.getvalue()
                for text in ("path[bot]", "[/tmp/x]", "[rejected]", "envy sync"):
                    self.assertIn(text, rendered)

    def test_doctor_cells_hints_and_actions_are_literal(self):
        results = [CheckResult(
            SECTION_SYSTEM, "name[bot]", "warn", "result[bold] [/tmp/x]",
            hint="hint[rejected] [/tmp/y]", action={"kind": "open-app", "name": "App[bot]"},
        )]
        runner.render(results)
        rendered = self.output.getvalue()
        for text in ("name[bot]", "result[bold] [/tmp/x]", "hint[rejected] [/tmp/y]", "open App[bot]"):
            self.assertIn(text, rendered)
        self.assertIn("completed with warnings", rendered)

    def test_doctor_json_does_not_gain_escape_characters(self):
        results = [CheckResult(SECTION_SYSTEM, "name[bot]", "error", "[/tmp/x]")]
        runner.render_json(results)
        payload = json.loads(self.output.getvalue())
        self.assertEqual(payload["results"][0], results[0].to_dict())
        self.assertEqual(payload["summary"]["exitCode"], 1)

    def test_config_values_and_dynamic_fields_are_literal_without_secrets(self):
        fields = [SimpleNamespace(path="envy.git.user", choices=())]
        view = {
            "values": {}, "fields": [{"path": "envy.agents.pi.model", "value": "model[bot]"}],
            "llm": {"credentials": {"cred[bot]": {"credentialSet": True}}}, "agents": {},
        }
        with patch.object(cli, "MACHINE_FIELDS", fields), patch.object(cli, "SECRET_FIELDS", []), patch.object(
            cli, "machine_manifest", return_value={"settings": {"envy.git.user": "renovate[bot] [/tmp/x]"}}
        ), patch.object(cli, "read_device_metadata", return_value={"machine_id": "machine[bot]"}), patch.object(
            secret_store, "read_secrets_yaml", return_value=({}, True)
        ), patch.object(secret_store, "read_secrets_data", return_value=({"token": "hidden-secret"}, True)), patch(
            "envy.agent.config_view", return_value=view
        ):
            cli.cmd_show(refresh=False, json_output=False)
        rendered = self.output.getvalue()
        for text in ("renovate[bot] [/tmp/x]", "machine[bot]", "model[bot]", "credential/cred[bot]", "<set>"):
            self.assertIn(text, rendered)
        self.assertNotIn("hidden-secret", rendered)

    def test_host_diff_preserves_values_and_software_ids(self):
        left = {"settings": {"envy.git.user": "renovate[bot] [/tmp/x]"}, "software": {"groups": {
            "pypi.user.tool": {"selection": {"effective": [{"id": "uvicorn[standard]"}]}},
        }}}
        right = {"settings": {"envy.git.user": "other[bot]"}}
        with patch.object(host, "resolve_machine_entry", side_effect=[("darwin", "a"), ("linux", "b")]), patch.object(
            host, "evaluate_machine_manifest", side_effect=[left, right]
        ):
            host.cmd_diff("a", "b", left_platform=None, right_platform=None, json_output=False)
        for text in ("renovate[bot] [/tmp/x]", "other[bot]", "uvicorn[standard]"):
            self.assertIn(text, self.output.getvalue())


if __name__ == "__main__":
    unittest.main()

import io
import json
import tempfile
import unittest
from pathlib import Path

from rich.console import Console
from typer.main import get_command
from typer.testing import CliRunner
from unittest.mock import patch

from envy.doctor import runner
from envy.doctor.model import info, ok, warn, SECTION_DOCTOR, SECTION_SYSTEM
from envy.jsonio import emit, emit_error
from envy.main import cli


class StructuredOutputTests(unittest.TestCase):
    def test_tui_is_registered_for_help_and_completion(self):
        self.assertIn("tui", get_command(cli).commands)
        result = CliRunner().invoke(cli, ["tui", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Rust/Ratatui frontend", result.output)

    def test_frontend_envelope_has_stable_success_and_error_shapes(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch("envy.jsonio.log.console", console):
            emit("software.add", data={"result": "dry-run"})
            success = json.loads(output.getvalue())
            output.seek(0)
            output.truncate(0)
            emit_error("software.add", "confirmation required", code="confirmation-required")
            failure = json.loads(output.getvalue())

        self.assertEqual(success["schemaVersion"], 1)
        self.assertEqual(success["command"], "software.add")
        self.assertTrue(success["ok"])
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["error"]["code"], "confirmation-required")

    def test_forced_terminal_color_and_narrow_width_cannot_change_json_values(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=True, color_system="standard", width=10)
        data = {"path": "/tmp/" + "long path [bot] 中文/" * 30, "text": "line one\nline two"}
        with patch("envy.jsonio.log.console", console), patch.dict("os.environ", {"FORCE_COLOR": "1"}):
            emit("config.show", data=data)
        text = output.getvalue()
        self.assertEqual(json.loads(text)["data"], data)
        self.assertNotIn("\x1b[", text)

    def test_habit_and_doctor_habits_json_survive_color_narrow_terminal_and_errors(self):
        from envy.doctor.checks import habits

        for command in (["habit", "check", "--json"], ["doctor", "habits", "--json"]):
            for manifest in ({"id": "synthetic", "platform": "linux", "habits": []}, None):
                output = io.StringIO()
                console = Console(file=output, force_terminal=True, color_system="standard", width=10)
                with patch("envy.jsonio.log.console", console), patch("envy.log.reset_consoles"), patch.dict("os.environ", {"FORCE_COLOR": "1"}), patch(
                    "envy.habit.machine_manifest", return_value=manifest
                ), patch.object(habits, "machine_manifest", return_value=manifest):
                    result = CliRunner().invoke(cli, command)
                text = output.getvalue()
                payload = json.loads(text)
                self.assertNotIn("\x1b[", text)
                self.assertEqual(result.exit_code, 0 if manifest else 1)
                self.assertEqual(payload["ok"], bool(manifest))
                self.assertIsNotNone(payload["data"])

    def test_doctor_json_has_stable_summary_and_serializable_details(self):
        results = [
            ok(SECTION_SYSTEM, "git", "available"),
            warn(SECTION_SYSTEM, "branch", "unexpected", hint="switch branch"),
            info(SECTION_DOCTOR, "source", "repo", path="/tmp/repo"),
        ]
        output = io.StringIO()
        console = Console(file=output, force_terminal=True, color_system="standard", width=10)
        with patch.object(runner.log, "console", console):
            runner.render_json(results, strict=True)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["summary"]["warn"], 1)
        self.assertEqual(payload["summary"]["exitCode"], 1)
        self.assertEqual(payload["results"][1]["hint"], "switch branch")


    def test_log_json_emits_stable_envelope(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            with patch("envy.journal.state_dir", return_value=state):
                from envy import journal

                journal.append({"operation": "apply", "result": "ok"})
                result = CliRunner().invoke(cli, ["log", "--json"])
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertIsInstance(payload["entries"], list)
        self.assertEqual(payload["entries"][0]["operation"], "apply")

    def test_storage_partial_failure_keeps_successes_warnings_and_unknown_sizes(self):
        import typer
        from envy.storage import cli as storage_cli

        result = {
            "items": [{"action": "go", "available": True},
                      {"action": "docker", "available": False, "logicalBytes": None}],
            "errors": ["docker"],
            "warnings": ["daemon unavailable"],
        }
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch("envy.jsonio.log.console", console), self.assertRaises(typer.Exit) as failure:
            storage_cli._perform("maintain", True, lambda: result)
        payload = json.loads(output.getvalue())
        self.assertEqual(failure.exception.exit_code, 1)
        self.assertEqual(payload["command"], "storage maintain")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "partial_failure")
        self.assertEqual(payload["warnings"], result["warnings"])
        self.assertEqual(payload["data"], result)
        self.assertIsNone(payload["data"]["items"][1]["logicalBytes"])

    def test_storage_failure_before_result_emits_one_error_document(self):
        import typer
        from envy.storage import cli as storage_cli
        from envy.storage.model import StorageError

        def unavailable():
            raise StorageError("Machine manifest unavailable")

        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch("envy.jsonio.log.console", console), self.assertRaises(typer.Exit) as failure:
            storage_cli._perform("status", True, unavailable)
        payload = json.loads(output.getvalue())
        self.assertEqual(failure.exception.exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "storage status")
        self.assertEqual(payload["error"]["code"], "storage_error")
        self.assertIsNone(payload["data"])

    def test_log_registered_with_alias(self):
        commands = get_command(cli).commands
        self.assertIn("log", commands)
        self.assertIn("logs", commands)


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from envy.doctor import runner
from envy.doctor.checks import habits
from envy.doctor.model import SECTION_RUNTIME, SECTION_STATE
from envy.habit import HabitCheck, check_habits
from envy.main import cli
from test_habit import _entry, _manifest


class DoctorHabitTests(unittest.TestCase):
    def test_thin_adapter_uses_shared_result_without_copying_verification_policy(self):
        manifest = _manifest(_entry("terminal-scratchpad", "niri"))
        verification = {"runtime": {"status": "unknown"}}
        shared = HabitCheck("terminal-scratchpad", "niri", "info", "manual", {"verification": verification})
        with patch.object(habits, "machine_manifest", return_value=manifest), patch(
            "envy.habit.check_habits", return_value=([shared], False)
        ) as check:
            results = habits.run_checks()
        check.assert_called_once_with(manifest)
        self.assertIs(results[0].details["verification"], verification)
        self.assertEqual(results[0].message, shared.message)
        self.assertEqual(results[0].status, shared.status)
        self.assertEqual(results[0].section, SECTION_RUNTIME)

    def test_native_judgment_and_errors_match_habit_check(self):
        manifest = _manifest(_entry("terminal-scratchpad", "niri"))
        expected, _ = check_habits(manifest)
        with patch.object(habits, "machine_manifest", return_value=manifest):
            actual = habits.run_checks()
        self.assertEqual(len(actual), len(expected))
        for shared, doctor in zip(expected, actual):
            self.assertEqual(doctor.status, shared.status)
            shared_verification = dict(shared.details["verification"])
            actual_verification = dict(doctor.details["verification"])
            shared_verification.pop("sampledAt")
            actual_verification.pop("sampledAt")
            self.assertEqual(shared_verification, actual_verification)
        with patch.object(habits, "machine_manifest", return_value=None):
            results = habits.run_checks()
        self.assertEqual(results[0].status, "error")
        self.assertEqual(results[0].details["verification"]["declaration"]["status"], "invalid")

    def test_default_and_runtime_state_scopes_route_without_config_secrets(self):
        self.assertIn("habits", runner.DEFAULT_CHECKS)
        for scope, section in (("runtime", SECTION_RUNTIME), ("state", SECTION_STATE)):
            manifest = _manifest(_entry("terminal-scratchpad", "niri"))
            with self.subTest(scope=scope), patch.dict(runner.CHECKS, {
                "config": lambda: self.fail("config/secrets must not run"),
                "system": lambda: self.fail("system must not run"),
                "agents": lambda: self.fail("agents must not run"),
            }), patch.object(runner.apps, "run_checks", return_value=[]), patch.object(
                habits, "machine_manifest", return_value=manifest
            ):
                results = runner.run_sections(runner.DEFAULT_CHECKS, [scope], log_progress=False)
            self.assertTrue(results)
            self.assertTrue(all(result.section == section for result in results))
            self.assertTrue(all("verification" in result.details for result in results))

    def test_app_only_scope_does_not_run_habit_probes(self):
        with patch.object(runner.apps, "run_checks", return_value=[]), patch.dict(
            runner.CHECKS, {"habits": lambda: self.fail("app-only must not read habits")}
        ):
            results = runner.run_sections(["apps", "habits"], ["codex"], log_progress=False)
        self.assertEqual(results, [])

    def test_new_habits_command_has_full_envelope_and_error_data(self):
        for manifest, expected_exit in ((_manifest(_entry("terminal-scratchpad", "niri")), 0), (None, 1)):
            with self.subTest(manifest=manifest), patch.object(habits, "machine_manifest", return_value=manifest):
                result = CliRunner().invoke(cli, ["doctor", "habits", "--json"])
            self.assertEqual(result.exit_code, expected_exit, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["command"], "doctor.habits")
            self.assertEqual(payload["ok"], expected_exit == 0)
            self.assertEqual(payload["data"]["results"], payload["results"])
            self.assertEqual(payload["data"]["summary"], payload["summary"])
            self.assertIsNotNone(payload["results"][0]["details"]["verification"])
            self.assertEqual(payload["warnings"], [])
            if expected_exit:
                self.assertIsInstance(payload["error"]["code"], str)


if __name__ == "__main__":
    unittest.main()

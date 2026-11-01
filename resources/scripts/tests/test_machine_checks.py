"""Machine selection and failure diagnostics without real Nix commands."""
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import typer
from rich.console import Console

from envy import log
from envy.workflows import check, generations, plan


class MachineCheckTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(patch.object(log, "console", Console(file=self.output, width=240, color_system=None)))

    def selection(self, status, existing):
        with patch.object(check, "run_process", return_value=subprocess.CompletedProcess([], 0, status, "")), patch.object(
            check, "machine_entries", return_value=existing
        ):
            return check.changed_machine_entries()

    def test_deleted_hosts_are_not_selected(self):
        self.assertEqual(self.selection(" D hosts/linux/gone.nix\0", [("darwin", "mac")]), [])
        self.assertEqual(self.selection("D  hosts/linux/gone.nix\0 M hosts/darwin/mac.nix\0", [("darwin", "mac")]), [("darwin", "mac")])

    def test_added_and_renamed_hosts_select_the_existing_destination(self):
        existing = [("linux", "new"), ("darwin", "mac")]
        for status in ("?? hosts/linux/new.nix\0", "R  hosts/linux/new.nix\0hosts/linux/old.nix\0"):
            with self.subTest(status=status):
                self.assertEqual(self.selection(status, existing), [("linux", "new")])

    def test_shared_changes_still_check_every_existing_machine(self):
        existing = [("darwin", "mac"), ("linux", "desk")]
        self.assertEqual(self.selection(" D hosts/linux/gone.nix\0 M home.nix\0", existing), existing)
        self.assertEqual(self.selection("", existing), [])

    def test_empty_stderr_does_not_replace_a_machine_failure_with_a_traceback(self):
        for stderr in (None, "", "\n \t\n"):
            with self.subTest(stderr=stderr), patch.object(check, "run_process", return_value=subprocess.CompletedProcess([], 7, "", stderr)):
                self.assertEqual(check.run_machine_checks([("darwin", "mac")]), [("darwin", "mac", False)])
        self.assertIn("machine failed", self.output.getvalue())

    def test_failure_hint_keeps_the_last_nonempty_line_and_length_limit(self):
        stderr = "first line\n" + "x" * 600 + "\n \n"
        with patch.object(check, "run_process", return_value=subprocess.CompletedProcess([], 7, "", stderr)), patch.object(log, "hint") as hint:
            check.run_machine_checks([("linux", "desk")])
        hint.assert_called_once_with("x" * 500)

    def test_build_only_builds_the_local_platform(self):
        with patch.object(check, "platform_name", return_value="darwin"), patch.object(
            check, "run_process", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as run:
            check.run_machine_checks([("darwin", "mac"), ("linux", "desk")], build=True)
        self.assertEqual(run.call_args_list[0].args[0], ["nix", "build", "--impure", "--no-link", check.machine_build_attr("darwin", "mac")])
        self.assertEqual(run.call_args_list[1].args[0], ["nix", "eval", "--impure", "--raw", check.machine_drv_attr("linux", "desk")])

    def test_check_exit_code_still_reports_failed_targets(self):
        with patch.object(check, "select_entries", return_value=[("linux", "desk")]), patch.object(
            check, "run_process", return_value=subprocess.CompletedProcess([], 7, "", "\n")
        ):
            with self.assertRaises(typer.Exit) as raised:
                check.check_or_exit()
        self.assertEqual(raised.exception.exit_code, 1)

    def test_closure_diff_blank_stderr_is_a_runtime_error(self):
        for stderr in (None, "", "\n \t\n", "first\nlast\n \n"):
            with self.subTest(stderr=stderr), patch.object(
                generations, "run_process", return_value=subprocess.CompletedProcess([], 7, "", stderr)
            ):
                with self.assertRaises(RuntimeError) as raised:
                    generations.closure_diff(Path("/before"), Path("/after"))
            self.assertEqual(str(raised.exception), "last" if stderr and "last" in stderr else "closure diff failed")

    def test_plan_keeps_json_when_the_closure_diff_fails_with_blank_stderr(self):
        current = generations.Generation(1, Path("/profile"), Path("/before"), "fixture", True)
        with patch.object(plan, "current_machine_id", return_value="mac"), patch.object(
            plan, "machine_build_attr", return_value="fixture"
        ), patch.object(plan, "current_generation", return_value=current), patch.object(
            plan, "run_process", return_value=subprocess.CompletedProcess([], 0, "/after\n", "")
        ), patch.object(generations, "run_process", return_value=subprocess.CompletedProcess([], 7, "", "\n \n")):
            plan.plan_configuration(json_output=True)
        payload = json.loads(self.output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "/after")
        self.assertEqual(payload["closureDiff"], "")

    def test_plan_blank_stderr_keeps_json_and_exit_code(self):
        for stderr in (None, "", "\n \t\n", "first\nlast\n \n"):
            for json_output in (False, True):
                with self.subTest(stderr=stderr, json_output=json_output), patch.object(plan, "current_machine_id", return_value="mac"), patch.object(
                    plan, "machine_build_attr", return_value="fixture"
                ), patch.object(plan, "run_process", return_value=subprocess.CompletedProcess([], 7, "", stderr)):
                    self.output.seek(0)
                    self.output.truncate()
                    with self.assertRaises(typer.Exit) as raised:
                        plan.plan_configuration(json_output=json_output)
                    self.assertEqual(raised.exception.exit_code, 7)
                    expected = "last" if stderr and "last" in stderr else "plan build failed"
                    if json_output:
                        payload = json.loads(self.output.getvalue())
                        self.assertFalse(payload["ok"])
                        self.assertEqual(payload["error"], expected)
                    else:
                        self.assertIn(expected, self.output.getvalue())


if __name__ == "__main__":
    unittest.main()

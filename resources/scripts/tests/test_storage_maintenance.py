"""Native maintenance regressions never run real cleanup commands."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from envy.storage import cli, maintenance
from envy.storage.model import Policy, StorageError


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.policy = Policy(self.home, "manual", 30, (), (), (), (), "", "",
                             maintenance.ACTIONS)
        self.run = patch.object(maintenance, "run_process", side_effect=self.command).start()
        self.which = patch.object(maintenance.shutil, "which", side_effect=lambda name: "/bin/" + name).start()
        self.platform = patch.object(maintenance, "PLATFORM", "linux").start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def command(argv, **kwargs):
        # Only a mock may run mutation commands in this suite.
        return subprocess.CompletedProcess(argv, 0, "purge" if argv[1:] == ["cache", "--help"] else "native output", "")

    def test_preview_never_invokes_cleanup_even_when_all_tools_are_available(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            data = maintenance.preview(self.policy)
        self.assertFalse(data["execute"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual([item["action"] for item in data["items"]], list(maintenance.ACTIONS))
        self.assertTrue(all(item["available"] for item in data["items"]))
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [
            ["docker", "info", "--format", "{{.ServerVersion}}"], ["pip", "cache", "--help"]])
        self.assertEqual(json.loads(json.dumps(data))["execute"], False)
        docker = next(item for item in data["items"] if item["action"] == "docker")
        self.assertEqual(len(docker["commands"]), 2)
        for command in docker["commands"]:
            self.assertEqual(command[command.index("--filter") + 1], "until=336h")

    def test_missing_commands_are_unavailable_and_never_run(self):
        self.which.side_effect = None
        self.which.return_value = None
        data = maintenance.maintain(self.policy)
        self.assertEqual(data["errors"], list(maintenance.ACTIONS))
        self.assertTrue(all("not available" in item["note"] for item in data["items"]))
        self.run.assert_not_called()

    def test_policy_validation_happens_before_probes_or_execution(self):
        policy = Policy(self.home, "auto", 30, (), (), (), (), "", "", ("docker",))
        for method in (maintenance.preview, maintenance.maintain):
            for actions in (["docker", "go"], ["unknown"]):
                with self.subTest(method=method.__name__, actions=actions), self.assertRaises(StorageError):
                    method(policy, actions)
        self.run.assert_not_called()
        empty = Policy(self.home, "manual", 30, (), (), (), (), "", "")
        with self.assertRaisesRegex(StorageError, "No maintenance action"):
            maintenance.maintain(empty)

    def test_duplicate_actions_execute_once_in_selection_order(self):
        data = maintenance.maintain(self.policy, ["npm", "go", "npm", "go"])
        self.assertEqual(data["errors"], [])
        self.assertEqual([item["action"] for item in data["items"]], ["npm", "go"])
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [
            ["npm", "cache", "verify"], ["go", "clean", "-cache"]])

    def test_native_commands_preserve_generations_volumes_and_source_caches(self):
        maintenance.maintain(self.policy)
        commands = [call.args[0] for call in self.run.call_args_list]
        for expected in (["nix-collect-garbage"],
                         ["docker", "builder", "prune", "-f", "--filter", "until=336h"],
                         ["docker", "image", "prune", "-f", "--filter", "until=336h"],
                         ["go", "clean", "-cache"],
                         ["uv", "cache", "prune"], ["npm", "cache", "verify"],
                         ["pip", "cache", "purge"], ["brew", "cleanup", "--prune=30"]):
            self.assertIn(expected, commands)
        forbidden = {"-d", "--delete-old", "volume", "system", "-modcache", "--ci", "--force", "--all", "-a"}
        self.assertFalse(any(forbidden.intersection(argv) for argv in commands))
        for call in self.run.call_args_list:
            self.assertTrue(call.kwargs["capture"])
            self.assertFalse(call.kwargs["check"])
            self.assertGreater(call.kwargs["timeout"], 0)

    def test_brew_environment_overrides_inherited_values(self):
        with patch.dict(os.environ, HOMEBREW_NO_AUTOREMOVE="0", HOMEBREW_NO_AUTO_UPDATE="0"):
            data = maintenance.maintain(self.policy, ["brew"])
        environment = self.run.call_args.kwargs["env"]
        self.assertEqual(environment["HOMEBREW_NO_AUTOREMOVE"], "1")
        self.assertEqual(environment["HOMEBREW_NO_AUTO_UPDATE"], "1")
        self.assertEqual(data["items"][0]["environment"], maintenance.BREW_ENV)

    def test_notes_explain_native_scope_and_nonuniform_age_limits(self):
        items = {item["action"]: item for item in maintenance.preview(self.policy)["items"]}
        self.assertIn("entire Go build cache", items["go"]["note"])
        self.assertIn("without an age cutoff", items["go"]["note"])
        self.assertIn("without an age cutoff", items["pip"]["note"])
        self.assertIn("writes", items["npm"]["note"])
        self.assertIn("old installed kegs", items["brew"]["note"])
        self.assertIn("not a uniform age cutoff", items["brew"]["note"])

    def test_pip_interpreter_environment_is_isolated_for_probe_and_execution(self):
        fixture = self.home / "pip-help-fixture"
        fixture.write_text(
            "#!/bin/sh\n"
            'if [ "${PYTHONPATH+set}${PYTHONHOME+set}${DEVELOPER_DIR+set}" ]; then\n'
            '  echo "wrong interpreter environment" >&2; exit 1\n'
            "fi\n"
            "echo 'purge'\n"
        )
        fixture.chmod(0o700)
        environments = []

        def command(argv, **kwargs):
            environment = kwargs.get("env")
            environments.append(environment)
            if argv[-1] == "--help":
                return subprocess.run([str(fixture)], env=environment, capture_output=True,
                                      text=True, timeout=5, check=False)
            # Execution stays mocked; only the temporary help fixture is run.
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.run.side_effect = command
        with patch.object(maintenance, "PLATFORM", "darwin"), patch.dict(
            os.environ, PYTHONPATH="/foreign/modules", PYTHONHOME="/foreign/python",
            DEVELOPER_DIR="/foreign/developer", PIP_CACHE_DIR="/selected/cache"
        ):
            data = maintenance.maintain(self.policy, ["pip"])
            self.assertEqual(data["errors"], [])
            self.assertEqual(os.environ["PYTHONHOME"], "/foreign/python")
            self.assertEqual(os.environ["DEVELOPER_DIR"], "/foreign/developer")
            self.assertEqual(len(environments), 2)
            self.assertEqual(environments[0], environments[1])
            for environment in environments:
                self.assertNotIn("PYTHONPATH", environment)
                self.assertNotIn("PYTHONHOME", environment)
                self.assertNotIn("DEVELOPER_DIR", environment)
                self.assertEqual(environment["PIP_CACHE_DIR"], "/selected/cache")
                self.assertEqual(environment["PATH"], os.environ["PATH"])

    def test_pip3_fallback_uses_supported_native_command(self):
        self.which.side_effect = lambda name: None if name == "pip" else "/bin/" + name
        data = maintenance.maintain(self.policy, ["pip"])
        self.assertEqual(data["errors"], [])
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [
            ["pip3", "cache", "--help"], ["pip3", "cache", "purge"]])

    def test_unsupported_pip_falls_back_before_mutation(self):
        def command(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "purge" if argv[0] == "pip3" else "old pip", "")
        self.run.side_effect = command
        data = maintenance.preview(self.policy, ["pip"])
        self.assertEqual(data["items"][0]["commands"], [["pip3", "cache", "purge"]])
        self.assertTrue(all(call.args[0][-1] == "--help" for call in self.run.call_args_list))

    def test_failed_pip_probe_is_unavailable_and_does_not_purge(self):
        for failure in (subprocess.CompletedProcess([], 1, "", "bad"),
                        subprocess.CompletedProcess([], 124, "", "timeout"),
                        subprocess.TimeoutExpired("pip", 20), OSError("cannot start")):
            with self.subTest(failure=failure):
                self.run.reset_mock()
                if isinstance(failure, Exception):
                    self.run.side_effect = failure
                else:
                    self.run.side_effect = None
                    self.run.return_value = failure
                data = maintenance.maintain(self.policy, ["pip"])
                self.assertEqual(data["errors"], ["pip"])
                self.assertTrue(all(call.args[0][-1] == "--help" for call in self.run.call_args_list))

    def test_docker_engine_unavailable_or_timed_out_never_prunes(self):
        for failure, note in ((subprocess.CompletedProcess([], 1, "", "offline"), "unavailable"),
                              (subprocess.CompletedProcess([], 124, "", "timeout"), "timed out"),
                              (subprocess.TimeoutExpired("docker", 15), "timed out"),
                              (OSError("cannot start"), "could not start")):
            with self.subTest(note=note):
                self.run.reset_mock()
                self.run.side_effect = failure if isinstance(failure, Exception) else None
                self.run.return_value = failure
                data = maintenance.maintain(self.policy, ["docker"])
                self.assertEqual(data["errors"], ["docker"])
                self.assertIn(note, data["items"][0]["note"])
                self.assertEqual(self.run.call_count, 1)

    def test_docker_builder_failure_stops_before_image_prune(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 0, "29", ""),
                                subprocess.CompletedProcess([], 42, "", "failed")]
        data = maintenance.maintain(self.policy, ["docker"])
        self.assertEqual(data["errors"], ["docker"])
        self.assertIn("exit code 42", data["items"][0]["note"])
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(len(data["items"][0]["output"]), 1)

    def test_execution_reports_timeouts_and_start_failures(self):
        for failure, code, message in ((subprocess.TimeoutExpired("go", 1800), 124, "timed out"),
                                       (OSError("cannot start"), 127, "could not start"),
                                       (subprocess.CompletedProcess([], 124, "", "timeout"), 124, "timed out"),
                                       (subprocess.CompletedProcess([], 127, "", "missing"), 127, "not available")):
            with self.subTest(code=code, message=message):
                self.run.side_effect = failure if isinstance(failure, Exception) else None
                self.run.return_value = failure
                data = maintenance.maintain(self.policy, ["go"])
                self.assertEqual(data["errors"], ["go"])
                self.assertEqual(data["items"][0]["output"][0]["returncode"], code)
                self.assertIn(message, data["items"][0]["note"])

    def test_darwin_nix_uses_captured_noninteractive_sudo_without_credentials(self):
        with patch.object(maintenance, "PLATFORM", "darwin"):
            preview = maintenance.preview(self.policy, ["nix"])
            self.run.assert_not_called()
            self.assertIn("no password is read", preview["items"][0]["note"])
            maintenance.maintain(self.policy, ["nix"])
        self.assertEqual(self.run.call_args.args[0], ["sudo", "-n", "-H", "nix-collect-garbage"])
        self.assertTrue(self.run.call_args.kwargs["capture"])
        self.assertEqual(self.run.call_args.kwargs["timeout"], 1800)

    def test_cli_json_preview_never_mutates_and_yes_executes_once(self):
        for flags, execute in (([], False), (["--yes"], True)):
            with self.subTest(execute=execute):
                self.run.reset_mock()
                output = io.StringIO()
                with patch.object(cli, "_context", return_value=(self.policy, self.home / "state")), patch.object(
                    cli.log, "console", Console(file=output, force_terminal=False)
                ):
                    result = CliRunner().invoke(cli.app, ["maintain", "--npm", "--json", *flags])
                self.assertEqual(result.exit_code, 0, result.output)
                data = json.loads(output.getvalue())
                self.assertTrue(data["ok"])
                self.assertEqual(data["data"]["execute"], execute)
                self.assertEqual(self.run.call_count, int(execute))
                self.assertEqual(result.output, "")

    def test_cli_native_failure_is_pure_json_and_nonzero(self):
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 23, "native stdout", "native stderr")
        output = io.StringIO()
        with patch.object(cli, "_context", return_value=(self.policy, self.home / "state")), patch.object(
            cli.log, "console", Console(file=output, force_terminal=False)
        ):
            result = CliRunner().invoke(cli.app, ["maintain", "--go", "--yes", "--json"])
        self.assertEqual(result.exit_code, 1, result.output)
        data = json.loads(output.getvalue())
        self.assertFalse(data["ok"])
        self.assertEqual(data["data"]["errors"], ["go"])
        self.assertEqual(result.output, "")


if __name__ == "__main__":
    unittest.main()

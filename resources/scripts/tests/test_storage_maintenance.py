"""Native maintenance regressions never run real cleanup commands."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from envy.process import run_process
from envy.storage import cli, go_modcache, maintenance
from envy.storage.model import Policy, StorageError


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.policy = Policy(self.home, "manual", 30, (), (), (), (), "", "",
                             tuple(name for name in maintenance.ACTIONS if name != "go-modcache"))
        self.run = patch.object(maintenance, "run_process", side_effect=self.command).start()
        self.which = patch.object(maintenance.shutil, "which", side_effect=lambda name: "/bin/" + name).start()
        self.platform = patch.object(maintenance, "PLATFORM", "linux").start()
        self.docker_report = patch.object(maintenance.docker, "inspect", return_value={"status": "available"}).start()
        self.docker_target = patch.object(maintenance.docker, "maintenance_target", return_value={"daemonId": "fixture"}).start()
        self.docker_verify = patch.object(maintenance.docker, "verify_target").start()
        patch.object(maintenance.docker, "bound_commands", side_effect=lambda target: maintenance.COMMANDS["docker"]).start()
        patch.object(maintenance.docker, "bound_environment", return_value={"PATH": "/fixture/bin"}).start()
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
        self.assertEqual([item["action"] for item in data["items"]], list(self.policy.maintenance))
        self.assertTrue(all(item["available"] for item in data["items"]))
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [
            ["pip", "cache", "--help"]])
        self.assertEqual(json.loads(json.dumps(data))["execute"], False)
        docker = next(item for item in data["items"] if item["action"] == "docker")
        self.assertEqual(len(docker["commands"]), 2)
        for command in docker["commands"]:
            self.assertEqual(command[command.index("--filter") + 1], "until=336h")

    def test_missing_commands_are_unavailable_and_never_run(self):
        self.which.side_effect = None
        self.which.return_value = None
        data = maintenance.maintain(self.policy)
        self.assertEqual(data["errors"], list(self.policy.maintenance))
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

    def test_docker_identity_drift_stops_before_any_cleanup(self):
        self.docker_verify.side_effect = StorageError("Docker target changed")
        data = maintenance.maintain(self.policy, ["docker"])
        self.assertEqual(data["errors"], ["docker"])
        self.run.assert_not_called()
        self.assertIn("target changed", data["items"][0]["note"])

    def test_module_download_cleanup_requires_separate_opt_in_and_pinned_target(self):
        with self.assertRaises(StorageError):
            maintenance.preview(self.policy, ["go-modcache"])
        policy = Policy(self.home, "manual", 30, (), (), (), (), "", "", ("go-modcache",))
        target = {"path": str(self.home / "downloads"), "resolvedPath": str(self.home / "downloads"),
                  "cwd": str(self.home)}
        binding = {"version": 1, "action": "go-modcache", "target": target,
                   "cwd": {"path": str(self.home)}, "executable": {"path": "/synthetic/go"},
                   "scope": {}, "roots": []}
        with (
            patch.object(maintenance.native_context, "capture", return_value=binding),
            patch.object(maintenance.native_context, "verify", return_value=binding) as verify,
        ):
            preview = maintenance.preview(policy)
            self.assertIn("without an age cutoff", preview["items"][0]["note"])
            self.run.assert_not_called()
            data = maintenance.maintain(policy)
        self.assertEqual(data["errors"], [])
        verify.assert_called_once_with("go-modcache", binding, policy, platform="linux")
        self.assertEqual(self.run.call_args.args[0], ["/synthetic/go", "clean", "-modcache"])
        self.assertEqual(self.run.call_args.kwargs["cwd"], str(self.home))
        self.assertEqual(self.run.call_args.kwargs["env"]["GOMODCACHE"], target["resolvedPath"])
        self.run.reset_mock()
        with (
            patch.object(maintenance.native_context, "capture", return_value=binding),
            patch.object(maintenance.native_context, "verify", side_effect=StorageError("target changed")),
        ):
            data = maintenance.maintain(policy)
        self.assertEqual(data["errors"], ["go-modcache"])
        self.run.assert_not_called()

    def test_module_cache_cannot_remove_an_ancestor_of_the_current_project(self):
        project = self.home / "project"
        nested = project / "subproject"
        nested.mkdir(parents=True)
        policy = Policy(self.home, "manual", 30, (), (), (), (), "", "", ("go-modcache",))
        with (
            patch.object(go_modcache.Path, "cwd", return_value=nested),
            patch.object(maintenance.native_context, "executable", return_value={"path": "/synthetic/go"}),
            patch.object(go_modcache, "probe", return_value=subprocess.CompletedProcess(
                [], 0, str(project) + chr(10), "")),
        ):
            preview = maintenance.preview(policy)
            result = maintenance.maintain(policy)
        self.assertFalse(preview["items"][0]["available"])
        self.assertIn("project", preview["items"][0]["note"])
        self.assertEqual(result["errors"], ["go-modcache"])
        self.run.assert_not_called()

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
                self.docker_target.side_effect = StorageError("Docker " + note)
                data = maintenance.maintain(self.policy, ["docker"])
                self.assertEqual(data["errors"], ["docker"])
                self.assertIn(note, data["items"][0]["note"])
                self.run.assert_not_called()

    def test_docker_builder_failure_stops_before_image_prune(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 42, "", "failed")]
        data = maintenance.maintain(self.policy, ["docker"])
        self.assertEqual(data["errors"], ["docker"])
        self.assertIn("exit code 42", data["items"][0]["note"])
        self.assertEqual(self.run.call_count, 1)
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

    def test_legacy_api_does_not_capture_confirmed_context(self):
        with patch.object(maintenance.native_context, "capture") as capture:
            maintenance.preview(self.policy, ["go"])
            maintenance.maintain(self.policy, ["go"])
        capture.assert_not_called()
        self.assertEqual(self.run.call_args.args[0], ["go", "clean", "-cache"])

    def test_bound_preview_failure_is_explicit_and_does_not_run_saved_commands(self):
        with patch.object(maintenance.native_context, "capture",
                          side_effect=StorageError("Native target metadata output limit exceeded")):
            data = maintenance.preview(self.policy, ["go"], bind_targets=True)
        self.assertFalse(data["items"][0]["available"])
        self.assertIn("output limit exceeded", data["items"][0]["note"])
        data["items"][0]["commands"] = [["never-execute-saved-json"]]
        result = maintenance.maintain(self.policy, ["go"], approved_preview=data)
        self.assertEqual(result["errors"], ["go"])
        self.run.assert_not_called()

    def test_approved_large_output_is_discarded_without_changing_failure_semantics(self):
        fixture = self.home / "large-output.py"
        fixture.write_text(
            "import os, sys\n"
            "for _ in range(64):\n"
            "    os.write(1, b'fake native stdout' * 4096)\n"
            "    os.write(2, b'fake native stderr' * 4096)\n"
            "sys.exit(int(sys.argv[1]))\n"
        )
        actions = ["go", "uv", "npm"]
        approved = {"execute": False, "items": [{"action": action, "available": True,
            "binding": {"version": 1, "action": action, "target": {}, "cwd": {},
                        "executable": {}, "scope": {}, "roots": []}} for action in actions]}
        current = {"cwd": {"path": str(self.home)}}
        results = []
        process_options = []
        subprocess_run = subprocess.run
        def fake_run(argv, **kwargs):
            # Only the temporary output fixture reaches the actual process boundary.
            self.assertEqual(argv[:2], [sys.executable, str(fixture)])
            process_options.append(kwargs)
            result = subprocess_run(argv, **kwargs)
            results.append(result)
            return result
        def commands(action, binding, **kwargs):
            return [[sys.executable, str(fixture), "42" if action == "uv" else "0"]]
        self.run.side_effect = run_process
        with patch.object(maintenance.native_context, "verify", return_value=current), \
                patch.object(maintenance.native_context, "commands", side_effect=commands), \
                patch.object(maintenance.native_context, "execution_environment", return_value=dict(os.environ)), \
                patch("envy.process.subprocess.run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            data = maintenance.maintain(self.policy, actions, approved_preview=approved)
        self.assertEqual(data["errors"], ["uv", "npm"])
        self.assertTrue(data["items"][0]["available"])
        self.assertEqual(data["items"][0]["output"][0]["returncode"], 0)
        self.assertEqual(data["items"][1]["output"][0]["returncode"], 42)
        self.assertIn("exit code 42", data["items"][1]["note"])
        self.assertIn("skipped", data["items"][2]["note"])
        self.assertEqual(data["items"][2]["output"], [])
        self.assertEqual(self.run.call_count, 2)
        for call, options, result in zip(self.run.call_args_list, process_options, results):
            self.assertFalse(call.kwargs["capture"])
            self.assertFalse(options["capture_output"])
            self.assertEqual(options["stdout"], subprocess.DEVNULL)
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertIsNone(result.stdout)
            self.assertIsNone(result.stderr)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        serialized = json.dumps(data)
        self.assertNotIn("fake native stdout", serialized)
        self.assertNotIn("fake native stderr", serialized)
        self.assertEqual(json.loads(serialized)["errors"], ["uv", "npm"])

    def test_approved_preview_validates_every_item_shape_before_execution(self):
        for approved in ({}, {"execute": True, "items": []},
                         {"execute": False, "items": [{"action": "go", "available": "yes"}]},
                         {"execute": False, "items": [{"action": "go", "available": True}]},
                         {"execute": False, "items": [{"action": "go", "available": True,
                             "binding": {"action": "go", "version": 1}}]}):
            with self.subTest(approved=approved), self.assertRaises(StorageError):
                maintenance.maintain(self.policy, ["go"], approved_preview=approved)
        self.run.assert_not_called()

    def test_approved_policy_validation_happens_before_context_probes(self):
        policy = Policy(self.home, "manual", 30, (), (), (), (), "", "", ("go",))
        with patch.object(maintenance.native_context, "capture") as capture, self.assertRaises(StorageError):
            maintenance.maintain(policy, ["go", "uv"], approved_preview={"execute": False, "items": []})
        capture.assert_not_called()
        self.run.assert_not_called()

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

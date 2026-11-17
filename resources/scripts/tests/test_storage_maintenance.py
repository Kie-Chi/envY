"""Native maintenance regressions never run real cleanup commands."""

import contextlib
import copy
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

    def reported_run(self, action, outputs, *, report=True, on_progress=None):
        binding = {"version": 1, "action": action, "target": {},
                   "cwd": {"path": str(self.home)}, "executable": {}, "scope": {}, "roots": []}
        approved = {"execute": False, "items": [
            {"action": action, "available": True, "binding": binding}]}
        remaining = iter(outputs)
        handles = []

        def command(argv, **kwargs):
            self.assertEqual(argv, maintenance.COMMANDS[action][len(handles) // 2])
            self.assertEqual(kwargs["cwd"], str(self.home))
            self.assertEqual(kwargs["env"], {"PATH": "/fixture/bin"})
            self.assertFalse(kwargs["capture"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 1800)
            code, stdout, stderr = next(remaining)
            for name, value in (("stdout", stdout), ("stderr", stderr)):
                handle = kwargs[name]
                if report:
                    handle.write(value)
                    handles.append(handle)
                else:
                    self.assertEqual(handle, subprocess.DEVNULL)
            if isinstance(code, BaseException):
                raise code
            return subprocess.CompletedProcess(argv, code)

        self.run.side_effect = command
        with (
            patch.object(maintenance.native_context, "verify", return_value=binding),
            patch.object(maintenance.native_context, "commands", return_value=maintenance.COMMANDS[action]),
            patch.object(maintenance.native_context, "execution_environment", return_value={"PATH": "/fixture/bin"}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            data = maintenance.maintain(self.policy, [action], approved_preview=approved,
                                        **({"report_reclaimed": True} if report else {}),
                                        **({"on_progress": on_progress} if on_progress is not None else {}))
        self.assertTrue(all(handle.closed for handle in handles))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("private.example", json.dumps(data))
        return data["items"][0]

    def test_native_reclaimed_summaries_and_decimal_units(self):
        cases = [
            ("nix", b"", b"3 store paths deleted, 1.5 MiB freed\n", 1572864),
            ("nix", b"3 store paths deleted, 1 MB freed\n", b"", None),
            ("uv", b"", b"Removed 12 files (1.5GiB)\n", 1610612736),
            ("uv", b"", b"Pruned 2 entries (2 MiB)\n", 2097152),
            ("brew", b"==> This operation has freed approximately 1.5GB of disk space.\n", b"", 1500000000),
            ("pip", b"Files removed: 10 (1.25 MB)\n", b"", 1250000),
            ("npm", b"Content garbage-collected: 3 (123 bytes)\n", b"", 123),
            ("pip", b"Files removed: 3\n", b"", None),
            ("go", b"Total reclaimed space: 1GB\n", b"", None),
            ("npm", b"Content garbage-collected: 0 (0 bytes)\n", b"", 0),
        ]
        for action, stdout, stderr, expected in cases:
            with self.subTest(action=action, expected=expected):
                item = self.reported_run(action, [(0, stdout, stderr)])
                self.assertEqual(item["reportedReclaimedBytes"], expected)
                self.assertEqual(item["output"][0]["reportedReclaimedBytes"], expected)

    def test_docker_reports_require_every_step_to_succeed_and_report(self):
        for second, expected in (((0, b"Total reclaimed space: 2MB\n", b""), 3000000),
                                 ((0, b"unrecognized summary", b""), None),
                                 ((42, b"Total reclaimed space: 2MB\n", b"private.example"), None)):
            with self.subTest(second=second):
                item = self.reported_run("docker", [(0, b"Total reclaimed space: 1MB\n", b""), second])
                self.assertEqual(item["reportedReclaimedBytes"], expected)
                self.assertEqual(item["output"][0]["reportedReclaimedBytes"], 1000000)
        item = self.reported_run("docker", [(42, b"Total reclaimed space: 1MB\n", b"private.example")])
        self.assertEqual(len(item["output"]), 1)
        self.assertIsNone(item["reportedReclaimedBytes"])
        self.assertIsNone(item["output"][0]["reportedReclaimedBytes"])

    def test_reclaimed_failures_and_unknown_output_do_not_leak(self):
        for failure in (42, 124, subprocess.TimeoutExpired("fixture", 1800), OSError("private.example")):
            with self.subTest(failure=failure):
                item = self.reported_run("pip", [(failure, b"Files removed: 1 (99 MB)\n", b"private.example")])
                self.assertFalse(item["available"])
                self.assertIsNone(item["reportedReclaimedBytes"])
                self.assertIsNone(item["output"][0]["reportedReclaimedBytes"])
        item = self.reported_run("pip", [(0, b"private.example/Files removed: 1 (99 MB)\n", b"private.example")])
        self.assertIsNone(item["reportedReclaimedBytes"])

    def test_reclaimed_reads_only_bounded_tail_and_complete_summary_lines(self):
        summary = b"Files removed: 1 (5 MB)\n"
        for payload, expected in ((summary + b"x" * 70000, None),
                                  (b"x" * 70000 + b"\n" + summary, 5000000),
                                  (b"x" * 70000 + summary, None)):
            with self.subTest(expected=expected):
                item = self.reported_run("pip", [(0, payload, b"")])
                self.assertEqual(item["reportedReclaimedBytes"], expected)
        with tempfile.TemporaryFile() as handle:
            handle.write(b"x" * 70000 + b"\n" + summary)
            with patch.object(maintenance, "_REPORT_TAIL_BYTES", 64), patch.object(
                handle, "read", wraps=handle.read
            ) as read:
                self.assertEqual(maintenance._reported_reclaimed("pip", handle), 5000000)
            read.assert_called_once_with(64)

    def test_progress_retains_intent_completed_and_interrupted_steps(self):
        snapshots = []
        def progress(data):
            snapshots.append(copy.deepcopy(data))
        with self.assertRaises(KeyboardInterrupt):
            self.reported_run("docker", [(0, b"Total reclaimed space: 1MB\n", b""),
                                          (KeyboardInterrupt(), b"", b"")], on_progress=progress)
        self.assertEqual(len(snapshots), 4)
        self.assertIn("target", snapshots[0]["items"][0]["binding"])
        self.assertNotIn("output", snapshots[0]["items"][0])
        output = snapshots[-1]["items"][0]["output"]
        self.assertEqual(output[0]["state"], "completed")
        self.assertEqual(output[0]["returncode"], 0)
        self.assertEqual(output[0]["reportedReclaimedBytes"], 1000000)
        self.assertEqual(output[1]["state"], "running")
        self.assertIsNone(output[1]["returncode"])
        self.assertIn("image prune", output[1]["command"])
        self.assertNotIn("reportedReclaimedBytes", output[1])

    def test_progress_persistence_failure_prevents_further_mutation(self):
        for fail_at, expected_calls in ((1, 0), (2, 0), (3, 1), (4, 1)):
            with self.subTest(fail_at=fail_at):
                self.run.reset_mock()
                snapshots = []
                def progress(data):
                    snapshots.append(copy.deepcopy(data))
                    if len(snapshots) == fail_at:
                        raise OSError("fixture persistence failure")
                with self.assertRaisesRegex(OSError, "fixture persistence failure"):
                    self.reported_run("docker", [(0, b"Total reclaimed space: 1MB\n", b"")],
                                      on_progress=progress)
                self.assertEqual(self.run.call_count, expected_calls)

    def test_progress_reports_final_failures_and_skipped_actions(self):
        binding = {"version": 1, "target": {}, "cwd": {}, "executable": {}, "scope": {}, "roots": []}
        approved = {"execute": False, "items": [
            {"action": action, "available": True, "binding": dict(binding, action=action)}
            for action in ("go", "uv")]}
        snapshots = []
        with patch.object(maintenance.native_context, "verify", side_effect=StorageError("fixture changed")):
            result = maintenance.maintain(self.policy, ["go", "uv"], approved_preview=approved,
                on_progress=lambda data: snapshots.append(copy.deepcopy(data)))
        self.run.assert_not_called()
        self.assertEqual(snapshots[-1], result)
        self.assertEqual(result["errors"], ["go", "uv"])
        self.assertEqual(result["items"][0]["output"][0]["state"], "failed")
        self.assertIn("skipped", result["items"][1]["note"])
        self.assertEqual(result["items"][1]["output"], [])

    def test_reclaimed_defaults_preserve_result_and_process_options(self):
        item = self.reported_run("pip", [(0, b"", b"")], report=False)
        self.assertNotIn("reportedReclaimedBytes", item)
        self.assertNotIn("reportedReclaimedBytes", item["output"][0])
        self.run.side_effect = self.command
        item = maintenance.maintain(self.policy, ["npm"], report_reclaimed=True)["items"][0]
        self.assertNotIn("reportedReclaimedBytes", item)
        self.assertTrue(self.run.call_args.kwargs["capture"])

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

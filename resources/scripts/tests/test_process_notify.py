import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from envy import log
from envy import process


class ProcessStreamTests(unittest.TestCase):
    def test_default_and_capture_modes_keep_existing_stream_behavior(self):
        with patch.object(process, "_maybe_prime_flake_sources"), \
                patch.object(process._ActivityReporter, "start"), \
                patch.object(process.subprocess, "run") as run:
            for options in ({}, {"capture": False}, {"capture": True},
                            {"capture": True, "stdout": None, "stderr": None}):
                with self.subTest(options=options):
                    run.return_value = subprocess.CompletedProcess([], 0, "", "")
                    process.run_process(["fake-process"], **options)
                    self.assertEqual(run.call_args.kwargs["capture_output"], options.get("capture", False))
                    self.assertIsNone(run.call_args.kwargs.get("stdout"))
                    self.assertIsNone(run.call_args.kwargs.get("stderr"))
                    self.assertFalse(run.call_args.kwargs["check"])

    def test_capture_rejects_explicit_streams_before_starting_process(self):
        with patch.object(process, "_maybe_prime_flake_sources") as prime, \
                patch.object(process.subprocess, "run") as run:
            for streams in ({"stdout": subprocess.DEVNULL}, {"stderr": subprocess.DEVNULL},
                            {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}):
                with self.subTest(streams=streams), self.assertRaisesRegex(ValueError, "capture"):
                    process.run_process(["fake-process"], capture=True, **streams)
            prime.assert_not_called()
            run.assert_not_called()

    def test_devnull_discards_large_output_and_preserves_returncode_and_json(self):
        fixture = (
            "import os, sys; "
            "[(os.write(1, b'fake stdout' * 8192), os.write(2, b'fake stderr' * 8192)) "
            "for _ in range(64)]; sys.exit(42)"
        )
        result = process.run_process([sys.executable, "-c", fixture], capture=False,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     check=False, timeout=10)
        self.assertEqual(result.returncode, 42)
        self.assertIsNone(result.stdout)
        self.assertIsNone(result.stderr)
        document = json.dumps({"returncode": result.returncode})
        self.assertEqual(json.loads(document), {"returncode": 42})
        self.assertNotIn("fake stdout", document)
        self.assertNotIn("fake stderr", document)


class NotifyEnabledTests(unittest.TestCase):
    def _enabled(self, *, envvar="1", tty=True, argv=("envy", "apply"), platform="darwin"):
        with patch.dict("os.environ", {process._NOTIFY_ENV: envvar}, clear=False), \
             patch.object(process.sys.stderr, "isatty", return_value=tty), \
             patch.object(process.sys, "argv", list(argv)), \
             patch.object(process.sys, "platform", platform):
            return process._notify_enabled()

    def test_enabled_on_interactive_non_json(self):
        self.assertTrue(self._enabled())

    def test_disabled_when_env_off(self):
        self.assertFalse(self._enabled(envvar="0"))
        self.assertFalse(self._enabled(envvar="false"))

    def test_disabled_when_not_a_tty(self):
        self.assertFalse(self._enabled(tty=False))

    def test_disabled_when_json_requested(self):
        self.assertFalse(self._enabled(argv=("envy", "status", "--json")))


    def test_activity_is_suppressed_while_prompt_is_active(self):
        with patch.object(log.diagnostic_console, "print") as printer:
            with log.interactive_prompt():
                log.activity("command", "still running")
        printer.assert_not_called()

    def test_activity_is_suppressed_for_shell_completion(self):
        with patch.dict("os.environ", {"_ENVY_COMPLETE": "bash_complete"}, clear=True), \
             patch.object(log.diagnostic_console, "print") as printer:
            self.assertTrue(log.is_completion_invocation())
            log.activity("command", "still running")
        printer.assert_not_called()

    def test_macos_uses_osascript(self):
        with patch.object(process.sys, "platform", "darwin"):
            command = process._notify_command("envy", "done")
        self.assertEqual(command[:2], ["osascript", "-e"])
        self.assertIn("done", command[2])

    def test_linux_uses_notify_send_when_available(self):
        with patch.object(process.sys, "platform", "linux"), \
             patch.object(process.shutil, "which", return_value="/usr/bin/notify-send"):
            command = process._notify_command("envy", "done")
        self.assertEqual(command[0], "/usr/bin/notify-send")
        self.assertIn("envy", command)
        self.assertIn("done", command)

    def test_linux_without_binary_returns_none(self):
        with patch.object(process.sys, "platform", "linux"), \
             patch.object(process.shutil, "which", return_value=None):
            self.assertIsNone(process._notify_command("envy", "done"))

    def test_unsupported_platform_returns_none(self):
        with patch.object(process.sys, "platform", "win32"):
            self.assertIsNone(process._notify_command("envy", "done"))


class FireNotificationTests(unittest.TestCase):
    def test_fire_spawns_detached_process(self):
        with patch.object(process, "_notify_command", return_value=["true"]), \
             patch.object(process.subprocess, "Popen") as popen:
            process._fire_completion_notification("nix build", 42.0, 0)
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))

    def test_fire_swallows_oserror(self):
        with patch.object(process, "_notify_command", return_value=["true"]), \
             patch.object(process.subprocess, "Popen", side_effect=OSError("no")):
            process._fire_completion_notification("nix build", 42.0, 1)  # no raise

    def test_fire_noop_when_unsupported(self):
        with patch.object(process, "_notify_command", return_value=None), \
             patch.object(process.subprocess, "Popen") as popen:
            process._fire_completion_notification("nix build", 42.0, 0)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import subprocess
import unittest
from unittest.mock import patch

from envy import log
from envy import process


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

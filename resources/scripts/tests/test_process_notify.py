import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
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


class StreamedStderrTests(unittest.TestCase):
    def test_progress_is_visible_before_exit_without_polluting_json(self):
        with tempfile.TemporaryDirectory() as directory:
            acknowledgement = Path(directory) / "seen"

            class Sink(io.StringIO):
                def write(self, text):
                    result = super().write(text)
                    if "进度 50%\r" in self.getvalue():
                        acknowledgement.touch()
                    return result

            diagnostics = Sink()
            fixture = (
                "import json, os, pathlib, sys, time; "
                "marker = pathlib.Path(sys.argv[1]); "
                "os.write(2, '进度 50%\\r'.encode()); "
                "deadline = time.monotonic() + 3; "
                "\nwhile not marker.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
                "print(json.dumps({'progressSeen': marker.exists(), 'input': sys.stdin.read()}))"
            )
            with patch.object(process.sys, "stderr", diagnostics):
                result = process.run_process(
                    [sys.executable, "-c", fixture, str(acknowledgement)],
                    capture=True, stream_stderr=True, stdin_data="fixture input", timeout=5,
                )
            self.assertEqual(json.loads(result.stdout), {"progressSeen": True, "input": "fixture input"})
            self.assertEqual(diagnostics.getvalue(), "进度 50%\r")
            self.assertEqual(result.stderr, diagnostics.getvalue())

    def test_large_outputs_are_drained_and_failure_diagnostics_are_retained(self):
        fixture = (
            "import os, sys; "
            "[(os.write(1, b'o' * 8192), os.write(2, b'e' * 8192)) for _ in range(128)]; "
            "sys.exit(42)"
        )
        diagnostics = io.StringIO()
        with patch.object(process.sys, "stderr", diagnostics):
            result = process.run_process(
                [sys.executable, "-c", fixture], capture=True, stream_stderr=True,
                check=False, timeout=10,
            )
        self.assertEqual(result.returncode, 42)
        self.assertEqual(result.stdout, "o" * 8192 * 128)
        self.assertEqual(result.stderr, "e" * 8192 * 128)
        self.assertEqual(diagnostics.getvalue(), result.stderr)

    def test_timeout_preserves_partial_output_and_diagnostics(self):
        fixture = "import sys, time; print('partial', flush=True); print('waiting', file=sys.stderr, flush=True); time.sleep(30)"
        diagnostics = io.StringIO()
        with patch.object(process.sys, "stderr", diagnostics):
            result = process.run_process(
                [sys.executable, "-c", fixture], capture=True, stream_stderr=True,
                check=False, timeout=1,
            )
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "partial\n")
        self.assertIn("waiting\n", result.stderr)
        self.assertIn("command timed out", result.stderr)
        self.assertEqual(diagnostics.getvalue(), "waiting\n")

    def _run_with_detached_descendant(self, mode):
        child_fixture = (
            "import pathlib, subprocess, sys, time; "
            "print('partial', flush=True); "
            "print('waiting', file=sys.stderr, flush=True); "
            "descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdout=None if sys.argv[2] == 'timeout' else subprocess.DEVNULL, start_new_session=True); "
            "pathlib.Path(sys.argv[1]).write_text(str(descendant.pid)); "
            "sys.exit(0) if sys.argv[2] == 'cancel-after-exit' else time.sleep(30)"
        )
        worker_fixture = (
            "import io, json, sys, threading\n"
            "from unittest.mock import patch\n"
            "from pathlib import Path\n"
            "from envy import process\n"
            "original_communicate = process.subprocess.Popen.communicate\n"
            "def communicate(child, *args, **kwargs):\n"
            "    result = original_communicate(child, *args, **kwargs)\n"
            "    Path(sys.argv[2] + '.returned').touch()\n"
            "    return result\n"
            "diagnostics = io.StringIO()\n"
            "with patch.object(process.sys, 'stderr', diagnostics), patch.object(process.subprocess.Popen, 'communicate', communicate):\n"
            "    try:\n"
            "        result = process.run_process([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]], capture=True, stream_stderr=True, check=False, timeout=1 if sys.argv[3] == 'timeout' else None)\n"
            "        report = {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}\n"
            "    except KeyboardInterrupt:\n"
            "        report = {'cancelled': True}\n"
            "report.update(diagnostics=diagnostics.getvalue(), threads=threading.active_count())\n"
            "print(json.dumps(report))\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "descendant"
            worker = subprocess.Popen(
                [sys.executable, "-c", worker_fixture, child_fixture, str(ready), mode],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            descendant = None
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "The detached descendant did not become ready")
                descendant = int(ready.read_text())
                if mode == "cancel-after-exit":
                    returned = Path(str(ready) + ".returned")
                    while not returned.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(returned.exists(), "The direct child did not finish communicating")
                if mode.startswith("cancel"):
                    os.kill(worker.pid, signal.SIGINT)
                try:
                    stdout, stderr = worker.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail(f"{mode} waited for the detached descendant to close its inherited streams")
                self.assertEqual(worker.returncode, 0, stderr)
                os.kill(descendant, 0)
                report = json.loads(stdout)
                self.assertEqual(report["threads"], 1, "The stderr relay thread must finish before returning")
                self.assertEqual(report["diagnostics"], "waiting\n")
                return report
            finally:
                if descendant is None and ready.exists():
                    descendant = int(ready.read_text())
                if descendant is not None:
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                worker.communicate(timeout=5)

    def test_cancellation_does_not_wait_for_descendant_stderr(self):
        self.assertTrue(self._run_with_detached_descendant("cancel")["cancelled"])

    def test_cancellation_during_relay_join_stops_the_reader(self):
        self.assertTrue(self._run_with_detached_descendant("cancel-after-exit")["cancelled"])

    def test_timeout_does_not_wait_for_descendant_stdout_or_stderr(self):
        report = self._run_with_detached_descendant("timeout")
        self.assertEqual(report["returncode"], 124)
        self.assertEqual(report["stdout"], "partial\n")
        self.assertIn("waiting\n", report["stderr"])
        self.assertIn("command timed out", report["stderr"])

    def test_check_raises_with_captured_diagnostics(self):
        diagnostics = io.StringIO()
        with patch.object(process.sys, "stderr", diagnostics), self.assertRaises(process.CommandError) as error:
            process.run_process(
                [sys.executable, "-c", "import sys; sys.stderr.write('fixture failure'); sys.exit(7)"],
                capture=True, stream_stderr=True,
            )
        self.assertEqual(error.exception.returncode, 7)
        self.assertEqual(error.exception.stderr, "fixture failure")
        self.assertEqual(diagnostics.getvalue(), "fixture failure")

    def test_streaming_requires_capture_before_starting_process(self):
        with patch.object(process, "_maybe_prime_flake_sources") as prime, \
                self.assertRaisesRegex(ValueError, "requires capture"):
            process.run_process(["fake-process"], stream_stderr=True)
        prime.assert_not_called()


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

"""Native output tests use fake commands and temporary private paths only."""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from envy.process import run_process
from envy.storage.model import StorageError
from envy.storage.output import NativeOutput, sanitize_output, _LINE_LIMIT, _STREAM_LIMIT


class NativeOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Darwin's system temporary directory may itself have a symlink alias.
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "logs"

    def test_callback_arrives_while_command_waits_for_release_and_both_streams_work(self):
        ready = threading.Event()
        done = threading.Event()
        events = []
        errors = []
        release = self.root / "release"
        os.mkfifo(release, 0o600)

        def receive(action, channel, text):
            events.append((action, channel, text))
            ready.set()

        script = (
            "import os,sys\n"
            "os.write(1, b'removed /fake/cache: 4 files\\n')\n"
            "with open(sys.argv[1], 'rb', buffering=0) as gate: gate.read(1)\n"
            "os.write(2, b'error: fake missing file\\n')\n"
        )
        with NativeOutput(self.directory, "fake", 0, receive) as output:
            def command():
                try:
                    run_process([sys.executable, "-c", script, str(release)],
                                stdout=output.stdout, stderr=output.stderr)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()

            worker = threading.Thread(target=command, daemon=True)
            worker.start()
            try:
                self.assertTrue(ready.wait(5), "No live output before command release")
                self.assertFalse(done.is_set())
            finally:
                # Nonblocking open avoids hanging the test if the command failed early.
                descriptor = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
                os.write(descriptor, b"x")
                os.close(descriptor)
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            output.finish()
            output.finish()
            self.assertFalse(output._thread.is_alive())
            output.raise_if_failed()
            output.stdout.seek(0)
            self.assertEqual(output.stdout.read(), b"removed /fake/cache: 4 files\n")
            self.assertEqual(events, [("fake", "stdout", "removed /fake/cache: 4 files\n"),
                                      ("fake", "stderr", "error: fake missing file\n")])
        self.assertTrue(output.stdout.closed)
        self.assertTrue(output.stderr.closed)

    def test_chunked_secrets_utf8_and_terminal_normalization(self):
        events = []
        observed = threading.Event()
        with NativeOutput(self.directory, "fake", 1,
                          lambda a, c, t: (events.append(t), observed.set())) as output:
            # Force the reader to see a partial credential before the rest arrives.
            original = os.pread
            partial = threading.Event()
            def pread(descriptor, size, offset):
                data = original(descriptor, size, offset)
                if b"token=top" in data:
                    partial.set()
                return data
            with patch("envy.storage.output.os.pread", side_effect=pread):
                output.stdout.write(b"token=top")
                self.assertTrue(partial.wait(5))
                self.assertEqual(events, [])
                output.stdout.write(b"secret\n")
                self.assertTrue(observed.wait(5))
            chunks = [b"https://user:pass", b"word@fake.invalid/cache?token=querysecret\n",
                      b"Authorization: Bear", b"er headersecret\n",
                      b"API_KEY='assignmentsecret'\nCookie: cookiesecret\n",
                      b"\x1b[31mremoved /fake/cache: 3\x1b[0m\rprogress\x00\n",
                      b"\xe6", b"\xb8\x85\xe7\x90\x86\n"]
            for chunk in chunks:
                output.stdout.write(chunk)
            output.finish()
            log = Path(output.metadata["stdoutPath"]).read_text()
            self.assertEqual(log, "".join(events))
            for secret in ("topsecret", "password", "querysecret", "headersecret",
                           "assignmentsecret", "cookiesecret", "user:"):
                self.assertNotIn(secret, log)
            self.assertIn("removed /fake/cache: 3 progress", log)
            self.assertIn("清理", log)
            self.assertNotIn("\x1b", log)
            self.assertNotIn("\x00", log)
            self.assertNotIn("\r", log)

    def test_overlong_lines_are_omitted_whole_including_unterminated_line(self):
        events = []
        with NativeOutput(self.directory, "fake", 0,
                          lambda a, c, t: events.append(t)) as output:
            output.stdout.write(b"token=" + b"s" * _LINE_LIMIT)
            output.stdout.write(b"secret suffix\nnormal /fake/path\n")
            output.stderr.write(b"z" * (_LINE_LIMIT + 1))
            output.finish()
            self.assertEqual(events.count("[overlong output line omitted]\n"), 2)
            self.assertIn("normal /fake/path\n", events)
            self.assertNotIn("secret suffix", "".join(events))

    def test_bounded_utf8_tails_permissions_and_small_metadata(self):
        with NativeOutput(self.directory, "fake", 2) as output:
            self.assertTrue(Path(output.metadata["stdoutPath"]).exists())
            line = ("清" * 3000 + "\n").encode()
            output.stdout.write(line * 130)
            output.stderr.write(b"last error\n")
            output.finish()
            self.assertEqual(output.metadata["truncatedStreams"], ["stdout"])
            for channel in ("stdout", "stderr"):
                path = Path(output.metadata[channel + "Path"])
                self.assertLessEqual(path.stat().st_size, _STREAM_LIMIT)
                path.read_bytes().decode("utf-8")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
            self.assertLess(len(json.dumps(output.metadata)), 1024)

    def test_none_directory_retains_raw_capture_without_persistent_paths(self):
        with NativeOutput(None, "fake", 0) as output:
            output.stdout.write(b"token=rawsecret\n")
            output.finish()
            output.stdout.seek(0)
            self.assertEqual(output.stdout.read(), b"token=rawsecret\n")
            self.assertEqual(output.metadata, {"truncatedStreams": []})
        self.assertFalse(self.directory.exists())

    def test_symlinks_rejected_before_command_and_again_before_persist(self):
        actual = self.root / "actual"
        actual.mkdir()
        self.directory.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(StorageError):
            NativeOutput(self.directory, "fake", 0)
        self.directory.unlink()
        self.directory.mkdir()
        victim = self.root / "victim"
        victim.write_text("untouched")
        target = self.directory / "000-fake-stdout.log"
        target.symlink_to(victim)
        with self.assertRaises(StorageError):
            NativeOutput(self.directory, "fake", 0)
        target.unlink()
        with NativeOutput(self.directory, "fake", 0) as output:
            target.unlink()
            target.symlink_to(victim)
            output.stdout.write(b"fake\n")
            output.finish()
            with self.assertRaises(StorageError):
                output.raise_if_failed()
        self.assertEqual(victim.read_text(), "untouched")
        self.assertIn("stdoutWrite", output.metadata["outputErrors"])

    def test_interrupt_drains_closes_and_does_not_mask_original_exception(self):
        events = []
        with self.assertRaises(KeyboardInterrupt):
            with NativeOutput(self.directory, "fake", 0,
                              lambda a, c, t: events.append(t)) as output:
                output.stdout.write(b"trailing partial")
                output.stderr.write(b"error tail")
                raise KeyboardInterrupt
        self.assertFalse(output._thread.is_alive())
        self.assertTrue(output.stdout.closed)
        self.assertCountEqual(events, ["trailing partial\n", "error tail\n"])
        self.assertEqual(Path(output.metadata["stderrPath"]).read_text(), "error tail\n")

    def test_interrupt_during_join_still_drains_persists_and_closes(self):
        with self.assertRaises(KeyboardInterrupt):
            with NativeOutput(self.directory, "fake", 0) as output:
                output.stdout.write(b"last partial")
                join = output._thread.join
                calls = []
                def interrupted_join():
                    calls.append(True)
                    if len(calls) == 1:
                        raise KeyboardInterrupt
                    join()
                with patch.object(output._thread, "join", side_effect=interrupted_join):
                    output.finish()
        self.assertFalse(output._thread.is_alive())
        self.assertTrue(output.stdout.closed)
        self.assertEqual(Path(output.metadata["stdoutPath"]).read_text(), "last partial\n")

    def test_callback_and_write_errors_are_safe_and_explicit(self):
        def broken(*args):
            raise ValueError("SECRET exception detail")
        output = NativeOutput(self.directory, "fake", 0, broken)
        with self.assertRaisesRegex(StorageError, "callback, stderrWrite, stdoutWrite") as raised:
            with output:
                output.stdout.write(b"first\nsecond\n")
                with patch("envy.storage.output.atomic_write_bytes",
                           side_effect=OSError("SECRET filesystem detail")):
                    output.finish()
                    output.finish()
                    output.stdout.seek(0)
                    self.assertEqual(output.stdout.read(), b"first\nsecond\n")
                    self.assertEqual(output.metadata["logError"],
                                     "Native output log could not be saved")
                    output.raise_if_failed()
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertNotIn("SECRET", json.dumps(output.metadata))
        self.assertFalse(output._thread.is_alive())

    def test_read_failure_is_reported_without_unhandled_thread_exception(self):
        with self.assertRaisesRegex(StorageError, "Read"):
            with NativeOutput(None, "fake", 0) as output:
                with patch("envy.storage.output.os.pread", side_effect=OSError("SECRET")):
                    output.finish()
                    output.raise_if_failed()
        self.assertFalse(output._thread.is_alive())
        self.assertNotIn("SECRET", json.dumps(output.metadata))

    def test_public_sanitizer_is_safe_for_multiline_failure_summaries(self):
        text = sanitize_output("error /fake/cache\nAPI_KEY=secret\n"
                               "Authorization: Bearer othersecret\nnormal count: 3\n"
                               "//fake.invalid/:_authToken=npmsecret\n")
        self.assertNotIn("secret", text)
        self.assertIn("error /fake/cache\n", text)
        self.assertIn("normal count: 3", text)

    def test_persistence_failure_does_not_replace_successful_command_status(self):
        with NativeOutput(self.directory, "fake", 0) as output:
            result = run_process([sys.executable, "-c", "print('fake success')"],
                                 stdout=output.stdout, stderr=output.stderr)
            with patch("envy.storage.output.atomic_write_bytes",
                       side_effect=OSError("SECRET")):
                output.finish()
        self.assertEqual(result.returncode, 0)
        self.assertIn("logError", output.metadata)
        self.assertNotIn("SECRET", json.dumps(output.metadata))

    def test_creation_failure_precedes_command(self):
        with patch("envy.storage.output.atomic_write_bytes", side_effect=OSError("fake")):
            with self.assertRaises(OSError):
                NativeOutput(self.directory, "fake", 0)
        self.assertFalse(any(thread.name.startswith("envy-output-")
                             for thread in threading.enumerate()))

    def test_invalid_action_and_step_cannot_escape_log_directory(self):
        for action, step in (("../fake", 0), ("fake", -1), ("fake", True)):
            with self.subTest(action=action, step=step), self.assertRaises(ValueError):
                NativeOutput(self.directory, action, step)
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()

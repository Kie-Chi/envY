import copy
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from envy.doctor.probes.command import BoundedRead, run_bounded
from envy.habit import check_habits, habits_from_manifest
from envy.habit_verification import _gvariant_string
from test_habit import _entry, _manifest


GNOME_SESSION = {
    "XDG_CURRENT_DESKTOP": "GNOME", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/bus",
    "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "synthetic-wayland",
}
NIRI_SESSION = {**GNOME_SESSION, "XDG_CURRENT_DESKTOP": "niri", "NIRI_SOCKET": "/synthetic/niri.sock"}
LAYOUT = {"names": ["English (US)"], "current_idx": 0}


def native_manifest(adapter="darwin.key-repeat", expected=1):
    platform, _, name = adapter.partition(".")
    context = "gnome" if name == "gnome-scratchpad" else "niri" if name == "niri-keyboard-layout" else "darwin"
    entry = _entry("native-preference", context)
    entry.update(kind="preference", gesture=None, readback={"adapter": adapter, "expected": expected})
    manifest = _manifest(entry)
    manifest["platform"] = platform
    return manifest


def evidence(manifest):
    results, failed = check_habits(manifest)
    return results[-1].details["verification"], failed


def read(text):
    return BoundedRead(text, "read", 0)


class NativeHabitVerificationTests(unittest.TestCase):
    def test_preference_allows_absent_or_null_gesture_but_binding_remains_strict(self):
        for value in (None, "absent"):
            manifest = native_manifest()
            if value == "absent":
                del manifest["habits"][0]["gesture"]
            habits, errors = habits_from_manifest(manifest)
            self.assertEqual(errors, [])
            self.assertIsNone(habits[0].gesture)
            self.assertIsNone(habits[0].to_dict()["gesture"])
            manifest["habits"][0]["kind"] = "binding"
            self.assertTrue(habits_from_manifest(manifest)[1])
        manifest["habits"][0].update(kind="preference", gesture=" ")
        self.assertTrue(habits_from_manifest(manifest)[1])

    def test_darwin_integer_stored_evidence_does_not_imply_runtime_or_pending(self):
        with patch("envy.habit_verification.sys.platform", "darwin"), patch(
            "envy.habit_verification.command.run_bounded", side_effect=[read("Type is integer\n"), read("1\n")]
        ) as probe:
            result, failed = evidence(native_manifest())
        self.assertFalse(failed)
        self.assertEqual(result["declaration"]["status"], "statically-consistent")
        self.assertEqual(result["stored"]["status"], "match")
        self.assertEqual(result["runtime"]["status"], "unknown")
        self.assertEqual(result["pending"]["status"], "unknown")
        self.assertEqual(probe.call_args_list[0].args[0], ["defaults", "read-type", "NSGlobalDomain", "KeyRepeat"])
        self.assertEqual(probe.call_args_list[1].args[0], ["defaults", "read", "NSGlobalDomain", "KeyRepeat"])
        self.assertEqual(len(result["declarationFingerprint"]), 64)
        self.assertIn("+00:00", result["sampledAt"])
        for axis in ("declaration", "stored", "runtime", "pending"):
            self.assertEqual(set(result[axis]), {"status", "reason", "source", "scope"})

    def test_darwin_bool_is_typed_and_uses_fixed_key(self):
        with patch("envy.habit_verification.sys.platform", "darwin"), patch(
            "envy.habit_verification.command.run_bounded", side_effect=[read("Type is boolean"), read("0")]
        ) as probe:
            result, failed = evidence(native_manifest("darwin.fn-state", False))
        self.assertFalse(failed)
        self.assertEqual(result["stored"]["status"], "match")
        self.assertEqual(probe.call_args.args[0][-1], "com.apple.keyboard.fnState")

    def test_darwin_type_mismatch_does_not_coerce_nsstring_or_integer_to_bool(self):
        for adapter, expected, native_type in (
            ("darwin.key-repeat", 1, "string"),
            ("darwin.fn-state", True, "integer"),
            ("darwin.key-repeat", 1, "boolean"),
        ):
            with self.subTest(adapter=adapter, native_type=native_type), patch(
                "envy.habit_verification.sys.platform", "darwin"
            ), patch("envy.habit_verification.command.run_bounded", return_value=read(f"Type is {native_type}")) as probe:
                result, failed = evidence(native_manifest(adapter, expected))
            self.assertTrue(failed)
            self.assertEqual(result["stored"]["status"], "mismatch")
            self.assertEqual(result["stored"]["reason"], "type-mismatch")
            self.assertEqual(probe.call_count, 1)

    def test_unknown_type_missing_key_permission_and_invalid_value_stay_unknown(self):
        responses = (
            [read("unknown type: integer")],
            [BoundedRead(None, "read-unavailable", 1)],
            [BoundedRead(None, "permission-denied")],
            [read("Type is integer"), read("false")],
            [read("Type is integer"), BoundedRead(None, "read-unavailable", 1)],
        )
        for response in responses:
            with self.subTest(response=response), patch("envy.habit_verification.sys.platform", "darwin"), patch(
                "envy.habit_verification.command.run_bounded", side_effect=response
            ):
                result, failed = evidence(native_manifest())
            self.assertFalse(failed)
            self.assertEqual(result["stored"]["status"], "unknown")

    def test_known_stored_drift_fails_check(self):
        with patch("envy.habit_verification.sys.platform", "darwin"), patch(
            "envy.habit_verification.command.run_bounded", side_effect=[read("Type is integer"), read("3")]
        ):
            result, failed = evidence(native_manifest())
        self.assertTrue(failed)
        self.assertEqual(result["stored"]["status"], "mismatch")
        self.assertEqual(result["pending"]["status"], "unknown")

    def test_gvariant_string_parser_rejects_expressions_and_non_string_types(self):
        self.assertEqual(_gvariant_string("'F12'\n"), "F12")
        self.assertEqual(_gvariant_string('"F12"'), "F12")
        self.assertEqual(_gvariant_string(r"'\u0046\x31'"), None)
        self.assertEqual(_gvariant_string(r"'\u0046\u0031\u0032'"), "F12")
        self.assertEqual(_gvariant_string(r"'a\'b\\c'"), "a'b\\c")
        for text in ("1", "true", "['F12']", "'F12'; code()", "@i 1", "'F12' 'F11'", "'bad\\'", "'a\nb'", r"'\uD800'", r"'\U00110000'"):
            with self.subTest(text=text):
                self.assertIsNone(_gvariant_string(text))

    def test_gnome_reads_only_binding_and_keeps_runtime_unknown(self):
        with patch("envy.habit_verification.sys.platform", "linux"), patch.dict(os.environ, GNOME_SESSION, clear=True), patch(
            "envy.habit_verification.command.run_bounded", return_value=read("'F12'")
        ) as probe:
            result, failed = evidence(native_manifest("linux.gnome-scratchpad", "F12"))
        self.assertFalse(failed)
        self.assertEqual(result["stored"]["status"], "match")
        self.assertEqual(result["runtime"]["status"], "unknown")
        self.assertEqual(result["pending"]["status"], "unknown")
        argv = probe.call_args.args[0]
        self.assertEqual(argv[:2], ["gsettings", "get"])
        self.assertTrue(argv[2].endswith("/custom-tilix-quake/"))
        self.assertEqual(argv[3:], ["binding"])

    def test_gnome_context_overrides_are_unknown_without_any_spawn(self):
        overrides = {
            "GSETTINGS_BACKEND": "memory",
            "GSETTINGS_SCHEMA_DIR": "/synthetic/schema-defaults",
            "DCONF_PROFILE": "/synthetic/profile",
            "DCONF_DB_DIR": "/synthetic/databases",
            "DCONF_USER_CONFIG_DIR": "/synthetic/config",
            "DCONF_RUN_DIR": "/synthetic/run",
            "GIO_MODULE_DIR": "/synthetic/modules",
            "GIO_EXTRA_MODULES": "/synthetic/extra-modules",
            "XDG_CONFIG_HOME": "/synthetic/other-config",
            "XDG_DATA_HOME": "/synthetic/other-data",
            "XDG_DATA_DIRS": "/synthetic/schema-search-path",
        }
        for key, value in overrides.items():
            with self.subTest(key=key), patch("envy.habit_verification.sys.platform", "linux"), patch.dict(
                os.environ, {**GNOME_SESSION, key: value}, clear=True
            ), patch("envy.habit_verification.command.run_bounded", return_value=read("'F12'")) as probe:
                result, failed = evidence(native_manifest("linux.gnome-scratchpad", "F12"))
            probe.assert_not_called()
            self.assertFalse(failed)
            self.assertEqual(result["stored"]["status"], "unknown")
            self.assertEqual(result["stored"]["reason"], "settings-context-unconfirmed")
            self.assertNotIn(value, json.dumps(result))

    def test_even_explicit_dconf_and_empty_schema_overrides_require_context_proof(self):
        for key, value in (("GSETTINGS_BACKEND", "dconf"), ("GSETTINGS_SCHEMA_DIR", ""),
                           ("DCONF_PROFILE", ""), ("GSETTINGS_SYNTHETIC_OVERRIDE", "synthetic")):
            with self.subTest(key=key, value=value), patch("envy.habit_verification.sys.platform", "linux"), patch.dict(
                os.environ, {**GNOME_SESSION, key: value}, clear=True
            ), patch("envy.habit_verification.command.run_bounded", return_value=read("'F12'")) as probe:
                result, failed = evidence(native_manifest("linux.gnome-scratchpad", "F12"))
            probe.assert_not_called()
            self.assertFalse(failed)
            self.assertEqual(result["stored"]["status"], "unknown")

    def test_gnome_requires_reliable_session_without_dbus_launch(self):
        for missing in ("XDG_CURRENT_DESKTOP", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY"):
            session = {key: value for key, value in GNOME_SESSION.items() if key != missing}
            with self.subTest(missing=missing), patch("envy.habit_verification.sys.platform", "linux"), patch.dict(
                os.environ, session, clear=True
            ), patch("envy.habit_verification.command.run_bounded") as probe:
                result, failed = evidence(native_manifest("linux.gnome-scratchpad", "F12"))
            probe.assert_not_called()
            self.assertFalse(failed)
            self.assertEqual(result["stored"]["reason"], "session-unavailable")

    def test_gnome_schema_tool_errors_and_invalid_gvariant_are_unknown(self):
        for response in (BoundedRead(None, "read-unavailable", 1), BoundedRead(None, "missing-tool"), read("true")):
            with self.subTest(response=response), patch("envy.habit_verification.sys.platform", "linux"), patch.dict(
                os.environ, GNOME_SESSION, clear=True
            ), patch("envy.habit_verification.command.run_bounded", return_value=response):
                result, failed = evidence(native_manifest("linux.gnome-scratchpad", "F12"))
            self.assertFalse(failed)
            self.assertEqual(result["stored"]["status"], "unknown")

    def test_niri_positive_is_scoped_to_native_layout_identity(self):
        payload = {**LAYOUT, "unrelated": {"private": "not exported"}}
        with patch("envy.habit_verification.sys.platform", "linux"), patch.dict(os.environ, NIRI_SESSION, clear=True), patch(
            "envy.habit_verification.command.run_bounded", return_value=read(json.dumps(payload))
        ) as probe:
            result, failed = evidence(native_manifest("linux.niri-keyboard-layout", LAYOUT))
        self.assertFalse(failed)
        self.assertEqual(result["stored"]["status"], "unknown")
        self.assertEqual(result["runtime"]["status"], "match")
        self.assertEqual(result["runtime"]["scope"], "running-compositor-layout-identity")
        self.assertEqual(result["pending"]["status"], "none")
        self.assertNotIn("private", json.dumps(result))
        probe.assert_called_once_with(["niri", "msg", "--json", "keyboard-layouts"])

    def test_niri_rejects_malformed_types_bounds_and_wrappers(self):
        invalid = (None, [], {"Ok": {"KeyboardLayouts": LAYOUT}}, {},
                   {"names": [], "current_idx": 0}, {"names": ["English (US)"], "current_idx": True},
                   {"names": ["English (US)"], "current_idx": 1},
                   {"names": ["English (US)"], "current_idx": -1},
                   {"names": ["English (US)"], "current_idx": 256},
                   {"names": [1], "current_idx": 0}, {"names": [""], "current_idx": 0})
        for payload in invalid:
            with self.subTest(payload=payload), patch("envy.habit_verification.sys.platform", "linux"), patch.dict(
                os.environ, NIRI_SESSION, clear=True
            ), patch("envy.habit_verification.command.run_bounded", return_value=read(json.dumps(payload))):
                result, failed = evidence(native_manifest("linux.niri-keyboard-layout", LAYOUT))
            self.assertFalse(failed)
            self.assertEqual(result["runtime"]["status"], "unknown")
            self.assertEqual(result["pending"]["status"], "unknown")

    def test_niri_layout_drift_and_missing_session(self):
        with patch("envy.habit_verification.sys.platform", "linux"), patch.dict(os.environ, NIRI_SESSION, clear=True), patch(
            "envy.habit_verification.command.run_bounded", return_value=read(json.dumps({"names": ["German"], "current_idx": 0}))
        ):
            result, failed = evidence(native_manifest("linux.niri-keyboard-layout", LAYOUT))
        self.assertTrue(failed)
        self.assertEqual(result["runtime"]["status"], "mismatch")
        self.assertEqual(result["pending"]["status"], "unknown")
        with patch("envy.habit_verification.sys.platform", "linux"), patch.dict(os.environ, {}, clear=True), patch(
            "envy.habit_verification.command.run_bounded"
        ) as probe:
            result, _ = evidence(native_manifest("linux.niri-keyboard-layout", LAYOUT))
        probe.assert_not_called()
        self.assertEqual(result["runtime"]["reason"], "session-unavailable")

    def test_niri_connect_timeout_byte_limit_tool_and_json_errors_are_unknown(self):
        for response in (BoundedRead(None, "read-unavailable", 1), BoundedRead(None, "timeout"),
                         BoundedRead(None, "output-limit"), BoundedRead(None, "missing-tool"), read("not JSON")):
            with self.subTest(response=response), patch("envy.habit_verification.sys.platform", "linux"), patch.dict(
                os.environ, NIRI_SESSION, clear=True
            ), patch("envy.habit_verification.command.run_bounded", return_value=response):
                result, failed = evidence(native_manifest("linux.niri-keyboard-layout", LAYOUT))
            self.assertFalse(failed)
            self.assertEqual(result["runtime"]["status"], "unknown")
            self.assertEqual(result["pending"]["status"], "unknown")

    def test_unknown_adapter_or_injected_command_never_spawns(self):
        for readback in ({"adapter": "invented", "expected": 1},
                         {"adapter": "darwin.key-repeat", "expected": 1, "command": ["touch", "/tmp/bad"]},
                         {"adapter": "defaults", "domain": "private", "expected": 1}):
            manifest = native_manifest()
            manifest["habits"][0]["readback"] = readback
            with self.subTest(readback=readback), patch("envy.habit_verification.command.run_bounded") as probe:
                result, failed = evidence(manifest)
            probe.assert_not_called()
            self.assertTrue(failed)
            self.assertEqual(result["declaration"]["status"], "invalid")

    def test_expected_types_are_strict_and_context_drift_stops_probes(self):
        for adapter, expected in (("darwin.key-repeat", True), ("darwin.fn-state", 1),
                                  ("linux.niri-keyboard-layout", None),
                                  ("linux.niri-keyboard-layout", {"names": ["English (US)"], "current_idx": False})):
            with self.subTest(adapter=adapter), patch("envy.habit_verification.command.run_bounded") as probe:
                _, failed = evidence(native_manifest(adapter, expected))
            probe.assert_not_called()
            self.assertTrue(failed)
        manifest = native_manifest()
        manifest["habits"][0]["context"] = "gnome"
        with patch("envy.habit_verification.command.run_bounded") as probe:
            _, failed = evidence(manifest)
        probe.assert_not_called()
        self.assertTrue(failed)

    def test_application_owned_and_old_niri_bindings_remain_manual_without_reads(self):
        app = native_manifest()
        app["habits"][0]["ownership"] = "application"
        iterm = _entry("terminal-scratchpad", "darwin", ownership="application")
        iterm["backend"] = "iTerm2"
        raycast = _entry("global-launcher", "darwin", ownership="application")
        raycast["backend"] = "Raycast"
        for manifest in (app, _manifest(iterm, raycast),
                         _manifest(_entry("terminal-scratchpad", "niri"), _entry("global-launcher", "niri"))):
            with patch("envy.habit_verification.command.run_bounded") as probe, patch(
                "pathlib.Path.read_text", side_effect=AssertionError("no private file reads")
            ):
                result, failed = evidence(manifest)
            probe.assert_not_called()
            self.assertFalse(failed)
            for axis in ("stored", "runtime", "pending"):
                self.assertEqual(result[axis]["status"], "unknown")

    def test_cross_host_and_missing_prerequisites_do_not_probe(self):
        manifest = native_manifest()
        with patch("envy.habit_verification.sys.platform", "linux"), patch("envy.habit_verification.command.run_bounded") as probe:
            result, failed = evidence(manifest)
        self.assertFalse(failed)
        probe.assert_not_called()
        self.assertEqual(result["stored"]["reason"], "host-platform-mismatch")
        manifest["habits"][0]["requirements"] = [{"group": "nix.user.package", "item": "required"}]
        with patch("envy.habit_verification.command.run_bounded") as probe:
            result, failed = evidence(manifest)
        probe.assert_not_called()
        self.assertTrue(failed)
        self.assertEqual(result["declaration"]["status"], "invalid")

    def test_one_invalid_declaration_does_not_relabel_unrelated_consistent_entries(self):
        manifest = _manifest(_entry("valid-binding", "niri"), _entry("invalid-binding", "niri"))
        manifest["habits"][1]["readback"] = {"adapter": "invented", "expected": "F12"}
        with patch("envy.habit_verification.command.run_bounded") as probe:
            results, failed = check_habits(manifest)
        probe.assert_not_called()
        self.assertTrue(failed)
        valid = next(result for result in results if result.habit == "valid-binding")
        invalid = next(result for result in results if result.habit == "invalid-binding")
        self.assertEqual(valid.details["verification"]["declaration"]["status"], "statically-consistent")
        self.assertEqual(invalid.details["verification"]["declaration"]["status"], "invalid")

    def test_declaration_conflict_is_invalid_even_when_one_normalized_entry_remains(self):
        clean = _manifest(_entry("terminal-scratchpad", "niri"))
        conflicted = copy.deepcopy(clean)
        conflicted["habits"].append(_entry("terminal-scratchpad", "niri", semantic="Different action"))
        first, _ = evidence(clean)
        second, failed = evidence(conflicted)
        self.assertTrue(failed)
        self.assertEqual(second["declaration"]["status"], "invalid")
        self.assertNotEqual(first["declarationFingerprint"], second["declarationFingerprint"])

    def test_samples_are_fresh_and_fingerprints_follow_declaration_identity(self):
        manifest = native_manifest()
        with patch("envy.habit_verification.sys.platform", "darwin"), patch(
            "envy.habit_verification.command.run_bounded",
            side_effect=[read("Type is integer"), read("1"), read("Type is integer"), read("3")]
        ) as probe:
            first, _ = evidence(manifest)
            second, _ = evidence(manifest)
        self.assertEqual(probe.call_count, 4)
        self.assertEqual(first["stored"]["status"], "match")
        self.assertEqual(second["stored"]["status"], "mismatch")
        self.assertEqual(first["declarationFingerprint"], second["declarationFingerprint"])
        self.assertNotEqual(first["sampledAt"], second["sampledAt"])
        variants = []
        for key, value in (("id", "other-machine"), ("platform", "linux")):
            variant = copy.deepcopy(manifest)
            variant[key] = value
            variants.append(variant)
        variant = copy.deepcopy(manifest)
        variant["habits"][0]["readback"]["expected"] = 2
        variants.append(variant)
        variant = copy.deepcopy(manifest)
        variant["habits"][0]["context"] = "different-context"
        variants.append(variant)
        variant = copy.deepcopy(manifest)
        variant["habits"][0]["requirements"] = [{"group": "nix.user.package", "item": "required"}]
        variants.append(variant)
        with patch("envy.habit_verification.command.run_bounded") as probe:
            for variant in variants:
                result = check_habits(variant, declaration_only=True)[0][-1].details["verification"]
                self.assertNotEqual(first["declarationFingerprint"], result["declarationFingerprint"])
        probe.assert_not_called()


class BoundedHabitReadTests(unittest.TestCase):
    def test_success_stderr_is_never_exported(self):
        result = run_bounded([sys.executable, "-c", "import sys; print('1'); print('PRIVATE', file=sys.stderr)"])
        self.assertEqual(result.stdout, "1\n")
        self.assertEqual(result.reason, "read")
        self.assertNotIn("PRIVATE", repr(result))

    def test_missing_tool_permission_and_command_failure_are_safe(self):
        result = run_bounded(["/nonexistent/synthetic-habit-probe"])
        self.assertEqual(result.reason, "missing-tool")
        with patch("envy.doctor.probes.command.subprocess.Popen", side_effect=PermissionError("PRIVATE")):
            result = run_bounded(["synthetic"])
        self.assertEqual(result.reason, "permission-denied")
        self.assertNotIn("PRIVATE", repr(result))
        result = run_bounded([sys.executable, "-c", "import sys; sys.stderr.write('PRIVATE'); sys.exit(1)"])
        self.assertEqual(result.reason, "read-unavailable")
        self.assertIsNone(result.stdout)

    def test_both_stdout_and_stderr_are_capped_during_read(self):
        for pipe in ("stdout", "stderr"):
            script = f"import sys;\nwhile True: sys.{pipe}.write('x' * 10000); sys.{pipe}.flush()"
            with self.subTest(pipe=pipe):
                started = time.monotonic()
                result = run_bounded([sys.executable, "-c", script], timeout=2, byte_limit=64)
                self.assertEqual(result.reason, "output-limit")
                self.assertIsNone(result.stdout)
                self.assertLess(time.monotonic() - started, 2)

    def test_timeout_reaps_process_and_descendant_holding_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pid"
            lock_file = Path(directory) / "lock"
            ready_file = Path(directory) / "ready"
            script = (
                "import fcntl, os, pathlib, time\n"
                "child = os.fork()\n"
                "if child:\n"
                f"    pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "else:\n"
                f"    lock = open({str(lock_file)!r}, 'w')\n"
                "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
                f"    pathlib.Path({str(ready_file)!r}).write_text('ready')\n"
                "time.sleep(30)\n"
            )
            started = time.monotonic()
            result = run_bounded([sys.executable, "-c", script], timeout=1)
            self.assertEqual(result.reason, "timeout")
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(ready_file.read_text(), "ready")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            # The OS releases this lock only after the descendant has exited.
            with lock_file.open() as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unconfirmed_cleanup(self, script, wait_results):
        real_popen = subprocess.Popen
        spawned = []
        waits = []

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            real_wait = process.wait
            spawned.append((process, real_wait))
            responses = iter(wait_results)

            def wait(timeout=None):
                waits.append(timeout)
                if timeout is None:
                    raise AssertionError("unbounded reap")
                response = next(responses, "unreapable")
                if response == "unreapable":
                    time.sleep(timeout)
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                return response

            process.wait = wait
            return process

        try:
            with patch("envy.doctor.probes.command.subprocess.Popen", side_effect=spawn):
                result = run_bounded([sys.executable, "-c", script], timeout=0.1, byte_limit=32)
            return result, waits
        finally:
            for process, real_wait in spawned:
                process.wait = real_wait
                process.kill()
                real_wait(timeout=2)

    def test_timeout_cleanup_cannot_wait_without_a_deadline(self):
        started = time.monotonic()
        result, waits = self._unconfirmed_cleanup("import time; time.sleep(30)", ["unreapable"])
        self.assertEqual(result.reason, "cleanup-unconfirmed")
        self.assertIsNone(result.stdout)
        self.assertTrue(all(timeout is not None and 0 < timeout <= 0.25 for timeout in waits))
        self.assertLess(time.monotonic() - started, 1)

    def test_output_limit_cleanup_failure_stays_unknown(self):
        result, waits = self._unconfirmed_cleanup("import os; os.write(1, b'x' * 1024)", ["unreapable"])
        self.assertEqual(result.reason, "cleanup-unconfirmed")
        self.assertIsNone(result.stdout)
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], 0.25)

    def test_success_is_discarded_when_final_cleanup_is_unconfirmed(self):
        result, waits = self._unconfirmed_cleanup("print('1')", [0, "unreapable"])
        self.assertEqual(result.reason, "cleanup-unconfirmed")
        self.assertIsNone(result.stdout)
        self.assertIsNone(result.returncode)
        self.assertEqual(len(waits), 2)
        self.assertTrue(0 < waits[0] <= 0.1)
        self.assertTrue(0 < waits[1] <= 0.25)

    def test_cleanup_failure_does_not_swallow_an_unexpected_collection_exception(self):
        with patch("envy.doctor.probes.command._collect_bounded", side_effect=RuntimeError("synthetic-failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic-failure"):
                self._unconfirmed_cleanup("import time; time.sleep(30)", ["unreapable"])

    def test_success_is_discarded_when_group_cleanup_is_denied(self):
        with patch("envy.doctor.probes.command.os.killpg", side_effect=PermissionError("synthetic-private-error")):
            result, waits = self._unconfirmed_cleanup("print('1')", [0, 0])
        self.assertEqual(result.reason, "cleanup-unconfirmed")
        self.assertIsNone(result.stdout)
        self.assertNotIn("synthetic-private-error", repr(result))
        self.assertEqual(len(waits), 2)

    def test_invalid_utf8_and_budget_at_eof(self):
        result = run_bounded([sys.executable, "-c", "import os; os.write(1, b'\\xff')"])
        self.assertEqual(result.reason, "invalid-encoding")
        result = run_bounded([sys.executable, "-c", "import os; os.write(1, b'1234')"], byte_limit=4)
        self.assertEqual(result.stdout, "1234")
        result = run_bounded([sys.executable, "-c", "import os; os.write(1, b'12345')"], byte_limit=4)
        self.assertEqual(result.reason, "output-limit")


if __name__ == "__main__":
    unittest.main()

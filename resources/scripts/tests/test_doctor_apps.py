import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from envy.doctor.checks.apps import auth, checkers, vscode
from envy.doctor.model import SECTION_AUTH, info
from envy.doctor.probes import command as command_probe
from envy.doctor.probes import filesystem
from envy.doctor.probes import vscode as vscode_probe
from envy.schemas.apps import EXPECTED_EXTENSIONS, AppSpec, PermissionReq

# Values that local app state may contain but doctor output must never repeat.
EMAIL = "person@example.com"
API_KEY = "sk-test-secret"
TOKEN = "gho_secret_token"
APP_ID = "cli_secret_app"
SENSITIVE = (EMAIL, API_KEY, TOKEN, APP_ID)


def completed(returncode=0, stdout="", stderr=""):
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class SafeOutputMixin:
    def assertNoSensitiveValues(self, results):
        rendered = json.dumps([result.to_dict() for result in results])
        for value in SENSITIVE:
            self.assertNotIn(value, rendered)


class TemporaryRootMixin:
    def make_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="envy-doctor-")
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)


class GenericCheckerTests(TemporaryRootMixin, unittest.TestCase):
    def setUp(self):
        for name, value in (("_tcc_result", None), ("_tcc_warned_flag", False)):
            self.enterContext(patch.object(checkers, name, value))

    def test_specs_without_signals_produce_no_results(self):
        self.assertEqual(checkers.run_app_checks(AppSpec(name="Empty")), [])

    def test_bundles_are_found_by_name_or_absolute_path(self):
        root = self.make_root()
        (root / "Tool.app").mkdir()
        with patch.object(filesystem, "APP_DIRS", [root]):
            self.assertEqual(filesystem.app_bundle(["Missing.app", "Tool.app"]), root / "Tool.app")
            self.assertEqual(filesystem.app_bundle([str(root / "Tool.app")]), root / "Tool.app")
            [missing] = checkers.check_installed(AppSpec(name="Gone", bundles=["Gone.app"]))
        self.assertEqual((missing.status, missing.action), ("warn", {"kind": "run", "argv": ["envy", "apply"]}))

    def test_missing_commands_are_listed_with_the_available_ones(self):
        spec = AppSpec(name="Tool", commands=["present", "absent"])
        with patch.object(checkers.command, "exists", side_effect=lambda name: name == "present"):
            [result] = checkers.check_commands(spec)
        self.assertEqual(result.status, "warn")
        self.assertEqual(result.message, "missing command(s): absent; available: present")

    def test_only_background_apps_warn_when_not_running(self):
        with patch.object(checkers.process, "app_running", return_value=False):
            [optional] = checkers.check_running(AppSpec(name="App", processes=["app"]))
            [required] = checkers.check_running(AppSpec(name="App", processes=["app"], should_run=True))
        self.assertEqual((optional.status, required.status), ("info", "warn"))

    def test_login_hint_disappears_once_the_app_runs_with_state(self):
        state = self.make_root() / "state.json"
        spec = AppSpec(name="App", processes=["app"], state_paths=[state], login_hint="Sign in once.")
        with patch.object(checkers.process, "app_running", return_value=True):
            self.assertEqual([result.message for result in checkers.check_login(spec)], ["Sign in once."])
            state.write_text("{}")
            self.assertEqual(checkers.check_login(spec), [])

    def test_permissions_distinguish_allowed_missing_and_denied(self):
        spec = AppSpec(name="Meet", bundle_id="com.example.meet", permissions=[
            PermissionReq("kTCCServiceCamera", "Camera", "video calls"),
            PermissionReq("kTCCServiceMicrophone", "Microphone", "audio"),
            PermissionReq("kTCCServiceScreenCapture", "Screen Recording", "sharing", tcc_client="com.example.helper"),
        ])
        records = {
            ("kTCCServiceCamera", "com.example.meet"): 2,
            ("kTCCServiceScreenCapture", "com.example.helper"): 0,
        }
        with patch.object(checkers.tcc, "permission_records", return_value=(records, True)):
            results = checkers.check_permissions(spec)
        self.assertEqual([result.status for result in results], ["ok", "warn", "warn"])
        self.assertIn("not requested/granted yet", results[1].message)
        self.assertIn("auth_value=0", results[2].message)

    def test_unreadable_tcc_database_is_probed_and_reported_once(self):
        spec = AppSpec(name="Meet", bundle_id="com.example.meet",
                       permissions=[PermissionReq("kTCCServiceCamera", "Camera", "calls")])
        with patch.object(checkers.tcc, "permission_records", return_value=({}, False)) as probe:
            first = checkers.check_permissions(spec)
            second = checkers.check_permissions(spec)
        self.assertEqual([result.name for result in first], ["TCC database"])
        self.assertEqual(second, [])
        probe.assert_called_once()

    def test_declared_custom_checkers_run_and_unknown_names_are_ignored(self):
        marker = [info(SECTION_AUTH, "App auth", "custom")]
        spec = AppSpec(name="App", checkers=["codex_auth", "unknown_checker"])
        with patch.dict(checkers._load_custom_checkers(), {"codex_auth": lambda _spec: marker}):
            self.assertEqual(checkers.run_app_checks(spec), marker)

    def test_command_probe_reports_timeouts_as_exit_124(self):
        result = command_probe.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)


class ChromeAccountTests(SafeOutputMixin, TemporaryRootMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_root()
        self.enterContext(patch.object(auth, "CHROME_ROOT", self.root))
        self.spec = AppSpec(name="Chrome")

    def write(self, relative: str, data: dict) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_missing_profiles_ask_to_open_chrome(self):
        [result] = auth.check_chrome_account(self.spec)
        self.assertEqual((result.status, result.section), ("warn", "stat"))

    def test_signed_in_and_synced_profiles_are_counted_without_the_account(self):
        self.write("Local State", {"profile": {"info_cache": {"Profile 1": {"user_name": EMAIL, "gaia_id": "123"}}}})
        self.write("Profile 1/Preferences", {"account_info": [{"email": EMAIL}], "sync": {"has_setup_completed": True}})
        self.write("Guest Profile/Preferences", {"account_info": [{"email": EMAIL}]})
        results = auth.check_chrome_account(self.spec)
        self.assertEqual([result.status for result in results], ["ok", "ok"])
        self.assertIn("1 profile(s)", results[0].message)
        self.assertNoSensitiveValues(results)

    def test_disabled_signin_and_missing_sync_are_distinguished(self):
        self.write("Default/Preferences", {"signin": {"allowed": False}})
        [disabled] = auth.check_chrome_account(self.spec)
        self.assertIn("sign-in appears disabled", disabled.message)
        self.write("Default/Preferences", {"sync": {"gaia_id": "123"}})
        self.assertEqual([result.status for result in auth.check_chrome_account(self.spec)], ["ok", "info"])


class CodexAuthTests(SafeOutputMixin, TemporaryRootMixin, unittest.TestCase):
    def setUp(self):
        self.auth_file = self.make_root() / "auth.json"
        self.enterContext(patch.object(auth, "CODEX_AUTH", self.auth_file))
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("OPENAI_API_KEY", None)
        self.spec = AppSpec(name="Codex")

    def test_auth_file_markers_are_detected_without_printing_tokens(self):
        cases = [
            (None, "warn", "is missing"),
            ({"tokens": {"access_token": " "}}, "warn", "no known auth marker"),
            ({"tokens": {"refresh_token": TOKEN}}, "ok", "marker is present"),
            ({"OPENAI_API_KEY": API_KEY}, "ok", "marker is present"),
        ]
        for data, status, message in cases:
            with self.subTest(data=data):
                if data is None:
                    self.auth_file.unlink(missing_ok=True)
                else:
                    self.auth_file.write_text(json.dumps(data))
                [result] = auth.check_codex_auth(self.spec)
                self.assertEqual(result.status, status)
                self.assertIn(message, result.message)
                self.assertNoSensitiveValues([result])

    def test_environment_key_is_reported_by_name_only(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": API_KEY}):
            [result] = auth.check_codex_auth(self.spec)
        self.assertEqual(result.status, "ok")
        self.assertNoSensitiveValues([result])


class CommandAuthTests(SafeOutputMixin, unittest.TestCase):
    spec = AppSpec(name="Tool")

    def check(self, checker, *outputs, exists=True):
        with patch.object(auth.command, "exists", return_value=exists), \
                patch.object(auth.command, "run", side_effect=list(outputs)) as run:
            results = checker(self.spec)
        self.assertNoSensitiveValues(results)
        return results, run

    def test_github_cli_reports_only_the_authentication_state(self):
        cases = ((0, "ok", "available"), (1, "warn", "not authenticated"), (124, "warn", "timed out"))
        for returncode, status, message in cases:
            with self.subTest(returncode=returncode):
                output = completed(returncode, stdout=f"Logged in as {EMAIL}", stderr=f"Token: {TOKEN}")
                [result], _ = self.check(auth.check_github_cli_auth, output)
                self.assertEqual(result.status, status)
                self.assertIn(message, result.message)
        results, run = self.check(auth.check_github_cli_auth, exists=False)
        self.assertEqual(results, [])
        run.assert_not_called()

    def test_tailscale_states_are_mapped_to_actionable_results(self):
        cases = {
            "running": (completed(stdout=json.dumps({"BackendState": "Running", "Self": {"DNSName": EMAIL}})),
                        "ok", "authenticated"),
            "needs login": (completed(stdout='{"BackendState": "NeedsLogin"}'), "warn", "not authenticated"),
            "no identity": (completed(stdout='{"BackendState": "Running"}'), "warn", "identity is missing"),
            "stopped": (completed(stdout='{"BackendState": "Stopped"}'), "warn", "Stopped"),
            "invalid json": (completed(stdout="not json"), "warn", "invalid JSON"),
            "failed": (completed(1, stderr=EMAIL), "warn", "could not read"),
            "timed out": (completed(124), "warn", "timed out"),
        }
        for name, (output, status, message) in cases.items():
            with self.subTest(name):
                [result], _ = self.check(auth.check_tailscale_auth, output)
                self.assertEqual(result.status, status)
                self.assertIn(message, result.message)
        [missing], _ = self.check(auth.check_tailscale_auth, exists=False)
        self.assertEqual((missing.status, missing.section), ("warn", "apps"))

    def test_lark_cli_checks_configuration_before_identity(self):
        configured = completed(stdout=json.dumps({"appId": APP_ID, "owner": EMAIL}))
        not_configured = json.dumps({"error": {"type": "config", "subtype": "not_configured"}})
        cases = {
            "not configured": ([completed(1, stderr=not_configured)], ["warn"], "is not configured"),
            "unreadable config": ([completed(1, stderr="boom")], ["warn"], "could not read"),
            "config timeout": ([completed(124)], ["warn"], "timed out"),
            "incomplete config": ([completed(stdout='{"appId": ""}')], ["warn"], "missing or incomplete"),
            "user identity": ([configured, completed(stdout='{"identity": "user"}')], ["ok", "ok"], "usable user"),
            "no identity": ([configured, completed(stdout='{"identity": "none"}')], ["ok", "warn"], "no usable"),
            "auth timeout": ([configured, completed(124)], ["ok", "warn"], "timed out"),
            "auth needs config": ([configured, completed(1, stdout=not_configured)], ["ok", "warn"], "before lark-cli"),
        }
        for name, (outputs, statuses, message) in cases.items():
            with self.subTest(name):
                results, _ = self.check(auth.check_lark_cli_auth, *outputs)
                self.assertEqual([result.status for result in results], statuses)
                self.assertIn(message, results[-1].message)
        self.assertEqual(self.check(auth.check_lark_cli_auth, exists=False)[0], [])


class VscodeCheckTests(TemporaryRootMixin, unittest.TestCase):
    spec = AppSpec(name="VS Code")

    def mode(self, value):
        return patch.object(vscode, "read_machine_nix", return_value={"envy.vscode.mode": value})

    def sync_results(self, keys, *, database=True):
        with self.mode("remote"), \
                patch.object(vscode_probe, "state_db_exists", return_value=database), \
                patch.object(vscode_probe, "state_keys", return_value=set(keys)):
            return [(result.name, result.status) for result in vscode.check_sync(self.spec)]

    def test_remote_mode_checks_sync_account_and_copilot_markers(self):
        self.assertEqual(self.sync_results([], database=False), [
            ("VS Code mode", "info"), ("VS Code settings sync", "warn"),
        ])
        self.assertEqual(self.sync_results([]), [
            ("VS Code mode", "info"), ("VS Code settings sync", "warn"),
            ("VS Code account", "warn"), ("VS Code copilot", "info"),
        ])
        keys = ["sync.enable", "userDataSyncAccountProvider", "github-session", "GitHub.copilot-chat"]
        self.assertEqual([status for _, status in self.sync_results(keys)], ["info", "ok", "ok", "ok"])

    def test_local_and_unknown_modes(self):
        with self.mode("local"):
            self.assertEqual([r.status for r in vscode.check_sync(self.spec)], ["info", "ok"])
        with self.mode("cloud"):
            self.assertEqual([r.status for r in vscode.check_sync(self.spec)], ["info", "warn"])

    def test_extensions_are_compared_only_in_local_mode(self):
        with self.mode("remote"):
            self.assertEqual(vscode.check_extensions(self.spec), [])

        def statuses(command, installed=()):
            with self.mode("local"), \
                    patch.object(vscode_probe, "code_command", return_value=command), \
                    patch.object(vscode_probe, "installed_extensions", return_value=set(installed)):
                return [(result.status, result.hint) for result in vscode.check_extensions(self.spec)]

        self.assertEqual([status for status, _ in statuses(None)], ["ok", "warn"])
        self.assertEqual([status for status, _ in statuses("code")], ["ok", "info"])
        partial = statuses("code", EXPECTED_EXTENSIONS[1:])
        self.assertEqual([status for status, _ in partial], ["ok", "warn"])
        self.assertIn(EXPECTED_EXTENSIONS[0], partial[1][1])
        self.assertEqual([status for status, _ in statuses("code", EXPECTED_EXTENSIONS)], ["ok", "ok"])

    def test_state_keys_are_read_without_failing_on_a_broken_database(self):
        database = self.make_root() / "state.vscdb"
        connection = sqlite3.connect(database)
        with connection:
            connection.execute("create table ItemTable (key text, value blob)")
            connection.execute("insert into ItemTable values ('sync.enable', 'true')")
        connection.close()
        with patch.object(vscode_probe, "STATE_DB", database):
            self.assertEqual(vscode_probe.state_keys(), {"sync.enable"})
            database.write_bytes(b"not a database")
            self.assertEqual(vscode_probe.state_keys(), set())


if __name__ == "__main__":
    unittest.main()

"""Runtime transactions must preserve application writes detected by guarded merges."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from envy import agent, log, structured_config
from envy.transaction import FileTransaction


class FileTransactionPreserveTests(unittest.TestCase):
    def test_preserve_current_is_explicit_and_does_not_suppress_errors(self):
        cases = [
            (False, False, False, False),
            (False, True, False, True),
            (False, False, True, False),
            (False, True, True, False),
            (True, False, False, True),
            (True, False, True, True),
            (True, True, False, True),
            (True, True, True, True),
        ]
        for preserve, commit, fail, retained in cases:
            with self.subTest(preserve=preserve, commit=commit, fail=fail), tempfile.TemporaryDirectory() as directory:
                existing = Path(directory) / "existing"
                created = Path(directory) / "created"
                existing.write_text("before")
                existing.chmod(0o640)

                def run():
                    with FileTransaction([existing, created]) as transaction:
                        existing.write_text("after")
                        existing.chmod(0o600)
                        created.write_text("new")
                        if preserve:
                            transaction.preserve_current()
                        if commit:
                            transaction.commit()
                        if fail:
                            raise RuntimeError("stop")

                if fail:
                    with self.assertRaisesRegex(RuntimeError, "stop"):
                        run()
                else:
                    run()
                self.assertEqual(existing.read_text(), "after" if retained else "before")
                self.assertEqual(existing.stat().st_mode & 0o777, 0o600 if retained else 0o640)
                self.assertEqual(created.exists(), retained)


class AgentRefreshConflictTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log.reset_consoles)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = self.home / "runtime"
        self.pi = self.home / ".pi" / "agent" / "models.json"
        self.codex = self.home / ".codex" / "config.toml"
        self.catalog = self.home / ".codex" / agent.CATALOG_NAME
        self.auth = self.home / ".codex" / "auth.json"
        self.machine = self.home / "machine.nix"
        self.machine.write_text("{ ... }: {\n}\n")
        self.secrets_file = self.home / "secrets.yaml"
        self.secrets_file.write_text("synthetic ciphertext\n")
        self.enterContext(patch.object(agent, "SECRETS_FILE", self.secrets_file))

        def set_secret(path, value):
            self.assertEqual(path, "llm/test/key")
            self.assertEqual(value, self.secret)
            self.secrets_file.write_text("updated synthetic ciphertext\n")
            log.ok("secrets", "secret value updated", key=path)

        self.enterContext(patch.object(agent, "set_secret_value", side_effect=set_secret))
        self.values = {
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.model": "synthetic-pi",
            "envy.agents.pi.endpoint": "test",
            "envy.agents.pi.credential": "test",
            "envy.agents.codex.adapter": "codex",
            "envy.agents.codex.models": [{"profile": "test"}],
            "envy.agentProfiles.test.endpoint": "test",
            "envy.agentProfiles.test.credential": "test",
            "envy.agentProfiles.test.api": "openai-responses",
            "envy.agentProfiles.test.model": "synthetic-codex",
            "envy.llm.endpoints.test.baseUrl": "https://example.invalid/v1",
            "envy.llm.endpoints.test.protocol": "openai-compatible",
            "envy.llm.credentials.test.secretPath": "llm/test/key",
        }
        self.catalog_data = {"models": [{"slug": "synthetic-codex"}]}
        self.secret = "synthetic-secret-never-print"
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        self.enterContext(patch.object(agent, "_RUNTIME_DIR", self.runtime))
        self.enterContext(patch.object(agent, "_read_values", return_value=self.values))
        self.enterContext(patch.object(agent, "machine_config_file", return_value=self.machine))
        self.enterContext(patch.object(agent, "current_machine_id", return_value="synthetic"))
        self.enterContext(patch.object(agent, "read_secrets_data", return_value=({
            "llm": {"test": {"key": self.secret}},
        }, True)))
        self.enterContext(patch.object(agent, "model_catalog", return_value=self.catalog_data))
        self.commit = self.enterContext(patch.object(agent, "offer_mutation_commit"))

    @contextmanager
    def during_pre_replace(self, target, mutate):
        """Run a real competing write after temporary fsync, before the real guard."""
        original = structured_config.atomic_write_text
        injections = []

        def write(path, text, **kwargs):
            if path == target:
                guard = kwargs["pre_replace"]

                def pre_replace():
                    injections.append(path)
                    mutate()
                    guard()

                kwargs["pre_replace"] = pre_replace
            return original(path, text, **kwargs)

        with patch.object(structured_config, "atomic_write_text", side_effect=write):
            yield injections
        self.assertEqual(injections, [target])
        self.assertEqual(list(target.parent.glob(f".{target.name}.envy-*")), [])

    def assert_partial_error(self, exc):
        self.assertIsInstance(exc.__cause__, structured_config.ConflictError)
        message = str(exc)
        self.assertIn("changed concurrently", message)
        self.assertIn("Close the application and retry", message)
        self.assertIn("without rollback", message)
        self.assertIn("Earlier writes may remain (partial refresh)", message)
        self.assertNotIn(self.secret, message)
        self.assertNotIn("competitor-private-value", message)

    def test_existing_app_edits_survive_real_refresh_conflicts(self):
        for target, original, competitor in (
            (self.pi, '{"before": true}', b'{"app": "competitor-private-value"}\n'),
            (self.codex, 'model = "before"\n', b'model = "competitor-private-value"\n'),
        ):
            with self.subTest(target=target.name):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original)
                with self.during_pre_replace(target, lambda: target.write_bytes(competitor)):
                    with self.assertRaises(agent.RuntimeRefreshConflict) as caught:
                        agent.refresh_runtime()
                self.assert_partial_error(caught.exception)
                self.assertEqual(target.read_bytes(), competitor)

    def test_app_files_created_during_refresh_are_not_deleted(self):
        for target, competitor in (
            (self.pi, b'{"app": "competitor-private-value"}\n'),
            (self.codex, b'model = "competitor-private-value"\n'),
        ):
            with self.subTest(target=target.name):
                target.unlink(missing_ok=True)
                with self.during_pre_replace(target, lambda: target.write_bytes(competitor)):
                    with self.assertRaises(agent.RuntimeRefreshConflict) as caught:
                        agent.refresh_runtime()
                self.assert_partial_error(caught.exception)
                self.assertTrue(target.exists(), "the application's new file must survive")
                self.assertEqual(target.read_bytes(), competitor)

    def test_later_conflict_preserves_completed_writes_and_subsequent_app_edits(self):
        for app_edits_pi in (False, True):
            with self.subTest(app_edits_pi=app_edits_pi):
                for target, text in (
                    (self.pi, '{"before": true}'),
                    (self.codex, 'model = "before"\n'),
                    (self.catalog, '{"models": ["before"]}'),
                    (self.auth, '{"app_auth": "before"}'),
                    (self.runtime / "pi.env", "before pi\n"),
                    (self.runtime / "codex.env", "before codex\n"),
                ):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                expected_pi = {}

                def compete():
                    # Both earlier targets have already been written by this refresh.
                    current = json.loads(self.pi.read_text())
                    self.assertIn("envy-pi", current["providers"])
                    self.assertEqual(json.loads(self.catalog.read_text()), self.catalog_data)
                    if app_edits_pi:
                        current["app"] = "competitor-private-value"
                        self.pi.write_text(json.dumps(current))
                    expected_pi["bytes"] = self.pi.read_bytes()
                    self.codex.write_text('model = "competitor-private-value"\n')

                with self.during_pre_replace(self.codex, compete):
                    with self.assertRaises(agent.RuntimeRefreshConflict) as caught:
                        agent.refresh_runtime()
                self.assert_partial_error(caught.exception)
                self.assertEqual(self.pi.read_bytes(), expected_pi["bytes"])
                self.assertEqual(json.loads(self.catalog.read_text()), self.catalog_data)
                self.assertEqual(self.codex.read_text(), 'model = "competitor-private-value"\n')
                self.assertEqual(self.auth.read_text(), '{"app_auth": "before"}')
                self.assertEqual((self.runtime / "pi.env").read_text(), "before pi\n")
                self.assertEqual((self.runtime / "codex.env").read_text(), "before codex\n")

    def test_nonconflict_io_failure_still_rolls_back_completed_runtime_writes(self):
        self.pi.parent.mkdir(parents=True)
        self.pi.write_text('{"before": true}')
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text('model = "before"\n')
        before = {path: path.read_bytes() for path in (self.pi, self.codex)}
        original = structured_config.atomic_write_text

        def fail_codex(path, text, **kwargs):
            if path == self.codex:
                raise OSError("synthetic IO failure")
            return original(path, text, **kwargs)

        with patch.object(structured_config, "atomic_write_text", side_effect=fail_codex):
            with self.assertRaisesRegex(OSError, "synthetic IO failure"):
                agent.refresh_runtime()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.auth.exists())
        self.assertFalse((self.runtime / "pi.env").exists())

    def test_cli_conflicts_emit_one_failure_envelope_and_never_report_success(self):
        for args, command in (
            (["refresh"], "agent.refresh"),
            (["set", "envy.agents.pi.model", "replacement", "--yes"], "agent.set"),
            (["profile", "use", "pi", "test", "--yes"], "agent.profile.use"),
            (["credential-set", "test", "--secret-path", "llm/test/key", "--stdin"], "agent.credential-set"),
        ):
            for structured in (False, True):
                with self.subTest(command=command, structured=structured):
                    agent._write_values(self.values)
                    self.pi.parent.mkdir(parents=True, exist_ok=True)
                    self.pi.write_text('{"before": true}')
                    competitor = b'{"app": "competitor-private-value"}\n'
                    with self.during_pre_replace(self.pi, lambda: self.pi.write_bytes(competitor)):
                        result = CliRunner().invoke(
                            agent.app, args + (["--json"] if structured else []), input=self.secret + "\n",
                        )
                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertEqual(self.pi.read_bytes(), competitor)
                    text = result.stdout
                    if structured:
                        payload = json.loads(text)
                        self.assertEqual(payload["schemaVersion"], 1)
                        self.assertEqual(payload["command"], command)
                        self.assertFalse(payload["ok"])
                        self.assertIsNone(payload["data"])
                        self.assertEqual(payload["warnings"], [])
                        self.assertEqual(payload["error"]["code"], "conflict")
                        text = payload["error"]["message"]
                    text = " ".join(text.split())
                    self.assertIn("changed concurrently", text)
                    self.assertIn("partial refresh", text)
                    self.assertNotIn("runtime configuration refreshed", result.output)
                    self.assertNotIn(self.secret, result.output)
                    self.assertNotIn("competitor-private-value", result.output)
                    if command == "agent.credential-set":
                        self.assertEqual(self.secrets_file.read_text(), "updated synthetic ciphertext\n")
                    self.commit.assert_not_called()

    def test_credential_set_success_keeps_json_stdout_clean(self):
        agent._write_values(self.values)
        result = CliRunner().invoke(agent.app, [
            "credential-set", "test", "--secret-path", "llm/test/key", "--stdin", "--json",
        ], input=self.secret + "\n")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "agent.credential-set")
        self.assertTrue(payload["data"]["runtimeRefreshed"])
        self.assertIn("secret value updated", result.stderr)
        self.assertNotIn(self.secret, result.output)
        self.assertEqual(json.loads(self.auth.read_text())["OPENAI_API_KEY"], self.secret)


if __name__ == "__main__":
    unittest.main()

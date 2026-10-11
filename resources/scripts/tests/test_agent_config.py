import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import typer

from envy import config
from envy.agent import _read_values, _render, _write_values, refine_managed


class AgentConfigTests(unittest.TestCase):
    def test_config_api_edit_is_validated_and_survives_refine(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values({"envy.agents.omp.adapter": "oh-my-pi"})
                config.set_config_value("envy.agents.omp.api", "openai-responses", quiet=True)
                result = refine_managed(machine_values={}, write=False)
                self.assertEqual(result["errors"], [])
                self.assertEqual(_read_values(path)["envy.agents.omp.api"], "openai-responses")
                before = path.read_text()
                for invalid in ("responses", "", "openai-codex-responses"):
                    with self.subTest(api=invalid), self.assertRaises(typer.BadParameter):
                        config.set_config_value("envy.agents.omp.api", invalid, quiet=True)
                    self.assertEqual(path.read_text(), before)

    def test_render_round_trips_scalars_and_lists(self):
        values = {
            "envy.llm.endpoints.stepfun-openai.baseUrl": "https://example.test/v1",
            "envy.llm.endpoints.stepfun-openai.protocol": "openai-compatible",
            "envy.agents.codex.adapter": "codex",
            "envy.agents.codex.model": "step-model",
            "envy.agents.codex.aliases": ["cx", "codex"],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ...\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values(values)
                self.assertEqual(_read_values(path), values)

    def test_render_keeps_agent_block_separate(self):
        rendered = _render({"envy.agents.dsh.adapter": "openai-compatible-cli"})
        self.assertIn("BEGIN ENVY MANAGED AGENTS", rendered)
        self.assertIn('envy.agents.dsh.adapter = "openai-compatible-cli";', rendered)

    def test_refine_migrates_legacy_llm_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text(
                '{\n'
                '  envy.llm.steps.url = "https://api.stepfun.com";\n'
                '  envy.llm.steps.model = "claude-opus-4-6";\n'
                '  envy.llm.deepseek.url = "https://api.deepseek.com";\n'
                '  envy.llm.deepseek.model = "deepseek-chat";\n'
                '}\n'
            )
            with patch("envy.agent.machine_config_file", return_value=path):
                result = refine_managed(machine_values={
                    "envy.llm.steps.url": "https://api.stepfun.com",
                    "envy.llm.steps.model": "claude-opus-4-6",
                    "envy.llm.deepseek.url": "https://api.deepseek.com",
                    "envy.llm.deepseek.model": "deepseek-chat",
                }, write=True)
                values = _read_values(path)
            self.assertTrue(result["changed"])
            self.assertEqual(values["envy.agents.cc.model"], "claude-opus-4-6")
            self.assertEqual(values["envy.llm.credentials.stepfun.secretPath"], "llm/steps/apikey")
            self.assertEqual(values["envy.llm.endpoints.stepfun-openai.baseUrl"], "https://api.stepfun.com/v1")

    def test_refine_is_idempotent_and_rejects_unknown_references(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ...\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                first = refine_managed(machine_values={}, write=True)
                second = refine_managed(machine_values={}, write=True)
                self.assertTrue(first["changed"])
                self.assertFalse(second["changed"])
                values = _read_values(path)
                values["envy.agents.dsh.endpoint"] = "missing-endpoint"
                _write_values(values)
                invalid = refine_managed(machine_values={}, write=False)
            self.assertIn("envy.agents.dsh.endpoint", invalid["errors"])

    def test_refine_rejects_incomplete_declared_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text(
                '{\n\n'
                '  # BEGIN ENVY MANAGED AGENTS\n'
                '  envy.agents.dsh.model = "demo";\n'
                '  envy.llm.credentials.custom.kind = "token";\n'
                '  # END ENVY MANAGED AGENTS\n\n}\n'
            )
            with patch("envy.agent.machine_config_file", return_value=path):
                result = refine_managed(machine_values={}, write=False)
            self.assertIn("envy.agents.dsh.adapter", result["errors"])
            self.assertIn("envy.llm.credentials.custom.secretPath", result["errors"])

    @contextmanager
    def migration_fixture(self, *, managed_config=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            path = Path(directory) / "machine.nix"
            values = {field.path: field.default_fn() for field in config.MACHINE_FIELDS}
            values["envy.git.email"] = "test@example.com"
            body = config._render_machine_block(values) if managed_config else ""
            legacy = (
                '  envy.llm.steps.url = "https://legacy.example";\n'
                '  envy.llm.steps.model = "legacy-model";\n'
                '  envy.llm.deepseek.url = "https://deepseek.example";\n'
                '  envy.llm.deepseek.model = "deepseek-model";\n'
            )
            path.write_text(
                '{ ... }: {\n' + body + '\n' + legacy
                + '  envy.software.nix.packages.exclude = [ "keep-me" ];\n}\n'
            )
            for target in ("envy.config.machine_config_file", "envy.agent.machine_config_file"):
                patches.enter_context(patch(target, return_value=path))
            patches.enter_context(patch.object(config, "read_legacy_config_nix", return_value={}))
            patches.enter_context(patch.object(config, "set_device_machine_id"))
            patches.enter_context(patch.object(config, "refine_device_metadata", return_value=config.RefineReport()))
            patches.enter_context(patch.object(config, "refine_sensitive_permissions", return_value=config.RefineReport()))
            patches.enter_context(patch.object(config, "refine_software_policy", return_value=config.RefineReport()))
            yield path

    def test_refine_all_converts_then_removes_legacy_assignments(self):
        for managed in (False, True):
            with self.subTest(managed=managed), self.migration_fixture(managed_config=managed) as path:
                report = config.refine_all(write=True, include_secrets=False)
                migrated = _read_values(path)
                self.assertTrue(report.ok)
                self.assertTrue(report.changed)
                self.assertEqual(migrated["envy.agents.cc.model"], "legacy-model")
                self.assertEqual(migrated["envy.llm.endpoints.deepseek-openai.baseUrl"], "https://deepseek.example")
                for legacy in config.LEGACY_MACHINE_KEYS:
                    self.assertIn(legacy, report.removed)
                    self.assertNotIn(legacy, path.read_text())
                self.assertIn('envy.software.nix.packages.exclude = [ "keep-me" ];', path.read_text())
                text = path.read_text()
                again = config.refine_all(write=True, include_secrets=False)
                self.assertTrue(again.ok)
                self.assertFalse(again.changed)
                self.assertEqual(path.read_text(), text)

    def test_refine_all_preserves_existing_agent_choices(self):
        with self.migration_fixture(managed_config=True) as path:
            current = {
                "envy.llm.endpoints.custom.baseUrl": "https://custom.example/v1",
                "envy.llm.credentials.custom.secretPath": "llm/custom/apikey",
                "envy.agents.codex.adapter": "codex",
                "envy.agents.codex.endpoint": "custom",
                "envy.agents.codex.credential": "custom",
                "envy.agents.codex.model": "gpt-6.1-sol",
            }
            _write_values(current)
            report = config.refine_all(write=True, include_secrets=False)
            self.assertTrue(report.ok)
            migrated = _read_values(path)
            self.assertEqual({key: migrated[key] for key in current}, current)
            self.assertEqual(migrated["envy.llm.endpoints.stepfun-anthropic.baseUrl"], "https://legacy.example")
            self.assertEqual(migrated["envy.agents.cc.model"], "legacy-model")
            self.assertNotIn("envy.llm.steps.url", path.read_text())

    def test_failed_agent_migration_keeps_legacy_source(self):
        with self.migration_fixture(managed_config=True) as path:
            _write_values({"envy.agents.codex.endpoint": "unknown"})
            before = path.read_text()
            report = config.refine_all(write=True, include_secrets=False)
            self.assertFalse(report.ok)
            self.assertEqual(path.read_text(), before)

    def test_invalid_legacy_url_does_not_create_block_or_delete_source(self):
        with self.migration_fixture() as path:
            path.write_text(path.read_text().replace("https://legacy.example", "invalid-url"))
            before = path.read_text()
            report = config.refine_all(write=True, include_secrets=False)
            self.assertFalse(report.ok)
            self.assertEqual(path.read_text(), before)

    def test_scalar_edit_migrates_llm_before_rewriting_old_config_block(self):
        with self.migration_fixture(managed_config=True) as path:
            config.set_config_value("envy.vscode.mode", "local")
            self.assertEqual(_read_values(path)["envy.agents.codex.model"], "legacy-model")
            self.assertNotIn("envy.llm.steps.url", path.read_text())
            self.assertIn('envy.vscode.mode = "local";', path.read_text())

    def test_check_flags_legacy_cleanup_and_does_not_write(self):
        with self.migration_fixture(managed_config=True) as path:
            refine_managed(machine_values=config.read_machine_nix(), write=True)
            before = path.read_text()
            report = config.refine_all(write=False, include_secrets=False)
            self.assertFalse(report.ok)
            self.assertIn("envy.llm.steps.url", report.errors)
            self.assertEqual(path.read_text(), before)


if __name__ == "__main__":
    unittest.main()

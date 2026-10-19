import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from envy import agent, config
from envy.agent import (
    _read_values, _render, _runtime_change_kind, _runtime_env, _set, _write_values, refine_managed,
)


class AgentConfigTests(unittest.TestCase):
    def test_runtime_change_kind_separates_live_and_apply_fields(self):
        self.assertEqual(_runtime_change_kind("envy.agents.pi.model"), "live")
        self.assertEqual(_runtime_change_kind("envy.llm.endpoints.stepfun.baseUrl"), "live")
        self.assertEqual(_runtime_change_kind("envy.agents.pi.moreThinking"), "apply")
        self.assertEqual(_runtime_change_kind("envy.agents.pi.skills"), "apply")

    def test_runtime_env_contains_current_endpoint_model_and_secret(self):
        values = {
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.endpoint": "stepfun",
            "envy.agents.pi.credential": "stepfun",
            "envy.agents.pi.model": "model/with space",
            "envy.llm.endpoints.stepfun.baseUrl": "https://example.test/v1",
            "envy.llm.endpoints.stepfun.protocol": "openai-compatible",
            "envy.llm.credentials.stepfun.secretPath": "llm/steps/apikey",
        }
        rendered = _runtime_env(values, {
            "adapter": "pi", "endpoint": "stepfun", "credential": "stepfun", "model": "model/with space",
        }, {"llm": {"steps": {"apikey": "secret-value"}}})
        self.assertIn("export OPENAI_BASE_URL=https://example.test/v1", rendered)
        self.assertIn("export OPENAI_API_KEY=secret-value", rendered)
        self.assertIn("export ENVY_AGENT_MODEL='model/with space'", rendered)

    def test_multiline_settings_survive_shared_reader_and_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text(
                '{ ... }: {\n' + agent.MANAGED_START + '\n'
                '  envy.agents.pi.adapter = "pi";\n'
                '  envy.agents.pi.additionalSettings = {\n'
                '    # braces in comments must not affect parsing: }\n'
                '    compaction = { keepRecentTokens = 27200; reserveTokens = 27200; };\n'
                '    defaultTools = [ "+codemode" "second" ];\n'
                '    label = "a;{b}";\n'
                '    enabled = true;\n'
                '  };\n' + agent.MANAGED_END + '\n}\n'
            )
            before = path.read_bytes()
            expected = {
                "compaction": {"keepRecentTokens": 27200, "reserveTokens": 27200},
                "defaultTools": ["+codemode", "second"],
                "label": "a;{b}", "enabled": True,
            }
            with patch.object(agent, "machine_config_file", return_value=path):
                for write in (False, True):
                    self.assertEqual(refine_managed(machine_values={}, write=write)["errors"], [])
                    self.assertEqual(path.read_bytes(), before)
                self.assertEqual(_read_values(path)["envy.agents.pi.additionalSettings"], expected)
                _set("envy.agents.pi.model", "test")
                self.assertEqual(_read_values(path)["envy.agents.pi.additionalSettings"], expected)

    def test_parse_errors_visible_without_strict_mode_and_never_print_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text(
                '{ ... }: {\n' + agent.MANAGED_START + '\n'
                '  envy.agents.pi.additionalSettings = { x = unsupported-secret-marker; };\n'
                + agent.MANAGED_END + '\n}\n'
            )
            before = path.read_bytes()
            for write in (False, True):
                with patch.object(config, "machine_config_file", return_value=path), patch.object(
                    agent, "log"
                ) as logger:
                    report = config.refine_agents(write=write, strict=False)
                    self.assertFalse(report.ok)
                    diagnostic = str(logger.error.call_args_list)
                    self.assertIn("envy.agents.pi.additionalSettings", diagnostic)
                    self.assertIn("line", diagnostic)
                    self.assertNotIn("unsupported-secret-marker", diagnostic)
                    self.assertEqual(path.read_bytes(), before)

    def test_config_entries_share_strict_validation(self):
        for command, write in ((config.cmd_check, False), (config.cmd_doctor, False), (config.cmd_refine, True)):
            with patch.object(config, "refine_all", return_value=config.RefineReport()) as refine:
                if write:
                    command(prune=False)
                else:
                    command()
                self.assertEqual(refine.call_args.kwargs["write"], write)
                self.assertTrue(refine.call_args.kwargs["strict"])
                self.assertTrue(refine.call_args.kwargs["include_secrets"])

    def test_list_settings_use_valid_nix_lists_through_both_commands(self):
        from envy.main import cli

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            outside = '  envy.software.nix.packages.exclude = [ "keep-me" ];\n'
            path.write_text("{ ... }: {\n" + outside + "}\n")
            with patch.object(agent, "machine_config_file", return_value=path), patch.object(
                agent, "offer_mutation_commit"
            ), patch.object(config, "machine_config_file", return_value=path), patch.object(
                config, "offer_mutation_commit"
            ):
                _write_values({"envy.agents.pi.adapter": "pi"})
                for command in ("agent", "config"):
                    for field, items in (
                        ("skills", ["animate", "break-ui"]),
                        ("aliases", ["pcli", "pi-cli"]),
                        ("args", ['a "quoted" argument', r"C:\work\file"]),
                        ("skills", []),
                    ):
                        key = f"envy.agents.pi.{field}"
                        with self.subTest(command=command, field=field, items=items):
                            result = CliRunner().invoke(cli, [command, "set", key, json.dumps(items), "--yes"])
                            self.assertEqual(result.exit_code, 0, result.output)
                            self.assertEqual(_read_values(path)[key], items)
                            self.assertIn(outside, path.read_text())
                            self.assertNotIn('", "', agent._render_value(items))

    def test_invalid_list_settings_and_unknown_fields_leave_policy_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch.object(agent, "machine_config_file", return_value=path):
                _write_values({"envy.agents.pi.adapter": "pi"})
                before = path.read_bytes()
                for field in ("skills", "args", "aliases"):
                    for value in ('"animate"', '["animate", 1]', '{}', 'null', '[', 'animate'):
                        with self.subTest(field=field, value=value), self.assertRaises(typer.BadParameter):
                            _set(f"envy.agents.pi.{field}", value)
                        self.assertEqual(path.read_bytes(), before)
                with self.assertRaises(typer.BadParameter):
                    _set("envy.agents.pi.typo", "unused")
                self.assertEqual(path.read_bytes(), before)
                _set("envy.agents.pi.model", "null")
                self.assertIsNone(_read_values(path)["envy.agents.pi.model"])

    @contextmanager
    def obsolete_agent_fixture(self):
        retained = {
            "envy.llm.endpoints.shared.baseUrl": "https://example.test/v1",
            "envy.llm.credentials.shared.secretPath": "llm/shared/apikey",
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.endpoint": "shared",
            "envy.agents.pi.credential": "shared",
            "envy.agents.omp2.adapter": "codex",
        }
        obsolete = {
            "envy.agents.omp.adapter": "oh-my-pi",
            "envy.agents.omp.endpoint": "missing-old-endpoint",
            "envy.agents.omp.credential": "shared",
            "envy.agents.omp.oldOption": "unused",
            "envy.agents.renamed.adapter": "oh-my-pi",
            "envy.agents.renamed.model": "old-model",
        }
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "machine.nix"
            outside = '  # hand-maintained policy\n  envy.agents.manual.adapter = "oh-my-pi";\n'
            path.write_text("{ ... }: {\n" + outside + "\n" + _render(retained | obsolete) + "\n}\n")
            stack.enter_context(patch.object(agent, "machine_config_file", return_value=path))
            stack.enter_context(patch.object(config, "machine_config_file", return_value=path))
            stack.enter_context(patch.object(agent, "read_secrets_data", side_effect=AssertionError("no secret reads")))
            stack.enter_context(patch.object(agent, "offer_mutation_commit"))
            yield path, retained, obsolete, outside

    def test_refine_reports_then_removes_only_obsolete_managed_agent_fields(self):
        with self.obsolete_agent_fixture() as (path, retained, obsolete, outside):
            before = path.read_bytes()
            checked = config.refine_agents(write=False)
            self.assertFalse(checked.ok)
            self.assertFalse(checked.changed)
            self.assertEqual(checked.removed, sorted(obsolete))
            self.assertEqual(checked.errors, sorted(obsolete))
            self.assertEqual(path.read_bytes(), before)

            refined = config.refine_agents(write=True)
            self.assertTrue(refined.ok)
            self.assertTrue(refined.changed)
            self.assertEqual(refined.removed, sorted(obsolete))
            self.assertEqual(_read_values(path), retained)
            self.assertIn(outside, path.read_text())
            once = path.read_bytes()
            again = config.refine_agents(write=True)
            self.assertTrue(again.ok)
            self.assertFalse(again.changed)
            self.assertEqual(again.removed, [])
            self.assertEqual(path.read_bytes(), once)

    def test_obsolete_cleanup_does_not_write_when_live_config_is_invalid(self):
        with self.obsolete_agent_fixture() as (path, retained, obsolete, _):
            _write_values(retained | obsolete | {"envy.agents.pi.endpoint": "missing-live-endpoint"})
            before = path.read_bytes()
            report = config.refine_agents(write=True)
            self.assertIn("envy.agents.pi.endpoint", report.errors)
            self.assertFalse(report.changed)
            self.assertEqual(path.read_bytes(), before)

    def test_obsolete_cleanup_does_not_rewrite_malformed_blocks(self):
        with self.obsolete_agent_fixture() as (path, _, _, _):
            path.write_text(path.read_text().replace(agent.MANAGED_END, '  envy.agents.omp.args = [\n' + agent.MANAGED_END))
            before = path.read_bytes()
            report = config.refine_agents(write=True)
            self.assertTrue(report.errors)
            self.assertFalse(report.changed)
            self.assertEqual(path.read_bytes(), before)

    def test_retired_omp_cannot_be_added_or_edited_through_either_cli(self):
        from envy.main import cli

        commands = [
            ["agent", "add", "omp", "--adapter", "pi"],
            ["agent", "add", "custom", "--adapter", "oh-my-pi"],
        ]
        for command in ("agent", "config"):
            commands.extend([
                [command, "set", "envy.agents.omp.model", "new-model"],
                [command, "set", "envy.agents.renamed.model", "new-model"],
                [command, "set", "envy.agents.pi.adapter", "oh-my-pi"],
            ])
        with self.obsolete_agent_fixture() as (path, _, _, _):
            before = path.read_bytes()
            for args in commands:
                with self.subTest(args=args):
                    result = CliRunner().invoke(cli, args + ["--yes"])
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn("OMP is retired", result.output)
                    self.assertEqual(path.read_bytes(), before)

    def test_pi_moreThinking_is_boolean_through_both_set_commands(self):
        key = "envy.agents.pi.moreThinking"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path), patch.object(
                agent, "offer_mutation_commit"
            ), patch.object(agent, "current_machine_id", return_value="test-machine"):
                _write_values({"envy.agents.pi.adapter": "pi"})
                for value in ("true", "false"):
                    for command in ("config", "agent"):
                        with self.subTest(value=value, command=command):
                            if command == "config":
                                config.set_config_value(key, value, quiet=True)
                            else:
                                agent.cmd_set(key, value, json_output=False, yes=True)
                            self.assertIs(_read_values(path)[key], value == "true")
                            self.assertIn(f"{key} = {value};", path.read_text())
                            result = refine_managed(machine_values={}, write=False)
                            self.assertEqual(result["errors"], [])

    def test_pi_moreThinking_rejects_non_boolean_values_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values({"envy.agents.pi.adapter": "pi"})
                before = path.read_text()
                for invalid in ("yes", "1", "", "null", '"true"', 1, None):
                    with self.subTest(value=invalid), self.assertRaises(typer.BadParameter):
                        _set("envy.agents.pi.moreThinking", invalid)
                    self.assertEqual(path.read_text(), before)

    def test_refine_migrates_legacy_more_thinking_to_camel_case(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text(
                "{ ... }: {\n"
                + agent._render({
                    "envy.agents.pi.adapter": "pi",
                    "envy.agents.pi.more_thinking": True,
                })
                + "\n}\n"
            )
            with patch("envy.agent.machine_config_file", return_value=path):
                result = refine_managed(machine_values={}, write=True)
                self.assertEqual(result["errors"], [])
                self.assertIn("envy.agents.pi.more_thinking", result["removed"])
                self.assertIn("envy.agents.pi.moreThinking", result["added"])
                values = _read_values(path)
                self.assertNotIn("envy.agents.pi.more_thinking", values)
                self.assertIs(values["envy.agents.pi.moreThinking"], True)
                self.assertNotIn("more_thinking", path.read_text())

    def test_refine_rejects_conflicting_legacy_and_current_thinking_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text(
                "{ ... }: {\n"
                + agent._render({
                    "envy.agents.pi.adapter": "pi",
                    "envy.agents.pi.more_thinking": True,
                    "envy.agents.pi.moreThinking": False,
                })
                + "\n}\n"
            )
            before = path.read_bytes()
            with patch("envy.agent.machine_config_file", return_value=path):
                result = refine_managed(machine_values={}, write=True)
                self.assertEqual(
                    result["errors"],
                    ["envy.agents.pi.moreThinking", "envy.agents.pi.more_thinking"],
                )
                self.assertEqual(path.read_bytes(), before)

    def test_refine_rejects_quoted_pi_moreThinking_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values({
                    "envy.agents.pi.adapter": "pi",
                    "envy.agents.pi.moreThinking": "true",
                })
                before = path.read_text()
                result = refine_managed(machine_values={}, write=True)
                self.assertEqual(result["errors"], ["envy.agents.pi.moreThinking"])
                self.assertEqual(path.read_text(), before)

    def test_pi_model_window_fields_round_trip_and_validate(self):
        values = {
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.contextWindow": 272000,
            "envy.agents.pi.maxTokens": 128000,
        }
        rendered = _render(values)
        self.assertIn("contextWindow = 272000;", rendered)
        self.assertIn("maxTokens = 128000;", rendered)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values(values)
                self.assertEqual(_read_values(path), values)
                for field in ("contextWindow", "maxTokens"):
                    key = f"envy.agents.pi.{field}"
                    for invalid in ("0", "-1", "not-a-number"):
                        with self.subTest(field=field, value=invalid), self.assertRaises(typer.BadParameter):
                            _set(key, invalid)
                        self.assertEqual(_read_values(path), values)
                    _set(key, "null")
                    values = dict(values)
                    values[key] = None
                    self.assertEqual(_read_values(path), values)

    def test_config_api_edit_is_validated_and_survives_refine(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values({"envy.agents.pi.adapter": "pi"})
                config.set_config_value("envy.agents.pi.api", "openai-responses", quiet=True)
                result = refine_managed(machine_values={}, write=False)
                self.assertEqual(result["errors"], [])
                self.assertEqual(_read_values(path)["envy.agents.pi.api"], "openai-responses")
                config.set_config_value("envy.agents.pi.api", "anthropic-messages", quiet=True)
                self.assertEqual(_read_values(path)["envy.agents.pi.api"], "anthropic-messages")
                config.set_config_value("envy.agents.pi.api", "null", quiet=True)
                self.assertIsNone(_read_values(path)["envy.agents.pi.api"])
                before = path.read_text()
                for invalid in ("responses", "", "openai-codex-responses"):
                    with self.subTest(api=invalid), self.assertRaises(typer.BadParameter):
                        config.set_config_value("envy.agents.pi.api", invalid, quiet=True)
                    self.assertEqual(path.read_text(), before)

    def test_render_round_trips_additional_settings(self):
        values = {
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.additionalSettings": {
                "defaultTools": ["+codemode"],
                "nested": {"enabled": True},
            },
        }
        rendered = _render(values)
        self.assertIn("builtins.fromJSON", rendered)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values(values)
                self.assertEqual(_read_values(path), values)

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
            # Refining an unmarked fixture also fills defaults. Do not depend
            # on the sandbox's hostname command producing a valid email.
            email_field = next(field for field in config.MACHINE_FIELDS if field.path == "envy.git.email")
            patches.enter_context(patch.object(email_field, "default_fn", return_value="test@example.com"))
            body = config._render_machine_block(values) if managed_config else ""
            legacy = (
                '  envy.llm.steps.url = "https://legacy.example";\n'
                '  envy.llm.steps.model = "legacy-model";\n'
                '  envy.llm.deepseek.url = "https://deepseek.example";\n'
                '  envy.llm.deepseek.model = "deepseek-model";\n'
            )
            path.write_text(
                '{ ... }: {\n' + body + '\n' + legacy
                + '  envy.git.email = "test@example.com";\n'
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

import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from envy import agent, error as envy_error
from envy.config import cli as config_cli
from envy.errno import ErrorCode
from envy.config import machine_nix, refine, updates
from envy.agent import (
    _read_values, _render, _runtime_change_kind, _runtime_env, _runtime_pi_models, _set, _write_values, refine_managed,
)


class AgentConfigTests(unittest.TestCase):
    def test_profile_values_and_profile_switch_are_runtime_fields(self):
        values = {
            "envy.agentProfiles.fast.endpoint": "stepfun-openai",
            "envy.agentProfiles.fast.model": "fast-model",
            "envy.agentProfiles.fast.credential": "stepfun",
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.profile": "fast",
        }
        profiles = agent._profile_values(values)
        self.assertEqual(agent._profile_agent_fields(profiles["fast"])["model"], "fast-model")
        self.assertEqual(agent._runtime_change_kind("envy.agentProfiles.fast.model"), "live")

    def test_validate_values_accepts_profiles_and_checks_profile_references(self):
        values = {
            "envy.agentProfiles.fast.endpoint": "stepfun-openai",
            "envy.agentProfiles.fast.model": "fast-model",
            "envy.agentProfiles.fast.api": "openai-responses",
            "envy.agentProfiles.fast.contextWindow": 1000,
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.profile": "fast",
        }
        self.assertEqual(agent._validate_values(values), [])
        values["envy.agents.pi.profile"] = "missing"
        self.assertEqual(agent._validate_values(values), ["envy.agents.pi.profile"])
        values["envy.agents.pi.profile"] = "fast"
        values["envy.agentProfiles.fast.unknown"] = "x"
        self.assertEqual(agent._validate_values(values), ["envy.agentProfiles.fast.unknown"])

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
        self.assertIn("unset OPENAI_BASE_URL", rendered)
        self.assertIn("ANTHROPIC_BASE_URL", rendered)

    def test_runtime_pi_models_switches_protocol_and_api(self):
        values = {
            "envy.llm.endpoints.stepfun.baseUrl": "https://example.test/messages",
            "envy.llm.endpoints.stepfun.protocol": "anthropic-compatible",
        }
        models = _runtime_pi_models(values, {
            "endpoint": "stepfun", "model": "claude-model",
        })
        provider = models["providers"]["envy-pi"]
        self.assertEqual(provider["api"], "anthropic-messages")
        self.assertEqual(provider["baseUrl"], "https://example.test/messages")
        self.assertEqual(provider["apiKey"], "$ANTHROPIC_API_KEY")
        self.assertEqual(provider["models"][0]["thinkingLevelMap"], {"off": None})

    def test_runtime_pi_models_thinking_level_map_matches_api(self):
        anthropic = {
            "envy.llm.endpoints.claude.baseUrl": "https://example.test",
            "envy.llm.endpoints.claude.protocol": "anthropic-compatible",
        }
        merged = _runtime_pi_models(anthropic, {"endpoint": "claude", "model": "m", "moreThinking": True})
        self.assertEqual(
            merged["providers"]["envy-pi"]["models"][0]["thinkingLevelMap"],
            {"off": None, "xhigh": "xhigh", "max": "max", "ultra": "ultra"},
        )
        openai = _runtime_pi_models({}, {"api": "openai-responses", "model": "m"})
        self.assertNotIn("thinkingLevelMap", openai["providers"]["envy-pi"]["models"][0])

    def test_runtime_pi_models_sol_excludes_unsupported_efforts(self):
        for api in ("openai-completions", "openai-responses"):
            for model in ("gpt-6.1-sol", "gpt-6.1-sol-qiye", "gpt-6.1-sol-2026-09-29"):
                for more_thinking in (False, True):
                    with self.subTest(api=api, model=model, more_thinking=more_thinking):
                        data = _runtime_pi_models({}, {
                            "api": api, "model": model, "moreThinking": more_thinking,
                        })["providers"]["envy-pi"]["models"][0]
                        expected = {"off": None, "minimal": None, "ultra": None}
                        if more_thinking:
                            expected.update({"xhigh": "xhigh", "max": "max"})
                        self.assertEqual(data["thinkingLevelMap"], expected)

    def test_runtime_pi_models_sol_mapping_respects_model_and_api_boundaries(self):
        for model in ("gpt-6-astra", "gpt-6.1-solaris", None):
            with self.subTest(model=model):
                data = _runtime_pi_models({}, {
                    "api": "openai-responses", "model": model, "moreThinking": True,
                })["providers"]["envy-pi"]["models"][0]
                self.assertEqual(data["thinkingLevelMap"], {"xhigh": "xhigh", "max": "max", "ultra": "ultra"})
        data = _runtime_pi_models({}, {
            "api": "anthropic-messages", "model": "gpt-6.1-sol", "moreThinking": True,
        })["providers"]["envy-pi"]["models"][0]
        self.assertEqual(data["thinkingLevelMap"], {"off": None, "xhigh": "xhigh", "max": "max", "ultra": "ultra"})

    def test_refresh_runtime_keeps_sol_mapping_after_profile_switch(self):
        values = {
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.profile": "selected",
            "envy.agents.pi.model": "old-model",
            "envy.agents.pi.moreThinking": True,
            "envy.agentProfiles.selected.endpoint": "test",
            "envy.agentProfiles.selected.api": "openai-responses",
            "envy.agentProfiles.selected.model": "gpt-6.1-sol-qiye",
            "envy.llm.endpoints.test.baseUrl": "https://example.test/v1",
            "envy.llm.endpoints.test.protocol": "openai-compatible",
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(agent, "_RUNTIME_DIR", home / "runtime"), patch.object(
                agent, "read_secrets_data", return_value=({}, True)
            ), patch.object(Path, "home", return_value=home):
                self.assertEqual(agent.refresh_runtime(values=values), ["pi"])
                target = home / ".pi" / "agent" / "models.json"
                model = json.loads(target.read_text())["providers"]["envy-pi"]["models"][0]
                self.assertEqual(model["id"], "gpt-6.1-sol-qiye")
                self.assertEqual(model["thinkingLevelMap"], {
                    "off": None, "minimal": None, "ultra": None, "xhigh": "xhigh", "max": "max",
                })
                values["envy.agentProfiles.selected.model"] = "gpt-6-astra"
                agent.refresh_runtime(values=values)
                model = json.loads(target.read_text())["providers"]["envy-pi"]["models"][0]
                self.assertEqual(model["thinkingLevelMap"], {"xhigh": "xhigh", "max": "max", "ultra": "ultra"})

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
                    self.assertEqual(refine_managed(write=write)["errors"], [])
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
                with patch.object(machine_nix, "machine_config_file", return_value=path), patch.object(
                    agent, "log"
                ), patch.object(envy_error, "log") as logger:
                    report = refine.refine_agents(write=write, strict=False)
                    self.assertFalse(report.ok)
                    diagnostic = str(logger.error.call_args_list)
                    self.assertIn("envy.agents.pi.additionalSettings", diagnostic)
                    self.assertIn("managed-block-syntax", diagnostic)
                    self.assertIn("line", diagnostic)
                    self.assertNotIn("unsupported-secret-marker", diagnostic)
                    self.assertEqual(path.read_bytes(), before)

    def test_config_entries_share_strict_validation(self):
        for command, write in (
            (config_cli.cmd_check, False), (config_cli.cmd_doctor, False), (config_cli.cmd_refine, True),
        ):
            with patch.object(refine, "refine_all", return_value=refine.RefineReport()) as refine_all:
                if write:
                    command(prune=False)
                else:
                    command()
                self.assertEqual(refine_all.call_args.kwargs["write"], write)
                self.assertTrue(refine_all.call_args.kwargs["strict"])
                self.assertTrue(refine_all.call_args.kwargs["include_secrets"])

    def test_list_settings_use_valid_nix_lists_through_both_commands(self):
        from envy.main import cli

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            outside = '  envy.software.nix.packages.exclude = [ "keep-me" ];\n'
            path.write_text("{ ... }: {\n" + outside + "}\n")
            with patch.object(agent, "machine_config_file", return_value=path), patch.object(
                agent, "offer_mutation_commit"
            ), patch.object(machine_nix, "machine_config_file", return_value=path), patch.object(
                config_cli, "offer_mutation_commit"
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
                                updates.set_config_value(key, value, quiet=True)
                            else:
                                agent.cmd_set(key, value, json_output=False, yes=True)
                            self.assertIs(_read_values(path)[key], value == "true")
                            self.assertIn(f"{key} = {value};", path.read_text())
                            result = refine_managed(write=False)
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

    def test_refine_rejects_retired_snake_case_thinking_field(self):
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
            before = path.read_bytes()
            with patch("envy.agent.machine_config_file", return_value=path):
                result = refine_managed(write=True)
                self.assertEqual(result["errors"], ["envy.agents.pi.more_thinking"])
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
                result = refine_managed(write=True)
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
                updates.set_config_value("envy.agents.pi.api", "openai-responses", quiet=True)
                result = refine_managed(write=False)
                self.assertEqual(result["errors"], [])
                self.assertEqual(_read_values(path)["envy.agents.pi.api"], "openai-responses")
                updates.set_config_value("envy.agents.pi.api", "anthropic-messages", quiet=True)
                self.assertEqual(_read_values(path)["envy.agents.pi.api"], "anthropic-messages")
                updates.set_config_value("envy.agents.pi.api", "null", quiet=True)
                self.assertIsNone(_read_values(path)["envy.agents.pi.api"])
                before = path.read_text()
                for invalid in ("responses", "", "openai-codex-responses"):
                    with self.subTest(api=invalid), self.assertRaises(typer.BadParameter):
                        updates.set_config_value("envy.agents.pi.api", invalid, quiet=True)
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

    def test_render_writes_strings_as_nix_literals(self):
        values = {
            "envy.agents.codex.adapter": "codex",
            "envy.agents.codex.model": 'step-${model} "q" \\ x',
            "envy.agents.codex.aliases": ["${cx}"],
        }
        rendered = _render(values)
        self.assertIn(r'envy.agents.codex.model = "step-\${model} \"q\" \\ x";', rendered)
        self.assertIn(r'envy.agents.codex.aliases = [ "\${cx}" ];', rendered)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values(values)
                self.assertEqual(agent.read_managed_values(path), (values, True, []))

    def test_render_keeps_agent_block_separate(self):
        rendered = _render({"envy.agents.dsh.adapter": "openai-compatible-cli"})
        self.assertIn("BEGIN ENVY MANAGED AGENTS", rendered)
        self.assertIn('envy.agents.dsh.adapter = "openai-compatible-cli";', rendered)

    def test_missing_block_is_reported_by_check_and_created_by_refine(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                checked = refine_managed(write=False)
                self.assertEqual(checked["errors"], ["envy agents managed block"])
                self.assertEqual(path.read_text(), "{ ... }: {\n}\n")
                created = refine_managed(write=True)
                values = _read_values(path)
            self.assertTrue(created["changed"])
            self.assertEqual(values, agent._initial_values())
            self.assertEqual(values["envy.agents.cc.aliases"], ["ccli"])

    def test_refine_is_idempotent_and_rejects_unknown_references(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ...\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                first = refine_managed(write=True)
                second = refine_managed(write=True)
                self.assertTrue(first["changed"])
                self.assertFalse(second["changed"])
                values = _read_values(path)
                values["envy.agents.dsh.endpoint"] = "missing-endpoint"
                _write_values(values)
                invalid = refine_managed(write=False)
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
                result = refine_managed(write=False)
            self.assertIn("envy.agents.dsh.adapter", result["errors"])
            self.assertIn("envy.llm.credentials.custom.secretPath", result["errors"])

    @contextmanager
    def machine_fixture(self, *, managed_config=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            path = Path(directory) / "machine.nix"
            values = {field.path: field.default_fn() for field in machine_nix.MACHINE_FIELDS}
            values["envy.git.email"] = "test@example.com"
            # Refining an unmarked fixture also fills defaults. Do not depend
            # on the sandbox's hostname command producing a valid email.
            email_field = next(field for field in machine_nix.MACHINE_FIELDS if field.path == "envy.git.email")
            patches.enter_context(patch.object(email_field, "default_fn", return_value="test@example.com"))
            body = machine_nix._render_machine_block(values) if managed_config else ""
            path.write_text(
                '{ ... }: {\n' + body + '\n'
                + '  envy.git.email = "test@example.com";\n'
                + '  envy.software.nix.packages.exclude = [ "keep-me" ];\n}\n'
            )
            for target in ("envy.config.machine_nix.machine_config_file", "envy.agent.machine_config_file"):
                patches.enter_context(patch(target, return_value=path))
            patches.enter_context(patch.object(machine_nix, "set_device_machine_id"))
            patches.enter_context(patch.object(refine, "refine_device_metadata", return_value=refine.RefineReport()))
            patches.enter_context(patch.object(refine, "refine_sensitive_permissions", return_value=refine.RefineReport()))
            patches.enter_context(patch.object(refine, "refine_software_policy", return_value=refine.RefineReport()))
            yield path

    def test_refine_all_creates_managed_blocks_and_keeps_hand_policy(self):
        for managed in (False, True):
            with self.subTest(managed=managed), self.machine_fixture(managed_config=managed) as path:
                report = refine.refine_all(write=True, include_secrets=False)
                self.assertTrue(report.ok)
                self.assertTrue(report.changed)
                self.assertEqual(_read_values(path), agent._initial_values())
                text = path.read_text()
                self.assertIn(machine_nix.MANAGED_START, text)
                self.assertIn('envy.software.nix.packages.exclude = [ "keep-me" ];', text)
                again = refine.refine_all(write=True, include_secrets=False)
                self.assertTrue(again.ok)
                self.assertFalse(again.changed)
                self.assertEqual(path.read_text(), text)

    def test_refine_all_preserves_existing_agent_choices(self):
        with self.machine_fixture(managed_config=True) as path:
            current = {
                "envy.llm.endpoints.custom.baseUrl": "https://custom.example/v1",
                "envy.llm.credentials.custom.secretPath": "llm/custom/apikey",
                "envy.agents.codex.adapter": "codex",
                "envy.agents.codex.endpoint": "custom",
                "envy.agents.codex.credential": "custom",
                "envy.agents.codex.model": "gpt-6.1-sol",
            }
            _write_values(current)
            report = refine.refine_all(write=True, include_secrets=False)
            self.assertTrue(report.ok)
            self.assertEqual(_read_values(path), current)

    def test_invalid_agent_block_blocks_machine_refine(self):
        with self.machine_fixture(managed_config=True) as path:
            _write_values({"envy.agents.codex.endpoint": "unknown"})
            before = path.read_text()
            report = refine.refine_all(write=True, include_secrets=False)
            self.assertFalse(report.ok)
            self.assertEqual(path.read_text(), before)

    def test_nested_profile_assignment_reports_the_supported_shape(self):
        values = {
            "envy.agentProfiles.nisl": {
                "api": "openai-responses",
                "endpoint": "nisl-openai",
                "model": "deepseek-v4.1-flash",
            },
            "envy.llm.endpoints.nisl-openai.baseUrl": "https://example.test/v1",
            "envy.llm.endpoints.nisl-openai.protocol": "openai-compatible",
        }
        issues = {item.path: item for item in agent._validate_issues(values)}
        self.assertEqual(set(issues), {"envy.agentProfiles.nisl"})
        nested = issues["envy.agentProfiles.nisl"]
        self.assertEqual(nested.code, ErrorCode.UNSUPPORTED_OBJECT)
        self.assertIn("whole-attribute-set", nested.message)
        self.assertIn("envy agent profile add nisl", nested.hint)

    def test_validation_reasons_name_the_problem_and_the_fix(self):
        values = {
            "envy.agentProfiles.fast.endpoint": "stepfun-openai",
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.contextWindow": "272000",
            "envy.agents.pi.profile": "ghost",
            "envy.agents.pi.typo": "x",
            "envy.llm.endpoints.stepfun-openai.baseUrl": "ftp://example.test",
            "envy.llm.endpoints.stepfun-openai.protocol": "openai-compatible",
        }
        issues = {(item.path, item.code): item for item in agent._validate_issues(values)}
        self.assertEqual(
            issues[("envy.agentProfiles.fast", ErrorCode.MISSING_REQUIRED)].message,
            "missing required field: model",
        )
        self.assertEqual(
            issues[("envy.agents.pi.contextWindow", ErrorCode.INVALID_TYPE)].message,
            "expected a positive integer or null",
        )
        self.assertEqual(
            issues[("envy.agents.pi.profile", ErrorCode.UNKNOWN_REFERENCE)].message,
            "profile 'ghost' is not declared",
        )
        self.assertEqual(
            issues[("envy.agents.pi.typo", ErrorCode.UNKNOWN_FIELD)].hint,
            "Allowed fields: " + ", ".join(agent._AGENT_FIELDS),
        )
        self.assertIn(
            "http://",
            issues[("envy.llm.endpoints.stepfun-openai.baseUrl", ErrorCode.INVALID_VALUE)].message,
        )

    def test_set_rejects_whole_object_paths_with_the_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                with self.assertRaises(typer.BadParameter) as caught:
                    _set("envy.agentProfiles.nisl", "{}")
                message = str(caught.exception)
                self.assertIn("whole-attribute-set", message)
                self.assertIn("envy agent profile add nisl", message)
                with self.assertRaises(typer.BadParameter) as caught:
                    _set("envy.agents.pi", "{}")
                self.assertIn("envy agent add pi", str(caught.exception))
            self.assertEqual(path.read_text(), "{ ... }: {\n}\n")

    def test_set_usage_errors_come_from_the_shared_error_module(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text("{ ... }: {\n}\n")
            with patch("envy.agent.machine_config_file", return_value=path):
                _write_values({"envy.agents.pi.adapter": "pi"})
                cases = (
                    (
                        "envy.agents.pi.skills",
                        '"animate"',
                        "expected a JSON array of strings. Location: envy.agents.pi.skills",
                    ),
                    (
                        "envy.agents.pi.models",
                        "{}",
                        "expected a JSON array of model objects. Location: envy.agents.pi.models",
                    ),
                    (
                        "envy.agents.pi.contextWindow",
                        "0",
                        "expected a positive integer or null. Location: envy.agents.pi.contextWindow",
                    ),
                    (
                        "envy.agents.pi.api",
                        "responses",
                        "api must be one of: openai-completions, openai-responses, anthropic-messages",
                    ),
                    (
                        "envy.agents.pi.moreThinking",
                        "yes",
                        "expected true or false. Location: envy.agents.pi.moreThinking",
                    ),
                )
                for target, value, message in cases:
                    with self.subTest(target=target), self.assertRaises(typer.BadParameter) as caught:
                        _set(target, value)
                    self.assertEqual(str(caught.exception), message)
                with self.assertRaises(typer.BadParameter) as caught:
                    agent.cmd_profile_use("pi", "ghost", json_output=False, yes=True)
                self.assertEqual(
                    str(caught.exception),
                    "unknown agent profile: ghost. Next: Run: envy agent profile list",
                )
                from envy.main import cli

                result = CliRunner().invoke(cli, ["agent", "add", "bad/name", "--adapter", "pi"])
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("invalid agent name: bad/name", result.output)

    def test_set_reports_malformed_block_reason_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.nix"
            path.write_text(
                "{ ... }: {\n" + agent.MANAGED_START + "\n"
                "  envy.agents.pi.additionalSettings = { x = unsupported-secret-marker; };\n"
                + agent.MANAGED_END + "\n}\n"
            )
            with patch("envy.agent.machine_config_file", return_value=path):
                with self.assertRaises(typer.BadParameter) as caught:
                    _set("envy.agents.pi.model", "demo")
            message = str(caught.exception)
            self.assertIn("cannot edit malformed agent managed block", message)
            self.assertIn("cannot parse the assignment", message)
            self.assertNotIn("unsupported-secret-marker", message)

    def test_config_set_routes_agent_profiles_through_agent_validation(self):
        with self.machine_fixture(managed_config=True):
            with self.assertRaises(typer.BadParameter) as caught:
                updates.set_config_value("envy.agentProfiles.fast.model", "demo-model", quiet=True)
            message = str(caught.exception)
            self.assertIn("missing required field: endpoint", message)
            self.assertNotIn("unknown managed machine field", message)


if __name__ == "__main__":
    unittest.main()

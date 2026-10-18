import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import click
from typer.main import get_command
from typer.testing import CliRunner

from envy import agent, agent_completion, config
from envy.main import cli


class AgentCompletionTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "machine.nix"
        self.path.write_text("{ ... }: {\n" + agent._render({
            "envy.agents.local-pi.adapter": "pi",
            "envy.agents.local-pi.endpoint": "custom",
            "envy.llm.endpoints.custom.baseUrl": "https://private.example/v1",
            "envy.llm.credentials.custom.secretPath": "llm/custom/apikey",
        }) + "\n}\n")
        self.before = self.path.read_bytes()
        self.enterContext(patch.object(agent, "machine_config_file", return_value=self.path))
        self.manifest = self.enterContext(patch.object(agent_completion, "machine_manifest", return_value={
            "agents": {"inherited": {"adapter": "codex"}, "other-pi": {"adapter": "pi"}, "omp": {"adapter": "oh-my-pi"}, "retired": {"adapter": "oh-my-pi"}},
            "llm": {"endpoints": {"shared": {"baseUrl": "https://example.test/v1"}},
                    "credentials": {"shared": {"secretPath": "llm/shared/token"}}},
        }))
        self.enterContext(patch.object(agent, "read_secrets_data", side_effect=AssertionError("no decryption in completion")))
        self.enterContext(patch.object(config, "read_secrets_data", side_effect=AssertionError("no decryption in completion")))
        self.enterContext(patch.object(agent, "_write_values", side_effect=AssertionError("no writes in completion")))

    def tearDown(self):
        self.assertEqual(self.path.read_bytes(), self.before)

    def complete(self, group, command_name, parameter_name, prefix, **params):
        command = get_command(cli).commands[group].commands[command_name]
        parameter = next(item for item in command.params if item.name == parameter_name)
        context = click.Context(command)
        context.params = params
        return [item.value for item in parameter.shell_complete(context, prefix)]

    def test_agent_and_config_set_offer_current_and_inherited_scalar_paths(self):
        for group in ("agent", "config"):
            paths = self.complete(group, "set", "path", "envy.agents.")
            self.assertIn("envy.agents.local-pi.moreThinking", paths)
            self.assertIn("envy.agents.inherited.model", paths)
            self.assertIn("envy.agents.other-pi.api", paths)
            self.assertNotIn("envy.agents.omp.api", paths)
            self.assertNotIn("envy.agents.retired.api", paths)
            self.assertNotIn("envy.agents.omp.moreThinking", paths)
            self.assertNotIn("envy.agents.local-pi.args", paths)
            self.assertIn("envy.llm.endpoints.custom.protocol", self.complete(group, "set", "path", "envy.llm.endpoints.c"))
            self.assertIn("envy.llm.credentials.shared.kind", self.complete(group, "set", "path", "envy.llm.credentials.s"))
        self.manifest.assert_called_with(completion=True)

    def test_values_follow_namespace_and_selected_field(self):
        examples = (
            ("envy.agents.local-pi.moreThinking", "t", ["true"]),
            ("envy.agents.other-pi.api", "openai-r", ["openai-responses"]),
            ("envy.agents.other-pi.api", "anthropic", ["anthropic-messages"]),
            ("envy.agents.omp.api", "", []),
            ("envy.agents.retired.api", "", []),
            ("envy.agents.inherited.endpoint", "c", ["custom"]),
            ("envy.agents.inherited.credential", "s", ["shared"]),
            ("envy.agents.inherited.adapter", "cl", ["claude-code"]),
            ("envy.llm.endpoints.custom.protocol", "an", ["anthropic-compatible"]),
            ("envy.llm.credentials.custom.kind", "t", ["token"]),
            ("envy.llm.credentials.custom.secretPath", "llm/custom", ["llm/custom/apikey"]),
            ("envy.agents.inherited.model", "", []),
            ("envy.llm.endpoints.custom.baseUrl", "", []),
            ("envy.llm.endpoints.custom.adapter", "", []),
        )
        for group in ("agent", "config"):
            for path, prefix, expected in examples:
                with self.subTest(group=group, path=path):
                    self.assertEqual(self.complete(group, "set", "value", prefix, path=path), expected)

    def test_show_includes_inherited_but_remove_only_suggests_managed_agents(self):
        self.assertEqual(self.complete("agent", "show", "name", ""), ["inherited", "local-pi", "other-pi"])
        for command in ("remove", "rm"):
            self.assertEqual(self.complete("agent", command, "name", ""), ["local-pi"])

    def test_add_and_credential_set_complete_metadata_references(self):
        for parameter, prefix, expected in (
            ("adapter", "pi", ["pi"]), ("adapter", "oh-my", []), ("api", "openai-r", ["openai-responses"]), ("api", "anthropic", ["anthropic-messages"]),
            ("endpoint", "c", ["custom"]), ("credential", "s", ["shared"]),
        ):
            self.assertEqual(self.complete("agent", "add", parameter, prefix), expected)
        self.assertEqual(self.complete("agent", "credential-set", "name", "c"), ["custom"])
        self.assertEqual(self.complete("agent", "credential-set", "secret_path", "llm/custom"), ["llm/custom/apikey"])
        self.assertEqual(self.complete("config", "secret-set", "yaml_path", "llm/custom"), ["llm/custom/apikey"])

    def test_failed_evaluation_uses_managed_metadata_and_static_choices(self):
        self.manifest.side_effect = OSError("Nix unavailable")
        self.assertEqual(self.complete("agent", "show", "name", ""), ["local-pi"])
        self.assertIn("envy.agents.local-pi.moreThinking", self.complete("agent", "set", "path", "envy.agents.local-pi."))
        self.assertEqual(self.complete("agent", "add", "endpoint", ""), ["custom"])
        self.assertEqual(self.complete("agent", "set", "value", "f", path="envy.agents.new.moreThinking"), ["false"])

    def test_static_completion_does_not_evaluate_and_missing_machine_is_safe(self):
        self.manifest.side_effect = AssertionError("static completion should not evaluate")
        self.assertEqual(self.complete("config", "set", "value", "ch", path="envy.mirrors.mode"), ["china"])
        self.assertEqual(self.complete("agent", "add", "adapter", "co"), ["codex"])
        self.assertEqual(self.complete("agent", "set", "value", "", path="envy.agents.pi.model"), [])
        self.assertEqual(self.complete("agent", "set", "path", "envy.mirrors."), [])
        with patch.object(agent, "_read_values", side_effect=OSError("missing machine")):
            self.assertEqual(self.complete("agent", "rm", "name", ""), [])

    def test_real_bash_and_zsh_protocols_complete_without_executing_set(self):
        for shell, args in (("bash", "envy agent set envy.agents.local-pi.moreThinking t"),
                            ("zsh", "envy config set envy.agents.local-pi.moreThinking t")):
            environment = {"_ENVY_COMPLETE": f"complete_{shell}", "COMP_WORDS": args,
                           "COMP_CWORD": "4", "_TYPER_COMPLETE_ARGS": args}
            result = CliRunner().invoke(cli, [], prog_name="envy", env=environment)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("true", result.output)
            self.assertNotIn("private.example", result.output)


if __name__ == "__main__":
    unittest.main()

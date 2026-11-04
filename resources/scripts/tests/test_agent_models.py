import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from envy import agent
from envy.agent_models import model_bindings
from envy.config import cli as config_cli
from envy.config import machine_nix, updates
from envy.main import cli
from envy.structured_config import update_file


class AgentModelsTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.directory)
        self.machine = self.home / "machine.nix"
        self.machine.write_text("{ ... }: {\n}\n")
        self.enterContext(patch.object(agent, "machine_config_file", return_value=self.machine))
        self.enterContext(patch.object(machine_nix, "machine_config_file", return_value=self.machine))
        self.enterContext(patch.object(config_cli, "offer_mutation_commit"))
        self.enterContext(patch.object(agent, "_RUNTIME_DIR", self.home / "runtime"))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        self.enterContext(patch.object(agent, "offer_mutation_commit"))
        self.secrets = self.enterContext(patch.object(agent, "read_secrets_data", return_value=({
            "llm": {"a": {"key": "fake-a ' \n$token"}, "b": {"key": "fake-b"}},
        }, True)))
        self.values = {
            "envy.agents.pi.adapter": "pi",
            "envy.llm.endpoints.a.baseUrl": "https://a.example/v1",
            "envy.llm.endpoints.b.baseUrl": "https://b.example",
            "envy.llm.endpoints.b.protocol": "anthropic-compatible",
            "envy.llm.credentials.a.secretPath": "llm/a/key",
            "envy.llm.credentials.b.secretPath": "llm/b/key",
            "envy.agentProfiles.fast.endpoint": "a",
            "envy.agentProfiles.fast.credential": "a",
            "envy.agentProfiles.fast.api": "openai-responses",
            "envy.agentProfiles.fast.model": "gpt-6.1-sol",
        }
        self.models = [
            {"profile": "fast"},
            {"model": "claude-test", "endpoint": "b", "credential": "b"},
            {"model": "other", "endpoint": "a", "credential": "a", "api": "openai-responses"},
        ]
        self.values["envy.agents.pi.models"] = self.models
        self.pi_models = self.home / ".pi" / "agent" / "models.json"

    def config(self, **kwargs):
        return {"adapter": "pi", "models": self.models} | kwargs

    def providers(self, **kwargs):
        return agent._runtime_pi_models(self.values, self.config(**kwargs))["providers"]

    def test_list_and_profile_reference_round_trip_through_both_editors(self):
        for command in ("agent", "config"):
            agent._write_values(self.values)
            result = CliRunner().invoke(cli, [command, "set", "envy.agents.pi.models", json.dumps(self.models), "--yes"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(agent._read_values(), self.values)
            self.assertEqual(agent.refine_managed(write=False)["errors"], [])
            self.assertIn('"profile" = "fast";', self.machine.read_text())
            self.assertEqual(agent._runtime_change_kind("envy.agents.pi.models"), "live")

    def test_list_only_uses_first_model_and_groups_connections(self):
        bindings = model_bindings(self.values, self.config())
        self.assertEqual(bindings[0]["selection"]["model"], "gpt-6.1-sol")
        providers = self.providers()
        self.assertEqual(len(providers), 2)
        openai = providers[bindings[0]["provider"]]
        self.assertEqual(openai["api"], "openai-responses")
        self.assertEqual([item["id"] for item in openai["models"]], ["gpt-6.1-sol", "other"])
        self.assertEqual(openai["models"][0]["thinkingLevelMap"], {"off": None, "minimal": None, "ultra": None})
        self.assertEqual(providers[bindings[1]["provider"]]["api"], "anthropic-messages")
        self.assertEqual(len({entry["apiKey"] for entry in bindings}), 2)
        self.assertEqual(set(providers), set(self.providers(models=list(reversed(self.models)))))

    def test_legacy_default_is_included_and_profile_default_deduplicated(self):
        bindings = model_bindings(self.values, self.config(model="default", endpoint="a", credential="a"))
        self.assertEqual(bindings[0]["selection"]["model"], "default")
        self.assertEqual(len(bindings), 4)
        bindings = model_bindings(self.values, self.config(profile="fast", model="stale", contextWindow=999))
        self.assertEqual(len(bindings), 3)
        self.assertEqual(bindings[0]["selection"]["model"], "gpt-6.1-sol")
        self.assertEqual(bindings[0]["selection"]["contextWindow"], 999)

    def test_pi_limits_propagate_to_profile_and_inline_models(self):
        self.values |= {
            "envy.agentProfiles.fast.contextWindow": None,
            "envy.agentProfiles.fast.maxTokens": None,
        }
        limits = {"contextWindow": 272000, "maxTokens": 128000}
        for models in ([], self.models):
            with self.subTest(models=models):
                config = self.config(profile="fast", model="stale", models=models, **limits)
                bindings = model_bindings(self.values, config)
                self.assertEqual(bindings[0]["selection"]["model"], "gpt-6.1-sol")
                for binding in bindings:
                    self.assertEqual({field: binding["selection"][field] for field in limits}, limits)
                for provider in agent._runtime_pi_models(self.values, config)["providers"].values():
                    for model in provider["models"]:
                        self.assertEqual({field: model[field] for field in limits}, limits)
        inline = {"model": "local", "endpoint": "b"}
        binding = model_bindings(self.values, self.config(
            models=[inline], endpoint="a", credential="a", api="openai-responses", **limits,
        ))[0]
        self.assertIsNone(binding["selection"]["credential"])
        self.assertEqual(binding["api"], "anthropic-messages")
        self.assertEqual(binding["apiKey"], "envy-no-key")

    def test_profile_limits_override_only_the_fields_they_supply(self):
        self.values |= {
            "envy.agentProfiles.fast.contextWindow": 10000,
            "envy.agentProfiles.fast.maxTokens": None,
        }
        config = self.config(profile="fast", contextWindow=272000, maxTokens=128000)
        bindings = model_bindings(self.values, config)
        self.assertEqual(len(bindings), 3)
        self.assertEqual(bindings[0]["selection"]["contextWindow"], 10000)
        for binding in bindings:
            self.assertEqual(binding["selection"]["maxTokens"], 128000)
        for binding in bindings[1:]:
            self.assertEqual(binding["selection"]["contextWindow"], 272000)

    def test_profile_fields_are_inherited_then_explicit_fields_override(self):
        self.values |= {
            "envy.agentProfiles.fast.contextWindow": 10000,
            "envy.agentProfiles.fast.maxTokens": 1000,
        }
        models = [{"profile": "fast"}, {"profile": "fast", "model": "other", "maxTokens": 2000}]
        bindings = model_bindings(self.values, self.config(models=models, contextWindow=272000, maxTokens=128000))
        first, second = [entry["selection"] for entry in bindings]
        self.assertEqual(second, first | {"model": "other", "maxTokens": 2000})
        self.assertEqual(len(self.providers(models=models)), 1)
        self.assertEqual(bindings[0]["provider"], bindings[1]["provider"])
        agent._write_values(self.values | {"envy.agents.pi.models": models})
        self.assertEqual(agent._read_values()["envy.agents.pi.models"], models)
        self.assertEqual(agent.refine_managed(write=False)["errors"], [])

    def test_explicit_null_clears_profile_values_instead_of_inheriting(self):
        self.values |= {"envy.agentProfiles.fast.contextWindow": 10000, "envy.agentProfiles.fast.maxTokens": 1000}
        models = [{"profile": "fast", "api": None, "credential": None, "contextWindow": None, "maxTokens": None}]
        config = self.config(models=models, contextWindow=272000, maxTokens=128000)
        binding = model_bindings(self.values, config)[0]
        self.assertEqual(binding["api"], "openai-completions")
        self.assertEqual(binding["apiKey"], "envy-no-key")
        self.assertIsNone(binding["selection"]["credential"])
        provider = next(iter(agent._runtime_pi_models(self.values, config)["providers"].values()))
        self.assertNotIn("contextWindow", provider["models"][0])
        self.assertNotIn("maxTokens", provider["models"][0])
        models[0] |= {"endpoint": "b", "credential": "b"}
        binding = model_bindings(self.values, self.config(models=models))[0]
        self.assertEqual(binding["api"], "anthropic-messages")
        self.assertEqual(binding["selection"]["credential"], "b")

    def test_resolved_profile_and_inline_entries_deduplicate(self):
        inline = {"model": "gpt-6.1-sol", "endpoint": "a", "credential": "a", "api": "openai-responses"}
        models = [{"profile": "fast", "model": "gpt-6.1-sol"}, inline]
        self.assertEqual(len(model_bindings(self.values, self.config(models=models))), 1)
        models[0]["maxTokens"] = 1000
        with self.assertRaisesRegex(ValueError, "conflicting metadata"):
            model_bindings(self.values, self.config(models=models))

    def test_same_model_different_protocol_endpoint_or_credential_remains_distinct(self):
        common = {"model": "same", "endpoint": "a", "credential": "a"}
        models = [common, common | {"api": "openai-responses"}, common | {"endpoint": "b"}, common | {"credential": "b"}]
        self.assertEqual(len(self.providers(models=models)), 4)
        self.assertEqual(len(self.providers(models=models + [common])), 4)
        equivalent = common | {"api": "openai-completions"}
        self.assertEqual(len(model_bindings(self.values, self.config(models=[common, equivalent]))), 1)

    def test_invalid_lists_are_rejected_without_rewriting_policy(self):
        agent._write_values(self.values)
        before = self.machine.read_bytes()
        invalid = [None, {}, ["fast"], [{}], [{"profile": "missing"}],
                   [{"profile": "fast", "model": None}], [{"profile": "fast", "endpoint": None}],
                   [{"profile": "fast", "apiKey": "never-write"}],
                   [{"model": "m", "endpoint": "missing"}], [{"model": "m", "endpoint": "a", "credential": "missing"}]]
        for field, value in (("model", ""), ("api", "invalid"), ("maxTokens", True),
                             ("contextWindow", 0), ("credential", []), ("profile", 7)):
            invalid.append([{"model": "m", "endpoint": "a", field: value}])
        invalid.append([{"profile": "fast"}, {"model": "gpt-6.1-sol", "endpoint": "a", "credential": "a",
                                             "api": "openai-responses", "contextWindow": 100}])
        for models in invalid:
            with self.subTest(models=models), self.assertRaises(typer.BadParameter):
                agent._set("envy.agents.pi.models", json.dumps(models))
            self.assertEqual(self.machine.read_bytes(), before)

    def test_other_adapters_and_instances_reject_nonempty_lists(self):
        for name, adapter in (("cc", "claude-code"), ("codex", "codex"), ("other-pi", "pi")):
            values = self.values | {f"envy.agents.{name}.adapter": adapter, f"envy.agents.{name}.models": self.models}
            self.assertIn(f"envy.agents.{name}.models", agent._validate_values(values))

    def test_runtime_keys_are_isolated_and_shell_quoted(self):
        agent.refresh_runtime(values=self.values)
        env = self.home / "runtime" / "pi.env"
        bindings = model_bindings(self.values, self.config())
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.pi_models.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fake-", self.pi_models.read_text())
        program = "import json, os; print(json.dumps(dict(os.environ)))"
        import sys
        result = subprocess.run(["bash", "-c", 'source "$1"; exec "$2" -c "$3"', "bash", str(env), sys.executable, program],
                                env={"PATH": os.environ["PATH"]}, text=True, capture_output=True, check=True)
        environment = json.loads(result.stdout)
        self.assertEqual(environment[bindings[0]["keyVariable"]], "fake-a ' \n$token")
        self.assertEqual(environment[bindings[1]["keyVariable"]], "fake-b")
        self.assertEqual(environment["ENVY_AGENT_PROVIDER"], bindings[0]["provider"])
        self.assertEqual(environment["ENVY_AGENT_MODEL"], "gpt-6.1-sol")

    def test_missing_key_does_not_fall_back_to_another_provider(self):
        env = agent._runtime_env(self.values, self.config(), {"llm": {"a": {"key": "fake-a"}}})
        variable = model_bindings(self.values, self.config())[1]["keyVariable"]
        self.assertIn(f"export {variable}=''", env)
        local = self.providers(models=[{"model": "local", "endpoint": "a"}])
        self.assertEqual(next(iter(local.values()))["apiKey"], "envy-no-key")

    def test_profile_switch_keeps_models_and_connection_ids(self):
        agent._write_values(self.values)
        agent.refresh_runtime(values=self.values)
        before = json.loads(self.pi_models.read_text())
        result = CliRunner().invoke(cli, ["agent", "profile", "use", "pi", "fast", "--yes"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(agent._read_values()["envy.agents.pi.models"], self.models)
        self.assertEqual(json.loads(self.pi_models.read_text()), before)
        agent._set("envy.agents.pi.models", json.dumps(self.models[:2]))
        self.assertEqual(agent._read_values()["envy.agents.pi.profile"], "fast")

    def test_refresh_keeps_pi_limits_separate_from_default_profile_limits(self):
        values = self.values | {
            "envy.agents.pi.profile": "fast",
            "envy.agents.pi.contextWindow": 272000,
            "envy.agents.pi.maxTokens": 128000,
            "envy.agentProfiles.fast.contextWindow": 10000,
            "envy.agentProfiles.fast.maxTokens": 1000,
        }
        agent.refresh_runtime(values=values)
        self.assertEqual(json.loads(self.pi_models.read_text()), agent._runtime_pi_models(
            values, self.config(profile="fast", contextWindow=272000, maxTokens=128000),
        ))
        models = {model["id"]: model for provider in json.loads(self.pi_models.read_text())["providers"].values()
                  for model in provider["models"]}
        self.assertEqual(models["gpt-6.1-sol"]["contextWindow"], 10000)
        self.assertEqual(models["gpt-6.1-sol"]["maxTokens"], 1000)
        self.assertEqual(models["claude-test"]["contextWindow"], 272000)
        self.assertEqual(models["other"]["maxTokens"], 128000)

    def test_profile_switch_preserves_pi_limits_for_other_candidates(self):
        values = self.values | {
            "envy.agents.pi.contextWindow": 272000,
            "envy.agents.pi.maxTokens": 128000,
            "envy.agentProfiles.budget.endpoint": "a",
            "envy.agentProfiles.budget.credential": "a",
            "envy.agentProfiles.budget.api": "openai-responses",
            "envy.agentProfiles.budget.model": "budget-model",
            "envy.agentProfiles.budget.contextWindow": 10000,
            "envy.agentProfiles.budget.maxTokens": 1000,
        }
        agent._write_values(values)
        for profile in ("budget", "fast"):
            with self.subTest(profile=profile):
                result = CliRunner().invoke(cli, ["agent", "profile", "use", "pi", profile, "--yes"])
                self.assertEqual(result.exit_code, 0, result.output)
                actual = agent._read_values()
                self.assertEqual(actual["envy.agents.pi.contextWindow"], 272000)
                self.assertEqual(actual["envy.agents.pi.maxTokens"], 128000)
                self.assertEqual(actual["envy.agents.pi.models"], self.models)
                models = {model["id"]: model for provider in json.loads(self.pi_models.read_text())["providers"].values()
                          for model in provider["models"]}
                self.assertEqual(models["claude-test"]["contextWindow"], 272000)
                self.assertEqual(models["other"]["maxTokens"], 128000)

    def test_pi_limit_edits_preserve_the_default_profile(self):
        agent._write_values(self.values | {"envy.agents.pi.profile": "fast"})
        for command in (agent._set, updates.set_config_value):
            for field in ("contextWindow", "maxTokens"):
                for value in ("272000", "null"):
                    with self.subTest(command=command.__name__, field=field, value=value):
                        command(f"envy.agents.pi.{field}", value)
                        actual = agent._read_values()
                        self.assertEqual(actual["envy.agents.pi.profile"], "fast")
                        self.assertEqual(actual["envy.agents.pi.models"], self.models)
        agent._set("envy.agents.pi.model", "replacement")
        self.assertNotIn("envy.agents.pi.profile", agent._read_values())

    def test_apply_and_refresh_remove_only_owned_providers(self):
        self.pi_models.parent.mkdir(parents=True)
        foreign = {"baseUrl": "https://foreign.example", "models": [{"id": "keep"}]}
        original = {"providers": {"manual": foreign, "envy-piano": foreign, "envy-pi": {}, "envy-pi-old": {}}, "other": 1}
        for live in (False, True):
            self.pi_models.write_text(json.dumps(original))
            data = agent._runtime_pi_models(self.values, self.config())
            if live:
                agent.refresh_runtime(values=self.values)
            else:
                update_file(self.pi_models, data, managed_key_prefixes={"providers": "envy-pi"})
            actual = json.loads(self.pi_models.read_text())
            self.assertEqual(actual, {"providers": {"manual": foreign, "envy-piano": foreign} | data["providers"], "other": 1})
        single = self.values | {"envy.agents.pi.models": [], "envy.agents.pi.profile": "fast"}
        agent.refresh_runtime(values=single)
        self.assertEqual(set(json.loads(self.pi_models.read_text())["providers"]), {"manual", "envy-piano", "envy-pi"})

    def test_refresh_failures_leave_existing_runtime_intact(self):
        agent.refresh_runtime(values=self.values)
        env = self.home / "runtime" / "pi.env"
        before = (env.read_bytes(), self.pi_models.read_bytes())
        self.secrets.return_value = ({}, False)
        self.assertEqual(agent.refresh_runtime(values=self.values), [])
        self.assertEqual((env.read_bytes(), self.pi_models.read_bytes()), before)
        self.secrets.return_value = ({}, True)
        with patch.object(agent, "atomic_write_text", side_effect=OSError("write failed")), self.assertRaises(OSError):
            agent.refresh_runtime(values=self.values | {"envy.agents.pi.models": self.models[:1]})
        self.assertEqual((env.read_bytes(), self.pi_models.read_bytes()), before)
        self.pi_models.write_text("invalid json")
        with self.assertRaises(ValueError):
            agent.refresh_runtime(values=self.values)
        self.assertEqual(env.read_bytes(), before[0])
        self.assertEqual(self.pi_models.read_text(), "invalid json")

    def test_clearing_list_only_configuration_restores_shared_default(self):
        agent.refresh_runtime(values=self.values)
        values = self.values | {"envy.agents.pi.models": []}
        agent.refresh_runtime(values=values)
        providers = json.loads(self.pi_models.read_text())["providers"]
        self.assertEqual(set(providers), {"envy-pi"})
        self.assertEqual(providers["envy-pi"]["models"][0]["id"], "gpt-6.1-sol")
        self.assertEqual(providers["envy-pi"]["baseUrl"], "https://stepfun.com/v1")
        self.assertIn("export ENVY_AGENT_PROVIDER=envy-pi", (self.home / "runtime" / "pi.env").read_text())

    def test_model_list_is_exposed_in_config_view_and_completion(self):
        from envy import agent_completion

        agent._write_values(self.values)
        view = agent.config_view({}, secret_data={}, decrypt_ok=True)
        self.assertEqual(json.loads(view["values"]["envy.agents.pi.models"]), self.models)
        self.assertEqual(view["agents"]["pi"]["models"], self.models)
        metadata = {"agents": {"pi": {"adapter": "pi"}}, "endpoints": {}, "credentials": {}}
        with patch.object(agent_completion, "_metadata", return_value=metadata):
            paths = agent_completion.complete_agent_paths(None, "envy.agents.pi.models")
        self.assertEqual(paths, [("envy.agents.pi.models", "agent setting")])

    def test_config_set_preserves_default_profile(self):
        agent._write_values(self.values | {"envy.agents.pi.profile": "fast"})
        updates.set_config_value("envy.agents.pi.models", json.dumps(self.models[:1]), quiet=True)
        self.assertEqual(agent._read_values()["envy.agents.pi.profile"], "fast")


if __name__ == "__main__":
    unittest.main()

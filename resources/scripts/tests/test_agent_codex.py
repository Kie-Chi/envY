"""Codex config.toml and auth.json rendering shared by activation and envy agent."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy import agent
from envy.agent_auth import update_auth
from envy.structured_config import update_file


FIXTURES = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].endswith(".json") else None


class CodexSettingsTests(unittest.TestCase):
    def values(self, **overrides):
        base = {
            "envy.llm.endpoints.xikai-openai.baseUrl": "https://api.xikai.site/v1",
            "envy.llm.endpoints.xikai-openai.protocol": "openai-compatible",
            "envy.llm.endpoints.deepseek-openai.baseUrl": "https://api.deepseek.com",
            "envy.llm.endpoints.deepseek-openai.protocol": "openai-compatible",
            "envy.llm.credentials.xikai.secretPath": "llm/xikai/apikey",
            "envy.llm.credentials.deepseek.secretPath": "llm/deepseek/apikey",
        }
        base.update(overrides)
        return base

    def agent(self, **overrides):
        base = {
            "adapter": "codex",
            "endpoint": "xikai-openai",
            "credential": "xikai",
            "model": "gpt-6.1-sol",
            "api": "openai-responses",
        }
        base.update(overrides)
        return base

    def test_responses_endpoint_uses_owned_provider(self):
        settings = agent._codex_settings(self.values(), self.agent())
        self.assertEqual(settings["model"], "gpt-6.1-sol")
        self.assertEqual(settings["model_provider"], "envy-xikai-openai")
        self.assertEqual(settings["model_providers"], {
            "envy-xikai-openai": {
                "name": "xikai-openai",
                "base_url": "https://api.xikai.site/v1",
                "wire_api": "responses",
                "requires_openai_auth": True,
            },
        })

    def test_missing_api_keeps_the_codex_default(self):
        settings = agent._codex_settings(self.values(), self.agent(api=None))
        self.assertNotIn("wire_api", settings["model_providers"]["envy-xikai-openai"])

    def test_completions_api_maps_to_chat(self):
        settings = agent._codex_settings(
            self.values(), self.agent(endpoint="deepseek-openai", credential="deepseek", api="openai-completions"),
        )
        self.assertEqual(
            settings["model_providers"]["envy-deepseek-openai"]["wire_api"], "chat",
        )

    def test_model_limits_are_forwarded(self):
        settings = agent._codex_settings(
            self.values(), self.agent(contextWindow=272000, maxTokens=128000),
        )
        self.assertEqual(settings["model_context_window"], 272000)
        self.assertEqual(settings["model_max_output_tokens"], 128000)

    def test_missing_credential_omits_openai_auth(self):
        settings = agent._codex_settings(self.values(), self.agent(credential=None))
        self.assertNotIn("requires_openai_auth", settings["model_providers"]["envy-xikai-openai"])

    def test_anthropic_wire_api_is_rejected(self):
        with self.assertRaises(ValueError):
            agent._codex_settings(self.values(), self.agent(api="anthropic-messages"))
        values = self.values(**{
            "envy.llm.endpoints.anthropic.baseUrl": "https://anthropic.example.invalid",
            "envy.llm.endpoints.anthropic.protocol": "anthropic-compatible",
        })
        with self.assertRaises(ValueError):
            agent._codex_settings(values, self.agent(endpoint="anthropic", api=None))

    def test_incomplete_selection_writes_nothing(self):
        values = self.values()
        self.assertEqual(agent._codex_settings(values, self.agent(model=None)), {})
        self.assertEqual(agent._codex_settings(values, self.agent(endpoint="missing")), {})

    def test_aggregate_keeps_every_provider_and_the_codex_default(self):
        agents = {
            "codex": self.agent(),
            "aa-alt": self.agent(endpoint="deepseek-openai", credential="deepseek", api="openai-completions"),
        }
        data, secret_path = agent._codex_runtime(self.values(), agents)
        self.assertEqual(data["model_provider"], "envy-xikai-openai")
        self.assertEqual(sorted(data["model_providers"]),
                         ["envy-deepseek-openai", "envy-xikai-openai"])
        self.assertEqual(secret_path, "llm/xikai/apikey")

    def test_non_codex_agents_are_ignored(self):
        data, secret_path = agent._codex_runtime(self.values(), {
            "cc": {"adapter": "claude-code", "endpoint": "xikai-openai", "model": "x"},
        })
        self.assertEqual((data, secret_path), ({}, ""))


class CodexAuthTests(unittest.TestCase):
    def test_preserves_codex_owned_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / ".codex" / "auth.json"
            target.parent.mkdir()
            target.write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "keep-me"},
                "OPENAI_API_KEY": "old",
            }))
            self.assertTrue(update_auth(target, "sk-new\n"))
            data = json.loads(target.read_text())
            self.assertEqual(data["auth_mode"], "apikey")
            self.assertEqual(data["OPENAI_API_KEY"], "sk-new")
            self.assertEqual(data["tokens"], {"access_token": "keep-me"})
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertFalse(update_auth(target, "sk-new"))

    def test_rejects_empty_key_and_invalid_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / ".codex" / "auth.json"
            with self.assertRaises(ValueError):
                update_auth(target, "  ")
            target.parent.mkdir()
            target.write_text("[]")
            with self.assertRaises(ValueError):
                update_auth(target, "sk-new")

    def test_config_merge_preserves_codex_state_and_replaces_envy_providers(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            target.write_text("""
[projects."/tmp/x"]
trust_level = "trusted"

[model_providers.custom]
name = "custom"

[model_providers.envy-stale]
name = "stale"
""")
            update_file(
                target,
                {"model_provider": "envy-xikai-openai", "model_providers": {
                    "envy-xikai-openai": {"name": "xikai-openai", "base_url": "https://api.xikai.site/v1"},
                }},
                format="toml",
                managed_key_prefixes={"model_providers": "envy"},
            )
            import tomllib
            data = tomllib.loads(target.read_text())
            self.assertEqual(data["model_provider"], "envy-xikai-openai")
            self.assertEqual(sorted(data["model_providers"]), ["custom", "envy-xikai-openai"])
            self.assertEqual(data["projects"]["/tmp/x"]["trust_level"], "trusted")


class CodexParityTests(unittest.TestCase):
    def test_nix_and_runtime_render_identical_settings(self):
        if FIXTURES is None:
            self.skipTest("no Nix fixtures supplied")
        fixtures = json.loads(FIXTURES.read_text())
        self.assertTrue(fixtures)
        for fixture in fixtures:
            values = {item["path"]: item["value"] for item in fixture["values"]}
            selection = dict(fixture["agent"])
            profiles = agent._profile_values(values)
            profile = selection.get("profile")
            if isinstance(profile, str) and profile in profiles:
                selection.update(agent._profile_agent_fields(profiles[profile]))
            data, _ = agent._codex_runtime(values, {"codex": selection})
            self.assertEqual(data, fixture["expected"], fixture["name"])


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.directory)
        self.enterContext(patch.object(agent, "_RUNTIME_DIR", self.home / "runtime"))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        self.enterContext(patch.object(agent, "read_secrets_data", return_value=({
            "llm": {"xikai": {"apikey": "sk-secret"}, "deepseek": {"apikey": "sk-deepseek"}},
        }, True)))
        self.values = {
            "envy.agents.codex.adapter": "codex",
            "envy.agents.codex.profile": "deepseek",
            "envy.agents.codex.endpoint": "xikai-openai",
            "envy.agents.codex.credential": "xikai",
            "envy.agents.codex.model": "gpt-6.1-sol",
            "envy.agentProfiles.deepseek.endpoint": "deepseek-openai",
            "envy.agentProfiles.deepseek.credential": "deepseek",
            "envy.agentProfiles.deepseek.api": "openai-completions",
            "envy.agentProfiles.deepseek.model": "deepseek-v4-pro",
            "envy.llm.endpoints.xikai-openai.baseUrl": "https://api.xikai.site/v1",
            "envy.llm.endpoints.deepseek-openai.baseUrl": "https://api.deepseek.com",
            "envy.llm.credentials.xikai.secretPath": "llm/xikai/apikey",
            "envy.llm.credentials.deepseek.secretPath": "llm/deepseek/apikey",
        }

    def test_refresh_applies_profile_to_config_and_auth(self):
        agent.refresh_runtime(values=self.values)
        config = (self.home / ".codex" / "config.toml").read_text()
        self.assertIn('model = "deepseek-v4-pro"', config)
        self.assertIn('model_provider = "envy-deepseek-openai"', config)
        self.assertIn('wire_api = "chat"', config)
        auth = json.loads((self.home / ".codex" / "auth.json").read_text())
        self.assertEqual(auth, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-deepseek"})

    def test_refresh_keeps_codex_owned_config_sections(self):
        target = self.home / ".codex" / "config.toml"
        target.parent.mkdir()
        target.write_text("""
[projects."/tmp/x"]
trust_level = "trusted"
""")
        agent.refresh_runtime(values=self.values)
        data = __import__("tomllib").loads(target.read_text())
        self.assertEqual(data["projects"]["/tmp/x"]["trust_level"], "trusted")
        self.assertEqual(data["model"], "deepseek-v4-pro")

    def test_without_codex_instance_nothing_is_written(self):
        agent.refresh_runtime(values={"envy.agents.pi.adapter": "pi"})
        self.assertFalse((self.home / ".codex").exists())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])

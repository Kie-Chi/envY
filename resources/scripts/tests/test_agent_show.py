import json
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from envy import agent
from envy.main import cli


class AgentShowTests(unittest.TestCase):
    def setUp(self):
        self.selection = {
            "adapter": "pi",
            "command": "pi",
            "aliases": ["pcli"],
            "profile": "fast",
            "model": "fast-model",
            "models": [],
            "api": None,
            "endpoint": "custom",
            "credential": "custom",
            "endpointConfigured": True,
            "credentialSet": False,
            "moreThinking": False,
            "args": ["--verbose"],
            "skills": ["animate"],
            "additionalSettings": {"compaction": {"keepRecentTokens": 1000}},
            "runtimeFiles": [{"target": ".pi/agent/models.json", "data": {"providers": {}}}],
            "futureAdapterMetadata": "internal",
        }
        self.enterContext(patch.object(agent, "_manifest_agents", return_value={"pi": self.selection}))
        self.runner = CliRunner()

    def test_default_show_only_includes_common_selections(self):
        result = self.runner.invoke(cli, ["agent", "show", "pi"])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["name"], "pi")
        selected = payload["agent"]
        self.assertEqual(selected["model"], "fast-model")
        self.assertEqual(selected["profile"], "fast")
        self.assertEqual(selected["aliases"], ["pcli"])
        self.assertFalse(selected["credentialSet"])
        self.assertFalse(selected["moreThinking"])
        for field in ("runtimeFiles", "additionalSettings", "args", "skills", "futureAdapterMetadata", "api", "models"):
            self.assertNotIn(field, selected)
        self.assertEqual(self.selection["runtimeFiles"][0]["target"], ".pi/agent/models.json")

    def test_detail_flags_and_command_aliases_show_complete_agent(self):
        for group in ("agent", "agents", "ag"):
            for command in ("show", "sh"):
                for flag in ("--detail", "--details", "-d"):
                    with self.subTest(group=group, command=command, flag=flag):
                        result = self.runner.invoke(cli, [group, command, "pi", flag])
                        self.assertEqual(result.exit_code, 0, result.output)
                        self.assertEqual(json.loads(result.output), {"name": "pi", "agent": self.selection})

    def test_json_preserves_complete_data_with_and_without_detail(self):
        for options in (["--json"], ["--json", "--detail"]):
            with self.subTest(options=options):
                result = self.runner.invoke(cli, ["ag", "sh", "pi", *options])
                self.assertEqual(result.exit_code, 0, result.output)
                payload = json.loads(result.output)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["command"], "agent.show")
                self.assertEqual(payload["data"], {"name": "pi", "agent": self.selection})

    def test_unknown_agent_keeps_json_not_found_error(self):
        result = self.runner.invoke(cli, ["ag", "sh", "missing", "--json"])
        self.assertEqual(result.exit_code, 1, result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertIn("unknown agent: missing", result.output)


if __name__ == "__main__":
    unittest.main()

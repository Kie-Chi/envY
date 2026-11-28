import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy.workflows import agent_update


class AgentReleaseTests(unittest.TestCase):
    def setUp(self):
        self.lock = {
            "schemaVersion": 1,
            "registry": "https://npm.test",
            "agents": {
                "claude": {"platforms": {
                    "linux-arm64": {"version": "1.0.0", "url": "c-arm", "integrity": "sha512-c-arm"},
                    "linux-x64": {"version": "1.0.0", "url": "c-x64", "integrity": "sha512-c-x64"},
                }},
                "codex": {"platforms": {
                    "linux-arm64": {"version": "2.0.0", "url": "x-arm", "integrity": "sha512-x-arm"},
                    "linux-x64": {"version": "2.0.0", "url": "x-x64", "integrity": "sha512-x-x64"},
                }},
            },
        }

    def test_resolve_agent_updates_only_selected_name(self):
        def metadata(_registry, path):
            if path == "@anthropic-ai/claude-code-linux-arm64/latest":
                return {"version": "1.1.0", "dist": {"tarball": "c2-arm", "integrity": "sha512-c2-arm"}}
            if path == "@anthropic-ai/claude-code-linux-x64/latest":
                return {"version": "1.1.0", "dist": {"tarball": "c2-x64", "integrity": "sha512-c2-x64"}}
            raise AssertionError(path)

        with patch.object(agent_update, "_get_json", side_effect=metadata):
            result = agent_update.resolve_agent(self.lock, "claude")
        self.assertEqual(result["claude"]["platforms"]["linux-x64"]["version"], "1.1.0")
        self.assertEqual(result["codex"], self.lock["agents"]["codex"])
        self.assertEqual(self.lock["agents"]["claude"]["platforms"]["linux-x64"]["version"], "1.0.0")

    def test_codex_resolution_uses_platform_tarballs(self):
        def metadata(_registry, path):
            if path == "@openai/codex/latest":
                return {"version": "2.1.0"}
            return {"version": "2.1.0-platform", "dist": {"tarball": path, "integrity": "sha512-x"}}

        with patch.object(agent_update, "_get_json", side_effect=metadata):
            result = agent_update.resolve_agent(self.lock, "codex")
        self.assertEqual(result["codex"]["platforms"]["linux-arm64"]["url"],
                         "@openai/codex/2.1.0-linux-arm64")
        self.assertEqual(result["claude"], self.lock["agents"]["claude"])

    def test_write_lock_is_json_and_preserves_complete_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-lock.json"
            with patch.object(agent_update, "AGENT_LOCK", path):
                agent_update._write_lock(self.lock)
            self.assertEqual(json.loads(path.read_text()), self.lock)


if __name__ == "__main__":
    unittest.main()

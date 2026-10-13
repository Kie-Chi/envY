import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import click
from typer.main import get_command

from envy import remote_agent


class RemoteCompletionTests(unittest.TestCase):
    def test_registered_completion_reads_explicit_config_without_starting_relay(self):
        command = get_command(remote_agent.app).commands["start"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote-agents.toml"
            path.write_text('''
[connections.server]
host = "private-host"
[connections.laptop]
host = "another-private-host"
[agents.codex]
command = ["codex"]
env = { API_KEY = "private-token" }
[agents.claude]
command = ["claude"]
''')
            before = path.read_bytes()
            context = click.Context(command)
            context.params = {"config_file": path}
            with patch.object(remote_agent.subprocess, "run", side_effect=AssertionError("no commands during completion")), patch.object(
                remote_agent, "_resolve_connection", side_effect=AssertionError("no relay resolution")
            ), patch.object(remote_agent, "_resolve_agent", side_effect=AssertionError("no agent resolution")):
                target = next(param for param in command.params if param.name == "target")
                agent = next(param for param in command.params if param.name == "agent")
                self.assertEqual(
                    [(item.value, item.help) for item in target.shell_complete(context, "s")],
                    [("server", "configured connection")],
                )
                self.assertEqual(
                    [(item.value, item.help) for item in agent.shell_complete(context, "co")],
                    [("codex", "configured remote agent")],
                )
            self.assertEqual(path.read_bytes(), before)

    def test_completion_uses_config_environment_and_legacy_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.toml"
            path.write_text('[targets.legacy]\nhost = "server"\n[agents.pi]\ncommand = ["pi"]\n')
            with patch.dict("os.environ", {"ENVY_REMOTE_AGENT_CONFIG": str(path)}):
                self.assertEqual(remote_agent.complete_connection_names(None, "leg"), [("legacy", "configured connection")])
                self.assertEqual(remote_agent.complete_remote_agent_names(None, "p"), [("pi", "configured remote agent")])

    def test_missing_or_malformed_configuration_has_no_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote-agents.toml"
            context = SimpleNamespace(params={"config_file": path})
            for text in (None, "invalid = [", 'connections = "invalid"\nagents = []\n'):
                if text is not None:
                    path.write_text(text)
                with self.subTest(text=text):
                    self.assertEqual(remote_agent.complete_connection_names(context, ""), [])
                    self.assertEqual(remote_agent.complete_remote_agent_names(context, ""), [])

    def test_completion_ignores_malformed_entries_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote-agents.toml"
            path.write_text('[connections]\ninvalid = "private-value"\n[connections.valid]\nhost = "host"\n')
            context = SimpleNamespace(params={"config_file": str(path)})
            self.assertEqual(remote_agent.complete_connection_names(context, ""), [("valid", "configured connection")])


if __name__ == "__main__":
    unittest.main()

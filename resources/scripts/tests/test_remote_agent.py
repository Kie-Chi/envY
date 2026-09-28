import tempfile
import unittest
from pathlib import Path
import shutil

from envy.remote_agent import (
    AgentConfigError,
    AgentSpec,
    Connection,
    _agent_argv,
    _install_authorized_key,
    _remote_agent_command,
    _remote_command_argv,
    _resolve_connection,
    _return_path_probe,
    _system_prompt,
)


class AgentRelayTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection(
            name="remote",
            method="ssh",
            host="remote-host",
            args=("-A",),
            command=(),
            alias=None,
            shell="zsh",
            remote_identity="~/.ssh/id_ed25519",
            return_host="local-host",
            return_user="chi",
            return_port=22,
            authorized_keys=Path("/tmp/authorized_keys"),
        )

    def test_ssh_connection_uses_configured_host_and_tty(self):
        argv = _remote_command_argv(self.connection, "printf ok", interactive=True)
        self.assertEqual(Path(argv[0]), Path(shutil.which("ssh") or "ssh"))
        self.assertEqual(argv[1:3], ["-tt", "-A"])
        self.assertEqual(argv[3:], ["remote-host", "printf ok"])

    def test_custom_config_path_stays_on_remote_home(self):
        spec = AgentSpec(
            name="custom",
            command=("my-agent",),
            args=(),
            prompt_args=("--system-prompt", "{system_prompt}"),
            config="~/.config/my-agent/config.toml",
            config_args=("--config", "{config}"),
            env={},
        )
        command = _agent_argv(spec, "work on local-host", self.connection, "launcher", [])
        remote = _remote_agent_command(spec, command, self.connection, "~")
        self.assertIn('"$HOME/.config/my-agent/config.toml"', remote)
        self.assertIn('cd "$HOME"', remote)
        self.assertIn("work on local-host", remote)

    def test_connection_requires_a_return_host(self):
        with self.assertRaises(AgentConfigError):
            _resolve_connection(
                {"connections": {"remote": {"method": "ssh", "host": "remote-host"}}},
                "remote",
                local_user="chi",
                local_host="local-host",
            )

    def test_prompt_requires_work_on_launcher_host(self):
        prompt = _system_prompt({}, self.connection, None, "launcher")
        self.assertIn("MUST connect back", prompt)
        self.assertIn("chi@local-host:22", prompt)

    def test_return_probe_uses_configured_remote_identity(self):
        probe = _return_path_probe(self.connection)
        self.assertIn('-i "$HOME/.ssh/id_ed25519"', probe)
        self.assertIn("chi@local-host", probe)

    def test_authorized_key_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ssh" / "authorized_keys"
            key = "ssh-ed25519 AAAATEST original-comment"
            self.assertTrue(_install_authorized_key(path, key, "envy-agent:test", dry_run=False))
            self.assertFalse(_install_authorized_key(path, key, "envy-agent:test", dry_run=False))
            self.assertEqual(len(path.read_text().splitlines()), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

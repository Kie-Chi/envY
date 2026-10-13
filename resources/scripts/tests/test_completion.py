import unittest
from types import SimpleNamespace
from unittest.mock import patch

from typer.main import get_command

from envy.config import (
    complete_machine_paths,
    complete_machine_values,
    complete_secret_paths,
)
from envy.doctor.app import complete_doctor_selection
from envy.habit import complete_habit_gestures, complete_habit_ids
from envy.key import complete_export_formats
from envy.main import cli
from envy.mirror import complete_measurement_providers, complete_mirror_targets


class CompletionTests(unittest.TestCase):
    def test_config_completion_never_reads_secret_values(self):
        self.assertTrue(any(path == "envy.mirrors.mode" for path, _ in complete_machine_paths(None, "envy.mirrors")))
        self.assertEqual(
            complete_machine_values(SimpleNamespace(params={"path": "envy.mirrors.mode"}), "c"),
            [("china", "envy.mirrors.mode")],
        )
        with patch("envy.agent_completion.machine_manifest", return_value={}), patch("envy.agent._read_values", return_value={}):
            secret_paths = complete_secret_paths(None, "llm/")
        self.assertTrue(secret_paths)
        self.assertTrue(all("apikey" not in description.casefold() or value.endswith("apikey") for value, description in secret_paths))

    def test_new_static_completion_callbacks(self):
        self.assertEqual(complete_habit_ids(None, "terminal"), [("terminal-scratchpad", "toggle the terminal scratchpad")])
        gestures = complete_habit_gestures(SimpleNamespace(params={"habit_id": "terminal-scratchpad"}), "F1")
        self.assertEqual(gestures, [("F10", "managed gesture"), ("F12", "managed gesture")])
        self.assertEqual(complete_export_formats(None, "S"), [("ssh", "SSH public key format")])
        self.assertEqual(complete_measurement_providers(None, "cu"), [("curl", "Measure each source URL with curl")])
        self.assertEqual(complete_mirror_targets(None, "py"), [("python", "Python / PyPI")])

    def test_doctor_completion_supports_prefix_and_comma_lists(self):
        sections = complete_doctor_selection(None, "section:")
        self.assertTrue(any(value == "section:apps" for value, _ in sections))
        values = complete_doctor_selection(None, "apps,app:")
        self.assertTrue(values)
        self.assertTrue(all(value.startswith("apps,app:") for value, _ in values))

    def test_new_arguments_register_shell_completion(self):
        commands = get_command(cli).commands
        mirror = commands["mirror"].commands
        for command_name, parameter_name in (
            ("sources", "target"),
            ("sources", "provider"),
            ("measure", "target"),
            ("measure", "provider"),
            ("set", "source"),
            ("reset", "target"),
        ):
            parameter = next(param for param in mirror[command_name].params if param.name == parameter_name)
            self.assertIsNotNone(parameter._custom_shell_complete, (command_name, parameter_name))


if __name__ == "__main__":
    unittest.main()

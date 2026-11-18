import json
import unittest
from unittest.mock import patch

from typer.main import get_command
from typer.testing import CliRunner

from envy.habit import check_habits, habits_from_manifest, normalize_policy_gesture
from envy.main import cli


def _entry(
    habit_id: str,
    context: str,
    *,
    gesture: str = "F12",
    semantic: str = "Toggle terminal scratchpad",
    ownership: str = "declarative",
    requirements: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "id": habit_id,
        "label": "Terminal scratchpad" if habit_id == "terminal-scratchpad" else "Global launcher",
        "gesture": gesture,
        "semantic": semantic,
        "context": context,
        "backend": "test backend",
        "binding": gesture,
        "ownership": ownership,
        "note": "",
        "requirements": requirements or [],
    }


def _manifest(*entries: dict, effective: dict[str, list[str]] | None = None, excluded: dict[str, list[str]] | None = None) -> dict:
    effective = effective or {}
    excluded = excluded or {}
    groups = {
        group: {
            "selection": {
                "effective": [{"id": item, "name": item} for item in items],
                "exclude": excluded.get(group, []),
            }
        }
        for group, items in effective.items()
    }
    for group, items in excluded.items():
        groups.setdefault(group, {"selection": {"effective": [], "exclude": items}})
    return {
        "schemaVersion": 2,
        "id": "test-machine",
        "platform": "linux",
        "habits": list(entries),
        "software": {"groups": groups},
    }


class HabitTests(unittest.TestCase):
    def test_habits_group_context_implementations_by_stable_id(self):
        manifest = _manifest(
            _entry("terminal-scratchpad", "niri"),
            _entry("terminal-scratchpad", "gnome"),
            _entry("global-launcher", "niri", gesture="Option+Space", semantic="Open launcher"),
        )

        habits, errors = habits_from_manifest(manifest)

        self.assertEqual(errors, [])
        self.assertEqual([habit.id for habit in habits], ["global-launcher", "terminal-scratchpad"])
        terminal = next(habit for habit in habits if habit.id == "terminal-scratchpad")
        self.assertEqual(terminal.gesture, "F12")
        self.assertEqual([item.context for item in terminal.implementations], ["gnome", "niri"])

    def test_habit_contract_rejects_duplicate_context_and_mixed_semantics(self):
        manifest = _manifest(
            _entry("terminal-scratchpad", "niri"),
            _entry("terminal-scratchpad", "niri", semantic="Open a terminal"),
        )

        _, errors = habits_from_manifest(manifest)

        self.assertTrue(any("inconsistent" in error for error in errors))

    def test_check_validates_effective_requirements_and_preserves_app_ownership(self):
        required = [{"group": "nix.user.package", "item": "fuzzel"}]
        manifest = _manifest(
            _entry("global-launcher", "niri", gesture="Option+Space", semantic="Open launcher", requirements=required),
            _entry(
                "global-launcher",
                "darwin",
                gesture="Option+Space",
                semantic="Open launcher",
                ownership="application",
                requirements=[{"group": "homebrew.system.cask", "item": "raycast"}],
            ),
            effective={
                "nix.user.package": ["fuzzel"],
                "homebrew.system.cask": ["raycast"],
            },
        )

        results, failed = check_habits(manifest)

        self.assertFalse(failed)
        self.assertTrue(any(
            result.status == "info"
            and result.context == "darwin"
            and "manual verification required" in result.message
            for result in results
        ))
        self.assertTrue(any(result.status == "ok" and "fuzzel" in result.message for result in results))
        self.assertTrue(any(
            result.status == "ok"
            and result.context == "niri"
            and "Nix renders binding from the implementation source" in result.message
            for result in results
        ))

    def test_check_reports_machine_policy_exclusions(self):
        manifest = _manifest(
            _entry(
                "terminal-scratchpad",
                "niri",
                requirements=[{"group": "nix.user.package", "item": "alacritty"}],
            ),
            effective={"nix.user.package": []},
            excluded={"nix.user.package": ["alacritty"]},
        )

        results, failed = check_habits(manifest)

        self.assertTrue(failed)
        self.assertTrue(any("excluded by machine policy" in result.message for result in results))

    def test_habit_subcommands_are_registered(self):
        commands = get_command(cli).commands
        self.assertIn("habit", commands)
        self.assertIn("habits", commands)
        habit_commands = commands["habit"].commands
        self.assertIn("set", habit_commands)
        self.assertIn("repair", habit_commands)

    def test_policy_gesture_normalization_uses_machine_paths(self):
        self.assertEqual(
            normalize_policy_gesture("terminal-scratchpad", "f8"),
            ("envy.habits.terminalScratchpad.gesture", "F8"),
        )
        self.assertEqual(
            normalize_policy_gesture("global-launcher", " option + space "),
            ("envy.habits.globalLauncher.gesture", "Option+Space"),
        )

    def test_cli_set_updates_versioned_machine_policy_and_can_apply(self):
        runner = CliRunner()
        with patch("envy.habit.set_config_value") as set_value, patch(
            "envy.habit.machine_config_file", return_value="/tmp/test-machine.nix"
        ) as machine_file, patch("envy.habit.offer_mutation_commit") as offer, patch(
            "envy.habit.apply_configuration"
        ) as apply_configuration:
            result = runner.invoke(
                cli,
                ["habit", "set", "global-launcher", "option + space", "--apply"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        set_value.assert_called_once_with(
            "envy.habits.globalLauncher.gesture", "Option+Space"
        )
        offer.assert_called_once()
        self.assertEqual(offer.call_args.args[0], [machine_file.return_value])
        apply_configuration.assert_called_once_with()

    def test_cli_set_rejects_unsupported_terminal_gesture(self):
        result = CliRunner().invoke(cli, ["habit", "set", "terminal-scratchpad", "F11"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("terminal scratchpad gesture must be one of", result.output)

    def test_cli_repair_applies_the_evaluated_desired_state(self):
        manifest = _manifest(_entry("terminal-scratchpad", "niri"))
        with patch("envy.habit.machine_manifest", return_value=manifest), patch(
            "envy.habit.apply_configuration"
        ) as apply_configuration:
            result = CliRunner().invoke(cli, ["habit", "repair"])

        self.assertEqual(result.exit_code, 0, result.output)
        apply_configuration.assert_called_once_with()

    def test_repair_allows_native_drift_without_spawning_readback(self):
        entry = _entry("key-repeat", "darwin")
        entry.update(kind="preference", gesture=None,
                     readback={"adapter": "darwin.key-repeat", "expected": 1})
        manifest = _manifest(entry)
        manifest["platform"] = "darwin"
        with patch("envy.habit.machine_manifest", return_value=manifest), patch(
            "envy.habit.apply_configuration"
        ) as apply_configuration, patch("envy.habit_verification.command.run_bounded") as probe:
            result = CliRunner().invoke(cli, ["habit", "repair"])
        self.assertEqual(result.exit_code, 0, result.output)
        apply_configuration.assert_called_once_with()
        probe.assert_not_called()

    def test_repair_still_rejects_invalid_declarations_and_missing_software(self):
        entry = _entry("terminal-scratchpad", "niri", requirements=[{"group": "nix.user.package", "item": "missing"}])
        for manifest in (_manifest(entry), {"habits": [None]}):
            with patch("envy.habit.machine_manifest", return_value=manifest), patch(
                "envy.habit.apply_configuration"
            ) as apply_configuration:
                result = CliRunner().invoke(cli, ["habit", "repair"])
            self.assertEqual(result.exit_code, 1)
            apply_configuration.assert_not_called()

    def test_check_json_success_failure_and_unavailable_preserve_aliases(self):
        valid = _manifest(_entry("terminal-scratchpad", "niri"))
        for manifest in (valid, {"habits": [None]}, None):
            with patch("envy.habit.machine_manifest", return_value=manifest) as evaluate:
                result = CliRunner().invoke(cli, ["habit", "check", "--json", "--refresh"])
            payload = json.loads(result.output)
            self.assertEqual(payload["command"], "habit.check")
            self.assertEqual(payload["ok"], manifest is valid)
            self.assertEqual(result.exit_code, 0 if manifest is valid else 1)
            self.assertEqual(payload["results"], payload["data"]["results"])
            self.assertEqual(payload["failed"], payload["data"]["failed"])
            self.assertIsNotNone(payload["data"])
            evaluate.assert_called_once_with(refresh=True)
            if manifest is not valid:
                self.assertIsInstance(payload["error"]["message"], str)

    def test_cli_list_json_uses_evaluated_manifest(self):
        manifest = _manifest(_entry("terminal-scratchpad", "niri"))
        with patch("envy.habit.machine_manifest", return_value=manifest):
            result = CliRunner().invoke(cli, ["habit", "list", "--json"])

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["machine"], "test-machine")
        self.assertEqual(payload["habits"][0]["id"], "terminal-scratchpad")


if __name__ == "__main__":
    unittest.main()

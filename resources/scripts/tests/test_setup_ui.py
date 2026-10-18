import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_setup_module():
    root = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
    path = root / "setup.py"
    spec = importlib.util.spec_from_file_location("envy_setup_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _group(option, label, include=(), exclude=(), effective=()):
    entries = lambda values: [{"id": value, "name": value} for value in values]
    return {
        "label": label,
        "optionPath": option,
        "ecosystem": "homebrew" if "homebrew" in option else "nix",
        "scope": "system" if "darwin" in option else "user",
        "kind": "cask" if "casks" in option else "package",
        "installer": "homebrew" if "homebrew" in option else "home-manager",
        "editable": {"include": False, "exclude": True},
        "selection": {
            "include": entries(include),
            "exclude": list(exclude),
            "effective": entries(effective),
        },
    }


def _manifest(platform, groups):
    return {
        "schemaVersion": 2,
        "id": f"test-{platform}",
        "platform": platform,
        "software": {"groups": groups},
    }


class SetupUiTests(unittest.TestCase):
    def test_yes_no_prompt_retries_blank_and_invalid_answers(self):
        setup = _load_setup_module()
        with patch.object(setup, "pt_prompt", side_effect=["", "later", "YES"]) as prompt:
            self.assertTrue(setup.prompt_yes_no("Continue?"))
        self.assertEqual(prompt.call_count, 3)

    def test_yes_no_prompt_accepts_explicit_no(self):
        setup = _load_setup_module()
        with patch.object(setup, "pt_prompt", side_effect=["unknown", "no"]) as prompt:
            self.assertFalse(setup.prompt_yes_no("Continue?"))
        self.assertEqual(prompt.call_count, 2)

    def test_yes_no_prompt_handles_invalid_terminal(self):
        setup = _load_setup_module()
        with patch.object(setup, "pt_prompt", side_effect=OSError(22, "Invalid argument")):
            self.assertFalse(setup.prompt_yes_no("Continue?"))

    def test_decrypt_failure_can_cancel_without_preparing_a_key(self):
        setup = _load_setup_module()
        with patch.object(setup, "prompt_secret_recovery_choice", return_value="cancel"):
            staged, values, replace, cancelled = setup.handle_decrypt_failure()
        self.assertIsNone(staged)
        self.assertEqual(values, {})
        self.assertFalse(replace)
        self.assertTrue(cancelled)

    def test_decrypt_failure_reset_prepares_a_key_without_writing_active_key(self):
        setup = _load_setup_module()
        staged = object()
        with patch.object(setup, "prompt_secret_recovery_choice", return_value="reset"), patch.object(
            setup, "prompt_yes_no", return_value=True
        ), patch.object(setup, "prepare_generated_device_age_key", return_value=staged):
            prepared, values, replace, cancelled = setup.handle_decrypt_failure()
        self.assertIs(prepared, staged)
        self.assertEqual(values, {})
        self.assertTrue(replace)
        self.assertFalse(cancelled)

    def test_decrypt_failure_import_rejects_a_key_without_writing_active_key(self):
        setup = _load_setup_module()
        staged = SimpleNamespace(path=Path("/private/staged"), cleanup=lambda: None)
        with patch.object(setup, "prompt_secret_recovery_choice", side_effect=["import", "cancel"]), patch.object(
            setup, "key_import", return_value=staged
        ), patch.object(setup, "read_secrets_data", return_value=({}, False)) as read:
            prepared, values, replace, cancelled = setup.handle_decrypt_failure()
        read.assert_called_once_with(age_key_file=staged.path)
        self.assertIsNone(prepared)
        self.assertEqual(values, {})
        self.assertFalse(replace)
        self.assertTrue(cancelled)

    def test_changes_summary_never_prints_secret_values(self):
        setup = _load_setup_module()
        secret = setup.SECRET_FIELDS[0]
        old_secret = "old-secret-value"
        new_secret = "new-secret-value"
        exclusions = setup.normalize_exclusions({})

        with setup.log.console.capture() as capture:
            changed = setup.show_changes(
                {secret.path: old_secret},
                {secret.path: new_secret},
                exclusions,
                exclusions,
                setup.groups_for_manifest(None, include_empty=True),
            )
        rendered = capture.get()

        self.assertTrue(changed)
        self.assertNotIn(old_secret, rendered)
        self.assertNotIn(new_secret, rendered)
        self.assertIn("<set>", rendered)

    def test_secret_values_are_masked_in_list_and_edit_context(self):
        setup = _load_setup_module()
        secret = setup.SECRET_FIELDS[0]
        value = "visible-only-in-the-edit-buffer"
        state = setup.AppState({secret.path: value})
        state.editing_field = secret

        list_text = "".join(part for _, part in setup._list_text(state))
        edit_text = "".join(part for _, part in setup._edit_context(state))

        self.assertNotIn(value, list_text)
        self.assertNotIn(value, edit_text)
        self.assertIn("<set>", list_text)
        self.assertIn("<set>", edit_text)

    def test_setup_field_values_are_normalized_to_text(self):
        setup = _load_setup_module()
        bool_field = next(
            field for field in setup.ALL_FIELDS
            if field.path == "envy.darwin.services.mihomo.tun"
        )
        self.assertEqual(setup.field_text_value(bool_field, True), "true")
        self.assertEqual(setup.field_text_value(bool_field, False), "false")
        self.assertEqual(setup.field_text_value(bool_field, None), "")
        self.assertEqual(setup.field_text_value(bool_field, 7), "7")

    def test_secret_values_from_data_normalizes_yaml_scalars(self):
        setup = _load_setup_module()
        values = setup.secret_values_from_data({"home": {"passwd": 12345}})
        self.assertEqual(values["home_passwd"], "12345")

    def test_policy_view_shows_machine_exclusion_checkboxes(self):
        setup = _load_setup_module()
        manifest = _manifest("linux", {
            "nix.user.package": _group(
                "envy.software.nix.packages", "Nix packages",
                ["git", "okular"], ["okular"], ["git"],
            ),
        })

        exclusions = {"nix.user.package": ["okular"]}
        state = setup.AppState({}, manifest, exclusions)
        rendered = "".join(part for _, part in setup._policy_text(state))

        self.assertIn("[x] git", rendered)
        self.assertIn("[ ] okular", rendered)
        self.assertIn("machine exclusion", rendered)
        setup.build_application(state)

    def test_darwin_cask_toggle_changes_checkbox_to_checked(self):
        setup = _load_setup_module()
        manifest = _manifest("darwin", {
            "homebrew.system.formula": _group(
                "envy.darwin.software.homebrew.formulae", "Homebrew formulae",
                ["gh"], [], ["gh"],
            ),
            "homebrew.system.cask": _group(
                "envy.darwin.software.homebrew.casks", "Homebrew casks",
                ["iterm2", "uuremote"], ["uuremote"], ["iterm2"],
            ),
        })
        state = setup.AppState({}, manifest, {"homebrew.system.cask": ["uuremote"]})
        state.mode = "policy"
        state.policy_group = next(
            index
            for index, group in enumerate(state.policy_groups)
            if group.key == "homebrew.system.cask"
        )
        state.policy_cursor = 1

        before = "".join(part for _, part in setup._policy_text(state))
        self.assertIn("[ ] uuremote", before)
        self.assertNotIn("stale exclusion", before)

        setup._toggle_policy_item(state)

        after = "".join(part for _, part in setup._policy_text(state))
        self.assertIn("[x] uuremote", after)
        self.assertIn("pending", after)

    def test_linux_policy_view_has_only_manifest_groups(self):
        setup = _load_setup_module()
        manifest = _manifest("linux", {
            "nix.user.package": _group(
                "envy.software.nix.packages", "Nix packages", ["git"], [], ["git"]
            ),
        })
        state = setup.AppState({}, manifest, {})
        state.mode = "policy"

        rendered = "".join(part for _, part in setup._policy_text(state))

        self.assertEqual([group.key for group in state.policy_groups], ["nix.user.package"])
        self.assertIn("Nix packages  (1/1)", rendered)
        self.assertIn("[x] git", rendered)
        self.assertNotIn("Homebrew", rendered)


if __name__ == "__main__":
    unittest.main()

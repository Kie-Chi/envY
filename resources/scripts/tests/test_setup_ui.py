import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput


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

    def test_ssh_identity_prepares_a_key_when_no_age_key_exists(self):
        setup = _load_setup_module()
        staged = object()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".ssh").mkdir()
            (home / ".ssh" / "id_ed25519").write_text("ssh-private-key\n")
            with patch.object(setup, "AGE_KEY_FILE", home / "age" / "keys.txt"), patch.object(
                setup, "AGE_KEY_DIR", home / "age"
            ), patch.object(setup, "HOME_DIR", home), patch.object(
                setup, "run_cmd", return_value="AGE-SECRET-KEY-TEST"
            ) as convert, patch.object(
                setup, "prepare_device_age_key", return_value=staged
            ) as prepare, patch.object(setup, "prepare_generated_device_age_key") as generate:
                self.assertIs(setup.prepare_setup_age_key(), staged)
        self.assertEqual(convert.call_args.args[0][:3], ["ssh-to-age", "-private-key", "-i"])
        prepare.assert_called_once_with("AGE-SECRET-KEY-TEST")
        generate.assert_not_called()

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


UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
ENTER, ESCAPE, BACKSPACE = "\r", "\x1b", "\x7f"


class SetupKeyBindingTests(unittest.TestCase):
    """Drive the menuconfig application through its real key bindings."""

    def setUp(self):
        self.setup = _load_setup_module()
        self.values = {
            field.path: self.setup.field_text_value(field, field.default_fn())
            for field in self.setup.ALL_FIELDS
        }
        self.values["envy.git.name"] = "Old Name"
        self.values["envy.vscode.mode"] = "remote"

    def press(self, state, *keys):
        async def run():
            with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
                app = self.setup.build_application(state)
                pipe.send_text("".join(keys))
                await asyncio.wait_for(app.run_async(), timeout=10)

        asyncio.run(run())
        return state.result

    def down_to(self, state, path):
        paths = [field.path for field in self.setup.get_visible_fields(state.values)]
        return DOWN * paths.index(path)

    def policy_state(self):
        manifest = _manifest("linux", {
            "nix.user.package": _group(
                "envy.software.nix.packages", "Nix packages",
                ["git", "okular"], ["okular"], ["git"],
            ),
        })
        return self.setup.AppState(dict(self.values), manifest, {"nix.user.package": ["okular"]})

    def test_text_field_edit_is_saved(self):
        state = self.setup.AppState(dict(self.values))
        result = self.press(
            state, self.down_to(state, "envy.git.name"), ENTER,
            BACKSPACE * len("Old Name"), "New Name", ENTER, "s",
        )
        self.assertEqual(result.values["envy.git.name"], "New Name")
        self.assertEqual(result.values["envy.vscode.mode"], "remote")

    def test_invalid_text_keeps_the_editor_open_until_corrected(self):
        state = self.setup.AppState(dict(self.values))
        # A rejected Enter must stay in the editor; otherwise "s" would be typed
        # into a reopened editor and the application would never exit.
        result = self.press(
            state, self.down_to(state, "envy.git.name"), ENTER,
            BACKSPACE * len("Old Name"), ENTER, "Fixed", ENTER, "s",
        )
        self.assertEqual(result.values["envy.git.name"], "Fixed")

    def test_escape_discards_the_edit(self):
        state = self.setup.AppState(dict(self.values))
        result = self.press(state, self.down_to(state, "envy.git.name"), ENTER, "zzz", ESCAPE, "s")
        self.assertEqual(result.values["envy.git.name"], "Old Name")

    def test_choice_field_moves_within_bounds_and_selects(self):
        state = self.setup.AppState(dict(self.values))
        result = self.press(state, self.down_to(state, "envy.vscode.mode"), ENTER, DOWN, DOWN, ENTER, "s")
        self.assertEqual(result.values["envy.vscode.mode"], "local")

    def test_quit_from_list_returns_no_result(self):
        state = self.setup.AppState(dict(self.values))
        self.assertIsNone(self.press(state, DOWN, "q"))

    def test_policy_toggle_is_saved_after_returning_to_the_list(self):
        result = self.press(self.policy_state(), "p", DOWN, " ", "p", "s")
        self.assertEqual(result.exclusions["nix.user.package"], [])

    def test_policy_reset_restores_original_exclusions(self):
        result = self.press(self.policy_state(), "p", " ", "r", ESCAPE, "s")
        self.assertEqual(result.exclusions["nix.user.package"], ["okular"])

    def test_policy_search_filters_before_toggle(self):
        result = self.press(self.policy_state(), "p", "/", "okul", ENTER, " ", "p", "s")
        self.assertEqual(result.exclusions["nix.user.package"], [])

    def test_policy_arrows_switch_and_wrap_groups(self):
        manifest = _manifest("darwin", {
            "homebrew.system.formula": _group(
                "envy.darwin.software.homebrew.formulae", "Homebrew formulae",
                ["gh"], [], ["gh"],
            ),
            "homebrew.system.cask": _group(
                "envy.darwin.software.homebrew.casks", "Homebrew casks",
                ["iterm2"], [], ["iterm2"],
            ),
        })
        state = self.setup.AppState(dict(self.values), manifest, {})
        item = {"homebrew.system.formula": "gh", "homebrew.system.cask": "iterm2"}
        first, second = (group.key for group in state.policy_groups)
        # Right moves to the second group; three lefts pass through the wrap
        # back to the first group.
        result = self.press(state, "p", RIGHT, " ", LEFT, LEFT, LEFT, " ", "p", "s")
        self.assertEqual(result.exclusions[first], [item[first]])
        self.assertEqual(result.exclusions[second], [item[second]])

    def test_quit_from_policy_returns_no_result(self):
        self.assertIsNone(self.press(self.policy_state(), "p", "q"))


class SetupMainFlowTests(unittest.TestCase):
    """Exercise main's phases with the terminal UI and file writers replaced."""

    def setUp(self):
        self.setup = _load_setup_module()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.machine = self.root / "machine.nix"
        self.age_key = self.root / "keys.txt"
        self.age_key.write_text("key")
        self.key = SimpleNamespace(public_key="age1test", cleanup=Mock())
        self.exit_handlers = []
        self.initial_values = None
        self.result = None
        self.machine_config = {field.path: "x" for field in self.setup.MACHINE_FIELDS}
        self.save_all = Mock()
        self.prompts = {"confirm": True, "apply": False}
        self.changed = True
        replacements = {
            "current_machine_id": lambda: "test-machine",
            "machine_file": lambda machine_id=None: self.machine,
            "read_secrets_data": lambda: ({}, True),
            "prepare_setup_age_key": lambda: self.key,
            "ensure_machine_configuration": self.ensure_machine,
            "machine_manifest": lambda: None,
            "read_managed_exclusions": lambda *args, **kwargs: {},
            "read_machine_nix": lambda: self.machine_config,
            "manifest_settings": lambda manifest: {},
            "menuconfig_loop": self.menuconfig,
            "show_changes": lambda *args: self.changed,
            "validate_required_fields": lambda values: True,
            "confirm_save": lambda: self.prompts["confirm"],
            "prompt_yes_no": lambda question: self.prompts["apply"],
            "save_all": self.save_all,
            "AGE_KEY_FILE": self.age_key,
            "read_sops_yaml_keys": lambda: {"test": "age1test"},
        }
        for name, value in replacements.items():
            patcher = patch.object(self.setup, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.setup.atexit, "register", self.exit_handlers.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"ENVY_SETUP_RESET_SECRETS": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def ensure_machine(self, machine_id):
        if not self.machine.exists():
            self.machine.write_text("{ ... }:\n{\n}\n")
        return True

    def menuconfig(self, values, manifest, exclusions):
        self.initial_values = dict(values)
        if self.result == "unchanged":
            return self.setup.SetupResult(values=dict(values), exclusions=dict(exclusions))
        return self.result

    def run_main(self):
        code = self.setup.main()
        for handler in self.exit_handlers:
            handler()
        return code

    def test_quit_discards_a_machine_created_for_setup(self):
        self.assertEqual(self.run_main(), 2)
        self.assertFalse(self.machine.exists())
        self.key.cleanup.assert_called()
        self.save_all.assert_not_called()

    def test_no_changes_keep_an_existing_machine_and_skip_saving(self):
        self.machine.write_text("existing")
        self.result = "unchanged"
        self.changed = False
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.machine.read_text(), "existing")
        self.key.cleanup.assert_called()
        self.save_all.assert_not_called()

    def test_missing_machine_fields_force_a_save_without_edits(self):
        self.machine.write_text("existing")
        self.machine_config = {}
        self.result = "unchanged"
        self.changed = False
        self.assertEqual(self.run_main(), 0)
        self.save_all.assert_called_once()

    def test_unregistered_key_forces_a_save_without_edits(self):
        self.machine.write_text("existing")
        self.result = "unchanged"
        self.changed = False
        with patch.object(self.setup, "read_sops_yaml_keys", lambda: {}):
            self.assertEqual(self.run_main(), 0)
        self.save_all.assert_called_once()

    def test_save_passes_edits_and_keeps_the_created_machine(self):
        self.result = self.setup.SetupResult(values={"envy.git.name": "New"}, exclusions={"g": []})
        self.assertEqual(self.run_main(), 0)
        args, kwargs = self.save_all.call_args
        self.assertEqual(args, ({"envy.git.name": "New"}, {"g": []}, "{ ... }:\n{\n}\n"))
        self.assertEqual(kwargs["replace_secrets"], False)
        self.assertIs(kwargs["prepared_key"], self.key)
        self.assertEqual(kwargs["provided_secrets"], {})
        self.assertTrue(kwargs["machine_created_for_setup"])
        self.assertTrue(self.machine.exists())

    def test_save_failure_discards_the_created_machine(self):
        self.result = self.setup.SetupResult(values={}, exclusions={})
        self.save_all.side_effect = OSError("disk full")
        self.assertEqual(self.run_main(), 1)
        self.assertFalse(self.machine.exists())

    def test_declined_confirmation_cancels_without_saving(self):
        self.result = self.setup.SetupResult(values={}, exclusions={})
        self.prompts["confirm"] = False
        self.assertEqual(self.run_main(), 2)
        self.key.cleanup.assert_called()
        self.assertFalse(self.machine.exists())
        self.save_all.assert_not_called()

    def test_invalid_required_fields_stop_before_saving(self):
        self.result = self.setup.SetupResult(values={}, exclusions={})
        with patch.object(self.setup, "validate_required_fields", lambda values: False):
            self.assertEqual(self.run_main(), 1)
        self.save_all.assert_not_called()

    def test_accepted_apply_runs_envy_apply_after_saving(self):
        self.result = self.setup.SetupResult(values={}, exclusions={})
        self.prompts["apply"] = True
        with patch("envy.main.cmd_apply") as apply:
            self.assertEqual(self.run_main(), 0)
        apply.assert_called_once_with()

    def test_reset_blanks_every_secret_field(self):
        with patch.dict(os.environ, {"ENVY_SETUP_RESET_SECRETS": "1"}), patch.object(
            self.setup, "prepare_generated_device_age_key", lambda: self.key
        ), patch.object(self.setup, "read_secrets_data", Mock(side_effect=AssertionError("read"))):
            self.assertEqual(self.run_main(), 2)
        secrets = {field.path for field in self.setup.SECRET_FIELDS}
        shown = {path: value for path, value in self.initial_values.items() if path in secrets}
        self.assertTrue(shown)
        self.assertEqual(set(shown.values()), {""})

    def test_cancelled_decrypt_recovery_stops_before_the_machine_phase(self):
        secrets = self.root / "secrets.yaml"
        secrets.write_text("encrypted")
        ensure = Mock()
        with patch.object(self.setup, "SECRETS_FILE", secrets), patch.object(
            self.setup, "read_secrets_data", lambda: ({}, False)
        ), patch.object(
            self.setup, "handle_decrypt_failure", lambda: (None, {}, False, True)
        ), patch.object(self.setup, "ensure_machine_configuration", ensure):
            self.assertEqual(self.run_main(), 2)
        ensure.assert_not_called()

    def test_key_preparation_failure_returns_an_error(self):
        with patch.object(self.setup, "prepare_setup_age_key", Mock(side_effect=RuntimeError("no key"))):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(self.machine.exists())


if __name__ == "__main__":
    unittest.main()

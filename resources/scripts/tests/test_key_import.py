"""Key import, recovery commands, and key reports against fake, or real, age and sops."""

import stat
import subprocess
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from envy import main, utils
from envy.keys import importing, operations, prompts, rotation
from envy.keys import recovery as key_recovery
from envy.keys.storage import PreparedDeviceKey

from key_harness import KeyWorld, real_tools_available


SECRETS = {"home": {"passwd": "hunter2"}}


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class KeyWorldTest(unittest.TestCase):
    real = False

    def setUp(self):
        self.world = KeyWorld(self, real=self.real)
        self.world.build()

    def key_file(self, name, identity):
        path = self.world.inputs / name
        path.write_text(f"# imported\n{identity.private}\n")
        return path

    def label_of(self, public):
        return next(label for label, value in self.world.keys().items() if value == public)


class ImportScenarios:
    def test_a_new_device_registers_and_is_resealed_by_an_authorized_one(self):
        world = self.world
        world.build(local=None, label=None)
        tablet = world.identity("tablet")
        before = world.snapshot()

        self.assertEqual(importing.key_import(age_path=str(self.key_file("tablet.txt", tablet))), tablet.public)

        self.assertEqual(world.local_publics(), [tablet.public])
        self.assertEqual(mode(world.age_key), 0o600)
        self.assertIn(tablet.public, world.keys().values())
        # It cannot reseal ciphertext it cannot read, so both stay unchanged.
        self.assertEqual(world.snapshot()["SECRETS_FILE"], before["SECRETS_FILE"])
        self.assertEqual(world.snapshot()["RECOVERY_KEY_FILE"], before["RECOVERY_KEY_FILE"])
        self.assertIn("cannot decrypt existing secrets.yaml", world.output.getvalue())
        self.assertEqual(world.leftovers(), [])

        world.install(world.identity("laptop"))
        operations.run_sops_updatekeys()
        operations.reencrypt_recovery_key(world.keys())

        self.assertEqual(world.read_secrets(tablet), SECRETS)
        self.assertEqual(world.read_recovery(tablet), world.identity("recovery").private)
        world.assert_consistent(self)

    def test_a_key_generated_on_a_registered_device_revokes_the_old_key(self):
        world = self.world
        old = world.identity("laptop")

        with patch.object(prompts, "confirm", return_value=True) as confirm:
            public = importing.key_import(generate=True)

        new = world.generated[-1]
        self.assertEqual(public, new.public)
        self.assertIn("registered as 'laptop'", confirm.call_args.args[0])
        self.assertEqual(world.local_publics(), [new.public])
        # The new key takes over the label in place; the old key loses access.
        self.assertEqual(world.keys(), {
            "laptop": new.public,
            "desk": world.identity("desk").public,
            "recovery": world.identity("recovery").public,
        })
        self.assertEqual(utils.read_device_metadata()["sops_label"], "laptop")
        self.assertFalse(world.can_open(world.secrets, old))
        self.assertFalse(world.can_open(world.recovery_file, old))
        for identity in (new, world.identity("desk")):
            self.assertEqual(world.read_secrets(identity), SECRETS)
            self.assertEqual(world.read_recovery(identity), world.identity("recovery").private)
        self.assertEqual(mode(world.age_key.with_name("keys.txt.bak")), 0o600)
        world.assert_consistent(self)
        self.assertEqual(world.leftovers(), [])

    def test_an_imported_key_can_take_over_under_a_new_label(self):
        world = self.world
        old = world.identity("laptop")
        tablet = world.identity("tablet")

        with patch.object(prompts, "confirm", return_value=True):
            public = importing.key_import(age_path=str(self.key_file("tablet.txt", tablet)), label="Tablet")

        self.assertEqual(public, tablet.public)
        self.assertEqual(list(world.keys()), ["tablet", "desk", "recovery"])
        self.assertEqual(world.keys()["tablet"], tablet.public)
        self.assertNotIn(old.public, world.keys().values())
        self.assertEqual(utils.read_device_metadata()["sops_label"], "tablet")
        self.assertIn("previous device key revoked", world.output.getvalue())
        world.assert_consistent(self)
        self.assertEqual(world.leftovers(), [])

    def test_importing_another_registered_key_leaves_the_old_label_to_the_user(self):
        world = self.world
        laptop = world.identity("laptop")
        desk = world.identity("desk")

        with patch.object(prompts, "confirm", return_value=True):
            public = importing.key_import(age_path=str(self.key_file("desk.txt", desk)))

        self.assertEqual(public, desk.public)
        self.assertEqual(world.keys()["laptop"], laptop.public)
        self.assertEqual(utils.read_device_metadata()["sops_label"], "desk")
        output = " ".join(world.output.getvalue().split())
        self.assertIn("still registered as 'laptop'", output)
        self.assertIn("envy key rm laptop", output)
        world.assert_consistent(self)

    def test_a_given_recovery_key_reseals_a_document_this_device_cannot_open(self):
        world = self.world
        laptop = world.identity("laptop")
        recovery = world.identity("recovery")
        key_recovery.write_encrypted_recovery_key(recovery.private, [world.identity("desk").public])
        recovery_file = self.key_file("recovery.txt", recovery)
        before = world.snapshot()

        with self.assertRaises(subprocess.CalledProcessError):
            rotation.key_seal_recovery()
        self.assertEqual(world.snapshot(), before)

        rotation.key_seal_recovery(str(recovery_file))

        for identity in (laptop, world.identity("desk")):
            self.assertEqual(world.read_recovery(identity), recovery.private)
        world.assert_consistent(self)
        self.assertEqual(world.leftovers(), [])

    def test_preparing_without_registering_leaves_the_device_untouched(self):
        world = self.world
        world.build(local=None, label=None)
        before = world.snapshot()

        prepared = importing.key_import(generate=True, register=False)

        self.assertIsInstance(prepared, PreparedDeviceKey)
        self.assertEqual(prepared.public_key, world.generated[-1].public)
        self.assertEqual(mode(prepared.path), 0o600)
        self.assertEqual(world.snapshot(), before)
        prepared.cleanup()
        self.assertEqual(world.leftovers(), [])

    def test_add_seal_and_recover_the_recovery_key(self):
        world = self.world
        world.build(recovery=False)

        rotation.key_add_recovery()

        recovery = world.generated[-1]
        self.assertEqual(world.keys()["recovery"], recovery.public)
        self.assertEqual(world.read_secrets(recovery), SECRETS)
        for device in ("laptop", "desk"):
            self.assertEqual(world.read_recovery(world.identity(device)), recovery.private)

        output = world.inputs / "recovered.txt"
        rotation.key_recover_recovery(str(output))
        self.assertEqual(output.read_text().strip(), recovery.private)
        self.assertEqual(mode(output), 0o600)

        world.recovery_file.unlink()
        rotation.key_seal_recovery(str(output))
        self.assertEqual(world.read_recovery(world.identity("desk")), recovery.private)
        world.assert_consistent(self)
        output.unlink()
        self.assertEqual(world.leftovers(), [])


class FakeImportTests(ImportScenarios, KeyWorldTest):
    def test_declining_to_overwrite_the_device_key_changes_nothing(self):
        world = self.world
        before = world.snapshot()

        with patch.object(prompts, "confirm", return_value=False):
            self.assertIsNone(importing.key_import(generate=True))

        self.assertEqual(world.snapshot(), before)
        self.assertEqual(world.generated, [])
        self.assertEqual(world.leftovers(), [])

    def test_ssh_keys_are_converted_to_age_identities(self):
        world = self.world
        world.build(local=None, label=None)
        ssh_key = world.inputs / "id_ed25519"
        ssh_key.write_text("FAKE-SSH-KEY tablet\n")

        self.assertEqual(importing.key_import(ssh_path=str(ssh_key)), world.identity("tablet").public)

        self.assertEqual(world.local_publics(), [world.identity("tablet").public])
        self.assertIn(["ssh-to-age", "-private-key", "-i", str(ssh_key)], world.calls)

    def test_missing_or_malformed_key_files_are_rejected(self):
        world = self.world
        world.build(local=None, label=None)
        malformed = world.inputs / "malformed.txt"
        malformed.write_text("not a key\n")
        before = world.snapshot()

        for arguments in (
            {"age_path": str(world.inputs / "missing.txt")},
            {"age_path": str(malformed)},
            {"ssh_path": str(world.inputs / "missing_ssh")},
        ):
            with self.subTest(arguments=arguments):
                self.assertIsNone(importing.key_import(**arguments))
                self.assertEqual(world.snapshot(), before)
        self.assertEqual(world.leftovers(), [])

    def test_a_replacing_import_refuses_what_it_cannot_complete(self):
        world = self.world
        tablet = self.key_file("tablet.txt", world.identity("tablet"))
        others = [world.identity("desk").public, world.identity("recovery").public]

        def unsealed_recovery():
            key_recovery.write_encrypted_recovery_key(world.identity("recovery").private, others)

        def unsealed_secrets():
            world._write_sops(world.secrets, others, "home:\n  passwd: hunter2\n")

        for label, prepare, error, message in (
            ("desk", None, ValueError, "belongs to another age key"),
            ("recovery", None, ValueError, "reserved"),
            # The old key could keep access to what it cannot re-encrypt.
            (None, unsealed_recovery, RuntimeError, "cannot open recovery-key.age"),
            (None, unsealed_secrets, RuntimeError, "cannot open secrets.yaml"),
        ):
            with self.subTest(message=message):
                world.build()
                if prepare:
                    prepare()
                before = world.snapshot()
                with patch.object(prompts, "confirm", return_value=True):
                    with self.assertRaisesRegex(error, message):
                        importing.key_import(age_path=str(tablet), label=label)
                self.assertEqual(world.snapshot(), before)
                self.assertEqual(world.leftovers(), [])

    def test_each_failed_step_of_a_replacing_import_rolls_back(self):
        world = self.world
        with patch.object(prompts, "confirm", return_value=True):
            importing.key_import(generate=True)
        steps = sum(call[0] in {"age", "age-keygen", "sops"} for call in world.calls)
        self.assertGreaterEqual(steps, 6)

        for step in range(1, steps + 1):
            world.build()
            before = world.snapshot()
            world.fail_at = step
            with self.subTest(step=step):
                try:
                    with patch.object(prompts, "confirm", return_value=True):
                        importing.key_import(generate=True)
                except (subprocess.CalledProcessError, RuntimeError, ValueError):
                    self.assertEqual(world.snapshot(), before)
                else:
                    # Only a probe of the old key may fail; the import then
                    # cannot tell which recipient it replaces.
                    self.assertEqual(world.failed[:2], ["age-keygen", "-y"])
                world.fail_at = None
                self.assertEqual(world.leftovers(), [])
                world.assert_consistent(self)

    def test_the_recovery_identity_is_refused_as_a_device_key(self):
        world = self.world
        world.build(local=None, label=None)
        before = world.snapshot()

        with self.assertRaisesRegex(ValueError, "duplicate age public key"):
            importing.key_import(age_path=str(self.key_file("recovery.txt", world.identity("recovery"))))

        self.assertEqual(world.snapshot(), before)
        self.assertEqual(world.leftovers(), [])

    def test_the_interactive_menu_dispatches_each_choice(self):
        world = self.world
        usb = self.key_file("usb.txt", world.identity("usb"))
        manual = self.key_file("manual.txt", world.identity("manual"))
        cases = [
            (None, None, None),
            (("quit", ""), None, None),
            (("usb", str(usb)), None, world.identity("usb").public),
            (("age_manual", ""), str(manual), world.identity("manual").public),
            (("ssh_manual", ""), EOFError(), None),
            (("generate", ""), None, "generated"),
        ]
        for choice, typed, expected in cases:
            with self.subTest(choice=choice):
                world.build(local=None, label=None)
                with patch.object(prompts, "scan_usb_key_files", return_value=[usb]), patch.object(
                    prompts, "_select_menu", return_value=choice
                ) as menu, patch.object(prompts, "prompt_input", side_effect=[typed]):
                    result = importing.key_import()
                if expected == "generated":
                    expected = world.generated[-1].public
                self.assertEqual(result, expected)
                items = menu.call_args.args[0]
                self.assertEqual(items[0][1:], ("usb", str(usb)))
                self.assertEqual([item[1] for item in items[1:]], ["age_manual", "ssh_manual", "generate", "quit"])

    def test_recovery_commands_refuse_unsafe_inputs(self):
        world = self.world
        stranger = self.key_file("stranger.txt", world.identity("stranger"))
        not_a_key = world.inputs / "notes.txt"
        not_a_key.write_text("# no identity here\n")
        cases = [
            # Without a secrets update, only this check stops a stranger from
            # adding a recovery recipient.
            ("add with an unauthorized device", {"recovery": False},
             lambda: world.install(world.identity("stranger")),
             lambda: rotation.key_add_recovery(reencrypt_secrets=False), RuntimeError, "authorized local device"),
            ("seal a key that is not the recovery key", {"sealed": False}, None,
             lambda: rotation.key_seal_recovery(str(stranger)), ValueError, "does not match"),
            ("seal a missing file", {"sealed": False}, None,
             lambda: rotation.key_seal_recovery(str(world.inputs / "missing")), FileNotFoundError, "not found"),
            # A sealed document must not make a given key file go unchecked.
            ("seal a missing file over a sealed key", {}, None,
             lambda: rotation.key_seal_recovery(str(world.inputs / "missing")), FileNotFoundError, "not found"),
            ("seal a key that is not the recovery key over a sealed key", {}, None,
             lambda: rotation.key_seal_recovery(str(stranger)), ValueError, "does not match"),
            ("seal a file without a key over a sealed key", {}, None,
             lambda: rotation.key_seal_recovery(str(not_a_key)), ValueError, "does not contain"),
            ("seal from a device key", {"sealed": False}, None, rotation.key_seal_recovery, RuntimeError,
             "not the recovery key"),
            ("seal without a recovery key", {"recovery": False}, None, rotation.key_seal_recovery, RuntimeError,
             "no recovery key"),
            ("recover without recovery-key.age", {"sealed": False}, None, rotation.key_recover_recovery,
             FileNotFoundError, "no recovery-key.age"),
            ("recover without a device key", {"local": None}, None, rotation.key_recover_recovery, RuntimeError,
             "no age key"),
        ]
        for name, build, prepare, operation, error, message in cases:
            with self.subTest(name):
                world.build(**build)
                if prepare:
                    prepare()
                before = world.snapshot()
                with self.assertRaisesRegex(error, message):
                    operation()
                self.assertEqual(world.snapshot(), before)

    def test_existing_recovery_material_is_kept_or_resealed(self):
        from envy.keys import storage

        world = self.world
        before = world.snapshot()
        rotation.key_add_recovery()
        self.assertEqual(world.snapshot(), before)

        keys = world.keys()
        keys["tablet"] = world.identity("tablet").public
        storage.write_sops_yaml_keys(keys)
        rotation.key_seal_recovery()

        self.assertEqual(world.read_recovery(world.identity("tablet")), world.identity("recovery").private)

    def test_recovered_key_is_printed_only_when_no_file_is_given(self):
        world = self.world
        rotation.key_recover_recovery()
        self.assertIn(world.identity("recovery").private, world.output.getvalue())

    def test_list_and_status_report_access_without_private_keys(self):
        world = self.world
        for prepare, expected in (
            (None, ["THIS DEVICE", "RECOVERY", "remote", "current device CAN decrypt"]),
            (lambda: world.install(world.identity("stranger")), ["NOT in .sops.yaml"]),
            (lambda: world.age_key.unlink(), ["no age key on this device"]),
        ):
            with self.subTest(expected=expected):
                world.build()
                if prepare:
                    prepare()
                world.output.truncate(0)
                operations.key_list()
                for text in expected:
                    self.assertIn(text, world.output.getvalue())
                self.assertNotIn("AGE-SECRET-KEY", world.output.getvalue())

        for prepare, decrypts in ((None, "YES"), (lambda: world.install(world.identity("stranger")), "NO")):
            with self.subTest(decrypts=decrypts):
                world.build()
                if prepare:
                    prepare()
                world.output.seek(0)
                world.output.truncate(0)
                operations.key_status()
                rows = {line.split("│")[1].strip(): line.split("│")[2].strip()
                        for line in world.output.getvalue().splitlines() if line.count("│") >= 3}
                self.assertEqual(rows["Can decrypt"], decrypts)
                self.assertEqual(rows["Recovery key"], "YES")
                self.assertNotIn("AGE-SECRET-KEY", world.output.getvalue())

    def test_export_writes_private_copies_or_prints(self):
        world = self.world
        laptop = world.identity("laptop")
        exported = world.inputs / "exported.txt"

        operations.key_export("age", str(exported))
        self.assertEqual(exported.read_text(), laptop.private + "\n")
        self.assertEqual(mode(exported), 0o600)

        ssh_copy = world.inputs / "ssh-copy"
        operations.key_export("ssh", str(ssh_copy))
        self.assertFalse(ssh_copy.exists())
        self.assertIn("no SSH key found", world.output.getvalue())

        ssh_key = world.home / ".ssh" / "id_ed25519"
        ssh_key.parent.mkdir()
        ssh_key.write_text("FAKE-SSH-KEY laptop\n")
        operations.key_export("ssh", str(ssh_copy))
        self.assertEqual(ssh_copy.read_text(), "FAKE-SSH-KEY laptop\n")
        self.assertEqual(mode(ssh_copy), 0o600)

        world.output.truncate(0)
        operations.key_export()
        self.assertIn(laptop.private, world.output.getvalue())

        with self.assertRaises(ValueError):
            operations.key_export("pem")
        world.age_key.unlink()
        with self.assertRaises(RuntimeError):
            operations.key_export()

    def test_cli_import_and_recovery_commands(self):
        world = self.world
        runner = CliRunner()
        world.build(local=None, label=None)
        tablet = self.key_file("tablet.txt", world.identity("tablet"))

        self.assertEqual(runner.invoke(main.cli, ["key", "import", "--age", str(tablet)]).exit_code, 0)
        self.assertEqual(world.local_publics(), [world.identity("tablet").public])
        self.assertEqual(runner.invoke(main.cli, ["key", "im", "--age", str(world.inputs / "x")]).exit_code, 1)
        with patch.object(prompts, "_select_menu", return_value=None):
            self.assertEqual(runner.invoke(main.cli, ["key", "import"]).exit_code, 2)

        world.build(recovery=False)
        self.assertEqual(runner.invoke(main.cli, ["key", "ar"]).exit_code, 0)
        saved = world.inputs / "recovery.txt"
        self.assertEqual(runner.invoke(main.cli, ["key", "rr", "-o", str(saved)]).exit_code, 0)
        world.recovery_file.unlink()
        self.assertEqual(runner.invoke(main.cli, ["key", "sr", str(world.inputs / "missing")]).exit_code, 1)
        self.assertFalse(world.recovery_file.exists())
        self.assertEqual(runner.invoke(main.cli, ["key", "sr", str(saved)]).exit_code, 0)
        for args in (["key", "ls"], ["key", "st"]):
            self.assertEqual(runner.invoke(main.cli, args).exit_code, 0)
        world.assert_consistent(self)


class HarnessIsolationTests(KeyWorldTest):
    def test_every_key_path_and_command_is_redirected(self):
        import sys

        from envy import utils

        base = self.world.root.parent
        for module_name, module in sys.modules.items():
            if module is not None and module_name.startswith("envy"):
                for name in ("ENVY_ROOT", "AGE_KEY_FILE", "SOPS_YAML", "SECRETS_FILE", "RECOVERY_KEY_FILE"):
                    value = getattr(module, name, None)
                    if value is not None:
                        self.assertTrue(value.is_relative_to(base), f"{module_name}.{name}")
                if hasattr(module, "run_cmd"):
                    self.assertIsNot(module.run_cmd, self.world._real_run, module_name)
        with self.assertRaises(AssertionError):
            utils.run_cmd(["git", "status"])


class SelectMenuTests(unittest.TestCase):
    def choose(self, keys):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        items = [([("", "First")], "first", 1), ([("usb", "USB "), ("", "Second")], "second", 2)]
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            return prompts._select_menu(items, title="Pick")

    def test_arrows_move_and_enter_confirms(self):
        self.assertEqual(self.choose("\r"), ("first", 1))
        self.assertEqual(self.choose("\x1b[B\x1b[B\r"), ("second", 2))
        self.assertEqual(self.choose("\x1b[B\x1b[A\x1b[A\r"), ("first", 1))

    def test_q_cancels(self):
        self.assertIsNone(self.choose("\x1b[Bq"))


@unittest.skipUnless(real_tools_available(), "age and sops are not installed")
class RealToolImportTests(ImportScenarios, KeyWorldTest):
    real = True


if __name__ == "__main__":
    unittest.main()

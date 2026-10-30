"""Key add/remove/export and transaction behavior without age, sops, or Git."""

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer

from envy import key
from envy.git_safety import SecretSafetyError
from envy.keys import storage


DEVICE = "age1" + "q" * 58
OTHER = "age1" + "p" * 58
RECOVERY = "age1" + "z" * 58
NEW = "age1" + "r" * 58


class KeyRepository(unittest.TestCase):
    """A temporary checkout whose key files replace the real ones."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sops = self.root / ".sops.yaml"
        self.secret = self.root / "secrets" / "secrets.yaml"
        self.recovery = self.root / "secrets" / "recovery-key.age"
        self.age_dir = self.root / "age"
        self.age_key = self.age_dir / "keys.txt"
        self.label = self.root / ".device-label"
        self.secret.parent.mkdir()
        self.secret.write_text("ciphertext\n")
        self.recovery.write_text("recovery ciphertext\n")

        def start(patcher):
            self.addCleanup(patcher.stop)
            return patcher.start()

        start(patch.multiple(
            key,
            ENVY_ROOT=self.root,
            SOPS_YAML=self.sops,
            SECRETS_FILE=self.secret,
            RECOVERY_KEY_FILE=self.recovery,
            AGE_KEY_FILE=self.age_key,
            AGE_KEY_DIR=self.age_dir,
            DEVICE_LABEL_FILE=self.label,
        ))
        start(patch.object(storage, "SOPS_YAML", self.sops))
        self.age = start(patch.object(key, "run_cmd", return_value=""))
        self.reencrypt = start(patch.object(key, "_reencrypt_recovery_key_with"))
        self.updatekeys = start(patch.object(key, "run_sops_updatekeys"))
        self.current = start(patch.object(key, "get_current_device_public_key", return_value=DEVICE))
        storage.write_sops_yaml_keys({"device": DEVICE, "other": OTHER, "recovery": RECOVERY})
        self.original = self.sops.read_bytes()

    def keys(self):
        return storage.read_sops_yaml_keys()


class KeyAddTests(KeyRepository):
    def test_invalid_reserved_and_duplicate_labels_change_nothing(self):
        for pubkey, label, message in (
            ("ssh-ed25519 AAAA", None, "invalid age public key"),
            (NEW, "recovery", "reserved"),
            (NEW, "envy_rotation_old_device", "reserved"),
            (NEW, "other", "already used"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                key.key_add(pubkey, label)
        self.assertEqual(self.sops.read_bytes(), self.original)
        self.age.assert_not_called()

    def test_existing_recipient_is_a_no_op(self):
        key.key_add(OTHER, "again")

        self.assertEqual(self.sops.read_bytes(), self.original)
        self.age.assert_not_called()
        self.updatekeys.assert_not_called()

    def test_recipient_rejected_by_age_is_not_registered(self):
        self.age.side_effect = subprocess.CalledProcessError(1, ["age"])

        with self.assertRaisesRegex(ValueError, "rejected by age"):
            key.key_add(NEW, "laptop")

        self.assertEqual(self.sops.read_bytes(), self.original)
        self.reencrypt.assert_not_called()

    def test_added_recipient_reencrypts_recovery_and_secrets(self):
        key.key_add(NEW, "Work Laptop")

        keys = self.keys()
        label = next(name for name, value in keys.items() if value == NEW)
        self.assertEqual(keys, {"device": DEVICE, "other": OTHER, "recovery": RECOVERY, label: NEW})
        self.reencrypt.assert_called_once_with(keys)
        self.updatekeys.assert_called_once_with()

    def test_failed_reencryption_rolls_back_the_recipient_list(self):
        self.updatekeys.side_effect = SecretSafetyError("cannot decrypt")

        with self.assertRaises(SecretSafetyError):
            key.key_add(NEW, "laptop")

        self.assertEqual(self.sops.read_bytes(), self.original)


class KeyRemoveTests(KeyRepository):
    def test_unknown_recovery_and_marker_labels_are_refused(self):
        for label, message in (
            ("missing", "not found"),
            ("recovery", "cannot remove the recovery key"),
            ("recovery_new", "rotation marker"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                key.key_remove(label)
        self.assertEqual(self.sops.read_bytes(), self.original)

    def test_last_device_key_cannot_be_removed(self):
        storage.write_sops_yaml_keys({"device": DEVICE, "recovery": RECOVERY})
        before = self.sops.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "at least one device key"):
            key.key_remove("device", force=True)

        self.assertEqual(self.sops.read_bytes(), before)

    def test_removing_the_current_device_requires_confirmation(self):
        with patch.object(key, "confirm", return_value=False) as confirm:
            key.key_remove("device")

        confirm.assert_called_once()
        self.assertEqual(self.sops.read_bytes(), self.original)
        self.updatekeys.assert_not_called()

    def test_removed_device_is_excluded_from_every_reencryption(self):
        key.key_remove("other")

        expected = {"device": DEVICE, "recovery": RECOVERY}
        self.assertEqual(self.keys(), expected)
        self.reencrypt.assert_called_once_with(expected)
        self.updatekeys.assert_called_once_with()

    def test_failed_reencryption_restores_the_removed_device(self):
        self.reencrypt.side_effect = RuntimeError("age failed")

        with self.assertRaises(RuntimeError):
            key.key_remove("other")

        self.assertEqual(self.sops.read_bytes(), self.original)


class KeyExportTests(KeyRepository):
    def test_export_requires_a_local_key_and_a_known_format(self):
        with self.assertRaisesRegex(RuntimeError, "no age key"):
            key.key_export("age")
        self.age_dir.mkdir()
        self.age_key.write_text("AGE-SECRET-KEY-TEST\n")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            key.key_export("pem")

    def test_age_export_writes_a_private_file(self):
        self.age_dir.mkdir()
        self.age_key.write_text("AGE-SECRET-KEY-TEST\n")
        output = self.root / "export" / "device.age.txt"
        output.parent.mkdir()

        key.key_export("age", str(output))

        self.assertEqual(output.read_text(), "AGE-SECRET-KEY-TEST\n")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


class KeyOperationTests(KeyRepository):
    def test_failed_operation_exits_without_committing(self):
        def fail():
            raise ValueError("bad label")

        with patch.object(key, "git_commit_sops_files") as commit:
            with self.assertRaises(typer.Exit) as raised:
                key._run_key_operation("add", fail)

        self.assertEqual(raised.exception.exit_code, 1)
        commit.assert_not_called()

    def test_only_changed_key_files_are_committed(self):
        with patch.object(key, "git_commit_sops_files") as commit:
            self.assertEqual(key._run_key_operation("noop", lambda: "same"), "same")
            commit.assert_not_called()
            key._run_key_operation("remove", key.key_remove, "other")
        commit.assert_called_once_with("remove")

    def test_registering_an_existing_recipient_reuses_its_label(self):
        with patch.object(key, "set_sops_label") as set_label:
            self.assertEqual(key.register_device_key(OTHER), "other")
        set_label.assert_called_once_with("other")
        self.assertEqual(self.sops.read_bytes(), self.original)

    def test_registration_never_takes_over_a_reserved_or_foreign_label(self):
        with patch.object(key, "set_sops_label") as set_label:
            with self.assertRaisesRegex(ValueError, "reserved"):
                key.register_device_key(NEW, "recovery")
            with self.assertRaisesRegex(ValueError, "belongs to another"):
                key.register_device_key(NEW, "other")
        set_label.assert_not_called()
        self.assertEqual(self.sops.read_bytes(), self.original)


class UpdateKeysTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.secret = Path(temporary.name) / "secrets.yaml"
        patcher = patch.object(key, "SECRETS_FILE", self.secret)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_secrets_file_is_skipped(self):
        with patch("envy.config.write_secrets_data") as write:
            key.run_sops_updatekeys()
        write.assert_not_called()

    def test_plaintext_or_undecryptable_secrets_fail_closed(self):
        self.secret.write_text("token: plain\n")
        with patch("envy.config.write_secrets_data") as write:
            with self.assertRaisesRegex(SecretSafetyError, "not sops-encrypted"):
                key.run_sops_updatekeys()
            with patch.object(key, "is_sops_encrypted", return_value=True), patch(
                "envy.config.read_secrets_data", return_value=({}, False)
            ):
                with self.assertRaisesRegex(SecretSafetyError, "cannot decrypt"):
                    key.run_sops_updatekeys()
        write.assert_not_called()

    def test_decrypted_secrets_are_rewritten_for_the_current_recipients(self):
        self.secret.write_text("ciphertext\n")
        with patch.object(key, "is_sops_encrypted", return_value=True), patch(
            "envy.config.read_secrets_data", return_value=({"home": {"passwd": "x"}}, True)
        ), patch("envy.config.write_secrets_data") as write:
            key.run_sops_updatekeys()
        write.assert_called_once_with({"home": {"passwd": "x"}})


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy import key
from envy.keys import identity, storage
from envy.transaction import FileTransaction, transaction_lock
from envy import utils


class KeyLifecycleTests(unittest.TestCase):
    def test_sops_recipient_parser_ignores_comments_and_preserves_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".sops.yaml"
            path.write_text(
                "keys:\n"
                "  # - &commented age1commented\n"
                "  - &device age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "  - &recovery age1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
                "\ncreation_rules:\n"
                "  - path_regex: secrets/secrets\\.yaml$\n"
                "    key_groups:\n"
                "      - age: [*device, *recovery]\n"
            )
            with patch.object(storage, "SOPS_YAML", path):
                self.assertEqual(
                    storage.read_sops_yaml_keys(),
                    {
                        "device": "age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "recovery": "age1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    },
                )

    def test_stale_label_cannot_be_used_when_local_key_is_unregistered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = root / ".device-label"
            sops_file = root / ".sops.yaml"
            metadata.write_text(
                'version = 1\n\n[device]\nsops_label = "old_device"\n'
            )
            sops_file.write_text(
                "keys:\n"
                "  - &old_device age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "  - &other age1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            )
            with patch.object(storage, "SOPS_YAML", sops_file), patch.object(
                utils, "DEVICE_LABEL_FILE", metadata
            ), patch.object(identity, "current_device_public_key", return_value="age1cccccccccccccccccccccccccccccccccccccccccccccccccccccccc"):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    identity.ensure_sops_label(allow_new=False)

    def test_registered_key_recovers_label_from_unique_public_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = root / ".device-label"
            sops_file = root / ".sops.yaml"
            metadata.write_text('version = 1\n\n[device]\nsops_label = "stale"\n')
            sops_file.write_text(
                "keys:\n"
                "  - &device age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            )
            with patch.object(storage, "SOPS_YAML", sops_file), patch.object(
                utils, "DEVICE_LABEL_FILE", metadata
            ), patch.object(identity, "current_device_public_key", return_value="age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"):
                self.assertEqual(identity.ensure_sops_label(allow_new=False), "device")
            self.assertIn('sops_label = "device"', metadata.read_text())

    def test_transaction_can_force_a_new_path_to_roll_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "new-file"
            with FileTransaction([path], missing_paths=[path]):
                path.write_text("temporary")
            self.assertFalse(path.exists())

    def test_transaction_lock_is_reentrant(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "lock"
            with transaction_lock(lock):
                with transaction_lock(lock):
                    self.assertTrue(lock.exists())

    def test_key_remove_rejects_rotation_marker(self):
        with patch.object(key, "read_sops_yaml_keys", return_value={
            "envy_rotation_old_device": "age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }):
            with self.assertRaisesRegex(ValueError, "rotation marker"):
                key.key_remove("envy_rotation_old_device")

    def test_run_cmd_pins_missing_age_file_instead_of_using_ambient_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-keys.txt"
            with patch.object(utils, "AGE_KEY_FILE", missing), patch.object(
                utils, "run_process", return_value=type("Result", (), {"stdout": "", "returncode": 0})()
            ) as process:
                utils.run_cmd(["sops", "--version"])
            self.assertEqual(process.call_args.kwargs["env"]["SOPS_AGE_KEY_FILE"], str(missing))


if __name__ == "__main__":
    unittest.main()

"""Secret refinement, encryption, and permission hardening branches of envy.config."""

import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from envy.config import cli, machine_nix, refine, secret_store, updates


CIPHERTEXT = (
    "value: ENC[AES256_GCM,data:x,iv:y,tag:z,type:str]\n"
    "sops:\n"
    "  mac: ENC[AES256_GCM,data:m,iv:y,tag:z,type:str]\n"
    "  age:\n"
    "    - recipient: age1device\n"
    "      enc: x\n"
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def start_config_patch(test: unittest.TestCase, name: str, value) -> None:
    """Patch a name in every envy.config module that looks it up."""
    holders = [module for module in (cli, machine_nix, refine, secret_store, updates) if hasattr(module, name)]
    test.assertTrue(holders, name)
    for module in holders:
        patcher = patch.object(module, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)


class SensitivePermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.age_dir = Path(temporary.name) / "age"
        self.age_dir.mkdir()
        self.age_dir.chmod(0o755)
        self.key = self.age_dir / "keys.txt"
        self.key.write_text("AGE-SECRET-KEY-TEST\n")
        self.key.chmod(0o644)
        self.backup = self.age_dir / "keys.txt.bak"
        self.backup.write_text("AGE-SECRET-KEY-OLD\n")
        self.backup.chmod(0o644)
        for name, value in (("AGE_KEY_DIR", self.age_dir), ("AGE_KEY_FILE", self.key)):
            start_config_patch(self, name, value)

    def test_check_reports_unsafe_modes_without_changing_them(self):
        report = refine.refine_sensitive_permissions(write=False)

        self.assertFalse(report.ok)
        self.assertEqual(
            sorted(report.errors),
            sorted(f"permissions:{path}" for path in (self.age_dir, self.key, self.backup)),
        )
        self.assertEqual((mode(self.age_dir), mode(self.key), mode(self.backup)), (0o755, 0o644, 0o644))

    def test_refine_hardens_the_key_directory_key_and_backups(self):
        report = refine.refine_sensitive_permissions(write=True)

        self.assertTrue(report.ok)
        self.assertTrue(report.changed)
        self.assertEqual((mode(self.age_dir), mode(self.key), mode(self.backup)), (0o700, 0o600, 0o600))
        self.assertFalse(refine.refine_sensitive_permissions(write=True).changed)


class SecretRefineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secrets_dir = self.root / "secrets"
        self.secrets_dir.mkdir()
        self.secret = self.secrets_dir / "secrets.yaml"
        self.secret.write_text(CIPHERTEXT)
        field = next(field for field in refine.SECRET_FIELDS if field.path == "home_passwd")
        self.field = replace(field, required=False, validators=[], default_fn=lambda: "")
        self.data = {}
        self.decrypt_ok = True
        self.credentials = {}
        self.written = []

        start_config_patch(self, "SECRETS_DIR", self.secrets_dir)
        start_config_patch(self, "SECRETS_FILE", self.secret)
        start_config_patch(self, "SECRET_FIELDS", [self.field])
        start_config_patch(self, "OBSOLETE_SECRET_PATHS", ["llm/retired/apikey"])
        start_config_patch(self, "read_secrets_data", lambda **_: (self.data, self.decrypt_ok))
        start_config_patch(self, "read_machine_nix", lambda *_: {})
        start_config_patch(self, "write_secrets_data", lambda data: self.written.append(data))
        credentials = patch("envy.agent.managed_credential_paths", side_effect=lambda: dict(self.credentials))
        credentials.start()
        self.addCleanup(credentials.stop)

    def test_plaintext_leftovers_block_refinement_before_decryption(self):
        (self.secrets_dir / ".secrets-plain-crash.yaml").write_text("token: plain\n")
        with patch.object(secret_store, "read_secrets_data", side_effect=AssertionError("must not decrypt")):
            report = refine.refine_secrets(write=True)

        self.assertEqual(report.errors, ["plaintext-secret-artifact"])
        self.assertEqual(self.written, [])

    def test_plaintext_leftovers_block_secret_writes_before_decryption(self):
        (self.secrets_dir / ".secrets-plain-crash.yaml").write_text("token: plain\n")
        for replace_file in (False, True):
            with self.subTest(replace=replace_file), patch.object(
                secret_store, "read_secrets_data", side_effect=AssertionError("must not decrypt")
            ), self.assertRaisesRegex(RuntimeError, "plaintext secret temporary files remain"):
                updates.set_secret_value("home/passwd", "new", replace=replace_file)
        self.assertEqual(self.written, [])
        self.assertEqual(self.secret.read_text(), CIPHERTEXT)

    def test_unreadable_or_undecryptable_secrets_are_errors(self):
        with patch.object(secret_store, "read_secrets_data", return_value=(None, False)):
            self.assertEqual(refine.refine_secrets(write=True).errors, ["secrets.yaml"])

        self.decrypt_ok = False
        self.assertEqual(refine.refine_secrets(write=True).errors, ["secrets.yaml"])
        self.assertEqual(self.written, [])

    def test_missing_field_is_created_but_a_scalar_parent_is_rejected(self):
        report = refine.refine_secrets(write=True, strict=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.added, ["home/passwd"])
        self.assertEqual(self.written, [{"home": {"passwd": ""}}])

        self.written.clear()
        self.data = {"home": "flat-string"}
        blocked = refine.refine_secrets(write=True, strict=True)
        self.assertIn("home/passwd", blocked.errors)
        self.assertEqual(self.written, [])

    def test_credential_paths_are_validated_created_and_normalized(self):
        self.data = {"home": {"passwd": "set"}, "llm": {"number": {"apikey": 42}}}
        self.credentials = {
            "bad": "../escape",
            "missing": "llm/missing/apikey",
            "number": "llm/number/apikey",
        }

        checked = refine.refine_secrets(write=False)
        self.assertIn("credential:bad", checked.errors)
        self.assertIn("llm/missing/apikey", checked.errors)
        self.assertIn("llm/number/apikey", checked.errors)
        self.assertEqual(self.written, [])

        self.credentials.pop("bad")
        refined = refine.refine_secrets(write=True)
        self.assertTrue(refined.ok, refined.errors)
        self.assertEqual(self.written[-1]["llm"], {"missing": {"apikey": ""}, "number": {"apikey": "42"}})
        self.assertIn("llm/missing/apikey", refined.warnings)

    def test_obsolete_paths_are_reported_until_pruned(self):
        self.data = {"home": {"passwd": "set"}, "llm": {"retired": {"apikey": "old"}, "kept": "yes"}}

        warned = refine.refine_secrets(write=True)
        self.assertIn("llm/retired/apikey", warned.warnings)
        self.assertEqual(self.written, [])

        pruned = refine.refine_secrets(write=True, prune=True)
        self.assertEqual(pruned.removed, ["llm/retired/apikey"])
        self.assertEqual(self.written, [{"home": {"passwd": "set"}, "llm": {"kept": "yes"}}])

    def test_strict_refinement_never_writes_alongside_errors(self):
        self.data = {"llm": {"number": {"apikey": 7}}}
        self.credentials = {"bad": "not a path", "number": "llm/number/apikey"}

        report = refine.refine_secrets(write=True, strict=True)

        self.assertFalse(report.ok)
        self.assertEqual(self.written, [])

    def test_merging_secrets_requires_decryption(self):
        self.decrypt_ok = False
        with self.assertRaisesRegex(RuntimeError, "without decrypting"):
            secret_store.write_secrets_yaml({"home_passwd": "new"})
        secret_store.write_secrets_yaml({"home_passwd": "new"}, replace=True)
        self.assertEqual(self.written, [{"home": {"passwd": "new"}}])


class SecretEncryptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secrets_dir = self.root / "secrets"
        self.secret = self.secrets_dir / "secrets.yaml"
        self.plain_dir = self.root / "tmp"
        self.plain_dir.mkdir()
        for target, value in (
            (patch.object(secret_store, "SECRETS_DIR", self.secrets_dir), None),
            (patch.object(secret_store, "SECRETS_FILE", self.secret), None),
            (patch.dict(os.environ, {"TMPDIR": str(self.plain_dir)}), None),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_encryption_uses_every_recipient_and_leaves_no_plaintext(self):
        seen = {}

        def sops(argv):
            output = Path(argv[argv.index("--output") + 1])
            source = Path(argv[-1])
            seen["argv"] = argv
            seen["plain_mode"] = mode(source)
            seen["plain"] = source.read_text()
            output.write_text(CIPHERTEXT)

        with patch("envy.keys.storage.read_sops_yaml_keys", return_value={"device": "age1a", "recovery": "age1b"}), patch.object(
            secret_store, "run_cmd", side_effect=sops
        ):
            secret_store.write_secrets_data({"home": {"passwd": "plain-value"}})

        self.assertEqual(seen["argv"][:6], ["sops", "--encrypt", "--config", "/dev/null", "--age", "age1a,age1b"])
        # Plaintext is staged outside the checkout, which path: flakes copy.
        self.assertEqual(Path(seen["argv"][-1]).parent, self.plain_dir)
        self.assertEqual(Path(seen["argv"][-2]).parent, self.secrets_dir)
        self.assertEqual(seen["plain_mode"], 0o600)
        self.assertIn("plain-value", seen["plain"])
        self.assertEqual(self.secret.read_text(), CIPHERTEXT)
        self.assertEqual(mode(self.secret), 0o644)
        self.assertEqual(list(self.plain_dir.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.secrets_dir.iterdir()), ["secrets.yaml"])

    def test_unencrypted_sops_output_is_never_published(self):
        self.secrets_dir.mkdir()
        self.secret.write_text(CIPHERTEXT)

        def sops(argv):
            Path(argv[argv.index("--output") + 1]).write_text("home:\n  passwd: plain-value\n")

        with patch("envy.keys.storage.read_sops_yaml_keys", return_value={"device": "age1a"}), patch.object(
            secret_store, "run_cmd", side_effect=sops
        ):
            with self.assertRaisesRegex(RuntimeError, "not encrypted"):
                secret_store.write_secrets_data({"home": {"passwd": "plain-value"}})

        self.assertEqual(self.secret.read_text(), CIPHERTEXT)
        self.assertEqual(list(self.plain_dir.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.secrets_dir.iterdir()), ["secrets.yaml"])

    def test_failed_sops_keeps_the_ciphertext_and_removes_the_plaintext(self):
        self.secrets_dir.mkdir()
        self.secret.write_text(CIPHERTEXT)

        def sops(argv):
            Path(argv[argv.index("--output") + 1]).write_text("partial")
            raise subprocess.CalledProcessError(1, argv)

        with patch("envy.keys.storage.read_sops_yaml_keys", return_value={"device": "age1a"}), patch.object(
            secret_store, "run_cmd", side_effect=sops
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                secret_store.write_secrets_data({"home": {"passwd": "plain-value"}})

        self.assertEqual(self.secret.read_text(), CIPHERTEXT)
        self.assertEqual(list(self.plain_dir.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.secrets_dir.iterdir()), ["secrets.yaml"])

    def test_encryption_requires_a_recipient(self):
        with patch("envy.keys.storage.read_sops_yaml_keys", return_value={}), patch.object(
            secret_store, "run_cmd", side_effect=AssertionError("must not run sops")
        ):
            with self.assertRaisesRegex(RuntimeError, "No age recipients"):
                secret_store.write_secrets_data({})
        self.assertFalse(self.secret.exists())


if __name__ == "__main__":
    unittest.main()

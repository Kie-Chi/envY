import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from envy import config, git_safety, main, sops_format, utils
from envy.doctor.checks import config as doctor_config
from envy.keys import recovery as key_recovery
from envy.process import CommandError, command_display, run_process
from envy.transaction import FileTransaction
from envy.workflows import check as check_workflow
from envy.workflows import update as update_workflow


class SafetyWorkflowTests(unittest.TestCase):
    def test_sensitive_backup_is_created_with_mode_0600(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "keys.txt"
            source.write_text("AGE-SECRET-KEY-TEST\n")
            source.chmod(0o644)
            backup = utils.backup_sensitive_file(source)
            self.assertIsNotNone(backup)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_secret_update_preserves_unknown_paths(self):
        captured = {}
        with tempfile.TemporaryDirectory() as temporary:
            secret = Path(temporary) / "secrets.yaml"
            secret.write_text("encrypted-placeholder")
            with patch.object(config, "SECRETS_FILE", secret), patch.object(
                config, "read_secrets_data", return_value=({"custom": {"keep": "yes"}}, True)
            ), patch.object(config, "write_secrets_data", side_effect=lambda data: captured.update(data)):
                config.write_secrets_yaml({})
        self.assertEqual(captured["custom"]["keep"], "yes")

    def test_failed_secret_encryption_preserves_previous_ciphertext(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "secrets.yaml"
            secret.write_text("value: ENC[AES256_GCM,data:x]\nsops:\n  age: []\n")
            original = secret.read_bytes()
            with patch.object(config, "SECRETS_DIR", root), patch.object(
                config, "SECRETS_FILE", secret
            ), patch("envy.key.read_sops_yaml_keys", return_value={"device": "age1test"}), patch.object(
                config, "run_cmd", side_effect=RuntimeError("encrypt failed")
            ):
                with self.assertRaises(RuntimeError):
                    config.write_secrets_data({"plain": "must-not-land"})
            self.assertEqual(secret.read_bytes(), original)
            self.assertNotIn(b"must-not-land", secret.read_bytes())

    def test_outgoing_plaintext_secret_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "secrets" / "secrets.yaml"
            secret.parent.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            secret.write_text("token: plaintext\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "plain"], cwd=root, check=True)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
            ).stdout.strip()
            with patch.object(git_safety, "ENVY_ROOT", root):
                with self.assertRaises(git_safety.SecretSafetyError):
                    git_safety.assert_outgoing_secrets_encrypted([commit])

    def test_staged_plaintext_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "secrets" / "secrets.yaml"
            secret.parent.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            secret.write_text("token: plaintext\n")
            subprocess.run(["git", "add", "secrets/secrets.yaml"], cwd=root, check=True)
            with patch.object(git_safety, "ENVY_ROOT", root):
                with self.assertRaises(git_safety.SecretSafetyError):
                    git_safety.assert_index_secret_encrypted()

    def test_staged_secret_deletion_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "secrets" / "secrets.yaml"
            secret.parent.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            secret.write_text("placeholder\n")
            subprocess.run(["git", "add", "secrets/secrets.yaml"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "secret"], cwd=root, check=True)
            secret.unlink()
            subprocess.run(["git", "add", "-u"], cwd=root, check=True)
            with patch.object(git_safety, "ENVY_ROOT", root):
                with self.assertRaises(git_safety.SecretSafetyError):
                    git_safety.assert_index_secret_encrypted()

    def test_sops_metadata_does_not_hide_plaintext_data(self):
        mixed = """\
token: plaintext
sops:
  age:
    - recipient: age1test
      enc: encrypted-key
  mac: ENC[AES256_GCM,data:test]
"""
        self.assertFalse(sops_format.content_is_sops_encrypted(mixed))

    def test_recovery_ciphertext_replaces_atomically_without_plaintext_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            secret_dir = Path(temporary)
            destination = secret_dir / "recovery-key.age"
            destination.write_text("old ciphertext\n")

            def encrypt(command, *, stdin_data, capture):
                self.assertIn("AGE-SECRET-KEY-TEST", stdin_data)
                output = Path(command[command.index("-o") + 1])
                output.write_text("age encrypted document\n")
                return ""

            with patch.object(key_recovery, "SECRETS_DIR", secret_dir), patch.object(
                key_recovery, "RECOVERY_KEY_FILE", destination
            ), patch.object(key_recovery, "run_cmd", side_effect=encrypt):
                key_recovery.write_encrypted_recovery_key(
                    "AGE-SECRET-KEY-TEST", ["age1recipient"]
                )

            self.assertEqual(destination.read_text(), "age encrypted document\n")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(secret_dir.glob(".recovery-encrypted-*")), [])

    def test_public_key_derivation_uses_private_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            age_dir = Path(temporary)

            def derive(command, *, capture):
                path = Path(command[-1])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.read_text(), "AGE-SECRET-KEY-TEST\n")
                return "age1public"

            with patch.object(key_recovery, "AGE_KEY_DIR", age_dir), patch.object(
                key_recovery, "run_cmd", side_effect=derive
            ):
                public_key = key_recovery.public_key_from_private("AGE-SECRET-KEY-TEST")

            self.assertEqual(public_key, "age1public")
            self.assertEqual(list(age_dir.iterdir()), [])

    def test_process_failure_raises_command_error(self):
        with self.assertRaises(CommandError) as raised:
            run_process(["/bin/sh", "-c", "exit 23"], capture=True)
        self.assertEqual(raised.exception.returncode, 23)

    def test_command_diagnostics_redact_secret_options_and_url_queries(self):
        rendered = command_display([
            "curl", "--api-key", "secret-value",
            "https://example.test/path?token=secret",
        ])
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("token=secret", rendered)
        self.assertIn("<redacted>", rendered)

    def test_esudo_interactive_fallback_failure_propagates(self):
        first = subprocess.CompletedProcess(["sudo"], 1, "", "bad password")
        failed = CommandError(subprocess.CompletedProcess(["sudo"], 7, "", "denied"))
        with patch.object(utils, "_get_sudo_passwd", return_value="secret"), patch.object(
            utils, "run_process", side_effect=[first, failed]
        ), patch.object(utils.sys.stdin, "isatty", return_value=True):
            with self.assertRaises(CommandError) as raised:
                utils.esudo("true", capture=True)
        self.assertEqual(raised.exception.returncode, 7)

    def test_cached_sudo_ticket_is_detected_without_a_prompt(self):
        available = subprocess.CompletedProcess(
            ["sudo", "-n", "-v"], 0, "", ""
        )
        with patch.object(utils.subprocess, "run", return_value=available) as run:
            self.assertTrue(utils._sudo_ticket_available())
        run.assert_called_once_with(
            ["sudo", "-n", "-v"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )

    def test_esudo_uses_cached_ticket_when_stdin_is_not_a_tty(self):
        completed = subprocess.CompletedProcess(["sudo", "-n", "true"], 0, "", "")
        with patch.object(utils, "_get_sudo_passwd", return_value=None), patch.object(
            utils, "_sudo_ticket_available", return_value=True
        ), patch.object(utils.sys.stdin, "isatty", return_value=False), patch.object(
            utils, "run_process", return_value=completed
        ) as run:
            self.assertIs(utils.esudo("true", capture=True), completed)
        run.assert_called_once_with(
            ["sudo", "-n", "true"],
            cwd=str(utils.ENVY_ROOT),
            capture=True,
            check=True,
            activity=None,
        )

    def test_darwin_apply_does_not_report_success_after_failure(self):
        failed = CommandError(subprocess.CompletedProcess(["sudo"], 1, "", "failed"))
        with patch.object(utils.shutil, "which", return_value="/bin/darwin-rebuild"), patch.object(
            utils, "esudo", side_effect=failed
        ), patch.object(utils.log, "ok") as success:
            with self.assertRaises(CommandError):
                utils.run_darwin_switch()
        success.assert_not_called()

    def test_darwin_bootstrap_enables_flakes_for_sudo_nix_invocation(self):
        with patch.object(utils.shutil, "which", return_value=None), patch.object(
            utils, "esudo"
        ) as sudo, patch.object(utils, "flake_target", return_value="path:.#mac"):
            utils.run_darwin_switch()

        sudo.assert_called_once_with(
            "--preserve-env=HOME,NIX_CONFIG",
            "nix", "--extra-experimental-features", "nix-command flakes",
            "run", "--inputs-from", "path:.", "darwin", "--", "switch",
            "--flake", "path:.#mac", "--impure",
            capture=False,
            activity="nix-darwin build and activation",
        )

    def test_update_restores_lock_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "flake.lock"
            lock.write_text("old\n")

            def mutate(*args, **kwargs):
                lock.write_text("new\n")
                return subprocess.CompletedProcess(args[0], 0, "", "")

            with patch.object(update_workflow, "ENVY_ROOT", root), patch.object(
                update_workflow, "run_process", side_effect=mutate
            ), patch.object(update_workflow, "check_or_exit", side_effect=typer.Exit(1)):
                with self.assertRaises(typer.Exit):
                    update_workflow.update_inputs(validate=True)
            self.assertEqual(lock.read_text(), "old\n")

    def test_file_transaction_restores_all_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.write_text("before")
            with self.assertRaises(RuntimeError):
                with FileTransaction([first, second]):
                    first.write_text("after")
                    second.write_text("created")
                    raise RuntimeError("stop")
            self.assertEqual(first.read_text(), "before")
            self.assertFalse(second.exists())

    def test_doctor_rejects_broad_age_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            age_dir = Path(temporary) / "age"
            age_dir.mkdir(mode=0o755)
            key = age_dir / "keys.txt"
            key.write_text("secret")
            key.chmod(0o644)
            with patch.object(doctor_config, "AGE_KEY_DIR", age_dir), patch.object(
                doctor_config, "AGE_KEY_FILE", key
            ):
                results = doctor_config._check_private_permissions()
            self.assertTrue(any(result.failed for result in results))

    def test_cross_platform_selection_discovers_both_host_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("hosts/darwin/mac.nix", "hosts/linux/pc.nix"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
            with patch.object(check_workflow, "ENVY_ROOT", root):
                self.assertEqual(
                    check_workflow.select_entries(all_machines=True),
                    [("darwin", "mac"), ("linux", "pc")],
                )

    def test_secret_set_help_allows_stdin_but_not_positional_value(self):
        result = CliRunner().invoke(config.app, ["secret-set", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--stdin", result.output)
        self.assertNotIn("[VALUE]", result.output)

    def test_clean_requires_yes_when_noninteractive(self):
        with patch.object(main.system_workflow.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(typer.Exit):
                main.cmd_clean(yes=False, older_than=None, brew=False)


if __name__ == "__main__":
    unittest.main()

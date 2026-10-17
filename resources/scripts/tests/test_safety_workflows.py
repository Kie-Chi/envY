import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
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


NIX_DARWIN_ETC_SCRIPT = Path(__file__).resolve().parents[1] / "nix-darwin-etc.sh"


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

    def test_secret_set_replace_initializes_new_device_recipient_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sops = root / ".sops.yaml"
            secret = root / "secrets.yaml"
            age_key = root / "keys.txt"
            device = root / ".device-label"
            sops.write_text("keys:\n  - &old age1old\n")
            secret.write_text("old ciphertext\n")
            captured = {}

            with patch.object(config, "SOPS_YAML", sops), patch.object(
                config, "SECRETS_FILE", secret
            ), patch.object(config, "AGE_KEY_FILE", age_key), patch.object(
                config, "DEVICE_LABEL_FILE", device
            ), patch("envy.keys.storage.AGE_KEY_FILE", age_key), patch(
                "envy.keys.storage.generate_device_age_key", side_effect=lambda: age_key.write_text("AGE-SECRET-KEY-TEST\n") or "age1new"
            ), patch("envy.keys.storage.current_device_public_key", return_value="age1new"), patch(
                "envy.keys.identity.ensure_sops_label", return_value="new_device"
            ), patch("envy.keys.storage.read_sops_yaml_keys", return_value={"old": "age1old"}), patch(
                "envy.keys.storage.write_sops_yaml_keys", side_effect=lambda keys: captured.update(keys)
            ), patch.object(config, "write_secrets_data", side_effect=lambda data: captured.update(data)):
                config.set_secret_value("llm/steps/apikey", "new-secret", replace=True)

            self.assertEqual(captured["new_device"], "age1new")
            self.assertEqual(captured["llm"]["steps"]["apikey"], "new-secret")

    def test_secret_set_without_replace_requires_decryption(self):
        with patch.object(config, "read_secrets_data", return_value=({}, False)):
            with self.assertRaises(RuntimeError):
                config.set_secret_value("llm/steps/apikey", "new-secret")

    def test_secret_refine_normalizes_scalar_types_when_writing(self):
        field = next(field for field in config.SECRET_FIELDS if field.path == "home_passwd")
        field = replace(field, required=False, validators=[])
        cases = [
            ("plain", "plain", False),
            ("", "", False),
            (12345, "12345", True),
            (True, "true", True),
            (False, "false", True),
            (1.25, "1.25", True),
            (None, "", True),
        ]
        for raw_value, expected, should_write in cases:
            with self.subTest(raw_value=raw_value):
                encrypted = {}
                with patch.object(config, "SECRET_FIELDS", [field]), patch.object(
                    config, "read_secrets_data", return_value=({"home": {"passwd": raw_value}}, True)
                ), patch.object(config, "read_machine_nix", return_value={}), patch.object(
                    config, "write_secrets_data", side_effect=lambda data: encrypted.update(data)
                ), patch("envy.agent.managed_credential_paths", return_value={}):
                    report = config.refine_secrets(write=True, strict=True)

                self.assertTrue(report.ok)
                self.assertEqual(report.changed, should_write)
                if should_write:
                    self.assertEqual(encrypted["home"]["passwd"], expected)

    def test_secret_refine_rejects_structured_values_without_writing(self):
        field = next(field for field in config.SECRET_FIELDS if field.path == "home_passwd")
        for raw_value in ([], ["secret"], {}, {"value": "secret"}):
            with self.subTest(raw_value=raw_value), patch.object(
                config, "SECRET_FIELDS", [field]
            ), patch.object(
                config, "read_secrets_data", return_value=({"home": {"passwd": raw_value}}, True)
            ), patch.object(config, "read_machine_nix", return_value={}), patch.object(
                config, "write_secrets_data"
            ) as write, patch("envy.agent.managed_credential_paths", return_value={}):
                report = config.refine_secrets(write=True, strict=True)

            self.assertFalse(report.ok)
            self.assertIn("home/passwd", report.errors)
            write.assert_not_called()

    def test_secret_refine_reports_non_string_types_without_writing(self):
        field = next(field for field in config.SECRET_FIELDS if field.path == "home_passwd")
        with patch.object(config, "SECRET_FIELDS", [field]), patch.object(
            config, "read_secrets_data", return_value=({"home": {"passwd": 12345}}, True)
        ), patch.object(config, "read_machine_nix", return_value={}), patch.object(
            config, "write_secrets_data"
        ) as write, patch("envy.agent.managed_credential_paths", return_value={}):
            report = config.refine_secrets(write=False, strict=True)

        self.assertFalse(report.ok)
        self.assertIn("home/passwd", report.errors)
        write.assert_not_called()

    def test_doctor_reports_non_string_secret_types(self):
        field = next(field for field in doctor_config.SECRET_FIELDS if field.path == "home_passwd")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            age_dir = root / "age"
            age_dir.mkdir(mode=0o700)
            age_key = age_dir / "keys.txt"
            age_key.write_text("placeholder")
            age_key.chmod(0o600)
            secret = root / "secrets.yaml"
            secret.write_text("encrypted")
            with patch.object(doctor_config, "SECRET_FIELDS", [field]), patch.object(
                doctor_config, "AGE_KEY_FILE", age_key
            ), patch.object(doctor_config, "AGE_KEY_DIR", age_dir), patch.object(
                doctor_config, "SECRETS_FILE", secret
            ), patch.object(
                doctor_config, "read_secrets_data", return_value=({"home": {"passwd": 12345}}, True)
            ), patch.object(
                doctor_config, "is_sops_encrypted", return_value=True
            ), patch.object(doctor_config, "assert_worktree_secret_encrypted"), patch.object(
                doctor_config, "assert_index_secret_encrypted"
            ), patch.object(doctor_config, "assert_head_secret_encrypted"), patch.object(
                doctor_config, "_check_secret_write_capability", return_value=[]
            ), patch.object(doctor_config, "_check_device_key_distinct_from_recovery", return_value=[]), patch(
                "envy.agent.managed_credential_paths", return_value={}
            ) as credentials:
                for raw_value in (12345, True, False, [], {}):
                    with self.subTest(raw_value=raw_value):
                        doctor_config.read_secrets_data.return_value = ({"home": {"passwd": raw_value}}, True)
                        results = doctor_config._check_secrets({})
                        type_errors = [
                            result for result in results
                            if result.name == "home/passwd" and result.failed
                            and "secret value" in result.message
                        ]
                        self.assertTrue(type_errors)
                        self.assertTrue(any("string" in result.message or "scalar" in result.message for result in type_errors))

    def test_secret_set_replace_rolls_back_key_and_recipients_on_encryption_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sops = root / ".sops.yaml"
            secret = root / "secrets.yaml"
            age_key = root / "keys.txt"
            device = root / ".device-label"
            sops.write_text("keys:\n  - &device age1old\n  - &recovery age1recovery\n")
            secret.write_text("old ciphertext\n")
            device.write_text('version = 1\n\n[device]\nsops_label = "device"\n')
            original = {
                path: path.read_bytes()
                for path in (sops, secret, device)
            }

            def write_keys(keys):
                sops.write_text("\n".join(f"{label}: {value}" for label, value in keys.items()))

            with patch.object(config, "SOPS_YAML", sops), patch.object(
                config, "SECRETS_FILE", secret
            ), patch.object(config, "AGE_KEY_FILE", age_key), patch.object(
                config, "DEVICE_LABEL_FILE", device
            ), patch.object(
                config, "read_secrets_data", side_effect=AssertionError("old secrets must not be read")
            ), patch("envy.keys.storage.AGE_KEY_FILE", age_key), patch(
                "envy.keys.storage.generate_device_age_key",
                side_effect=lambda: age_key.write_text("AGE-SECRET-KEY-NEW\n") or "age1new",
            ), patch("envy.keys.storage.current_device_public_key", return_value="age1new"), patch(
                "envy.keys.identity.ensure_sops_label", return_value="new_device"
            ), patch(
                "envy.keys.storage.read_sops_yaml_keys",
                return_value={"device": "age1old", "recovery": "age1recovery"},
            ), patch("envy.keys.storage.write_sops_yaml_keys", side_effect=write_keys), patch.object(
                config, "write_secrets_data", side_effect=RuntimeError("encrypt failed")
            ):
                with self.assertRaises(RuntimeError):
                    config.set_secret_value("llm/steps/apikey", "secret-value", replace=True)

            self.assertEqual(sops.read_bytes(), original[sops])
            self.assertEqual(secret.read_bytes(), original[secret])
            self.assertEqual(device.read_bytes(), original[device])
            self.assertFalse(age_key.exists())

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

    def test_darwin_bootstrap_prepares_etc_before_switch(self):
        with patch.object(utils.shutil, "which", return_value=None), patch.object(
            utils, "esudo"
        ) as sudo, patch.object(utils, "flake_target", return_value="path:.#mac"):
            utils.run_darwin_switch()

        self.assertEqual(sudo.call_count, 2)
        self.assertEqual(
            sudo.call_args_list[0].args,
            ("--preserve-env=ENVY_NIX_DARWIN_ETC_ROOT", str(utils.ENVY_ROOT / "resources/scripts/nix-darwin-etc.sh")),
        )
        self.assertEqual(sudo.call_args_list[0].kwargs["activity"], "preparing nix-darwin /etc files")
        self.assertEqual(
            sudo.call_args_list[1].args,
            (
                "--preserve-env=HOME,NIX_CONFIG",
                "nix", "--extra-experimental-features", "nix-command flakes",
                "run", "--inputs-from", "path:.", "darwin", "--", "switch",
                "--flake", "path:.#mac", "--impure",
            ),
        )
        self.assertEqual(sudo.call_args_list[1].kwargs["activity"], "nix-darwin build and activation")

    def test_nix_darwin_etc_script_moves_unmanaged_files_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "static").mkdir()
            (root / "bashrc").write_text("old bash\n")
            (root / "zshrc").write_text("old zsh\n")
            env = dict(os.environ, ENVY_NIX_DARWIN_ETC_ROOT=str(root))

            first = subprocess.run(["bash", str(NIX_DARWIN_ETC_SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual((root / "bashrc.before-nix-darwin").read_text(), "old bash\n")
            self.assertEqual((root / "zshrc.before-nix-darwin").read_text(), "old zsh\n")
            self.assertFalse((root / "bashrc").exists())
            self.assertFalse((root / "zshrc").exists())

            second = subprocess.run(["bash", str(NIX_DARWIN_ETC_SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)

    def test_nix_darwin_etc_script_refuses_existing_backup_without_moving_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "static").mkdir()
            (root / "bashrc").write_text("current bash\n")
            (root / "bashrc.before-nix-darwin").write_text("old bash\n")
            (root / "zshrc").write_text("current zsh\n")
            env = dict(os.environ, ENVY_NIX_DARWIN_ETC_ROOT=str(root))

            result = subprocess.run(["bash", str(NIX_DARWIN_ETC_SCRIPT)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "bashrc").read_text(), "current bash\n")
            self.assertEqual((root / "bashrc.before-nix-darwin").read_text(), "old bash\n")
            self.assertEqual((root / "zshrc").read_text(), "current zsh\n")

    def test_nix_darwin_etc_script_leaves_current_static_links_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "static"
            static.mkdir()
            (static / "bashrc").write_text("managed bash\n")
            (root / "bashrc").symlink_to(static / "bashrc")
            env = dict(os.environ, ENVY_NIX_DARWIN_ETC_ROOT=str(root))

            result = subprocess.run(["bash", str(NIX_DARWIN_ETC_SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "bashrc").is_symlink())
            self.assertFalse((root / "bashrc.before-nix-darwin").exists())

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

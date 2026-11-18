"""Isolated safety regressions for the restic boundary; no remote writes."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from envy.storage.backend import Archive, _mount_source
from envy.storage.model import Policy, StorageError


class ArchiveBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.password = self.home / "password"
        self.password.write_text("fixture-restic-password")
        self.password.chmod(0o600)
        self.source = self.home / "source"
        self.source.write_bytes(b"local data must survive")
        self.policy = Policy(
            home=self.home, mode="manual", quarantine_days=30, rules=(),
            protected=(), mandatory=(), overrides=(),
            repository="sftp:fixture@nas.invalid:/share/archive", password_file=str(self.password),
        )
        self.archive = Archive(self.policy, self.home / "state")
        self.runner = patch("envy.storage.backend.run_process")
        self.run = self.runner.start()
        self.addCleanup(self.runner.stop)

    def result(self, code=0, stdout="", stderr=""):
        self.run.return_value = subprocess.CompletedProcess(["restic"], code, stdout, stderr)

    def assert_source_retained(self):
        self.assertEqual(self.source.read_bytes(), b"local data must survive")

    def test_password_permissions_and_owner(self):
        for mode in (0o644, 0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                self.password.chmod(mode)
                with self.assertRaises(StorageError):
                    self.archive.command(["cat", "config"])
        self.password.chmod(0o600)
        actual_stat = self.password.stat()
        foreign_stat = MagicMock(st_mode=actual_stat.st_mode, st_uid=os.getuid() + 10001)
        with patch.object(Path, "stat", return_value=foreign_stat):
            with self.assertRaises(StorageError):
                self.archive.environment()
        self.run.assert_not_called()

    def test_missing_password_is_controlled_failure(self):
        self.password.unlink()
        with self.assertRaises(StorageError):
            self.archive.command(["cat", "config"])
        self.run.assert_not_called()

    def test_private_managed_password_symlink_and_environment(self):
        link = self.home / "managed-secret"
        link.symlink_to(self.password)
        self.archive.policy = replace(self.policy, password_file=str(link))
        with patch.dict(os.environ, {"RESTIC_PASSWORD": "untrusted", "RESTIC_REPOSITORY": "/wrong"}):
            env = self.archive.environment()
        self.assertNotIn("RESTIC_PASSWORD", env)
        self.assertEqual(env["RESTIC_PASSWORD_FILE"], str(link))
        self.assertEqual(env["RESTIC_REPOSITORY"], self.policy.repository)
        self.assertNotIn("fixture-restic-password", str(env))

    def local_repository(self, expected="//fixture@nas.invalid/archive"):
        volume = self.home / "volume"
        repository = volume / "nested" / "archive"
        repository.mkdir(parents=True, exist_ok=True)
        self.archive.policy = replace(self.policy, repository=str(repository), mount_source=expected)
        return volume, repository

    def test_expected_mount_source_accepts_nearest_mounted_ancestor(self):
        volume, repository = self.local_repository()
        with patch.object(Path, "is_mount", lambda path: path in {volume, Path("/")}), \
                patch("envy.storage.backend._mount_source", return_value=self.archive.policy.mount_source) as probe:
            self.assertEqual(self.archive.environment()["RESTIC_REPOSITORY"], str(repository))
        probe.assert_called_once_with(volume)
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_wrong_mount_source_at_same_mountpoint_rejects_before_restic(self):
        volume, repository = self.local_repository()
        with patch.object(Path, "is_mount", lambda path: path == volume), \
                patch("envy.storage.backend._mount_source", return_value="//other.invalid/wrong"):
            with self.assertRaises(StorageError):
                self.archive.command(["cat", "config"])
        self.assertTrue(repository.is_dir())
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_missing_expected_mount_source_rejects_before_restic(self):
        volume, _ = self.local_repository(expected="")
        with patch.object(Path, "is_mount", lambda path: path == volume), \
                patch("envy.storage.backend._mount_source") as probe:
            with self.assertRaises(StorageError):
                self.archive.command(["cat", "config"])
            probe.assert_not_called()
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_mount_loss_rejects_existing_local_fallback(self):
        _, repository = self.local_repository()
        for mounted in (set(), {Path("/")}):
            with self.subTest(mounted=mounted), \
                    patch.object(Path, "is_mount", lambda path: path in mounted), \
                    patch("envy.storage.backend._mount_source") as probe:
                with self.assertRaises(StorageError):
                    self.archive.command(["cat", "config"])
                probe.assert_not_called()
        self.assertTrue(repository.is_dir())
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_unknown_mount_source_rejects_before_restic(self):
        volume, _ = self.local_repository()
        with patch.object(Path, "is_mount", lambda path: path == volume), \
                patch("envy.storage.backend._mount_source", return_value=""):
            with self.assertRaises(StorageError):
                self.archive.command(["cat", "config"])
        self.run.assert_not_called()
        self.assert_source_retained()

    def mount_output(self, text, code=0):
        def run(argv, **kwargs):
            self.assertEqual(argv, ["/sbin/mount"], "restic must not run during mount validation")
            self.assertEqual(kwargs, {"capture": True, "check": False, "timeout": 10})
            return subprocess.CompletedProcess(argv, code, text, "fixture-probe-secret")
        self.run.side_effect = run

    def test_macos_mount_table_matches_exact_mountpoint(self):
        mount = Path("/Volumes/archive")
        self.mount_output("/dev/disk1 on / (apfs, local)\n"
                          "//fixture@nas.invalid/share on /Volumes/archive (smbfs, nodev, nosuid)\n"
                          "//other/share on /Volumes/archive-other (smbfs)\n")
        with patch("envy.storage.backend.sys.platform", "darwin"):
            self.assertEqual(_mount_source(mount), "//fixture@nas.invalid/share")
        self.run.assert_called_once()
        self.assert_source_retained()

    def test_macos_mount_table_decodes_spaces_and_octal(self):
        for table in (
            "//fixture/My Share on /Volumes/My Archive (smbfs, nodev)\n",
            r"//fixture/My\040Share on /Volumes/My\040Archive (smbfs, nodev)" + "\n",
        ):
            with self.subTest(table=table):
                self.mount_output(table)
                with patch("envy.storage.backend.sys.platform", "darwin"):
                    self.assertEqual(_mount_source(Path("/Volumes/My Archive")), "//fixture/My Share")
        self.assert_source_retained()

    def test_macos_mount_table_rejects_missing_and_duplicate_matches(self):
        line = "//fixture/share on /Volumes/archive (smbfs)\n"
        for table in ("", line.replace("/Volumes/archive", "/Volumes/archive-other"), line * 2):
            with self.subTest(table=table):
                self.mount_output(table)
                with patch("envy.storage.backend.sys.platform", "darwin"):
                    with self.assertRaises(StorageError):
                        _mount_source(Path("/Volumes/archive"))
        self.assert_source_retained()

    def test_macos_probe_failure_rejects_before_restic(self):
        volume, _ = self.local_repository()
        for code in (1, 124, 127):
            with self.subTest(code=code):
                self.run.reset_mock()
                self.mount_output("", code)
                with patch("envy.storage.backend.sys.platform", "darwin"), \
                        patch.object(Path, "is_mount", lambda path: path == volume):
                    with self.assertRaises(StorageError) as error:
                        self.archive.command(["cat", "config"])
                self.assertNotIn("fixture-probe-secret", str(error.exception))
                self.run.assert_called_once()
                self.assert_source_retained()
        self.run.side_effect = OSError("fixture-probe-secret")
        with patch("envy.storage.backend.sys.platform", "darwin"), \
                patch.object(Path, "is_mount", lambda path: path == volume):
            with self.assertRaises(StorageError) as error:
                self.archive.command(["cat", "config"])
        self.assertNotIn("fixture-probe-secret", str(error.exception))
        self.assert_source_retained()

    def test_linux_mountinfo_optional_fields_and_octal(self):
        table = ("20 1 8:1 / / rw - ext4 /dev/root rw\n"
                 r"36 20 0:42 / /mnt/My\040Archive rw shared:7 master:1 - cifs //fixture/My\040Share rw"
                 "\n")
        with patch("envy.storage.backend.sys.platform", "linux"), \
                patch.object(Path, "read_text", autospec=True, return_value=table) as read:
            self.assertEqual(_mount_source(Path("/mnt/My Archive")), "//fixture/My Share")
        read.assert_called_once_with(Path("/proc/self/mountinfo"))
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_linux_mountinfo_rejects_wrong_duplicate_and_malformed_entries(self):
        line = "36 20 0:42 / /mnt/archive rw shared:7 - cifs //fixture/share rw\n"
        for table in ("", line.replace("/mnt/archive", "/mnt/archive-other"), line * 2,
                      "36 20 0:42 / /mnt/archive rw cifs //fixture/share rw\n",
                      "36 20 0:42 / /mnt/archive - cifs //fixture/share rw\n",
                      "36 20 0:42 / /mnt/archive rw - cifs\n"):
            with self.subTest(table=table), patch("envy.storage.backend.sys.platform", "linux"), \
                    patch.object(Path, "read_text", return_value=table):
                with self.assertRaises(StorageError):
                    _mount_source(Path("/mnt/archive"))
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_linux_probe_permission_failure_rejects_before_restic(self):
        volume, _ = self.local_repository()
        with patch("envy.storage.backend.sys.platform", "linux"), \
                patch.object(Path, "is_mount", lambda path: path == volume), \
                patch.object(Path, "read_text", side_effect=PermissionError("fixture-probe-secret")):
            with self.assertRaises(StorageError) as error:
                self.archive.command(["cat", "config"])
        self.assertNotIn("fixture-probe-secret", str(error.exception))
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_parsed_wrong_volume_rejects_before_restic_on_both_platforms(self):
        volume, _ = self.local_repository()
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                self.run.reset_mock()
                self.mount_output(f"//other/wrong on {volume} (smbfs)\n")
                table = f"36 20 0:42 / {volume} rw - cifs //other/wrong rw\n"
                with patch("envy.storage.backend.sys.platform", platform), \
                        patch.object(Path, "is_mount", lambda path: path == volume), \
                        patch.object(Path, "read_text", return_value=table):
                    with self.assertRaises(StorageError):
                        self.archive.command(["cat", "config"])
                if platform == "darwin":
                    self.run.assert_called_once()
                else:
                    self.run.assert_not_called()
                self.assert_source_retained()

    def assert_sftp_boundary(self, argv, env, command):
        self.assertEqual(argv[0], "restic")
        option = argv[argv.index("-o") + 1]
        self.assertTrue(option.startswith("sftp.args="))
        ssh_args = shlex.split(option.removeprefix("sftp.args="))
        for argument in ("-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no",
                         "-oConnectTimeout=8", "-oPreferredAuthentications=publickey", "-oPasswordAuthentication=no",
                         "-oKbdInteractiveAuthentication=no"):
            self.assertIn(argument, ssh_args)
        self.assertEqual(argv[argv.index("-o") + 2], command)
        self.assertEqual(env["SSH_AUTH_SOCK"], "/fixture/agent.sock")
        self.assertEqual({key for key in env if key.startswith("RESTIC_")},
                         {"RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE", "RESTIC_CACHE_DIR"})
        self.assertEqual(env["RESTIC_REPOSITORY"], self.policy.repository)
        self.assertEqual(env["RESTIC_PASSWORD_FILE"], str(self.password))
        for secret in ("fixture-login-password", "fixture-restic-password", "injected-value"):
            self.assertNotIn(secret, str(argv))
            self.assertNotIn(secret, str(env))

    def injected_environment(self):
        return patch.dict(os.environ, {
            "SSH_AUTH_SOCK": "/fixture/agent.sock", "RESTIC_PASSWORD": "fixture-login-password",
            "RESTIC_REPOSITORY": "injected-value", "RESTIC_PASSWORD_FILE": "injected-value",
            "RESTIC_REPOSITORY_FILE": "injected-value", "RESTIC_CACHE_DIR": "injected-value",
            "RESTIC_SFTP_COMMAND": "injected-value", "RESTIC_EXTRA_OPTION": "injected-value",
        })

    def test_sftp_command_uses_fixed_ssh_options_and_clean_environment(self):
        self.result(stdout="fixture-output")
        with self.injected_environment():
            self.assertEqual(self.archive.command(["cat", "config"]), "fixture-output")
        args, kwargs = self.run.call_args
        self.assert_sftp_boundary(args[0], kwargs["env"], "cat")
        self.assert_source_retained()

    def test_sftp_stream_dump_uses_fixed_ssh_options_and_clean_environment(self):
        process = MagicMock()
        content = b"fixture archived content"
        process.stdout = io.BytesIO(content)
        process.wait.return_value = 0
        process.__enter__.return_value = process
        with self.injected_environment(), \
                patch("envy.storage.backend.subprocess.Popen", return_value=process) as popen:
            self.assertEqual(self.archive.content_hash("a" * 64, str(self.source)),
                             hashlib.sha256(content).hexdigest())
        args, kwargs = popen.call_args
        self.assert_sftp_boundary(args[0], kwargs["env"], "dump")
        self.run.assert_not_called()
        self.assert_source_retained()

    def test_repository_and_ssh_failures_do_not_verify_or_leak(self):
        secret = "fixture-output-secret"
        for code in (1, 3, 124, 127, 255):
            with self.subTest(code=code):
                self.result(code, json.dumps({"id": "a" * 64}), secret)
                with self.assertRaises(StorageError) as error:
                    self.archive.identity()
                self.assertNotIn(secret, str(error.exception))
                self.assertNotIn("fixture-restic-password", str(error.exception))
        self.assert_source_retained()

    def test_identity_requires_valid_response(self):
        for output in ("", "not json", "[]", "{}", '{"id": 123}', '{"id": "short"}'):
            with self.subTest(output=output):
                self.result(stdout=output)
                with self.assertRaises(StorageError):
                    self.archive.identity()
        self.result(stdout=json.dumps({"id": "a" * 64}))
        self.assertEqual(self.archive.identity(), "a" * 64)

    def test_incomplete_backup_preserves_source_and_removes_private_listing(self):
        outputs = ("", "not json", "[]", "null", "{}", '{"message_type":"summary"}',
                   '{"message_type":"summary","snapshot_id":"short"}')
        for output in outputs:
            with self.subTest(output=output):
                self.result(stdout=output)
                with self.assertRaises(StorageError):
                    self.archive.backup([str(self.source)], "fixture")
                self.assert_source_retained()
                self.assertEqual(list(self.archive.state.glob("*.paths")), [])

    def test_failed_backup_rejects_summary_and_keeps_source(self):
        self.result(3, json.dumps({"message_type": "summary", "snapshot_id": "a" * 64}),
                    "fixture-output-secret")
        with self.assertRaises(StorageError) as error:
            self.archive.backup([str(self.source)], "fixture")
        self.assertNotIn("fixture-output-secret", str(error.exception))
        self.assert_source_retained()
        self.assertEqual(list(self.archive.state.glob("*.paths")), [])

    def test_successful_backup_uses_private_raw_path_listing(self):
        def run(argv, **kwargs):
            listing = Path(argv[argv.index("--files-from-raw") + 1])
            self.assertEqual(listing.stat().st_mode & 0o777, 0o600)
            self.assertEqual(listing.read_bytes(), os.fsencode(self.source) + b"\x00")
            self.assertNotIn("fixture-restic-password", str(argv))
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "message_type": "summary", "snapshot_id": "a" * 64}), "")
        self.run.side_effect = run
        self.assertEqual(self.archive.backup([str(self.source)], "fixture"), "a" * 64)
        self.assertEqual(list(self.archive.state.glob("*.paths")), [])
        self.assert_source_retained()

    def test_failed_dump_cannot_return_content_hash(self):
        for code in (124, 255, -9):
            with self.subTest(code=code):
                process = MagicMock()
                process.stdout = io.BytesIO(b"partial backup content")
                process.wait.return_value = code
                process.__enter__.return_value = process

                def start(argv, **kwargs):
                    kwargs["stderr"].write(b"fixture-login-password fixture-restic-password")
                    return process

                with patch("envy.storage.backend.subprocess.Popen", side_effect=start):
                    with self.assertRaisesRegex(StorageError, "verification failed") as error:
                        self.archive.content_hash("a" * 64, str(self.source))
                self.assertNotIn("fixture-login-password", str(error.exception))
                self.assertNotIn("fixture-restic-password", str(error.exception))
                self.assert_source_retained()

    def test_failed_restore_retains_existing_source_and_hides_diagnostics(self):
        self.result(1, "fixture-output-secret", "fixture-output-secret")
        target = self.home / "restore"
        with self.assertRaises(StorageError) as error:
            self.archive.restore("a" * 64, target, [str(self.source)])
        self.assertNotIn("fixture-output-secret", str(error.exception))
        self.assert_source_retained()
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()

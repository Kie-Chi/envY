from __future__ import annotations

import os
import shutil
import socket as unix_socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
SCRIPT = ROOT / "resources" / "scripts" / "nix-trust.sh"
BEGIN = "# BEGIN ENVY MANAGED NIX MIRROR"
END = "# END ENVY MANAGED NIX MIRROR"
THALHEIM = "https://cache.thalheim.io"
THALHEIM_KEY = "cache.thalheim.io-1:R7msbosLEZKrxk/lKxf9BTjOOH7Ax3H0Qj0/6wiHOgc="
NIXOS = "https://cache.nixos.org/"
NIXOS_KEY = "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
USTC = "https://mirrors.ustc.edu.cn/nix-channels/store"


class NixTrustPythonTests(unittest.TestCase):
    def test_repair_uses_sops_backed_esudo(self):
        from envy import nix_trust

        result = subprocess.CompletedProcess([], 0, "", "")
        context = nix_trust.NixTrustContext(mode="china", user="policy-user")
        with patch.object(nix_trust, "esudo", return_value=result) as sudo, patch.object(
            nix_trust, "nix_trust_script", return_value=SCRIPT
        ):
            self.assertIs(
                nix_trust.run_nix_trust("repair", context=context),
                result,
            )

        sudo.assert_called_once_with(
            "bash", str(SCRIPT), "repair", "--mode", "china", "--user", "policy-user",
            capture=False,
            activity="Nix daemon trust repair",
        )

    def test_repair_can_explicitly_reload_the_daemon(self):
        from envy import nix_trust

        result = subprocess.CompletedProcess([], 0, "", "")
        context = nix_trust.NixTrustContext(mode="china", user="policy-user")
        with patch.object(nix_trust, "esudo", return_value=result) as sudo, patch.object(
            nix_trust, "nix_trust_script", return_value=SCRIPT
        ):
            nix_trust.run_nix_trust("repair", context=context, reload=True)

        sudo.assert_called_once_with(
            "bash", str(SCRIPT), "repair", "--mode", "china", "--user", "policy-user",
            "--reload",
            capture=False,
            activity="Nix daemon trust repair",
        )


class NixTrustScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.system_config = self.root / "nix.conf"
        self.custom_config = self.root / "nix.custom.conf"
        self.system_config.write_text("!include nix.custom.conf\n")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.uname = self.bin_dir / "uname"

    def tearDown(self):
        self.root.chmod(0o700)
        self.temporary.cleanup()

    def run_helper(
        self,
        action: str,
        *,
        mode: str = "china",
        systemctl: Path | str | None = None,
        launchctl: Path | str = "missing-launchctl",
        platform: str = "Linux",
        socket: Path | str | None = None,
        nix: Path | str | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if systemctl is None:
            systemctl = self._systemctl()
        socket_path = Path(socket or (self.root / "daemon.socket"))
        if not socket_path.exists():
            listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
            try:
                listener.bind(str(socket_path))
            finally:
                listener.close()
        self.uname.write_text(f"#!{shutil.which('bash')}\nprintf '%s\\n' {platform!r}\n")
        self.uname.chmod(0o755)
        if nix is None:
            nix = self._nix()
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["ENVY_NIX_TRUST_NIX"] = str(nix)
        if environment:
            env.update(environment)
        return subprocess.run(
            [
                "bash",
                str(SCRIPT),
                action,
                "--mode",
                mode,
                "--user",
                "policy-user",
                "--system-config",
                str(self.system_config),
                "--custom-config",
                str(self.custom_config),
                "--systemctl",
                str(systemctl),
                "--launchctl",
                str(launchctl),
                "--socket",
                str(socket or (self.root / "daemon.socket")),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_upstream_policy_always_trusts_sops_nix_cache(self):
        result = self.run_helper("repair", mode="upstream")

        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.custom_config.read_text()
        self.assertIn(f"extra-substituters = {THALHEIM}", text)
        self.assertIn(f"extra-trusted-substituters = {THALHEIM}", text)
        self.assertIn(f"extra-trusted-public-keys = {THALHEIM_KEY}", text)
        self.assertNotIn(NIXOS, text)
        self.assertNotIn(USTC, text)
        self.assertIn("extra-trusted-users = policy-user", text)
        self.assertIn("download-attempts = 3", text)
        self.assertIn("http2 = false", text)
        self.assertIn("extra-experimental-features = nix-command flakes", text)

    def test_old_marker_block_is_upgraded_without_touching_admin_content(self):
        self.custom_config.write_text(
            "admin-setting = keep\n"
            f"{BEGIN}\n"
            "extra-trusted-substituters = https://old.invalid\n"
            f"{END}\n"
            "admin-tail = keep\n"
        )

        result = self.run_helper("repair")

        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.custom_config.read_text()
        self.assertTrue(text.startswith("admin-setting = keep\n"))
        self.assertTrue(text.endswith("admin-tail = keep\n"))
        self.assertIn(f"substituters = {USTC}", text)
        self.assertIn(f"extra-substituters = {THALHEIM} {NIXOS}", text)
        self.assertIn(f"extra-trusted-substituters = {THALHEIM} {NIXOS} {USTC}", text)
        self.assertIn("http2 = false", text)
        self.assertNotIn("old.invalid", text)
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)

    def test_repair_is_idempotent_and_restarts_only_after_change(self):
        calls = self.root / "systemctl.calls"
        systemctl = self.root / "systemctl"
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        systemctl.write_text(
            f"#!{bash}\n"
            "set -eu\n"
            "if [ \"$1\" = is-active ]; then\n"
            "  [ \"${3:-}\" = nix-daemon.service ]\n"
            "  exit\n"
            "fi\n"
            f"printf '%s\\n' \"$*\" >> {str(calls)!r}\n"
        )
        systemctl.chmod(0o755)

        first = self.run_helper("repair", systemctl=systemctl)
        inode = self.custom_config.stat().st_ino
        second = self.run_helper("repair", systemctl=systemctl)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.custom_config.stat().st_ino, inode)
        self.assertTrue(
            calls.is_file(),
            f"first={first.stdout!r}/{first.stderr!r}; "
            f"second={second.stdout!r}/{second.stderr!r}",
        )
        self.assertEqual(calls.read_text().splitlines(), ["restart nix-daemon.service"])

    def test_malformed_markers_fail_without_modifying_file(self):
        original = f"admin-setting = keep\n{BEGIN}\nunfinished = true\n"
        self.custom_config.write_text(original)

        result = self.run_helper("repair")

        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed", result.stderr)
        self.assertEqual(self.custom_config.read_text(), original)

    def test_configured_socket_is_not_ready_when_nix_cannot_connect(self):
        self.assertEqual(self.run_helper("repair").returncode, 0)
        nix = self.root / "nix-failing"
        nix.write_text(f"#!{shutil.which('bash')}\nexit 1\n")
        nix.chmod(0o755)
        result = self.run_helper("status", nix=nix)
        self.assertEqual(result.returncode, 1)
        self.assertIn("daemon is not ready", result.stdout)

    def test_status_reports_repair_without_writing(self):
        original = "admin-setting = keep\n"
        self.custom_config.write_text(original)

        result = self.run_helper("status")

        self.assertEqual(result.returncode, 1)
        self.assertIn("requires repair", result.stdout)
        self.assertEqual(self.custom_config.read_text(), original)

    def test_repair_does_not_require_nix_rpc_after_daemon_restart(self):
        nix = self.root / "nix-failing"
        nix.write_text(f"#!{shutil.which('bash')}\nexit 1\n")
        nix.chmod(0o755)

        result = self.run_helper("repair", nix=nix)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(BEGIN, self.custom_config.read_text())

    def test_ready_configuration_needs_no_sudo_or_write(self):
        self.assertEqual(self.run_helper("repair").returncode, 0)
        inode = self.custom_config.stat().st_ino
        socket_path = self.root / "daemon.socket"
        self.root.chmod(0o500)
        try:
            result = self.run_helper(
                "repair", socket=socket_path,
                environment={"ENVY_NIX_TRUST_SUDO": "missing-sudo"}
            )
        finally:
            self.root.chmod(0o700)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.custom_config.stat().st_ino, inode)

    @unittest.skipIf(os.geteuid() == 0, "root does not need sudo for the test directory")
    def test_missing_sudo_fails_explicitly_when_repair_needs_root(self):
        systemctl = self._systemctl()
        socket_path = self.root / "daemon.socket"
        listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.close()
        self.root.chmod(0o500)
        try:
            result = self.run_helper(
                "repair", systemctl=systemctl, socket=socket_path,
                environment={"ENVY_NIX_TRUST_SUDO": "missing-sudo"}
            )
        finally:
            self.root.chmod(0o700)

        self.assertEqual(result.returncode, 2)
        self.assertIn("sudo is unavailable", result.stderr)
        self.assertFalse(self.custom_config.exists())

    def test_official_install_without_include_manages_only_parent_marker_block(self):
        original = "build-users-group = nixbld\ntrusted-users = root admin"
        self.system_config.write_text(original)
        self.system_config.chmod(0o640)
        self.custom_config.write_text("inactive-setting = keep\n")

        status = self.run_helper("status")
        self.assertEqual(status.returncode, 1)
        self.assertEqual(self.system_config.read_text(), original)

        result = self.run_helper("repair")

        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.system_config.read_text()
        self.assertTrue(text.startswith(original + "\n"))
        self.assertIn(f"substituters = {USTC}", text)
        self.assertIn(f"extra-substituters = {THALHEIM} {NIXOS}", text)
        self.assertIn(f"extra-trusted-substituters = {THALHEIM} {NIXOS} {USTC}", text)
        self.assertIn("http2 = false", text)
        self.assertEqual(self.system_config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.custom_config.read_text(), "inactive-setting = keep\n")
        inode = self.system_config.stat().st_ino
        self.assertEqual(self.run_helper("repair").returncode, 0)
        self.assertEqual(self.system_config.stat().st_ino, inode)

    def test_required_and_absolute_custom_includes_are_recognized(self):
        for include in ("include nix.custom.conf", f"!include {self.custom_config} # custom"):
            with self.subTest(include=include):
                self.system_config.write_text(include + "\n")
                self.custom_config.unlink(missing_ok=True)

                result = self.run_helper("repair")

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.system_config.read_text(), include + "\n")
                self.assertIn(BEGIN, self.custom_config.read_text())

    def test_darwin_restarts_only_the_loaded_daemon_and_only_after_changes(self):
        for service in ("org.nixos.nix-daemon", "systems.determinate.nix-daemon"):
            with self.subTest(service=service):
                self.custom_config.unlink(missing_ok=True)
                calls = self.root / "launchctl.calls"
                calls.unlink(missing_ok=True)
                launchctl = self._launchctl(service)

                first = self.run_helper("repair", platform="Darwin", launchctl=launchctl)
                second = self.run_helper("repair", platform="Darwin", launchctl=launchctl)

                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(calls.read_text().splitlines(), [f"kickstart -k system/{service}"])

    def test_failed_darwin_restart_rolls_back_and_can_be_retried(self):
        original = "admin-setting = keep\n"
        self.custom_config.write_text(original)
        launchctl = self._launchctl("org.nixos.nix-daemon", restart_status=7)

        result = self.run_helper("repair", platform="Darwin", launchctl=launchctl)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("restored the previous configuration", result.stderr)
        self.assertEqual(self.custom_config.read_text(), original)
        self.assertEqual(self.run_helper("status").returncode, 1)
        launchctl = self._launchctl("org.nixos.nix-daemon")
        self.assertEqual(self.run_helper("repair", platform="Darwin", launchctl=launchctl).returncode, 0)

    def test_missing_darwin_daemon_fails_and_rolls_back(self):
        original = "admin-setting = keep\n"
        self.custom_config.write_text(original)

        result = self.run_helper(
            "repair", platform="Darwin", launchctl="missing-launchctl"
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("no active daemon was found", result.stderr)
        self.assertEqual(self.custom_config.read_text(), original)

    def test_symlink_configuration_is_never_replaced(self):
        target = self.root / "external.conf"
        target.write_text("external-setting = keep\n")
        self.custom_config.symlink_to(target)

        result = self.run_helper("repair")

        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.custom_config.is_symlink())
        self.assertEqual(target.read_text(), "external-setting = keep\n")

    def _nix(self) -> Path:
        nix = self.bin_dir / "nix"
        nix.write_text(
            f"#!{shutil.which('bash')}\n"
            'if [ "$1" = store ] && [ "$2" = info ] && [ "$4" = daemon ]; then exit 0; fi\n'
            'exit 1\n'
        )
        nix.chmod(0o755)
        return nix

    def _systemctl(self) -> Path:
        systemctl = self.root / "systemctl"
        systemctl.write_text(
            f"#!{shutil.which('bash')}\n"
            'if [ "$1" = is-active ]; then exit 0; fi\n'
            'exit 0\n'
        )
        systemctl.chmod(0o755)
        return systemctl

    def _launchctl(self, service: str, *, restart_status: int = 0) -> Path:
        launchctl = self.root / "launchctl"
        launchctl.write_text(
            f"#!{shutil.which('bash')}\n"
            'if [ "$1" = print ]; then\n'
            f'  [ "$2" = "system/{service}" ]\n'
            "  exit\n"
            "fi\n"
            f'printf "%s\\n" "$*" >> {str(self.root / "launchctl.calls")!r}\n'
            f"exit {restart_status}\n"
        )
        launchctl.chmod(0o755)
        return launchctl


if __name__ == "__main__":
    unittest.main()

import os
import shutil
import socket as unix_socket
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))


class SetupScriptTests(unittest.TestCase):
    def test_bootstrap_features_reach_both_setup_runtimes_on_both_platforms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "resources" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(ROOT / "setup.sh", root / "setup.sh")
            for name in ("mirror-env.sh", "nix-trust.sh"):
                shutil.copyfile(ROOT / "resources" / "scripts" / name, scripts / name)
            (root / "nix.conf").write_text("!include nix.custom.conf\n")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stubs = {
                "uname": 'printf "%s\\n" "$ENVY_TEST_PLATFORM"\n',
                "python3": (
                    'if [ "$1" = -c ]; then exit 1; fi\n'
                    'printf "%s\\n" "$NIX_CONFIG" > "$ENVY_TEST_CAPTURE"\n'
                    'printf "runtime:python\\n"\n'
                ),
                "nix": (
                    'if [ "$1" = store ] && [ "$2" = info ]; then exit 0; fi\n'
                    'printf "%s\\n" "$NIX_CONFIG" > "$ENVY_TEST_CAPTURE"\n'
                    'printf "runtime:nix\\n"\n'
                    '[ "$1" = run ] && [ "$2" = "path:$ENVY_ROOT#setup" ]\n'
                ),
            }
            for name, body in stubs.items():
                command = bin_dir / name
                command.write_text(f"#!{shutil.which('bash')}\nset -eu\n" + body)
                command.chmod(0o755)
            capture = root / "captured-config"
            daemon_socket = root / "daemon.socket"
            listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
            try:
                listener.bind(str(daemon_socket))
            finally:
                listener.close()
            systemctl = bin_dir / "systemctl"
            systemctl.write_text(
                f"#!{shutil.which('bash')}\n"
                'if [ "$1" = is-active ]; then exit 0; fi\n'
                'exit 0\n'
            )
            systemctl.chmod(0o755)
            launchctl = bin_dir / "launchctl"
            launchctl.write_text(
                f"#!{shutil.which('bash')}\n"
                'if [ "$1" = print ]; then exit 0; fi\n'
                'exit 0\n'
            )
            launchctl.chmod(0o755)
            existing_config = "experimental-features = ca-derivations\nmax-jobs = 2"
            for platform in ("Darwin", "Linux"):
                for mirror in ("china", "upstream"):
                    for runtime in ("nix", "python"):
                        with self.subTest(platform=platform, mirror=mirror, runtime=runtime):
                            env = dict(os.environ)
                            env.pop("ENVY_MIRROR_ENV_APPLIED", None)
                            env.update({
                                "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                                "ENVY_DEV_SHELL": "1" if runtime == "python" else "0",
                                "ENVY_TEST_PLATFORM": platform,
                                "ENVY_TEST_CAPTURE": str(capture),
                                "ENVY_NIX_SYSTEM_CONFIG": str(root / "nix.conf"),
                                "ENVY_NIX_CUSTOM_CONFIG": str(root / "nix.custom.conf"),
                                "ENVY_NIX_TRUST_SYSTEMCTL": str(systemctl),
                                "ENVY_NIX_TRUST_LAUNCHCTL": str(launchctl),
                                "ENVY_NIX_TRUST_SOCKET": str(daemon_socket),
                                "NIX_CONFIG": existing_config,
                            })
                            result = subprocess.run(
                                ["bash", str(root / "setup.sh"), "--mirror", mirror],
                                env=env,
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertIn(f"runtime:{runtime}", result.stdout)
                            config = capture.read_text()
                            self.assertTrue(config.startswith(existing_config + "\n"))
                            self.assertIn(
                                "extra-experimental-features = nix-command flakes",
                                config.splitlines(),
                            )
                            # setup runtime receives only feature toggles. The
                            # daemon trust helper owns cache endpoints so they
                            # are not sent as untrusted client overrides.
                            self.assertNotIn("mirrors.ustc.edu.cn", config)
                            daemon_config = (root / "nix.custom.conf").read_text()
                            self.assertIn("extra-trusted-substituters =", daemon_config)
                            self.assertIn("extra-trusted-public-keys =", daemon_config)
                            self.assertEqual(
                                "mirrors.ustc.edu.cn" in daemon_config, mirror == "china"
                            )


if __name__ == "__main__":
    unittest.main()

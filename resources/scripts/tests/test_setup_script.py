import os
import shutil
import socket as unix_socket
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))


USTC_INSTALLER = "https://mirrors.ustc.edu.cn/misc/brew-install.sh"
UPSTREAM_INSTALLER = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
HOMEBREW_ENV = {
    "china": [
        "https://mirrors.ustc.edu.cn/homebrew-bottles/api",
        "https://ghcr.nju.edu.cn",
        "https://mirrors.ustc.edu.cn/homebrew-bottles",
        "https://mirrors.ustc.edu.cn/brew.git",
        "https://github.com/Homebrew/homebrew-core",
    ],
    "upstream": [
        "https://formulae.brew.sh/api",
        "https://ghcr.io",
        "https://ghcr.io/v2/homebrew/core",
        "https://github.com/Homebrew/brew",
        "https://github.com/Homebrew/homebrew-core",
    ],
}


# No network or real installer is used, even on hosts with Homebrew installed.
CURL_STUB = '''\
url=""
output=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        https://*) url="$1"; shift ;;
        -o) output="$2"; shift 2 ;;
        *) shift ;;
    esac
done
printf '%s\\n' "$url" >> "$ENVY_TEST_LOG"
if [ "${ENVY_TEST_DOWNLOAD_FAILURE:-}" = all ] \\
    || { [ "${ENVY_TEST_DOWNLOAD_FAILURE:-}" = ustc ] \\
        && [ "$url" = 'https://mirrors.ustc.edu.cn/misc/brew-install.sh' ]; }; then
    printf 'touch "$ENVY_TEST_PARTIAL_EXECUTED"\\n' > "$output"
    exit 22
fi
cat > "$output" <<'INSTALLER'
set -eu
printf '%s\\n' "$HOMEBREW_API_DOMAIN" "$HOMEBREW_ARTIFACT_DOMAIN" \\
    "$HOMEBREW_BOTTLE_DOMAIN" "$HOMEBREW_BREW_GIT_REMOTE" \\
    "$HOMEBREW_CORE_GIT_REMOTE" \\
    > "$ENVY_TEST_INSTALLED_ENV"
if [ "${ENVY_TEST_INSTALLER_FAILURE:-0}" = 1 ]; then exit 9; fi
printf '#!/bin/bash\\nprintf "Homebrew 7.0.0\\\\n"\\n' > "$ENVY_TEST_BREW_PATH"
chmod +x "$ENVY_TEST_BREW_PATH"
INSTALLER
'''


def isolated_environment(root: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("HOMEBREW_", "ENVY_")) and key != "NIX_CONFIG"
    }
    env.update({
        "PATH": f"{root / 'bin'}{os.pathsep}/usr/bin:/bin",
        "TMPDIR": str(root),
        "ENVY_TEST_LOG": str(root / "calls.log"),
        "ENVY_TEST_BREW_PATH": str(root / "bin" / "brew"),
        "ENVY_HOMEBREW_BIN_DIRS": str(root / "bin"),
        "ENVY_TEST_INSTALLED_ENV": str(root / "installed-env"),
        "ENVY_TEST_PARTIAL_EXECUTED": str(root / "partial-executed"),
    })
    return env


class SetupScriptTests(unittest.TestCase):
    def run_requires(
        self, root: Path, platform: str, *, brew: bool = False,
        mirror: str = "china", extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        stubs = {
            "uname": f"printf '%s\\n' {platform!r}\n",
            "nix": "printf 'nix 2.33\\n'\n",
            "curl": CURL_STUB,
        }
        if brew:
            stubs["brew"] = 'printf "Homebrew 7.0.0\\n"\nexit "${ENVY_TEST_BREW_EXIT:-0}"\n'
        for name, body in stubs.items():
            path = bin_dir / name
            path.write_text("#!/bin/bash\nset -eu\n" + body)
            path.chmod(0o755)
        env = isolated_environment(root)
        env.update({"ENVY_MIRROR": mirror, **(extra_env or {})})
        return subprocess.run(
            ["/bin/bash", str(ROOT / "requires.sh")],
            env=env, capture_output=True, text=True, check=False, timeout=30,
        )

    def test_requires_installs_homebrew_only_on_darwin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Darwin")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "calls.log").read_text().splitlines(), [USTC_INSTALLER])
            self.assertEqual((root / "installed-env").read_text().splitlines(), HOMEBREW_ENV["china"])
            self.assertTrue((root / "bin" / "brew").exists())
            self.assertFalse(list(root.glob("envy-homebrew.*")))

    def test_requires_does_not_install_homebrew_on_linux(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Linux")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "calls.log").exists())
            self.assertFalse((root / "bin" / "brew").exists())

    def test_requires_reuses_existing_homebrew(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Darwin", brew=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "calls.log").exists())

    def test_requires_uses_official_homebrew_sources_in_upstream_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Darwin", mirror="upstream")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "calls.log").read_text().splitlines(), [UPSTREAM_INSTALLER])
            self.assertEqual((root / "installed-env").read_text().splitlines(), HOMEBREW_ENV["upstream"])

    def test_requires_preserves_explicit_homebrew_endpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            override = "https://trusted.example/install.sh"
            values = ["https://trusted.example/" + name for name in ("api", "artifacts", "bottles", "brew", "core")]
            result = self.run_requires(root, "Darwin", extra_env={
                "ENVY_HOMEBREW_INSTALL_URL": override,
                "HOMEBREW_API_DOMAIN": values[0],
                "HOMEBREW_ARTIFACT_DOMAIN": values[1],
                "HOMEBREW_BOTTLE_DOMAIN": values[2],
                "HOMEBREW_BREW_GIT_REMOTE": values[3],
                "HOMEBREW_CORE_GIT_REMOTE": values[4],
                # The installer still needs its environment when setup has
                # already applied the shared bootstrap environment marker.
                "ENVY_MIRROR_ENV_APPLIED": "china",
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "calls.log").read_text().splitlines(), [override])
            self.assertEqual((root / "installed-env").read_text().splitlines(), values)

    def test_requires_download_fallback_never_executes_partial_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Darwin", extra_env={"ENVY_TEST_DOWNLOAD_FAILURE": "ustc"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "calls.log").read_text().splitlines(), [USTC_INSTALLER, UPSTREAM_INSTALLER])
            self.assertEqual((root / "installed-env").read_text().splitlines(), HOMEBREW_ENV["china"])
            self.assertFalse((root / "partial-executed").exists())
            self.assertFalse(list(root.glob("envy-homebrew.*")))

    def test_requires_stops_when_all_downloads_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Darwin", extra_env={"ENVY_TEST_DOWNLOAD_FAILURE": "all"})
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "bin" / "brew").exists())
            self.assertFalse((root / "partial-executed").exists())
            self.assertFalse(list(root.glob("envy-homebrew.*")))

    def test_requires_does_not_fallback_from_explicit_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            override = "https://trusted.example/install.sh"
            result = self.run_requires(root, "Darwin", extra_env={
                "ENVY_HOMEBREW_INSTALL_URL": override,
                "ENVY_TEST_DOWNLOAD_FAILURE": "all",
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "calls.log").read_text().splitlines(), [override])
            self.assertFalse((root / "partial-executed").exists())

    def test_requires_does_not_rerun_a_failed_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Darwin", extra_env={"ENVY_TEST_INSTALLER_FAILURE": "1"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "calls.log").read_text().splitlines(), [USTC_INSTALLER])
            self.assertFalse((root / "bin" / "brew").exists())
            self.assertFalse(list(root.glob("envy-homebrew.*")))

    def test_requires_reports_broken_existing_homebrew_without_installing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_requires(root, "Darwin", brew=True, extra_env={"ENVY_TEST_BREW_EXIT": "1"})
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "calls.log").exists())
            self.assertIn("could not run", result.stderr)

    def test_homebrew_bootstrap_endpoints_match_catalog(self):
        catalog = (ROOT / "modules/mirrors/catalog.nix").read_text()
        for mode in ("china", "upstream"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result = self.run_requires(root, "Darwin", mirror=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                for endpoint in (root / "installed-env").read_text().splitlines():
                    self.assertIn(f'"{endpoint}"', catalog)
                installer = USTC_INSTALLER if mode == "china" else UPSTREAM_INSTALLER
                self.assertIn(f'installerUrl = "{installer}";', catalog)

    def test_mirror_environment_does_not_export_homebrew_on_linux(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bin").mkdir()
            uname = root / "bin/uname"
            uname.write_text("#!/bin/bash\nprintf 'Linux\\n'\n")
            uname.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", "-uc", '. "$1"; env', "mirror-test", str(ROOT / "resources/scripts/mirror-env.sh")],
                env=isolated_environment(root), capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(any(line.startswith("HOMEBREW_") for line in result.stdout.splitlines()))

    def test_bootstrap_features_reach_both_setup_runtimes_on_both_platforms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "resources" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(ROOT / "setup.sh", root / "setup.sh")
            shutil.copyfile(ROOT / "requires.sh", root / "requires.sh")
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
                "curl": CURL_STUB,
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
                            env = isolated_environment(root)
                            # Force every Darwin combination through a fresh
                            # fake install rather than using a previous stub.
                            (bin_dir / "brew").unlink(missing_ok=True)
                            env.update({
                                "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
                                "ENVY_DEV_SHELL": "1" if runtime == "python" else "0",
                                "ENVY_TEST_PLATFORM": platform,
                                "ENVY_TEST_CAPTURE": str(capture),
                                "ENVY_NIX_SYSTEM_CONFIG": str(root / "nix.conf"),
                                "ENVY_NIX_CUSTOM_CONFIG": str(root / "nix.custom.conf"),
                                "ENVY_NIX_TRUST_SYSTEMCTL": str(systemctl),
                                "ENVY_NIX_TRUST_LAUNCHCTL": str(launchctl),
                                "ENVY_NIX_TRUST_SOCKET": str(daemon_socket),
                                "ENVY_TEST_BREW_PATH": str(bin_dir / "brew"),
                                "ENVY_HOMEBREW_BIN_DIRS": str(bin_dir),
                                "NIX_CONFIG": existing_config,
                            })
                            result = subprocess.run(
                                ["/bin/bash", str(root / "setup.sh"), "--mirror", mirror],
                                env=env,
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertIn(f"runtime:{runtime}", result.stdout)
                            if platform == "Darwin":
                                self.assertEqual(
                                    (root / "installed-env").read_text().splitlines(), HOMEBREW_ENV[mirror]
                                )
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

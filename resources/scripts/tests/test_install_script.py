import os
import pty
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
INSTALL_SCRIPT = ROOT / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source"
        self.source.mkdir()
        setup = self.source / "setup.sh"
        setup.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            ". \"$ENVY_ROOT/resources/scripts/mirror-env.sh\"\n"
            "printf '%s\\n' \"${ENVY_ROOT:?}\" > \"$ENVY_ROOT/setup-ran\"\n"
            "printf '%s\\n' \"${ENVY_MIRROR:?}\" > \"$ENVY_ROOT/setup-mirror\"\n"
            "printf '%s\\n' \"${NIX_CONFIG:-}\" > \"$ENVY_ROOT/setup-nix-config\"\n"
        )
        setup.chmod(0o755)
        mirror_script = self.source / "resources" / "scripts" / "mirror-env.sh"
        mirror_script.parent.mkdir(parents=True)
        mirror_script.write_text((ROOT / "resources" / "scripts" / "mirror-env.sh").read_text())
        self._git(self.source, "init", "-q")
        self._git(self.source, "symbolic-ref", "HEAD", "refs/heads/master")
        self._git(self.source, "add", "setup.sh", "resources/scripts/mirror-env.sh")
        self._git(
            self.source,
            "-c", "user.name=envY Test",
            "-c", "user.email=envy@example.invalid",
            "commit", "-qm", "fixture",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_clone_only_creates_checkout_without_running_setup(self):
        target = self.root / "checkout"

        result = self._install(target, "--no-setup")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / ".git").is_dir())
        self.assertTrue((target / "setup.sh").is_file())
        self.assertFalse((target / "setup-ran").exists())
        self.assertIn("Checkout ready", result.stdout)

    def test_existing_checkout_is_reused_without_fetch_or_reset(self):
        target = self.root / "checkout"
        self.assertEqual(self._install(target, "--no-setup").returncode, 0)
        head = self._git_output(target, "rev-parse", "HEAD")

        result = self._install(
            target,
            "--repo", str(self.root / "does-not-exist"),
            "--no-setup",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._git_output(target, "rev-parse", "HEAD"), head)
        self.assertIn("Using existing checkout", result.stdout)

    def test_non_repository_target_is_rejected_without_deleting_it(self):
        target = self.root / "occupied"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep\n")

        result = self._install(target, "--no-setup")

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.exists())
        self.assertIn("not a Git checkout", result.stderr)

    def test_successful_clone_hands_off_to_setup_with_selected_target(self):
        target = self.root / "checkout"

        terminal, input_fd = pty.openpty()
        try:
            result = self._install(target, stdin=input_fd)
        finally:
            os.close(input_fd)
            os.close(terminal)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / "setup-ran").read_text().strip(), str(target))
        self.assertEqual((target / "setup-mirror").read_text().strip(), "china")
        self.assertIn("mirrors.ustc.edu.cn", (target / "setup-nix-config").read_text())

    def test_upstream_mirror_does_not_inject_china_nix_config(self):
        target = self.root / "checkout"

        terminal, input_fd = pty.openpty()
        try:
            result = self._install(target, "--mirror", "upstream", stdin=input_fd)
        finally:
            os.close(input_fd)
            os.close(terminal)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / "setup-mirror").read_text().strip(), "upstream")
        self.assertNotIn("mirrors.ustc.edu.cn", (target / "setup-nix-config").read_text())

    def test_invalid_mirror_is_rejected_before_clone(self):
        target = self.root / "checkout"

        result = self._install(target, "--mirror", "invalid", "--no-setup")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertIn("--mirror must be", result.stderr)

    def test_help_documents_mirror_selection(self):
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), "--help"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--mirror MODE", result.stdout)
        self.assertIn("ENVY_MIRROR", result.stdout)
        self.assertIn("ENVY_ROOT", result.stdout)
        self.assertIn("$HOME/.envy", result.stdout)

    def test_deprecated_checkout_environment_alias_is_still_accepted(self):
        for variable in ("ENVY_DOTFILES", "DOTFILES_DIR"):
            with self.subTest(variable=variable):
                target = self.root / f"legacy-{variable.lower()}-checkout"

                result = self._install(
                    target, "--no-setup", root_variable=variable
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((target / ".git").is_dir())

    def test_bootstrap_mirror_environment_is_idempotent(self):
        script = ROOT / "resources" / "scripts" / "mirror-env.sh"
        environment = dict(os.environ)
        for name in ("ENVY_MIRROR_ENV_APPLIED", "NIX_CONFIG"):
            environment.pop(name, None)
        environment["ENVY_MIRROR"] = "china"

        result = subprocess.run(
            [
                "bash", "-c",
                '. "$1"; . "$1"; printf "%s\\n" "$NIX_CONFIG"',
                "envy-mirror-test", str(script),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines().count(
                "substituters = https://mirrors.ustc.edu.cn/nix-channels/store https://cache.nixos.org/"
            ),
            1,
        )
        self.assertIn("https://mirrors.ustc.edu.cn/nix-channels/store", result.stdout)
        self.assertIn("https://cache.nixos.org/", result.stdout)
        self.assertIn("extra-substituters = https://cache.thalheim.io", result.stdout)
        self.assertEqual(
            result.stdout.splitlines().count(
                "extra-experimental-features = nix-command flakes"
            ),
            1,
        )

    def test_upstream_bootstrap_also_uses_sops_nix_cache(self):
        script = ROOT / "resources" / "scripts" / "mirror-env.sh"
        environment = dict(os.environ)
        for name in ("ENVY_MIRROR_ENV_APPLIED", "NIX_CONFIG"):
            environment.pop(name, None)
        environment["ENVY_MIRROR"] = "upstream"

        result = subprocess.run(
            [
                "bash", "-c",
                '. "$1"; . "$1"; printf "%s\\n" "$NIX_CONFIG"',
                "envy-mirror-test", str(script),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("extra-substituters ="), 1)
        self.assertIn("https://cache.thalheim.io", result.stdout)
        self.assertNotIn("mirrors.ustc.edu.cn", result.stdout)
        self.assertEqual(
            result.stdout.splitlines().count(
                "extra-experimental-features = nix-command flakes"
            ),
            1,
        )

    def test_setup_without_a_terminal_fails_with_clone_only_guidance(self):
        target = self.root / "checkout"

        result = self._install(target)

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((target / ".git").is_dir())
        self.assertIn("interactive setup requires a terminal", result.stderr)
        self.assertIn("--no-setup", result.stderr)

    def _install(
        self,
        target: Path,
        *extra: str,
        stdin=None,
        root_variable: str = "ENVY_ROOT",
    ) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        for name in (
            "ENVY_ROOT", "ENVY_DOTFILES", "DOTFILES_DIR",
            "ENVY_MIRROR", "ENVY_MIRROR_ENV_APPLIED", "NIX_CONFIG",
            "npm_config_registry", "UV_DEFAULT_INDEX", "GOPROXY",
            "RUSTUP_DIST_SERVER", "RUSTUP_UPDATE_ROOT",
            "CARGO_REGISTRIES_CRATES_IO_INDEX",
            "CARGO_REGISTRIES_CRATES_IO_PROTOCOL",
        ):
            environment.pop(name, None)
        environment.update({
            "ENVY_REPOSITORY_URL": str(self.source),
            "ENVY_BRANCH": "master",
            root_variable: str(target),
        })
        return subprocess.run(
            ["bash", str(INSTALL_SCRIPT), *extra],
            cwd=self.root,
            env=environment,
            stdin=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _git(root: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )

    @staticmethod
    def _git_output(root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()

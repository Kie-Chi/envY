import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
SCRIPT = ROOT / "modules/desktops/darwin/check-app-management.sh"
BASH = shutil.which("bash")
STORE_PATH = next((path for path in Path(sys.executable).resolve().parents
                   if path.parent == Path("/nix/store")), None)


@unittest.skipUnless(BASH, "Provide bash for application permission checks")
class DarwinAppManagementTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.apps = self.root / "Home Manager Apps"
        self.apps.mkdir()
        self.script = self.root / "probe.sh"
        # Darwin activation uses the system touch; Linux sandboxes use PATH.
        self.script.write_text(SCRIPT.read_text().replace("/usr/bin/touch", shutil.which("touch")))

    def check(self, directory=None):
        return subprocess.run(
            [BASH, str(self.script), str(directory or self.apps)],
            text=True, capture_output=True, timeout=5,
        )

    def test_empty_and_missing_directories_succeed(self):
        for directory in (self.apps, self.root / "missing"):
            with self.subTest(directory=directory):
                result = self.check(directory)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_writable_application_is_checked(self):
        app = self.apps / "Writable App.app"
        app.mkdir()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((app / ".DS_Store").is_file())

    @unittest.skipUnless(STORE_PATH, "Provide a Nix store path for immutable link regression")
    def test_immutable_store_link_is_skipped_and_other_apps_are_checked(self):
        (self.apps / "A Store Link.app").symlink_to(STORE_PATH, target_is_directory=True)
        app = self.apps / "Z Writable.app"
        app.mkdir()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((app / ".DS_Store").is_file())
        self.assertTrue((self.apps / "A Store Link.app").is_symlink())

    def test_legacy_linkapps_directory_is_left_for_migration(self):
        link = self.root / "legacy"
        link.symlink_to(self.apps, target_is_directory=True)
        app = self.apps / "Existing.app"
        app.mkdir()
        result = self.check(link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((app / ".DS_Store").exists())

    def test_real_write_failure_is_reported_without_resetting_permissions(self):
        app = self.apps / "Broken.app"
        app.mkdir()
        (app / ".DS_Store").symlink_to(self.root / "missing" / "marker")
        # Intercept reset calls in an isolated copy, including the old absolute path.
        reset_log = self.root / "reset.log"
        stub = self.root / "tccutil"
        stub.write_text('#!/bin/sh\nprintf reset >> "$RESET_LOG"\n')
        stub.chmod(0o755)
        probe = self.root / "check.sh"
        probe.write_text(self.script.read_text().replace("/usr/bin/tccutil", str(stub)))
        environment = {
            **os.environ, "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            "RESET_LOG": str(reset_log),
        }
        subprocess.run([str(stub), "reset", "SystemPolicyAppBundles"],
                       env=environment, check=True, timeout=5)
        self.assertEqual(reset_log.read_text(), "reset")
        reset_log.unlink()
        result = subprocess.run(
            [BASH, str(probe), str(self.apps)], text=True, capture_output=True, timeout=5,
            env=environment,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot update application", result.stderr)
        self.assertIn("Broken.app", result.stderr)
        self.assertIn(".DS_Store", result.stderr)
        self.assertIn("App Management", result.stderr)
        self.assertFalse(reset_log.exists())

    def test_non_store_application_link_is_still_checked(self):
        target = self.root / "writable target"
        target.mkdir()
        (self.apps / "External.app").symlink_to(target, target_is_directory=True)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / ".DS_Store").is_file())


if __name__ == "__main__":
    unittest.main()

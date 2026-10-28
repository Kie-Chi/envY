import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy import config, utils


class DeviceMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.metadata = self.root / ".device-label"
        self.machines = self.root / "hosts" / "darwin"
        self.machines.mkdir(parents=True)
        (self.machines / "test-mac.nix").write_text("{ ... }: {}\n")

    def tearDown(self):
        self.tempdir.cleanup()

    def patches(self):
        return (
            patch.object(utils, "DEVICE_LABEL_FILE", self.metadata),
            patch.object(config, "DEVICE_LABEL_FILE", self.metadata),
            patch.object(
                config,
                "versioned_machine_file",
                side_effect=lambda machine_id=None: self.machines / f"{machine_id or 'test-mac'}.nix",
            ),
        )

    def test_machine_selector_is_stored_in_device_toml(self):
        self.metadata.write_text('version = 1\n\n[device]\nsops_label = "old_label"\n')
        patches = self.patches()
        with patches[0], patches[1], patches[2]:
            utils.set_device_machine_id("test-mac")
            values = utils.read_device_metadata()

        self.assertEqual(values["machine_id"], "test-mac")
        self.assertEqual(values["sops_label"], "old_label")
        self.assertIn("[device]", self.metadata.read_text())

    def test_config_refine_completes_and_validates_device_metadata(self):
        self.metadata.write_text('[device]\nsops_label = "test-mac"\n')
        patches = self.patches()
        with patches[0], patches[1], patches[2], patch.dict(os.environ, {"ENVY_MACHINE": "test-mac"}):
            report = config.refine_device_metadata(write=True)
            check = config.refine_device_metadata(write=False)
            values = utils.read_device_metadata()

        self.assertTrue(report.ok)
        self.assertTrue(check.ok)
        self.assertEqual(values["machine_id"], "test-mac")
        self.assertEqual(values["sops_label"], "test_mac")
        self.assertTrue(self.metadata.read_text().startswith("version = 1"))

    def test_one_line_label_is_rejected_without_rewriting_it(self):
        self.metadata.write_text("test-mac\n")
        patches = self.patches()
        with patches[0], patches[1], patches[2]:
            report = config.refine_device_metadata(write=True)

        self.assertFalse(report.ok)
        self.assertEqual(self.metadata.read_text(), "test-mac\n")

    def test_cli_reports_unusable_label_without_traceback(self):
        (self.root / "flake.nix").write_text("{}\n")
        (self.root / "resources" / "scripts" / "envy").mkdir(parents=True)
        self.metadata.write_text("test-mac\n")
        scripts = Path(utils.__file__).resolve().parents[1]
        environment = os.environ | {
            "ENVY_ROOT": str(self.root),
            "HOME": str(self.root),
            "PYTHONPATH": str(scripts),
            "NO_COLOR": "1",
        }
        result = subprocess.run(
            [sys.executable, "-m", "envy", "host", "status"],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("invalid TOML", output)
        self.assertIn("version = 1", output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(self.metadata.read_text(), "test-mac\n")

    def test_config_check_rejects_invalid_toml_without_rewriting_it(self):
        invalid = "version = [\n"
        self.metadata.write_text(invalid)
        patches = self.patches()
        with patches[0], patches[1], patches[2]:
            report = config.refine_device_metadata(write=False)

        self.assertFalse(report.ok)
        self.assertEqual(self.metadata.read_text(), invalid)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy import config
from envy.schemas.common.config import (
    COMMON_MACHINE_FIELDS,
    OBSOLETE_MACHINE_KEYS as COMMON_OBSOLETE_MACHINE_KEYS,
)
from envy.schemas.darwin.config import (
    MACHINE_FIELDS as DARWIN_MACHINE_FIELDS,
    OBSOLETE_MACHINE_KEYS as PLATFORM_DARWIN_OBSOLETE_MACHINE_KEYS,
)


DARWIN_MACHINE_SCHEMA = COMMON_MACHINE_FIELDS + DARWIN_MACHINE_FIELDS
DARWIN_OBSOLETE_MACHINE_KEYS = (
    COMMON_OBSOLETE_MACHINE_KEYS + PLATFORM_DARWIN_OBSOLETE_MACHINE_KEYS
)


class DarwinMachineConfigTests(unittest.TestCase):
    def setUp(self):
        # This fixture exercises Darwin-only boolean and obsolete fields even
        # when the cross-platform suite is running on a Linux worker.
        self.schema_patch = patch.multiple(
            config,
            MACHINE_FIELDS=DARWIN_MACHINE_SCHEMA,
            OBSOLETE_MACHINE_KEYS=DARWIN_OBSOLETE_MACHINE_KEYS,
        )
        self.schema_patch.start()
        self.addCleanup(self.schema_patch.stop)
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.machines = self.root / "hosts" / "darwin"
        self.machines.mkdir(parents=True)
        self.machine = self.machines / "test-mac.nix"
        self.machine.write_text(
            """{ ... }:

{
  imports = [ ../default.nix ];
  envy.software.nix.packages.exclude = [ "okular" ];
}
"""
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def values(self):
        values = {field.path: field.default_fn() for field in config.MACHINE_FIELDS}
        values["envy.user.name"] = "chi"
        values["envy.user.home"] = "/Users/chi"
        values["envy.repository.path"] = "/Users/chi/.envy"
        values["envy.git.name"] = "Chi"
        values["envy.git.email"] = "chi@example.com"
        values["envy.darwin.services.mihomo.tun"] = "false"
        return values

    def machine_file_patch(self):
        return patch.object(
            config,
            "versioned_machine_file",
            side_effect=lambda machine_id=None: self.machines / f"{machine_id or 'test-mac'}.nix",
        )

    def test_writer_preserves_hand_maintained_machine_policy(self):
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ), patch.object(config, "set_device_machine_id"):
            config.write_machine_nix(self.values())

        text = self.machine.read_text()
        self.assertIn('envy.software.nix.packages.exclude = [ "okular" ];', text)
        self.assertIn(config.MANAGED_START, text)
        self.assertIn("envy.darwin.services.mihomo.tun = false;", text)

    def test_second_write_replaces_only_managed_block(self):
        values = self.values()
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ), patch.object(config, "set_device_machine_id"):
            config.write_machine_nix(values)
            values["envy.vscode.mode"] = "local"
            config.write_machine_nix(values)

        text = self.machine.read_text()
        self.assertEqual(text.count(config.MANAGED_START), 1)
        self.assertIn('envy.vscode.mode = "local";', text)
        self.assertIn('envy.software.nix.packages.exclude = [ "okular" ];', text)

    def test_mirror_block_is_independent_from_machine_config_block(self):
        overrides = {
            "envy.mirrors.overrides.npm.source": "chsrc:npm/npmmirror",
            "envy.mirrors.overrides.npm.registry": "https://registry.npmmirror.com",
        }
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ), patch.object(config, "set_device_machine_id"):
            config.write_machine_nix(self.values())
            config.write_mirror_overrides(overrides)
            config.set_config_value("envy.vscode.mode", "local")
            config.write_mirror_overrides({
                "envy.mirrors.overrides.npm.source": "chsrc:npm/huawei",
                "envy.mirrors.overrides.npm.registry": "https://mirrors.huaweicloud.com/repository/npm/",
            })

            text = self.machine.read_text()
            self.assertEqual(text.count(config.MIRROR_MANAGED_START), 1)
            self.assertIn('envy.vscode.mode = "local";', text)
            self.assertIn("chsrc:npm/huawei", text)
            self.assertNotIn("npmmirror", text)
            self.assertEqual(
                config.read_mirror_overrides(),
                {
                    "envy.mirrors.overrides.npm.source": "chsrc:npm/huawei",
                    "envy.mirrors.overrides.npm.registry": "https://mirrors.huaweicloud.com/repository/npm/",
                },
            )

    def test_mirror_reader_ignores_hand_policy_outside_generated_block(self):
        self.machine.write_text(
            """{ ... }:

{
  envy.mirrors.overrides.npm.registry = "https://hand.example/";
  # BEGIN ENVY MANAGED MIRROR OVERRIDES
  # `envy mirror` owns this block. Other machine policy stays intact.
  envy.mirrors.overrides.npm.source = "chsrc:npm/huawei";
  # END ENVY MANAGED MIRROR OVERRIDES
}
"""
        )
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ):
            self.assertEqual(
                config.read_mirror_overrides(),
                {"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"},
            )

    def test_reader_understands_strings_and_booleans(self):
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ), patch.object(config, "set_device_machine_id"):
            config.write_machine_nix(self.values())
            values = config.read_machine_nix()

        self.assertEqual(values["envy.user.name"], "chi")
        self.assertEqual(values["envy.darwin.services.mihomo.tun"], "false")

    def test_writer_uses_schema_default_for_hidden_conditional_field(self):
        values = self.values()
        values.pop("envy.darwin.services.mihomo.tun")
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ), patch.object(config, "set_device_machine_id"):
            config.write_machine_nix(values)
        self.assertIn("envy.darwin.services.mihomo.tun = false;", self.machine.read_text())

    def test_set_value_persists_habit_policy_through_managed_machine_block(self):
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ), patch.object(config, "set_device_machine_id"):
            config.write_machine_nix(self.values())
            config.set_config_value("envy.habits.terminalScratchpad.gesture", "F8")
            values = config.read_machine_nix()

        self.assertEqual(values["envy.habits.terminalScratchpad.gesture"], "F8")
        self.assertIn(
            'envy.habits.terminalScratchpad.gesture = "F8";',
            self.machine.read_text(),
        )

    def test_writer_removes_obsolete_flat_proxy_assignments(self):
        self.machine.write_text(
            self.machine.read_text().replace(
                "  envy.software.nix.packages.exclude",
                '  envy.darwin.proxy.mode = "keep";\n'
                "  envy.darwin.proxy.tun = true;\n"
                "  envy.software.nix.packages.exclude",
            )
        )
        with self.machine_file_patch(), patch.object(
            config, "current_machine_id", return_value="test-mac"
        ), patch.object(config, "set_device_machine_id"):
            config.write_machine_nix(self.values())

        text = self.machine.read_text()
        self.assertNotIn("envy.darwin.proxy.mode", text)
        self.assertNotIn("envy.darwin.proxy.tun", text)


if __name__ == "__main__":
    unittest.main()

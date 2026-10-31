import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer

from envy.config import cli, machine_nix, refine, updates
from envy.nix_literals import LiteralReader
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
        schema = {"MACHINE_FIELDS": DARWIN_MACHINE_SCHEMA, "OBSOLETE_MACHINE_KEYS": DARWIN_OBSOLETE_MACHINE_KEYS}
        for module in (cli, machine_nix, refine, updates):
            schema_patch = patch.multiple(
                module, **{name: value for name, value in schema.items() if hasattr(module, name)}
            )
            schema_patch.start()
            self.addCleanup(schema_patch.stop)
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
        values = {field.path: field.default_fn() for field in machine_nix.MACHINE_FIELDS}
        values["envy.user.name"] = "chi"
        values["envy.user.home"] = "/Users/chi"
        values["envy.repository.path"] = "/Users/chi/.envy"
        values["envy.git.name"] = "Chi"
        values["envy.git.email"] = "chi@example.com"
        values["envy.darwin.services.mihomo.tun"] = "false"
        return values

    def machine_file_patch(self):
        return patch.object(
            machine_nix,
            "versioned_machine_file",
            side_effect=lambda machine_id=None: self.machines / f"{machine_id or 'test-mac'}.nix",
        )

    def test_writer_preserves_hand_maintained_machine_policy(self):
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            machine_nix.write_machine_nix(self.values())

        text = self.machine.read_text()
        self.assertIn('envy.software.nix.packages.exclude = [ "okular" ];', text)
        self.assertIn(machine_nix.MANAGED_START, text)
        self.assertIn("envy.darwin.services.mihomo.tun = false;", text)

    def test_second_write_replaces_only_managed_block(self):
        values = self.values()
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            machine_nix.write_machine_nix(values)
            values["envy.vscode.mode"] = "local"
            machine_nix.write_machine_nix(values)

        text = self.machine.read_text()
        self.assertEqual(text.count(machine_nix.MANAGED_START), 1)
        self.assertIn('envy.vscode.mode = "local";', text)
        self.assertIn('envy.software.nix.packages.exclude = [ "okular" ];', text)

    def test_mirror_block_is_independent_from_machine_config_block(self):
        overrides = {
            "envy.mirrors.overrides.npm.source": "chsrc:npm/npmmirror",
            "envy.mirrors.overrides.npm.registry": "https://registry.npmmirror.com",
        }
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(updates, "current_machine_id", return_value="test-mac"), patch.object(
            machine_nix, "set_device_machine_id"
        ):
            machine_nix.write_machine_nix(self.values())
            machine_nix.write_mirror_overrides(overrides)
            updates.set_config_value("envy.vscode.mode", "local")
            machine_nix.write_mirror_overrides({
                "envy.mirrors.overrides.npm.source": "chsrc:npm/huawei",
                "envy.mirrors.overrides.npm.registry": "https://mirrors.huaweicloud.com/repository/npm/",
            })

            text = self.machine.read_text()
            self.assertEqual(text.count(machine_nix.MIRROR_MANAGED_START), 1)
            self.assertIn('envy.vscode.mode = "local";', text)
            self.assertIn("chsrc:npm/huawei", text)
            self.assertNotIn("npmmirror", text)
            self.assertEqual(
                machine_nix.read_mirror_overrides(),
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
            machine_nix, "current_machine_id", return_value="test-mac"
        ):
            self.assertEqual(
                machine_nix.read_mirror_overrides(),
                {"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"},
            )

    def test_reader_understands_strings_and_booleans(self):
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            machine_nix.write_machine_nix(self.values())
            values = machine_nix.read_machine_nix()

        self.assertEqual(values["envy.user.name"], "chi")
        self.assertEqual(values["envy.darwin.services.mihomo.tun"], "false")

    def test_writer_uses_schema_default_for_hidden_conditional_field(self):
        values = self.values()
        values.pop("envy.darwin.services.mihomo.tun")
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            machine_nix.write_machine_nix(values)
        self.assertIn("envy.darwin.services.mihomo.tun = false;", self.machine.read_text())

    def test_set_value_persists_habit_policy_through_managed_machine_block(self):
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(updates, "current_machine_id", return_value="test-mac"), patch.object(
            machine_nix, "set_device_machine_id"
        ):
            machine_nix.write_machine_nix(self.values())
            updates.set_config_value("envy.habits.terminalScratchpad.gesture", "F8")
            values = machine_nix.read_machine_nix()

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
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            machine_nix.write_machine_nix(self.values())

        text = self.machine.read_text()
        self.assertNotIn("envy.darwin.proxy.mode", text)
        self.assertNotIn("envy.darwin.proxy.tun", text)

    def test_unknown_or_invalid_values_are_rejected_without_writing(self):
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            machine_nix.write_machine_nix(self.values())
        before = self.machine.read_text()
        cases = (
            ("envy.vscode.nope", "local", "unknown managed machine field: envy.vscode.nope"),
            ("envy.git.email", "not-an-email", "invalid value for envy.git.email"),
        )
        for (path, value, message), quiet in itertools.product(cases, (False, True)):
            with self.subTest(path=path, quiet=quiet), self.machine_file_patch(), patch.object(
                machine_nix, "set_device_machine_id"
            ) as select, patch.object(updates, "log") as log:
                with self.assertRaisesRegex(typer.BadParameter, message):
                    updates.set_config_value(path, value, quiet=quiet)
                self.assertEqual(self.machine.read_text(), before)
                select.assert_not_called()
                log.ok.assert_not_called()
                if path == "envy.vscode.nope":
                    self.assertEqual(log.error.called, not quiet)

    def test_writers_select_the_machine_they_wrote(self):
        other = self.machines / "other-mac.nix"
        other.write_text(self.machine.read_text())
        overrides = {"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"}
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id") as select:
            machine_nix.write_machine_nix(self.values())
            machine_nix.write_mirror_overrides(overrides)
            self.assertEqual([call.args for call in select.call_args_list], [("test-mac",), ("test-mac",)])

            select.reset_mock()
            before = self.machine.read_text()
            machine_nix.write_machine_nix(self.values(), machine_id="other-mac")
            machine_nix.write_mirror_overrides(overrides, machine_id="other-mac")
            self.assertEqual([call.args for call in select.call_args_list], [("other-mac",), ("other-mac",)])
            self.assertEqual(self.machine.read_text(), before)
            self.assertIn(machine_nix.MANAGED_START, other.read_text())
            self.assertIn(machine_nix.MIRROR_MANAGED_START, other.read_text())

            select.reset_mock()
            with self.assertRaises(FileNotFoundError):
                machine_nix.write_machine_nix(self.values(), machine_id="missing-mac")
            with self.assertRaises(FileNotFoundError):
                machine_nix.write_mirror_overrides(overrides, machine_id="missing-mac")
            select.assert_not_called()

    def test_mirror_values_are_nix_escaped_and_paths_are_checked(self):
        value = 'https://mirror.example/a"b\\c/${x}'
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            # The second write replaces an existing block.
            for written in ("placeholder", value):
                machine_nix.write_mirror_overrides({"envy.mirrors.overrides.npm.registry": written})
            line = next(
                line for line in self.machine.read_text().splitlines()
                if line.lstrip().startswith("envy.mirrors.overrides.npm.registry")
            )
            literal = line.split("=", 1)[1].strip().removesuffix(";")
            self.assertEqual(literal, r'"https://mirror.example/a\"b\\c/\${x}"')
            self.assertEqual(LiteralReader(literal).value(), value)
            self.assertEqual(machine_nix.read_mirror_overrides(), {"envy.mirrors.overrides.npm.registry": value})

            before = self.machine.read_text()
            for path in ("envy.mirrors.overrides.npm registry", "envy.vscode.mode", 'envy.mirrors.overrides.x";'):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "invalid mirror override path"):
                    machine_nix.write_mirror_overrides({path: "x"})
            self.assertEqual(self.machine.read_text(), before)

    def test_values_are_written_as_nix_literals(self):
        value = 'Chi ${builtins.currentSystem} "q" \\ $${x}\n'
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            machine_nix.write_machine_nix(self.values())
            lines = len(self.machine.read_text().splitlines())
            updates.set_config_value("envy.git.name", value, quiet=True)

            text = self.machine.read_text()
            self.assertEqual(len(text.splitlines()), lines)
            line = next(line for line in text.splitlines() if line.lstrip().startswith("envy.git.name"))
            literal = line.split("=", 1)[1].strip().removesuffix(";")
            self.assertEqual(literal, r'"Chi \${builtins.currentSystem} \"q\" \\ $\${x}\n"')
            self.assertEqual(LiteralReader(literal).value(), value)
            self.assertEqual(machine_nix.read_machine_nix()["envy.git.name"], value)

    def test_hand_written_interpolation_survives_rewrites_verbatim(self):
        home = '"${config.home.homeDirectory}"'
        registry = '"https://${mirrorHost}/npm/"'
        # A legacy flat assignment moves into the managed block unchanged.
        self.machine.write_text(self.machine.read_text().replace(
            "  imports = [ ../default.nix ];\n",
            f"  imports = [ ../default.nix ];\n  envy.user.home = {home};\n",
        ))
        with self.machine_file_patch(), patch.object(
            machine_nix, "current_machine_id", return_value="test-mac"
        ), patch.object(machine_nix, "set_device_machine_id"):
            values = self.values()
            values.pop("envy.user.home")
            values.update(machine_nix.read_machine_nix())
            machine_nix.write_machine_nix(values)
            updates.set_config_value("envy.git.name", "Kie", quiet=True)
            machine_nix.write_mirror_overrides({"envy.mirrors.overrides.npm.registry": "placeholder"})
            self.machine.write_text(self.machine.read_text().replace('"placeholder"', registry))
            machine_nix.write_mirror_overrides(
                machine_nix.read_mirror_overrides() | {"envy.mirrors.overrides.pip.index": "https://pip.example/"}
            )

            text = self.machine.read_text()
            self.assertEqual(text.count("envy.user.home ="), 1)
            self.assertIn(f"  envy.user.home = {home};\n", text)
            self.assertIn(f"  envy.mirrors.overrides.npm.registry = {registry};\n", text)
            self.assertIn('  envy.git.name = "Kie";\n', text)
            self.assertEqual(machine_nix.read_machine_nix()["envy.user.home"], home.strip('"'))


if __name__ == "__main__":
    unittest.main()

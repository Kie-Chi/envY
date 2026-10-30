"""Device and recovery key rotation and repair against fake, or real, age and sops."""

import stat
import subprocess
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from envy import key, main
from envy.git_safety import SecretSafetyError
from envy.transaction import FileTransaction

from key_harness import KeyWorld


SECRETS = {"home": {"passwd": "hunter2"}}
# Failures surface as these; anything else escaping an operation is a bug.
FAILURES = (subprocess.CalledProcessError, RuntimeError, ValueError, SecretSafetyError)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def crash_without_rollback():
    """Leave files as a killed process would, instead of restoring them."""
    return patch.object(FileTransaction, "rollback", lambda self: None)


class RotationScenarios:
    """Scenarios that run against fake tools and, when installed, real ones."""

    real = False

    def setUp(self):
        self.world = KeyWorld(self, real=self.real)
        self.world.build()

    def test_device_rotation_retires_the_old_key_everywhere(self):
        world = self.world
        old = world.identity("laptop")

        key.key_rotate()

        new = world.generated[-1]
        keys = world.keys()
        self.assertEqual(keys["laptop"], new.public)
        self.assertNotIn(old.public, keys.values())
        self.assertEqual(world.local_publics(), [new.public])
        for identity in (new, world.identity("desk"), world.identity("recovery")):
            self.assertEqual(world.read_secrets(identity), SECRETS)
        self.assertFalse(world.can_open(world.secrets, old))
        self.assertFalse(world.can_open(world.recovery_file, old))
        self.assertEqual(world.read_recovery(new), world.identity("recovery").private)
        # The retired identity survives only as a private local backup.
        self.assertEqual(mode(world.age_key.with_name("keys.txt.bak")), 0o600)
        self.assertEqual(mode(world.age_key), 0o600)
        world.assert_consistent(self)
        self.assertEqual(world.leftovers(), [])

    def test_recovery_rotation_replaces_the_recovery_key_everywhere(self):
        world = self.world
        old = world.identity("recovery")

        key.key_rotate(recovery=True)

        new = world.generated[-1]
        self.assertEqual(world.keys()["recovery"], new.public)
        self.assertEqual(world.read_secrets(new), SECRETS)
        self.assertFalse(world.can_open(world.secrets, old))
        self.assertFalse(world.can_open(world.recovery_file, old))
        for device in ("laptop", "desk"):
            self.assertEqual(world.read_recovery(world.identity(device)), new.private)
        self.assertEqual(world.local_publics(), [world.identity("laptop").public])
        world.assert_consistent(self)
        self.assertEqual(world.leftovers(), [])

    def steps(self, operation):
        """Run once to number the age/sops calls an operation makes."""
        operation()
        calls = [call for call in self.world.calls if call[0] in {"age", "age-keygen", "sops", "ssh-to-age"}]
        self.assertGreaterEqual(len(calls), 3)
        return calls

    def assert_each_failure_rolls_back(self, operation, steps):
        """Fail each listed age/sops call: the operation aborts and restores
        every key file, or a tolerated probe failure lets it finish safely."""
        world = self.world
        aborted = 0
        for step in steps:
            world.build()
            before = world.snapshot()
            world.fail_at = step
            try:
                operation()
            except FAILURES:
                aborted += 1
                with self.subTest(step=step, failed=world.failed):
                    self.assertEqual(world.snapshot(), before)
            else:
                with self.subTest(step=step, failed=world.failed):
                    # Only a read-only key probe may fail without aborting.
                    self.assertEqual(world.failed[:2], ["age-keygen", "-y"])
            world.fail_at = None
            with self.subTest(step=step, failed=world.failed):
                self.assertEqual(world.leftovers(), [])
                world.assert_consistent(self)
        self.assertGreaterEqual(aborted, len(steps) - 1)

    def assert_each_crash_is_repaired(self, operation, steps):
        world = self.world
        for step in steps:
            with self.subTest(step=step):
                world.build()
                world.fail_at = step
                with crash_without_rollback():
                    try:
                        operation()
                    except FAILURES:
                        pass
                world.fail_at = None

                key.key_repair()

                world.assert_consistent(self)
                self.assertEqual(world.read_secrets(world.identity("desk")), SECRETS)
                self.assertTrue(world.can_open(world.recovery_file, world.identity("desk")))
                self.assertEqual(world.leftovers(), [])


class FakeRotationTests(RotationScenarios, unittest.TestCase):
    def test_added_devices_gain_and_removed_devices_lose_access(self):
        world = self.world
        tablet, desk = world.identity("tablet"), world.identity("desk")

        key.key_add(tablet.public, "tablet")

        self.assertEqual(world.keys()["tablet"], tablet.public)
        self.assertEqual(world.read_secrets(tablet), SECRETS)
        self.assertEqual(world.read_recovery(tablet), world.identity("recovery").private)

        key.key_remove("desk")

        self.assertNotIn("desk", world.keys())
        self.assertFalse(world.can_open(world.secrets, desk))
        self.assertFalse(world.can_open(world.recovery_file, desk))
        world.assert_consistent(self)
        self.assertEqual(world.leftovers(), [])

    def test_add_and_remove_refuse_unsafe_changes(self):
        world = self.world
        cases = [
            (lambda: key.key_add("ssh-ed25519 AAAA"), ValueError),
            (lambda: key.key_add(world.identity("tablet").public, "recovery"), ValueError),
            (lambda: key.key_add(world.identity("tablet").public, "desk"), ValueError),
            (lambda: key.key_remove("recovery"), ValueError),
            (lambda: key.key_remove("missing"), ValueError),
            (lambda: key.key_remove("envy_rotation_old_laptop"), ValueError),
        ]
        for operation, error in cases:
            with self.subTest(operation=operation):
                before = world.snapshot()
                with self.assertRaises(error):
                    operation()
                self.assertEqual(world.snapshot(), before)
        before = world.snapshot()
        key.key_add(world.identity("desk").public, "again")
        self.assertEqual(world.snapshot(), before)
        with patch.object(key, "confirm", return_value=False):
            key.key_remove("laptop")
        self.assertEqual(world.snapshot(), before)
        world.build(devices=("laptop",))
        before = world.snapshot()
        with self.assertRaises(RuntimeError):
            key.key_remove("laptop", force=True)
        self.assertEqual(world.snapshot(), before)

    def test_every_failed_add_or_remove_step_rolls_back(self):
        tablet = self.world.identity("tablet").public
        for operation in (lambda: key.key_add(tablet, "tablet"), lambda: key.key_remove("desk")):
            self.world.build()
            self.assert_each_failure_rolls_back(operation, range(1, len(self.steps(operation)) + 1))

    def test_every_failed_device_rotation_step_rolls_back(self):
        steps = range(1, len(self.steps(key.key_rotate)) + 1)
        self.assert_each_failure_rolls_back(key.key_rotate, steps)

    def test_every_failed_recovery_rotation_step_rolls_back(self):
        operation = lambda: key.key_rotate(recovery=True)  # noqa: E731
        self.assert_each_failure_rolls_back(operation, range(1, len(self.steps(operation)) + 1))

    def test_repair_finishes_a_device_rotation_killed_at_any_step(self):
        steps = range(1, len(self.steps(key.key_rotate)) + 1)
        self.assert_each_crash_is_repaired(key.key_rotate, steps)

    def test_repair_settles_a_recovery_rotation_killed_at_any_step(self):
        operation = lambda: key.key_rotate(recovery=True)  # noqa: E731
        self.assert_each_crash_is_repaired(operation, range(1, len(self.steps(operation)) + 1))

    def test_device_rotation_requires_a_registered_local_key_and_recovery(self):
        world = self.world
        for build, install in (
            ({"local": None}, None),
            ({}, "stranger"),
            ({"recovery": False}, None),
        ):
            with self.subTest(build=build, install=install):
                world.build(**build)
                if install:
                    world.install(world.identity(install))
                before = world.snapshot()
                with self.assertRaises((RuntimeError, ValueError)):
                    key.key_rotate()
                self.assertEqual(world.snapshot(), before)
                self.assertEqual(world.generated, [])

    def test_recovery_rotation_requires_an_authorized_device_and_a_recovery_key(self):
        world = self.world
        for build, install in (({"local": None}, None), ({}, "stranger"), ({"recovery": False}, None)):
            with self.subTest(build=build, install=install):
                world.build(**build)
                if install:
                    world.install(world.identity(install))
                before = world.snapshot()
                with self.assertRaises(RuntimeError):
                    key.key_rotate(recovery=True)
                self.assertEqual(world.snapshot(), before)

    def test_repair_adopts_a_new_key_stored_before_its_recipient_update(self):
        world = self.world
        old, new = world.identity("laptop"), world.identity("fresh")
        # A rotation killed between storing both identities and .sops.yaml.
        world.install(old, new)

        key.key_repair()

        self.assertEqual(world.keys()["laptop"], new.public)
        self.assertFalse(world.can_open(world.secrets, old))
        self.assertEqual(world.read_secrets(new), SECRETS)
        world.assert_consistent(self)

    def test_repair_hardens_the_private_key_and_its_backups(self):
        world = self.world
        backup = world.age_key.with_name("keys.txt.bak1")
        backup.write_text(world.identity("laptop").private + "\n")
        for path, value in ((world.age_key.parent, 0o755), (world.age_key, 0o644), (backup, 0o644)):
            path.chmod(value)

        key.key_repair()

        self.assertEqual(
            (mode(world.age_key.parent), mode(world.age_key), mode(backup)), (0o700, 0o600, 0o600)
        )

    def test_repair_refuses_states_it_cannot_prove_safe(self):
        world = self.world
        stranger = world.identity("stranger")
        cases = {
            "unregistered local key": lambda: world.install(stranger),
            "missing recovery-key.age": lambda: world.recovery_file.unlink(),
            "recovery key matches neither marker": lambda: self.seal_recovery_with(stranger, pending="other"),
            "recovery key does not match": lambda: self.seal_recovery_with(stranger),
        }
        for name, corrupt in cases.items():
            with self.subTest(name):
                world.build()
                corrupt()
                before = world.snapshot()
                with self.assertRaises(RuntimeError):
                    key.key_repair()
                self.assertEqual(world.snapshot(), before)

    def seal_recovery_with(self, identity, pending=None):
        from envy.keys import recovery, storage

        keys = self.world.keys()
        if pending:
            keys["recovery_new"] = self.world.identity(pending).public
            storage.write_sops_yaml_keys(keys)
        recovery.write_encrypted_recovery_key(identity.private, keys.values())

    def test_repair_completes_a_recovery_rotation_whose_new_key_was_sealed(self):
        from envy.keys import recovery, storage

        world = self.world
        new = world.identity("newrecovery")
        keys = world.keys()
        keys["recovery_new"] = new.public
        storage.write_sops_yaml_keys(keys)
        recovery.write_encrypted_recovery_key(new.private, [keys["laptop"], keys["desk"]])

        key.key_repair()

        self.assertEqual(world.keys()["recovery"], new.public)
        self.assertEqual(world.read_secrets(new), SECRETS)
        self.assertFalse(world.can_open(world.secrets, world.identity("recovery")))
        world.assert_consistent(self)

    def test_cli_reports_failures_and_leaves_the_repository_unchanged(self):
        world = self.world
        runner = CliRunner()
        before = world.snapshot()
        world.fail_at = 3

        failed = runner.invoke(main.cli, ["key", "rotate"])

        self.assertEqual(failed.exit_code, 1)
        self.assertEqual(world.snapshot(), before)
        world.fail_at = None
        for args in (["key", "rotate"], ["key", "rotate", "--recovery"], ["key", "repair"]):
            with self.subTest(args=args):
                self.assertEqual(runner.invoke(main.cli, args).exit_code, 0)
        world.assert_consistent(self)


@unittest.skipUnless(__import__("key_harness").real_tools_available(), "age and sops are not installed")
class RealToolRotationTests(RotationScenarios, unittest.TestCase):
    real = True

    def test_rotations_failing_or_killed_mid_way_stay_recoverable(self):
        # Real tools are slow, so only interrupt each rotation where every
        # file has started to change: the last secrets re-encryption.
        for operation in (key.key_rotate, lambda: key.key_rotate(recovery=True)):
            self.world.build()
            calls = self.steps(operation)
            step = max(index for index, call in enumerate(calls, 1) if call[:2] == ["sops", "--encrypt"])
            self.assert_each_failure_rolls_back(operation, [step])
            self.assert_each_crash_is_repaired(operation, [step])


if __name__ == "__main__":
    unittest.main()

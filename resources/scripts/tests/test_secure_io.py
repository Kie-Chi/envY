import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy import secure_io


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class SecureIoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-secure-io-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def leftovers(self, directory: Path) -> list[str]:
        return sorted(path.name for path in directory.iterdir() if ".envy-" in path.name)

    def test_new_file_defaults_to_0644_and_leaves_no_temporary(self):
        target = self.root / "nested" / "file.txt"
        secure_io.atomic_write_text(target, "hello\n")
        self.assertEqual((target.read_text(), mode(target)), ("hello\n", 0o644))
        self.assertEqual(self.leftovers(target.parent), [])

    def test_existing_mode_is_kept_unless_a_mode_is_given(self):
        target = self.root / "file.txt"
        target.write_text("old")
        target.chmod(0o600)
        secure_io.atomic_write_text(target, "new")
        self.assertEqual((target.read_text(), mode(target)), ("new", 0o600))
        secure_io.atomic_write_text(target, "newer", mode=0o640)
        self.assertEqual((target.read_text(), mode(target)), ("newer", 0o640))

    def test_private_parent_is_created_or_tightened(self):
        parent = self.root / "shared"
        parent.mkdir()
        parent.chmod(0o755)
        secure_io.atomic_write_bytes(parent / "keys.txt", b"key", mode=0o600, private_parent=True)
        self.assertEqual((mode(parent), mode(parent / "keys.txt")), (0o700, 0o600))

        fresh = self.root / "fresh" / "deeper"
        secure_io.ensure_private_directory(fresh)
        self.assertEqual(mode(fresh), 0o700)

    def test_failed_write_keeps_the_original_and_removes_the_temporary(self):
        target = self.root / "file.txt"
        target.write_text("original")
        for function in ("fsync", "replace"):
            with self.subTest(failing=function):
                with patch.object(secure_io.os, function, side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        secure_io.atomic_write_text(target, "partial")
                self.assertEqual(target.read_text(), "original")
                self.assertEqual(self.leftovers(self.root), [])

    def test_secure_copy_never_leaves_a_readable_target(self):
        source = self.root / "source"
        source.write_bytes(b"token")
        source.chmod(0o644)
        destination = self.root / "copy"
        destination.write_bytes(b"old")
        destination.chmod(0o644)
        secure_io.secure_copy(source, destination)
        self.assertEqual((destination.read_bytes(), mode(destination)), (b"token", 0o600))

    def test_secure_temporary_path_is_private_and_always_removed(self):
        work = self.root / "work"
        with self.assertRaises(RuntimeError):
            with secure_io.secure_temporary_path(work, prefix=".plain-", suffix=".yaml") as path:
                self.assertEqual(mode(path), 0o600)
                self.assertTrue(path.name.startswith(".plain-") and path.name.endswith(".yaml"))
                path.write_text("plaintext")
                raise RuntimeError("interrupted")
        self.assertFalse(path.exists())
        self.assertEqual(list(work.iterdir()), [])

    def test_replace_prepared_file_applies_the_mode_and_moves_it(self):
        prepared = self.root / ".prepared"
        prepared.write_text("data")
        prepared.chmod(0o600)
        destination = self.root / "destination"
        destination.write_text("old")
        secure_io.replace_prepared_file(prepared, destination, mode=0o644)
        self.assertFalse(prepared.exists())
        self.assertEqual((destination.read_text(), mode(destination)), ("data", 0o644))


if __name__ == "__main__":
    unittest.main()

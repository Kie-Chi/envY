import os
import stat
import tempfile
import threading
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


    def test_default_writer_supports_a_directory_without_read_permission(self):
        parent = self.root / "write-only"
        parent.mkdir()
        parent.chmod(0o300)
        try:
            target = parent / "file"
            secure_io.atomic_write_text(target, "written")
            self.assertEqual(target.read_text(), "written")
        finally:
            parent.chmod(0o700)
        self.assertEqual(self.leftovers(parent), [])

    def test_guard_runs_after_content_fsync_and_before_replace(self):
        target = self.root / "guarded"
        target.write_bytes(b"original")
        events = []
        original_fsync = os.fsync
        original_replace = os.replace

        def fsync(descriptor):
            original_fsync(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                events.append("fsync")

        def guard():
            self.assertEqual(events, ["fsync"])
            self.assertEqual(target.read_bytes(), b"original")
            temporary = next(self.root.glob(".guarded.envy-*"))
            self.assertEqual(temporary.read_bytes(), b"updated")
            self.assertEqual(mode(temporary), 0o600)
            events.append("guard")

        def replace(source, destination, **kwargs):
            self.assertEqual(events, ["fsync", "guard"])
            events.append("replace")
            return original_replace(source, destination, **kwargs)

        with patch.object(secure_io.os, "fsync", side_effect=fsync), \
                patch.object(secure_io.os, "replace", side_effect=replace):
            secure_io.atomic_write_bytes(target, b"updated", mode=0o600, pre_replace=guard)
        self.assertEqual(events, ["fsync", "guard", "replace"])
        self.assertEqual(target.read_bytes(), b"updated")
        self.assertEqual(self.leftovers(self.root), [])

    def test_guard_failure_preserves_competitor_and_cleans_temporary(self):
        for use_text in [False, True]:
            with self.subTest(use_text=use_text):
                target = self.root / "guarded"
                if target.exists():
                    target.chmod(0o600)
                target.write_text("original")

                def reject():
                    target.write_text("competitor")
                    target.chmod(0o440)
                    raise RuntimeError("conflict")

                writer = secure_io.atomic_write_text if use_text else secure_io.atomic_write_bytes
                with self.assertRaisesRegex(RuntimeError, "conflict"):
                    writer(target, "updated" if use_text else b"updated", mode=0o600, pre_replace=reject)
                self.assertEqual((target.read_text(), mode(target)), ("competitor", 0o440))
                self.assertEqual(self.leftovers(self.root), [])

    def test_parent_binding_races_never_write_payload_or_leave_orphan_temporaries(self):
        for moment in ("parent_open", "temporary_open"):
            with self.subTest(moment=moment):
                root = self.root / moment
                parent = root / "parent"
                parent.mkdir(parents=True)
                target = parent / "file"
                target.write_text("original")
                target.chmod(0o640)
                moved = root / "moved"
                opened, changed = threading.Event(), threading.Event()
                original_open = os.open
                temporary_names, competitor_files, errors = [], [], []

                def competitor():
                    try:
                        if not opened.wait(5):
                            raise AssertionError("writer did not reach creation boundary")
                        parent.rename(moved)
                        parent.mkdir()
                        target.write_text("competitor target")
                        target.chmod(0o440)
                        if temporary_names:
                            competitor_file = parent / temporary_names[0]
                            competitor_file.write_text("competitor temporary name")
                            competitor_file.chmod(0o440)
                            competitor_files.append(competitor_file)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        changed.set()

                def open_then_race(path, flags, *args, **kwargs):
                    descriptor = original_open(path, flags, *args, **kwargs)
                    boundary = (
                        moment == "parent_open" and path == parent and flags & os.O_DIRECTORY
                        or moment == "temporary_open" and flags & os.O_CREAT
                    )
                    if boundary:
                        if flags & os.O_CREAT:
                            temporary_names.append(Path(path).name)
                        opened.set()
                        if not changed.wait(5):
                            os.close(descriptor)
                            raise AssertionError("competitor timed out")
                    return descriptor

                def guard():
                    self.fail("directory binding drift must be rejected before writing payload")

                thread = threading.Thread(target=competitor, daemon=True)
                thread.start()
                try:
                    with patch.object(secure_io.os, "open", side_effect=open_then_race):
                        with self.assertRaises(OSError):
                            secure_io.atomic_write_text(target, "DECLARED_SECRET", mode=0o600, pre_replace=guard)
                finally:
                    opened.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(((moved / "file").read_text(), mode(moved / "file")), ("original", 0o640))
                self.assertEqual((target.read_text(), mode(target)), ("competitor target", 0o440))
                self.assertEqual(sorted(path.name for path in moved.iterdir()), ["file"])
                self.assertEqual(set(parent.iterdir()), {target, *competitor_files})
                for competitor_file in competitor_files:
                    self.assertEqual((competitor_file.read_text(), mode(competitor_file)),
                                     ("competitor temporary name", 0o440))

    def test_guard_failure_cleans_temporary_even_after_parent_rename(self):
        parent = self.root / "parent"
        parent.mkdir()
        target = parent / "file"
        target.write_text("original")
        moved = self.root / "moved"

        def reject():
            parent.rename(moved)
            parent.mkdir()
            target.write_text("competitor")
            raise RuntimeError("conflict")

        with self.assertRaisesRegex(RuntimeError, "conflict"):
            secure_io.atomic_write_text(target, "updated", pre_replace=reject)
        self.assertEqual(target.read_text(), "competitor")
        self.assertEqual((moved / "file").read_text(), "original")
        self.assertEqual(self.leftovers(parent) + self.leftovers(moved), [])

if __name__ == "__main__":
    unittest.main()

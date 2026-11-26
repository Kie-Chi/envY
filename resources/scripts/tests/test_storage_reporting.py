"""Storage metadata probes bound time, output and file reads."""

import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

from envy.storage.measurement import ScanBudget
from envy.storage.model import StorageError
from envy.storage.reporting import probe, read_plist


class ReportingTests(unittest.TestCase):
    def test_command_success_is_captured(self):
        result = probe([sys.executable, "-c", "print('metadata')"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "metadata\n")

    def test_large_output_is_rejected_without_parsing_truncated_data(self):
        result = probe([sys.executable, "-c", "print('x'*65536)"], max_bytes=1000)
        self.assertEqual(result.returncode, 125)
        self.assertEqual(result.stdout, "")

    def test_deadline_applies_to_slow_trickles_and_shared_budget(self):
        started = time.monotonic()
        result = probe([sys.executable, "-c",
                        "import time; [(print('x',flush=True),time.sleep(.02)) for _ in range(100)]"],
                       timeout=.1)
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started, 2)
        result = probe([sys.executable, "-c", "raise Exception('must not start')"],
                       budget=ScanBudget(seconds=0))
        self.assertEqual(result.returncode, 124)

    def test_native_exit_125_keeps_metadata_error_diagnostics(self):
        result = probe([sys.executable, "-c", "import sys; print('unsupported option',file=sys.stderr); sys.exit(125)"])
        self.assertEqual(result.returncode, 125)
        self.assertIn("unsupported option", result.stderr)
        self.assertNotIn("output limit", result.stderr)

    def test_plist_limits_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "Info.plist"
            path.write_bytes(plistlib.dumps({"CFBundleVersion": "1"}))
            self.assertEqual(read_plist(path)["CFBundleVersion"], "1")
            path.write_bytes(b"x" * (2 * 1024**2 + 1))
            with self.assertRaises(StorageError):
                read_plist(path)
            link = root / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_plist(link)


if __name__ == "__main__":
    unittest.main()

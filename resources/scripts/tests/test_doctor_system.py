"""Observe crash leftovers only in isolated fixture directories."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy.doctor.checks import sources, system


class WorkflowLeftoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-doctor-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.age = self.root / "age"
        self.tmp = self.root / "tmp"
        self.cached_tmp = self.root / "cached-tmp"
        for directory in (self.age, self.tmp, self.cached_tmp, self.root / "secrets"):
            directory.mkdir()
        self.enterContext(patch.object(system, "ENVY_ROOT", self.root))
        self.enterContext(patch.object(system, "AGE_KEY_DIR", self.age))
        self.enterContext(patch.dict("os.environ", {"TMPDIR": str(self.tmp)}))
        self.enterContext(patch("tempfile.gettempdir", return_value=str(self.tmp)))

    def write(self, directory, name):
        path = directory / name
        path.write_text("fixture\n")
        return path

    def test_current_and_legacy_leftovers_are_sorted_without_reading_or_removing_them(self):
        paths = [
            self.write(self.age, ".derive-crash.txt"),
            self.write(self.age, "rotate_old.txt"),
            self.write(self.tmp, ".envy-prepared-key-crash.txt"),
            self.write(self.tmp, ".envy-secrets-plain-crash.yaml"),
            self.write(self.root / "secrets", ".secrets-plain-crash.yaml"),
            self.write(self.root / "secrets", ".secrets-encrypted-crash.yaml"),
            self.write(self.root / "secrets", ".recovery-encrypted-crash.age"),
        ]
        before = {path: path.read_bytes() for path in paths}
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read contents")):
            self.assertEqual(system._workflow_leftovers(), sorted(paths))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_cached_temp_directory_and_plaintext_directory_can_differ(self):
        prepared = self.write(self.cached_tmp, ".envy-prepared-key-crash.txt")
        plaintext = self.write(self.tmp, ".envy-secrets-plain-crash.yaml")
        with patch("tempfile.gettempdir", return_value=str(self.cached_tmp)):
            self.assertEqual(system._workflow_leftovers(), sorted([prepared, plaintext]))

    def test_no_tmpdir_uses_the_writers_fallbacks(self):
        prepared = self.write(self.cached_tmp, ".envy-prepared-key-crash.txt")
        with patch.dict("os.environ", {}, clear=True), patch("tempfile.gettempdir", return_value=str(self.cached_tmp)), patch.object(Path, "glob", autospec=True) as glob:
            glob.side_effect = lambda path, pattern: [prepared] if path == self.cached_tmp and pattern == ".envy-prepared-key-*.txt" else []
            self.assertEqual(system._workflow_leftovers(), [prepared])
            self.assertIn((Path("/tmp"), ".envy-secrets-plain-*.yaml"), [call.args for call in glob.call_args_list])

    def test_normal_files_and_matching_directories_are_not_leftovers(self):
        for directory, name in ((self.age, "keys.txt"), (self.age, "keys.txt.bak"), (self.tmp, "unrelated.txt"), (self.root / "secrets", "secrets.yaml")):
            self.write(directory, name)
        (self.tmp / ".envy-prepared-key-directory.txt").mkdir()
        self.assertEqual(system._workflow_leftovers(), [])

    def test_system_check_reports_only_names_not_private_contents(self):
        path = self.write(self.tmp, ".envy-secrets-plain-crash.yaml")
        with patch.object(system.shutil, "which", return_value="fixture-command"), patch.object(
            system, "machine_manifest", return_value={}
        ), patch.object(system, "platform_name", return_value="linux"), patch.object(
            system, "run_process", side_effect=[subprocess.CompletedProcess([], 0, "master\n", ""), subprocess.CompletedProcess([], 0, "", "")]
        ), patch.object(sources, "run_checks", return_value=[]):
            results = system.run_checks()
        result = next(row for row in results if row.name == "workflow leftovers")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.message, path.name)
        self.assertNotIn("fixture", str(result.to_dict()))
        self.assertNotIn(str(path.parent), str(result.to_dict()))


if __name__ == "__main__":
    unittest.main()

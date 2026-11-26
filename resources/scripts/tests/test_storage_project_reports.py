"""Explicit project reports use isolated Git fixtures and bounded read-only probes."""

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy.storage import project_reports, reporting
from envy.storage.measurement import ScanBudget


class ProjectReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.project = self.home / "project with spaces"
        self.project.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.git("init", "--quiet", "--template=")
        self.probe = reporting.probe

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.project), "-c", "core.fsmonitor=false", *args],
                              check=True, capture_output=True, text=True, env=self.env)

    def probe_without_activity(self, argv, **kwargs):
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 1, "", "")
        return self.probe(argv, **kwargs)

    def inspect(self, projects=None, *, budget=None, probe=None):
        with patch.object(reporting, "probe", side_effect=probe or self.probe_without_activity):
            return project_reports.inspect(projects if projects is not None else [self.project],
                                           home=self.home, budget=budget or ScanBudget())

    def output(self, relative, name="artifact", content=b"valuable output"):
        path = self.project / relative
        path.mkdir()
        (path / name).write_bytes(content)
        return path

    def test_tracked_descendants_are_found_even_under_ignored_directory(self):
        target = self.output("target")
        dist = self.output("dist")
        (dist / "nested").mkdir()
        (dist / "nested" / "source.js").write_text("source")
        (self.project / ".gitignore").write_text("target/\ndist/\n")
        self.git("add", "--force", "dist/nested/source.js")
        (self.project / "Cargo.toml").write_text("contents must not be parsed")
        (self.project / "Cargo.lock").write_text("contents must not be parsed")
        with patch.object(Path, "read_text", side_effect=AssertionError("no source reads")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no source reads")):
            data = self.inspect()
        entries = {entry["relativePath"]: entry for entry in data["items"]}
        self.assertEqual(set(entries), {"target", "dist"})
        self.assertEqual(entries["target"]["git"], {"ignored": True, "tracked": False, "reasons": []})
        self.assertEqual(entries["dist"]["git"], {"ignored": True, "tracked": True, "reasons": []})
        self.assertEqual(entries["target"]["logicalBytes"], len(b"valuable output"))
        self.assertTrue(all(entry["action"] == "report" for entry in entries.values()))
        self.assertTrue(all(entry["policyEligibleBytes"] is None for entry in entries.values()))
        self.assertTrue(all(entry["nativeReclaimableBytes"] is None for entry in entries.values()))
        self.assertTrue(data["projects"][0]["files"]["Cargo.toml"]["present"])
        self.assertTrue(data["projects"][0]["files"]["Cargo.lock"]["present"])
        self.assertFalse(data["projects"][0]["files"]["package.json"]["present"])
        self.assertTrue(target.is_dir() and dist.is_dir())

    def test_only_known_existing_directories_are_measured(self):
        self.output("node_modules")
        self.output(".venv")
        self.output("custom-output")
        (self.project / "build").write_text("a file, not a directory")
        data = self.inspect()
        self.assertEqual({entry["relativePath"] for entry in data["items"]}, {"node_modules", ".venv"})
        candidates = {row["relativePath"]: row for row in data["projects"][0]["candidates"]}
        self.assertEqual(candidates["build"]["reasons"], ["not_directory"])
        self.assertFalse(candidates["dist"]["present"])
        self.assertEqual(candidates["dist"]["status"], "absent")
        self.assertEqual(set(candidates), set(project_reports.CANDIDATES))

    def test_explicit_scopes_with_same_git_root_are_independent_and_deduplicated(self):
        child = self.project / "src"
        child.mkdir()
        (child / "target").mkdir()
        (child / "target" / "nested-output").write_bytes(b"nested")
        self.output("target")
        data = self.inspect([child, self.project, child])
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["projects"][0]["root"], str(child))
        self.assertEqual(data["projects"][1]["root"], str(self.project))
        self.assertTrue(all(report["gitRoot"] == str(self.project) for report in data["projects"]))
        self.assertEqual(data["projects"][1]["status"], "complete")
        self.assertNotEqual(data["projects"][0]["itemIds"], data["projects"][1]["itemIds"])
        self.assertEqual(data["projects"][2]["status"], "duplicate")
        self.assertEqual(data["projects"][0]["itemIds"], data["projects"][2]["itemIds"])

    def test_nested_cargo_project_scans_requested_scope_with_correct_git_evidence(self):
        scope = self.project / "resources" / "scripts" / "envy-tui"
        scope.mkdir(parents=True)
        (scope / "Cargo.toml").write_text("contents must not be parsed")
        (scope / "Cargo.lock").write_text("contents must not be parsed")
        (scope / "target").mkdir()
        (scope / "target" / "output").write_bytes(b"cargo output")
        (scope / "dist").mkdir()
        (scope / "dist" / "tracked-source").write_text("source")
        self.output("target", name="tracked-root-output")
        self.output("dist")
        (self.project / ".gitignore").write_text(
            "/target/\n/resources/scripts/envy-tui/target/\n/resources/scripts/envy-tui/dist/\n")
        self.git("add", "--force", "target/tracked-root-output",
                 "resources/scripts/envy-tui/dist/tracked-source")
        calls = []
        def probe(argv, **kwargs):
            calls.append(argv)
            return self.probe_without_activity(argv, **kwargs)
        data = self.inspect([scope], probe=probe)
        report = data["projects"][0]
        self.assertEqual(report["root"], str(scope))
        self.assertEqual(report["gitRoot"], str(self.project))
        self.assertTrue(report["files"]["Cargo.toml"]["present"])
        self.assertTrue(report["files"]["Cargo.lock"]["present"])
        entries = {entry["relativePath"]: entry for entry in data["items"]}
        self.assertEqual(set(entries), {"target", "dist"})
        self.assertEqual(entries["target"]["logicalBytes"], len(b"cargo output"))
        self.assertEqual(entries["target"]["git"], {"ignored": True, "tracked": False, "reasons": []})
        self.assertEqual(entries["dist"]["git"], {"ignored": True, "tracked": True, "reasons": []})
        self.assertTrue(all(entry["path"] == str(scope / relative) for relative, entry in entries.items()))
        self.assertTrue(all(entry["projectRoot"] == str(scope) for entry in entries.values()))
        self.assertTrue(all(entry["gitRoot"] == str(self.project) for entry in entries.values()))
        self.assertTrue(all(entry["action"] == "report" for entry in entries.values()))
        self.assertTrue(all(argv[argv.index("-C") + 1] == str(scope) for argv in calls if argv[0] == "git"))

    def test_no_project_does_not_discover_repositories(self):
        with patch.object(project_reports, "open_directory", side_effect=AssertionError("no walking")), \
                patch.object(reporting, "probe", side_effect=AssertionError("no discovery")):
            data = project_reports.inspect([], home=self.home, budget=ScanBudget())
        self.assertEqual(data, {"items": [], "projects": [], "warnings": []})

    def test_root_home_and_home_ancestor_are_rejected_without_commands(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("unsafe root must not reach commands")
        data = self.inspect([Path("/"), self.home, self.home.parent], probe=forbidden)
        self.assertFalse(data["items"])
        self.assertTrue(all(report["status"] == "rejected" for report in data["projects"]))
        self.assertTrue(all(report["reasons"] == ["unsafe_project_root"] for report in data["projects"]))

    def test_actual_git_root_home_is_rejected_even_for_child_input(self):
        child = self.project / "child"
        child.mkdir()
        def home_root(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, str(self.home) + "\n", "")
        data = self.inspect([child], probe=home_root)
        self.assertEqual(data["projects"][0]["reasons"], ["unsafe_project_root"])
        self.assertFalse(data["items"])

    def test_root_and_intermediate_symlinks_are_rejected_without_commands(self):
        link = self.home / "link"
        link.symlink_to(self.project, target_is_directory=True)
        (self.project / "child").mkdir()
        def forbidden(*args, **kwargs):
            raise AssertionError("symlink must not reach commands")
        data = self.inspect([link, link / "child"], probe=forbidden)
        self.assertFalse(data["items"])
        self.assertTrue(all(report["reasons"] == ["symlink_skipped"] for report in data["projects"]))

    def test_candidate_external_symlink_is_not_measured_or_probed(self):
        outside = self.base / "outside"
        outside.mkdir()
        valuable = outside / "valuable"
        valuable.write_bytes(b"valuable")
        (self.project / "target").symlink_to(outside, target_is_directory=True)
        calls = []
        def probe(argv, **kwargs):
            calls.append(argv)
            return self.probe_without_activity(argv, **kwargs)
        data = self.inspect(probe=probe)
        self.assertFalse(data["items"])
        candidate = data["projects"][0]["candidates"][0]
        self.assertEqual(candidate["reasons"], ["symlink_skipped"])
        self.assertTrue(all("rev-parse" in argv for argv in calls))
        self.assertEqual(valuable.read_bytes(), b"valuable")

    def test_invalid_missing_and_outside_git_roots_keep_structured_reports(self):
        invalid = self.home / "not-a-repo"
        invalid.mkdir()
        missing = self.home / "missing"
        data = self.inspect([invalid, missing])
        self.assertFalse(data["items"])
        self.assertEqual(len(data["projects"]), 2)
        self.assertIn("git_root_command_failed", data["projects"][0]["reasons"])
        self.assertEqual(data["projects"][1]["reasons"], ["path_missing"])
        def wrong_root(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, str(self.base / "elsewhere") + "\n", "")
        self.assertEqual(self.inspect(probe=wrong_root)["projects"][0]["reasons"], ["invalid_git_root"])

    def test_permission_failure_does_not_become_empty_or_zero(self):
        with patch.object(project_reports, "open_directory", side_effect=PermissionError(errno.EACCES, "denied")):
            data = self.inspect()
        self.assertFalse(data["items"])
        self.assertEqual(data["projects"][0]["status"], "unknown")
        self.assertEqual(data["projects"][0]["reasons"], ["permission_denied"])
        self.assertTrue(data["warnings"])

    def test_activity_observations_are_report_only(self):
        self.output("target")
        for code, stdout, stderr, expected in (
                (0, "p123\n", "", "active"), (1, "", "", "inactive"),
                (1, "", "cannot inspect directory", "unknown"),
                (127, "", "missing", "unknown"), (124, "", "timeout", "unknown"),
                (125, "p123\n", "too large", "unknown")):
            with self.subTest(code=code, stderr=stderr):
                def probe(argv, **kwargs):
                    if argv[0] == "lsof":
                        return subprocess.CompletedProcess(argv, code, stdout, stderr)
                    return self.probe(argv, **kwargs)
                entry = self.inspect(probe=probe)["items"][0]
                self.assertEqual(entry["activity"]["status"], expected)
                self.assertEqual(entry["action"], "report")
                self.assertIsNone(entry["policyEligibleBytes"])

    def test_git_failure_preserves_unknown_flags_and_measurement(self):
        self.output("target")
        def probe(argv, **kwargs):
            if "check-ignore" in argv or "ls-files" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "failure")
            return self.probe_without_activity(argv, **kwargs)
        entry = self.inspect(probe=probe)["items"][0]
        self.assertIsNone(entry["git"]["ignored"])
        self.assertIsNone(entry["git"]["tracked"])
        self.assertEqual(entry["logicalBytes"], len(b"valuable output"))

    def test_oversized_tracked_output_uses_real_bounded_probe_and_stays_unknown(self):
        self.output("dist")
        def probe(argv, **kwargs):
            if "ls-files" in argv:
                return self.probe([sys.executable, "-c", "print('x' * 200000)"], **kwargs)
            return self.probe_without_activity(argv, **kwargs)
        data = self.inspect(probe=probe)
        self.assertIsNone(data["items"][0]["git"]["tracked"])
        self.assertIn("tracked_command_output_limit", data["items"][0]["git"]["reasons"])
        self.assertEqual(data["projects"][0]["status"], "partial")

    def test_budget_deadline_is_shared_and_prevents_any_commands(self):
        for budget in (ScanBudget(seconds=0), ScanBudget(max_entries=0), ScanBudget(max_depth=1)):
            with self.subTest(budget=budget):
                def forbidden(*args, **kwargs):
                    raise AssertionError("exhausted budget must not start commands")
                data = self.inspect([self.project, self.project / "missing"], budget=budget, probe=forbidden)
                self.assertFalse(data["items"])
                self.assertTrue(all("scan_budget_exhausted" in report["reasons"] for report in data["projects"]))

    def test_measurement_budget_is_not_reset_for_each_candidate_or_project(self):
        first = self.output("target")
        self.output("dist")
        for number in range(50):
            (first / str(number)).write_bytes(b"x")
        budget = ScanBudget(max_entries=45)
        data = self.inspect([self.project, self.home / "another"], budget=budget)
        self.assertLessEqual(budget.entries, 45)
        self.assertIsNone(data["items"][0]["logicalBytes"])
        self.assertIn("scan_budget_exhausted", data["items"][0]["reasons"])
        self.assertIn("scan_budget_exhausted", data["projects"][1]["reasons"])

    def test_git_environment_cannot_route_report_to_another_repository(self):
        self.output("target")
        def probe(argv, **kwargs):
            if argv[0] == "git":
                self.assertNotIn("GIT_DIR", kwargs["env"])
                self.assertEqual(kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
            return self.probe_without_activity(argv, **kwargs)
        with patch.dict(os.environ, {"GIT_DIR": str(self.base / "foreign")}):
            data = self.inspect(probe=probe)
        self.assertEqual(len(data["items"]), 1)


if __name__ == "__main__":
    unittest.main()

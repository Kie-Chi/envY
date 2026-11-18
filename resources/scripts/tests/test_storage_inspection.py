"""Read-only storage reports use isolated metadata and no lifecycle writes."""

import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from envy.storage import cli, inspection, measurement, application_reports, docker, project_reports
from envy.storage.measurement import ScanBudget, measure
from envy.storage.model import StorageError


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_sparse_and_hardlinked_files_have_separate_nonduplicated_metrics(self):
        path = self.root / "sparse"
        with path.open("wb") as handle:
            handle.seek(16 * 1024**2)
            handle.write(b"x")
        os.link(path, self.root / "alias")
        data = measure(self.root, ScanBudget())
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["logicalBytes"], path.stat().st_size)
        self.assertEqual(data["allocatedBytes"], (path.stat().st_blocks + self.root.stat().st_blocks) * 512)
        self.assertLess(data["allocatedBytes"], data["logicalBytes"])
        self.assertEqual(data["hardlinksDeduplicated"], 1)

    def test_root_intermediate_and_child_links_are_not_followed(self):
        target = self.root / "target"
        target.mkdir()
        (target / "valuable").write_bytes(b"x" * 2048)
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        child = target / "child"
        child.mkdir()
        for root in (link, link / "child"):
            result = measure(root, ScanBudget())
            self.assertEqual(result["status"], "skipped")
            self.assertIsNone(result["allocatedBytes"])
            self.assertEqual(result["reasons"], ["symlink_skipped"])
        result = measure(self.root, ScanBudget())
        self.assertEqual(result["logicalBytes"], 2048)
        self.assertEqual(result["symlinksSkipped"], 1)

    def test_budget_is_shared_and_exhaustion_is_unknown_not_zero(self):
        (self.root / "one").write_bytes(b"x")
        budget = ScanBudget(max_entries=1)
        first = measure(self.root, budget)
        second = measure(self.root, budget)
        for result in (first, second):
            self.assertIsNone(result["allocatedBytes"])
            self.assertIn("scan_budget_exhausted", result["reasons"])
        self.assertEqual(first["observedLogicalBytes"], 1)
        self.assertEqual(budget.entries, 1)
        self.assertIsNone(measure(self.root, ScanBudget(seconds=0))["logicalBytes"])

    def test_permission_failure_is_distinct_from_absence(self):
        with patch.object(measurement, "open_directory", side_effect=PermissionError(errno.EACCES, "private")):
            result = measure(self.root, ScanBudget())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reasons"], ["permission_denied"])
        self.assertIsNone(result["logicalBytes"])
        result = measure(self.root / "absent", ScanBudget())
        self.assertEqual(result["status"], "absent")
        self.assertEqual(result["allocatedBytes"], 0)

    def test_deep_tree_stops_before_fd_or_recursion_exhaustion(self):
        root = self.root
        for _ in range(8):
            root = root / "nested"
            root.mkdir()
        (root / "valuable").write_bytes(b"x")
        result = measure(self.root, ScanBudget(max_depth=3))
        self.assertEqual(result["status"], "partial")
        self.assertIn("depth_limit", result["reasons"])
        self.assertIsNone(result["logicalBytes"])

    def test_directory_replaced_during_open_is_not_counted_as_original(self):
        child = self.root / "child"
        child.mkdir()
        opened = measurement.os.open
        replaced = False

        def replace(path, flags, **kwargs):
            nonlocal replaced
            if path == "child" and "dir_fd" in kwargs and not replaced:
                child.rename(self.root / "original")
                child.mkdir()
                (child / "new-data").write_bytes(b"replacement")
                replaced = True
            return opened(path, flags, **kwargs)

        with patch.object(measurement.os, "open", side_effect=replace):
            result = measure(self.root, ScanBudget())
        self.assertIn("directory_changed", result["reasons"])
        self.assertIsNone(result["allocatedBytes"])
        self.assertTrue((child / "new-data").exists())


def frontend_contract_fixture():
    """Generate a stable frontend fixture through the real inspect CLI serializer."""
    from contextlib import ExitStack

    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        home = Path(temporary).resolve()
        (home / ".codex").mkdir()
        opened = measurement.open_directory

        def open_fixture(path):
            if path == home / ".codex":
                raise PermissionError(errno.EACCES, "synthetic denied")
            return opened(path)

        stack.enter_context(patch.object(measurement, "open_directory", side_effect=open_fixture))
        for name, module in (("docker", docker), ("applications", application_reports), ("projects", project_reports)):
            stack.enter_context(patch.object(module, "inspect", return_value={
                "status": "unknown", "items": [], "warnings": [name + ": synthetic provider unavailable"],
            }))
        inspect = inspection.inspect
        stack.enter_context(patch.object(inspection, "inspect", side_effect=lambda root, **kwargs: inspect(root, platform="darwin", **kwargs)))
        stack.enter_context(patch.object(cli.inspection.Path, "home", return_value=home))
        output = io.StringIO()
        stack.enter_context(patch.object(cli.log, "console", Console(file=output, color_system=None)))
        result = CliRunner().invoke(cli.app, ["inspect", "--json"])
        if result.exit_code:
            raise AssertionError(result.output) from result.exception
        # Normalize only the synthetic temporary home; no personal paths are read.
        return json.loads(output.getvalue().replace(str(home), "/fixture/home"))


class InspectionTests(unittest.TestCase):
    def setUp(self):
        for module in (docker, application_reports, project_reports):
            mocked = patch.object(module, "inspect", return_value={"items": [], "warnings": []})
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_cli_producer_matches_the_shared_rust_consumer_fixture(self):
        fixture = cli.ENVY_ROOT / "resources/scripts/envy-tui/src/storage/fixtures/inspect-darwin.json"
        expected = json.loads(fixture.read_text())
        self.assertEqual(frontend_contract_fixture(), expected)
        rows = {row["id"]: row for row in expected["data"]["items"]}
        self.assertIsNone(rows["codex-data"]["allocatedBytes"])
        self.assertEqual(rows["codex-data"]["status"], "unknown")
        self.assertEqual(rows["nix-cache"]["allocatedBytes"], 0)
        self.assertEqual(rows["nix-tarballs"]["includedIn"], "nix-cache")

    def test_nested_rows_are_annotated_and_never_summed(self):
        items = [{"id": "parent", "path": "/home/cache"},
                 {"id": "child", "path": "/home/cache/tool"}]
        inspection.annotate_containment(items)
        self.assertIsNone(items[0]["includedIn"])
        self.assertEqual(items[1]["includedIn"], "parent")

    def test_report_does_not_invoke_commands_or_write_storage_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            output = io.StringIO()
            with patch.object(cli.log, "console", Console(file=output, color_system=None)), patch.object(cli, "_context", side_effect=AssertionError("policy must not load")),                     patch.object(cli, "locked", side_effect=AssertionError("state must not lock")),                     patch.object(cli.inspection.Path, "home", return_value=home):
                result = CliRunner().invoke(cli.app, ["inspect", "--json"])
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["command"], "storage inspect")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["data"]["readOnly"])
            self.assertFalse(payload["data"]["policyEvaluated"])
            self.assertFalse(payload["data"]["totalsAdditive"])
            self.assertEqual(payload["warnings"], payload["data"]["warnings"])
            self.assertTrue(all(item["action"] == "report" for item in payload["data"]["items"]))
            self.assertEqual(list(home.iterdir()), [])

    def test_cli_error_keeps_json_envelope(self):
        output = io.StringIO()
        with patch.object(cli.log, "console", Console(file=output, color_system=None)), \
                patch.object(inspection, "inspect", side_effect=StorageError("scan failed")):
            result = CliRunner().invoke(cli.app, ["inspect", "--json"])
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "storage_error")

    def test_terminal_unknowns_are_not_formatted_as_zero(self):
        entry = {"id": "blocked", "path": "/missing", "status": "unknown", "note": "permission denied"}
        with patch.object(inspection, "inspect", return_value={"items": [entry]}):
            output = io.StringIO()
            with patch.object(cli.log, "console", Console(file=output, width=160, color_system=None)):
                result = CliRunner().invoke(cli.app, ["inspect"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("unknown", output.getvalue())
        self.assertNotIn("0.000", output.getvalue())


if __name__ == "__main__":
    unittest.main()

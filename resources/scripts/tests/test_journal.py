import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer

from envy import journal
from envy.process import CommandError


class JournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        patcher = patch("envy.journal.state_dir", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_append_creates_jsonl_record_with_required_fields(self):
        journal.append({"schemaVersion": 1, "operation": "apply", "result": "ok"})
        lines = journal.journal_path().read_text().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["operation"], "apply")
        # The private state directory must not be world/group readable.
        self.assertEqual(self.state.stat().st_mode & 0o077, 0)

    def test_read_returns_newest_first(self):
        for name in ("apply", "sync", "push"):
            journal.append({"operation": name, "result": "ok"})
        self.assertEqual(
            [r["operation"] for r in journal.read()], ["push", "sync", "apply"]
        )

    def test_read_limit_truncates_to_newest(self):
        for name in ("a", "b", "c", "d", "e"):
            journal.append({"operation": name, "result": "ok"})
        self.assertEqual([r["operation"] for r in journal.read(limit=2)], ["e", "d"])

    def test_read_failed_filter(self):
        journal.append({"operation": "apply", "result": "ok"})
        journal.append({"operation": "push", "result": "fail"})
        self.assertEqual([r["operation"] for r in journal.read(failed=True)], ["push"])

    def test_read_operation_filter(self):
        journal.append({"operation": "apply", "result": "ok"})
        journal.append({"operation": "push", "result": "ok"})
        self.assertEqual(
            [r["operation"] for r in journal.read(operation="push")], ["push"]
        )

    def test_read_skips_malformed_lines(self):
        journal.append({"operation": "apply", "result": "ok"})
        with journal.journal_path().open("a", encoding="utf-8") as handle:
            handle.write("not json\n")
        self.assertEqual(len(journal.read()), 1)

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(journal.read(), [])

    def test_decorator_records_success(self):
        @journal.record_operation("apply")
        def op():
            return "done"

        self.assertEqual(op(), "done")
        record = journal.read()[0]
        self.assertEqual(record["operation"], "apply")
        self.assertEqual(record["result"], "ok")
        self.assertEqual(record["exitCode"], 0)
        self.assertIn("durationMs", record)
        self.assertIn("timestamp", record)

    def test_decorator_records_typer_exit_and_reraises(self):
        @journal.record_operation("push")
        def op():
            raise typer.Exit(code=2)

        with self.assertRaises(typer.Exit):
            op()
        record = journal.read()[0]
        self.assertEqual(record["result"], "fail")
        self.assertEqual(record["exitCode"], 2)

    def test_decorator_records_typer_exit_zero_as_ok(self):
        @journal.record_operation("apply")
        def op():
            raise typer.Exit(code=0)

        with self.assertRaises(typer.Exit):
            op()
        self.assertEqual(journal.read()[0]["result"], "ok")

    def test_decorator_records_command_error_exit_code(self):
        import subprocess

        @journal.record_operation("sync")
        def op():
            raise CommandError(subprocess.CompletedProcess(["git"], 3, "", "boom"))

        with self.assertRaises(CommandError):
            op()
        record = journal.read()[0]
        self.assertEqual(record["result"], "fail")
        self.assertEqual(record["exitCode"], 3)

    def test_decorator_records_generic_exception(self):
        @journal.record_operation("apply")
        def op():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            op()
        record = journal.read()[0]
        self.assertEqual(record["result"], "fail")
        self.assertEqual(record["exitCode"], 1)

    def test_decorator_captures_detail_callable(self):
        @journal.record_operation("push", detail=lambda **kw: {"remote": kw.get("remote")})
        def op(*, remote):
            return remote

        op(remote="origin")
        self.assertEqual(journal.read()[0]["detail"], {"remote": "origin"})

    def test_decorator_detail_failure_does_not_break_operation(self):
        @journal.record_operation("apply", detail=lambda **kw: 1 / 0)
        def op():
            return "ok"

        self.assertEqual(op(), "ok")
        self.assertEqual(journal.read()[0]["detail"], {})

    def test_decorator_skip_predicate_bypasses_journal(self):
        @journal.record_operation(
            "rollback", skip=lambda *, dry_run=False, **_: dry_run
        )
        def op(*, dry_run=False):
            return "ran"

        self.assertEqual(op(dry_run=True), "ran")
        self.assertEqual(journal.read(), [])
        op(dry_run=False)
        self.assertEqual(len(journal.read()), 1)

    def test_terminal_confirmation_requires_causal_order_matching_identity_and_complete_prior_events(self):
        start = {"event": "started", "operationId": "fixture", "operation": "apply"}
        terminal = {"event": "terminal", "operationId": "fixture", "operation": "apply", "result": "ok",
                    "terminalId": "terminal-one", "priorEventsPersisted": True, "journalPersisted": True}
        confirm = {"event": "terminal-confirmed", "operationId": "fixture", "terminalId": "terminal-one"}
        cases = [
            ([start, terminal], False),
            ([confirm, start, terminal], False),
            ([start, terminal, {**confirm, "terminalId": "other-terminal"}], False),
            ([start, terminal, {**confirm, "operationId": "other-operation"}], False),
            ([terminal, confirm], False),
            ([start, {**terminal, "priorEventsPersisted": False}, confirm], False),
            ([start, terminal, {**terminal, "terminalId": "terminal-two"}, confirm], False),
            ([start, terminal, confirm], True),
        ]
        for index, (records, expected) in enumerate(cases):
            with self.subTest(case=index), patch.object(journal, "state_dir", return_value=self.state / str(index)):
                for record in records:
                    self.assertTrue(journal.append(record))
                entries = journal.read()
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["journalPersisted"], expected)
                self.assertNotIn("terminalId", entries[0])
                self.assertNotIn("priorEventsPersisted", entries[0])

    def test_interleaved_operations_confirm_only_their_own_terminal(self):
        for operation_id in ("one", "two"):
            self.assertTrue(journal.append({"event": "started", "operationId": operation_id, "operation": "apply"}))
        for operation_id in ("one", "two"):
            self.assertTrue(journal.append({"event": "terminal", "operationId": operation_id, "operation": "apply",
                "result": "ok", "priorEventsPersisted": True, "terminalId": f"terminal-{operation_id}"}))
        self.assertTrue(journal.append({"event": "terminal-confirmed", "operationId": "two", "terminalId": "terminal-two"}))
        entries = {entry["operationId"]: entry for entry in journal.read()}
        self.assertFalse(entries["one"]["journalPersisted"])
        self.assertTrue(entries["two"]["journalPersisted"])
        self.assertTrue(journal.append({"event": "terminal-confirmed", "operationId": "one", "terminalId": "terminal-one"}))
        self.assertTrue(all(entry["journalPersisted"] for entry in journal.read()))

    def test_snapshot_shape(self):
        journal.append({"operation": "apply", "result": "ok"})
        payload = journal.snapshot(limit=10)
        self.assertEqual(payload["schemaVersion"], journal.JOURNAL_SCHEMA_VERSION)
        self.assertIn("machine", payload)
        self.assertIn("platform", payload)
        self.assertEqual(len(payload["entries"]), 1)


if __name__ == "__main__":
    unittest.main()

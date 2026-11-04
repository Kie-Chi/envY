"""Shared-branch sync against disposable local Git remotes, never the checkout."""
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer
from rich.console import Console

from envy import journal
from envy.workflows import git as workflow


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-sync-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote.git"
        self.local = self.root / "local"
        self.peer = self.root / "peer"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "HOME": str(self.root),
            "GIT_AUTHOR_NAME": "envy tests", "GIT_AUTHOR_EMAIL": "tests@example.invalid",
            "GIT_COMMITTER_NAME": "envy tests", "GIT_COMMITTER_EMAIL": "tests@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "ENVY_FLAKE_SOURCE_MIRROR": "0",
        })
        self.git(self.root, "init", "--bare", "--initial-branch=master", str(self.remote))
        self.git(self.root, "clone", "--quiet", str(self.remote), str(self.local))
        self.commit(self.local, "history.txt", "initial\n")
        self.git(self.local, "push", "origin", "master")
        self.git(self.root, "clone", "--quiet", str(self.remote), str(self.peer))
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.enterContext(patch.object(workflow, "ENVY_ROOT", self.local))
        self.journal = self.enterContext(patch.object(journal, "append"))
        self.enterContext(patch.object(journal, "current_machine_id", return_value="test-machine"))
        self.enterContext(patch.object(workflow, "current_machine_id", return_value="test-machine"))
        self.enterContext(patch.object(workflow, "machine_build_attr", return_value="fixture-target"))
        self.refine = self.enterContext(patch.object(workflow, "refine_before_apply"))
        self.require = self.enterContext(patch.object(workflow, "require_current_machine_file"))
        self.apply = self.enterContext(patch.object(workflow, "run_apply"))
        self.output = io.StringIO()
        self.enterContext(patch.object(workflow.log, "console", Console(file=self.output, width=180, color_system=None)))
        self.commands = []
        self.build_exit = 0
        actual_run = workflow.run_process

        def run(command, **kwargs):
            self.commands.append(list(command))
            if command[0] == "nix":
                return subprocess.CompletedProcess(command, self.build_exit, "", "")
            if command[0] != "git":
                raise AssertionError(f"unexpected external command: {command}")
            kwargs.setdefault("timeout", 30)
            return actual_run(command, **kwargs)

        self.enterContext(patch.object(workflow, "run_process", side_effect=run))

    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, env=self.env, capture_output=True,
                              text=True, check=True, timeout=30).stdout.strip()

    def commit(self, root, name, content):
        (root / name).write_text(content)
        self.git(root, "add", "--", name)
        self.git(root, "commit", "-m", "test: update fixture")
        return self.git(root, "rev-parse", "HEAD")

    def advance_remote(self, name="history.txt", content="remote\n"):
        revision = self.commit(self.peer, name, content)
        self.git(self.peer, "push", "origin", "master")
        return revision

    def sync(self, *, remote="origin", no_apply=True, build_only=False):
        workflow.sync(remote=remote, branch="master", no_apply=no_apply, build_only=build_only)

    def assert_no_activation(self):
        self.refine.assert_not_called()
        self.require.assert_not_called()
        self.apply.assert_not_called()
        self.assertFalse(any(command[0] == "nix" for command in self.commands))

    def test_dirty_worktree_refuses_before_fetch_and_preserves_index(self):
        (self.local / "staged.txt").write_text("staged\n")
        self.git(self.local, "add", "--", "staged.txt")
        before = self.git(self.local, "diff", "--cached", "--binary")
        with self.assertRaises(typer.Exit):
            self.sync()
        self.assertEqual(self.git(self.local, "diff", "--cached", "--binary"), before)
        self.assertFalse(any(command[1] == "fetch" for command in self.commands))
        self.assert_no_activation()

    def test_wrong_or_detached_branch_refuses_before_fetch(self):
        for detached in (False, True):
            with self.subTest(detached=detached):
                if detached:
                    self.git(self.local, "checkout", "--detach")
                else:
                    self.git(self.local, "checkout", "-b", "other")
                self.commands.clear()
                with self.assertRaises(typer.Exit):
                    self.sync()
                self.assertFalse(any(command[1] == "fetch" for command in self.commands))
                self.assert_no_activation()

    def test_no_remotes_refuses_without_activation(self):
        isolated = self.root / "no-remote"
        self.git(self.root, "init", "--initial-branch=master", str(isolated))
        self.commit(isolated, "history.txt", "initial\n")
        with patch.object(workflow, "ENVY_ROOT", isolated), self.assertRaises(typer.Exit):
            self.sync(remote=None)
        self.assertIn("no Git remotes", self.output.getvalue())
        self.assert_no_activation()

    def test_missing_remote_branch_refuses_when_explicitly_selected(self):
        self.git(self.remote, "update-ref", "-d", "refs/heads/master")
        self.git(self.local, "update-ref", "-d", "refs/remotes/origin/master")
        before = self.git(self.local, "rev-parse", "HEAD")
        with self.assertRaises(typer.Exit):
            self.sync()
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertIn("remote branch does not exist", self.output.getvalue())
        self.assert_no_activation()

    def add_backup(self):
        backup = self.root / "backup.git"
        self.git(self.root, "clone", "--bare", "--quiet", str(self.remote), str(backup))
        settings = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "remote.backup.url",
                    "GIT_CONFIG_VALUE_0": str(backup), "GIT_CONFIG_KEY_1": "remote.backup.fetch",
                    "GIT_CONFIG_VALUE_1": "+refs/heads/*:refs/remotes/backup/*"}
        self.env.update(settings)
        os.environ.update(settings)
        return backup

    def test_missing_remote_branch_is_skipped_when_scanning_all_remotes(self):
        self.add_backup()
        before = self.git(self.local, "rev-parse", "HEAD")
        self.git(self.remote, "update-ref", "-d", "refs/heads/master")
        self.git(self.local, "update-ref", "-d", "refs/remotes/origin/master")
        self.sync(remote=None)
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertIn("does not provide", self.output.getvalue())
        self.assert_no_activation()

    def test_no_remote_provides_master_refuses(self):
        self.git(self.remote, "update-ref", "-d", "refs/heads/master")
        self.git(self.local, "update-ref", "-d", "refs/remotes/origin/master")
        with self.assertRaises(typer.Exit):
            self.sync(remote=None)
        self.assertIn("no remote provides", self.output.getvalue())
        self.assert_no_activation()

    def test_no_apply_fast_forwards_without_publishing(self):
        revision = self.advance_remote()
        self.sync()
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), revision)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), revision)
        self.assertIn(["git", "merge", "--ff-only", "origin/master"], self.commands)
        self.assertFalse(any(command[1] == "push" for command in self.commands))
        self.assert_no_activation()

    def test_local_ahead_keeps_local_history_without_publishing(self):
        remote_revision = self.git(self.remote, "rev-parse", "master")
        local_revision = self.commit(self.local, "local.txt", "local\n")
        self.sync()
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), local_revision)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_revision)
        self.assertFalse(any(command[1] in {"merge", "push"} for command in self.commands))
        self.assert_no_activation()

    def test_compatible_divergence_merges_without_activation(self):
        local_revision = self.commit(self.local, "local.txt", "local\n")
        remote_revision = self.advance_remote()
        self.sync()
        self.git(self.local, "merge-base", "--is-ancestor", local_revision, "HEAD")
        self.git(self.local, "merge-base", "--is-ancestor", remote_revision, "HEAD")
        self.assertEqual(self.git(self.local, "show", "HEAD:history.txt"), "remote")
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_revision)
        self.assert_no_activation()

    def test_conflict_keeps_merge_state_and_stops_before_activation(self):
        local_revision = self.commit(self.local, "history.txt", "local\n")
        remote_revision = self.advance_remote()
        with self.assertRaises(typer.Exit):
            self.sync(no_apply=False)
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), local_revision)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_revision)
        self.assertTrue((self.local / ".git/MERGE_HEAD").is_file())
        self.assertIn("manual conflict resolution", self.output.getvalue())
        self.assert_no_activation()

    def test_build_only_runs_after_preflight_and_never_activates(self):
        events = []
        self.refine.side_effect = lambda: events.append("refine")
        self.require.side_effect = lambda: events.append("require")
        self.sync(no_apply=False, build_only=True)
        self.assertEqual(events, ["refine", "require"])
        self.assertIn(["nix", "build", "--impure", "--no-link", "fixture-target"], self.commands)
        self.apply.assert_not_called()

    def test_failed_build_preserves_exit_code_without_activation(self):
        self.build_exit = 7
        with self.assertRaises(typer.Exit) as raised:
            self.sync(no_apply=False, build_only=True)
        self.assertEqual(raised.exception.exit_code, 7)
        self.refine.assert_called_once()
        self.require.assert_called_once()
        self.apply.assert_not_called()

    def test_default_mode_activates_only_after_preflight(self):
        events = []
        self.refine.side_effect = lambda: events.append("refine")
        self.require.side_effect = lambda: events.append("require")
        self.apply.side_effect = lambda: events.append("apply")
        self.sync(no_apply=False)
        self.assertEqual(events, ["refine", "require", "apply"])
        self.assertFalse(any(command[0] == "nix" for command in self.commands))

    def test_failed_preflight_stops_before_build_or_activation(self):
        self.refine.side_effect = typer.Exit(1)
        with self.assertRaises(typer.Exit):
            self.sync(no_apply=False, build_only=True)
        self.require.assert_not_called()
        self.apply.assert_not_called()
        self.assertFalse(any(command[0] == "nix" for command in self.commands))


if __name__ == "__main__":
    unittest.main()

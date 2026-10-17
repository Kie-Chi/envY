import os
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import typer
from rich.console import Console

from envy import git_safety, journal
from envy.workflows import git as git_workflow


ENCRYPTED_SECRET = """\
token: ENC[AES256_GCM,data:fixture]
sops:
  age:
    - recipient: age1fixture
      enc: fixture
  mac: ENC[AES256_GCM,data:fixture]
"""


class PushIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-push-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote.git"
        self.local = self.root / "local"
        self.peer = self.root / "peer"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "GIT_AUTHOR_NAME": "envy tests",
            "GIT_AUTHOR_EMAIL": "envy-tests@example.invalid",
            "GIT_COMMITTER_NAME": "envy tests",
            "GIT_COMMITTER_EMAIL": "envy-tests@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "ENVY_FLAKE_SOURCE_MIRROR": "0",
        })
        self.git(self.root, "init", "--bare", "--initial-branch=master", str(self.remote))
        self.git(self.root, "clone", "--quiet", str(self.remote), str(self.local))
        self.write(self.local, "secrets/secrets.yaml", ENCRYPTED_SECRET)
        for name in ("history.txt", "staged.txt", "unstaged.txt"):
            self.write(self.local, name, "initial\n")
        self.write(self.local, "hosts/darwin/one.nix", "{ ... }: {}\n")
        self.commit(self.local, "test: initialize repository")
        self.git(self.local, "push", "origin", "master")
        self.git(self.root, "clone", "--quiet", str(self.remote), str(self.peer))

        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.enterContext(patch.object(git_workflow, "ENVY_ROOT", self.local))
        self.enterContext(patch.object(git_safety, "ENVY_ROOT", self.local))
        self.enterContext(patch.object(journal, "append"))
        self.enterContext(patch.object(journal, "current_machine_id", return_value="one"))
        self.enterContext(patch.object(git_workflow, "current_machine_id", return_value="one"))
        self.enterContext(patch.object(git_workflow, "platform_name", return_value="darwin"))
        self.output = StringIO()
        self.enterContext(patch.object(
            git_workflow.log, "console", Console(file=self.output, force_terminal=False, width=180),
        ))
        self.commands = []
        run_process = git_workflow.run_process

        def recorded(command, **kwargs):
            self.commands.append(list(command))
            return run_process(command, **kwargs)

        self.enterContext(patch.object(git_workflow, "run_process", side_effect=recorded))

    def git(self, root, *args):
        result = subprocess.run(
            ["git", *args], cwd=root, env=self.env, capture_output=True,
            text=True, check=True, timeout=30,
        )
        return result.stdout.strip()

    def write(self, root, relative, value):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def commit(self, root, message):
        self.git(root, "add", "-A")
        self.git(root, "commit", "-m", message)
        return self.git(root, "rev-parse", "HEAD")

    def advance_remote(self, relative="history.txt", value="remote change\n"):
        self.write(self.peer, relative, value)
        commit = self.commit(self.peer, "test: advance remote")
        self.git(self.peer, "push", "origin", "master")
        return commit

    def push(self, *, yes=True, machine_only=False, self_only=False, remote="origin"):
        git_workflow.push(
            msg="test: publish local changes", remote=remote, branch="master",
            machine_only=machine_only, self_only=self_only, yes=yes,
        )

    def actions(self):
        return [command[1] for command in self.commands]

    def assert_no_publication(self):
        self.assertNotIn("commit", self.actions())
        self.assertNotIn("push", self.actions())

    def make_divergence(self):
        self.write(self.local, "history.txt", "local history\n")
        local_commit = self.commit(self.local, "test: diverge locally")
        remote_commit = self.advance_remote(value="remote history\n")
        return local_commit, remote_commit

    def add_backup_remote(self):
        backup = self.root / "backup.git"
        self.git(self.root, "clone", "--bare", "--quiet", str(self.remote), str(backup))
        # Apply the test-only remote through the environment, never git config.
        settings = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "remote.backup.url",
            "GIT_CONFIG_VALUE_0": str(backup),
            "GIT_CONFIG_KEY_1": "remote.backup.fetch",
            "GIT_CONFIG_VALUE_1": "+refs/heads/*:refs/remotes/backup/*",
        }
        self.env.update(settings)
        os.environ.update(settings)
        return backup

    def test_new_remote_branch_is_pushed_without_syncing_a_missing_ref(self):
        self.git(self.remote, "update-ref", "-d", "refs/heads/master")
        self.git(self.local, "update-ref", "-d", "refs/remotes/origin/master")
        self.write(self.local, "staged.txt", "initial remote publication\n")
        self.push()

        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), self.git(self.remote, "rev-parse", "master"))
        self.assertIn(["git", "push", "-u", "origin", "master"], self.commands)
        self.assertNotIn("merge", self.actions())
        self.assertNotIn("stash", self.actions())

    def test_multi_remote_sync_selects_the_newest_history_and_publishes_to_both(self):
        backup = self.add_backup_remote()
        remote_commit = self.advance_remote()
        self.write(self.peer, "history.txt", "newest remote history\n")
        newest_commit = self.commit(self.peer, "test: advance backup remote")
        self.git(self.peer, "push", "backup", "master")
        self.write(self.local, "staged.txt", "pending local change\n")
        self.push(remote=None)

        published = self.git(self.local, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), published)
        self.assertEqual(self.git(backup, "rev-parse", "master"), published)
        self.git(self.local, "merge-base", "--is-ancestor", remote_commit, "HEAD")
        self.git(self.local, "merge-base", "--is-ancestor", newest_commit, "HEAD")
        self.assertEqual(self.git(self.remote, "show", "master:history.txt"), "newest remote history")

    def test_divergent_remotes_stop_before_backup_merge_or_publication(self):
        backup = self.add_backup_remote()
        backup_peer = self.root / "backup-peer"
        self.git(self.root, "clone", "--quiet", str(backup), str(backup_peer))
        self.advance_remote()
        self.write(backup_peer, "history.txt", "independent backup history\n")
        self.commit(backup_peer, "test: diverge backup remote")
        self.git(backup_peer, "push", "origin", "master")
        self.write(self.local, "staged.txt", "local pending change\n")
        self.git(self.local, "add", "staged.txt")
        before_index = self.git(self.local, "diff", "--cached", "--binary")
        with self.assertRaises(typer.Exit):
            self.push(remote=None)

        self.assertEqual(self.git(self.local, "diff", "--cached", "--binary"), before_index)
        self.assertNotIn("merge", self.actions())
        self.assertNotIn("stash", self.actions())
        self.assert_no_publication()

    def test_unfinished_rebase_is_rejected_before_fetch_or_backup(self):
        (self.local / ".git/rebase-merge").mkdir()
        with self.assertRaises(typer.Exit):
            self.push()

        self.assertNotIn("fetch", self.actions())
        self.assertNotIn("stash", self.actions())
        self.assert_no_publication()

    def test_clean_remote_ahead_fast_forwards_without_commit_or_push(self):
        remote_commit = self.advance_remote()
        self.push()

        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), remote_commit)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_commit)
        self.assertNotIn("stash", self.actions())
        self.assert_no_publication()

    def test_dirty_remote_ahead_is_synchronized_before_commit_and_push(self):
        remote_commit = self.advance_remote()
        self.write(self.local, "staged.txt", "staged local change\n")
        self.git(self.local, "add", "staged.txt")
        self.write(self.local, "unstaged.txt", "unstaged local change\n")
        self.write(self.local, "new.txt", "new local file\n")
        self.push()

        local_commit = self.git(self.local, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), local_commit)
        self.git(self.local, "merge-base", "--is-ancestor", remote_commit, "HEAD")
        self.assertEqual(self.git(self.remote, "show", "master:history.txt"), "remote change")
        self.assertEqual(self.git(self.remote, "show", "master:new.txt"), "new local file")
        self.assertEqual(self.git(self.local, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.local, "stash", "list"), "")
        actions = self.actions()
        self.assertLess(actions.index("merge"), actions.index("commit"))
        self.assertLess(actions.index("commit"), actions.index("push"))

    def test_sync_restores_mixed_index_unstaged_and_untracked_changes(self):
        self.write(self.local, "old-backup.txt", "existing stash\n")
        self.git(self.local, "stash", "push", "--include-untracked", "-m", "existing backup")
        existing_stash = self.git(self.local, "stash", "list", "--format=%H")
        self.advance_remote()
        self.write(self.local, "staged.txt", "staged version\n")
        self.git(self.local, "add", "staged.txt")
        self.write(self.local, "staged.txt", "unstaged version of staged file\n")
        self.write(self.local, "unstaged.txt", "another unstaged version\n")
        self.write(self.local, "new.txt", "untracked version\n")
        before_index = self.git(self.local, "diff", "--cached", "--binary")
        before_worktree = self.git(self.local, "diff", "--binary")
        before_status = self.git(self.local, "status", "--porcelain")

        with patch.object(git_workflow, "confirm_push_scope", return_value=False):
            with self.assertRaises(typer.Abort):
                self.push(yes=False)

        self.assertEqual(self.git(self.local, "diff", "--cached", "--binary"), before_index)
        self.assertEqual(self.git(self.local, "diff", "--binary"), before_worktree)
        self.assertEqual(self.git(self.local, "status", "--porcelain"), before_status)
        self.assertEqual((self.local / "new.txt").read_text(), "untracked version\n")
        self.assertEqual(self.git(self.local, "stash", "list", "--format=%H"), existing_stash)
        self.assert_no_publication()

    def test_clean_divergence_is_merged_and_pushed(self):
        self.write(self.local, "staged.txt", "local committed change\n")
        local_commit = self.commit(self.local, "test: local commit")
        remote_commit = self.advance_remote()
        self.push()

        parents = self.git(self.local, "rev-list", "--parents", "-1", "HEAD").split()
        self.assertEqual(set(parents[1:]), {local_commit, remote_commit})
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), self.git(self.remote, "rev-parse", "master"))
        self.assertNotIn("stash", self.actions())

    def test_dirty_divergence_restores_changes_after_merging_history(self):
        self.write(self.local, "unstaged.txt", "local committed change\n")
        local_commit = self.commit(self.local, "test: local commit")
        remote_commit = self.advance_remote()
        self.write(self.local, "staged.txt", "local pending change\n")
        self.git(self.local, "add", "staged.txt")
        self.push()

        self.git(self.local, "merge-base", "--is-ancestor", local_commit, "HEAD")
        self.git(self.local, "merge-base", "--is-ancestor", remote_commit, "HEAD")
        self.assertEqual(self.git(self.remote, "show", "master:staged.txt"), "local pending change")
        self.assertEqual(self.git(self.local, "stash", "list"), "")

    def test_clean_merge_conflict_stops_before_publication(self):
        local_commit, remote_commit = self.make_divergence()
        with self.assertRaises(typer.Exit):
            self.push()

        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), local_commit)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_commit)
        self.assertTrue((self.local / ".git/MERGE_HEAD").exists())
        self.assertIn("history.txt", self.git(self.local, "diff", "--name-only", "--diff-filter=U"))
        self.assert_no_publication()

    def test_dirty_merge_conflict_retains_stash_and_stops_before_publication(self):
        local_commit, remote_commit = self.make_divergence()
        self.write(self.local, "staged.txt", "pending staged change\n")
        self.git(self.local, "add", "staged.txt")
        self.write(self.local, "new.txt", "pending untracked file\n")
        with self.assertRaises(typer.Exit):
            self.push()

        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), local_commit)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_commit)
        stash = self.git(self.local, "stash", "list", "-1", "--format=%H")
        self.assertTrue(stash)
        self.assertEqual(self.git(self.local, "show", f"{stash}^2:staged.txt"), "pending staged change")
        self.assertEqual(self.git(self.local, "show", f"{stash}^3:new.txt"), "pending untracked file")
        self.assertTrue((self.local / ".git/MERGE_HEAD").exists())
        self.assertNotIn(["git", "stash", "apply", "--index", stash], self.commands)
        self.assert_no_publication()
        self.assertIn("git stash apply --index", self.output.getvalue())

    def test_unstaged_restore_conflict_retains_backup_and_stops_before_publication(self):
        remote_commit = self.advance_remote("staged.txt", "remote staged file\n")
        self.write(self.local, "staged.txt", "local unstaged file\n")
        with self.assertRaises(typer.Exit):
            self.push()

        stash = self.git(self.local, "stash", "list", "-1", "--format=%H")
        self.assertTrue(stash)
        self.assertEqual(self.git(self.local, "show", f"{stash}:staged.txt"), "local unstaged file")
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), remote_commit)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_commit)
        self.assertIn("staged.txt", self.git(self.local, "diff", "--name-only", "--diff-filter=U"))
        self.assertFalse((self.local / ".git/MERGE_HEAD").exists())
        self.assert_no_publication()

    def test_staged_restore_failure_retains_backup_and_stops_before_publication(self):
        remote_commit = self.advance_remote("staged.txt", "remote staged file\n")
        self.write(self.local, "staged.txt", "local staged file\n")
        self.git(self.local, "add", "staged.txt")
        with self.assertRaises(typer.Exit):
            self.push()

        stash = self.git(self.local, "stash", "list", "-1", "--format=%H")
        self.assertTrue(stash)
        self.assertEqual(self.git(self.local, "show", f"{stash}^2:staged.txt"), "local staged file")
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_commit)
        self.assert_no_publication()

    def test_unfinished_merge_is_rejected_before_fetch_or_stash(self):
        self.make_divergence()
        self.git(self.local, "fetch", "origin")
        result = subprocess.run(
            ["git", "merge", "--no-edit", "origin/master"],
            cwd=self.local, env=self.env, capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        with self.assertRaises(typer.Exit):
            self.push()

        self.assertNotIn("fetch", self.actions())
        self.assertNotIn("stash", self.actions())
        self.assert_no_publication()

    def test_out_of_scope_worktree_is_rejected_before_fetch_and_staging(self):
        self.write(self.local, "staged.txt", "shared change\n")
        with self.assertRaises(typer.Exit):
            self.push(self_only=True)

        self.assertNotIn("fetch", self.actions())
        self.assertNotIn("stash", self.actions())
        self.assertNotIn("add", self.actions())
        self.assert_no_publication()

    def test_out_of_scope_outgoing_commit_is_rejected_before_sync(self):
        self.write(self.local, "staged.txt", "shared committed change\n")
        local_commit = self.commit(self.local, "test: shared commit")
        self.advance_remote()
        with self.assertRaises(typer.Exit):
            self.push(machine_only=True)

        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), local_commit)
        self.assertNotIn("merge", self.actions())
        self.assertNotIn("stash", self.actions())
        self.assertNotIn("add", self.actions())
        self.assert_no_publication()

    def test_scope_is_rechecked_after_synchronization(self):
        machine_path = "hosts/darwin/one.nix"
        self.write(self.local, machine_path, "{ ... }: { envy.user.name = \"test\"; }\n")
        before_index = self.git(self.local, "diff", "--cached", "--binary")
        impacts = [
            ([], set(), {"origin": 0}),
            (["modules/shared.nix"], {"new-history"}, {"origin": 1}),
        ]
        with patch.object(git_workflow, "outgoing_impact", side_effect=impacts), patch.object(
            git_workflow, "sync_before_push"
        ) as synchronize, patch.object(git_workflow, "enforce_secret_safety"):
            with self.assertRaises(typer.Exit):
                self.push(self_only=True)

        synchronize.assert_called_once()
        self.assertEqual(self.git(self.local, "diff", "--cached", "--binary"), before_index)
        self.assertNotIn("add", self.actions())
        self.assert_no_publication()

    def test_incoming_plaintext_secret_is_rejected_before_sync_and_backup(self):
        self.write(self.local, "staged.txt", "local committed change\n")
        local_commit = self.commit(self.local, "test: local commit")
        remote_commit = self.advance_remote("secrets/secrets.yaml", "token: not-encrypted\n")
        self.write(self.local, "new.txt", "local pending file\n")
        with self.assertRaises(typer.Exit):
            self.push()

        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), local_commit)
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), remote_commit)
        self.assertTrue((self.local / "new.txt").is_file())
        self.assertNotIn("merge", self.actions())
        self.assertNotIn("stash", self.actions())
        self.assert_no_publication()

    def test_scope_is_rechecked_after_the_local_commit(self):
        self.write(self.local, "hosts/darwin/one.nix", "{ ... }: { envy.user.name = \"test\"; }\n")
        published = self.git(self.remote, "rev-parse", "master")
        impacts = [
            ([], set(), {"origin": 0}),
            ([], set(), {"origin": 0}),
            (["modules/unexpected.nix"], {"new-commit"}, {"origin": 1}),
        ]
        with patch.object(git_workflow, "outgoing_impact", side_effect=impacts), patch.object(
            git_workflow, "enforce_secret_safety"
        ):
            with self.assertRaises(typer.Exit):
                self.push(self_only=True)

        self.assertIn("commit", self.actions())
        self.assertNotIn("push", self.actions())
        self.assertEqual(self.git(self.remote, "rev-parse", "master"), published)

    def test_plaintext_secret_is_rejected_before_fetch_and_backup(self):
        self.write(self.local, "secrets/secrets.yaml", "token: not-encrypted\n")
        with self.assertRaises(typer.Exit):
            self.push()

        self.assertNotIn("fetch", self.actions())
        self.assertNotIn("stash", self.actions())
        self.assertNotIn("add", self.actions())
        self.assert_no_publication()


if __name__ == "__main__":
    unittest.main()

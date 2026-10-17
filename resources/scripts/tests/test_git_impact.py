from io import StringIO
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import typer
from rich.console import Console
from typer.testing import CliRunner

from envy import main
from envy.workflows import git as git_workflow
from envy.workflows import system as system_workflow


class GitImpactTests(unittest.TestCase):
    def test_refine_creates_missing_machine_after_explicit_mode_selection(self):
        missing = Path("/tmp/missing-machine.nix")
        report = SimpleNamespace(ok=True)
        with patch.object(system_workflow, "current_machine_file", return_value=missing), patch.object(
            system_workflow.sys.stdin, "isatty", return_value=True
        ), patch.object(main.typer, "confirm", return_value=True), patch.object(
            main.typer, "prompt", return_value="copy"
        ), patch.object(system_workflow, "initialize_machine") as initialize, patch.object(
            system_workflow, "refine_all", return_value=report
        ), patch.object(
            system_workflow, "ensure_nix_daemon_trust"
        ):
            system_workflow.refine_before_apply()

        initialize.assert_called_once_with("missing-machine", "copy")

    def test_refine_noninteractive_missing_machine_stops_before_migration(self):
        missing = Path("/tmp/missing-machine.nix")
        with patch.object(system_workflow, "current_machine_file", return_value=missing), patch.object(
            system_workflow.sys.stdin, "isatty", return_value=False
        ), patch.object(system_workflow, "initialize_machine") as initialize, patch.object(
            system_workflow, "refine_all"
        ) as refine, patch.object(system_workflow, "ensure_nix_daemon_trust"):
            with self.assertRaises(typer.Exit):
                system_workflow.refine_before_apply()

        initialize.assert_not_called()
        refine.assert_not_called()

    def test_verify_identity_populates_manifest_cache(self):
        manifest = {"settings": {"envy.user.home": str(Path.home())}}
        with patch.object(system_workflow, "machine_manifest", return_value=manifest) as evaluate, patch.object(
            system_workflow, "device_public_keys", return_value=["age1-local"]
        ), patch.object(
            system_workflow, "read_sops_yaml_keys", return_value={"device": "age1-local"}
        ), patch.object(
            system_workflow, "SECRETS_FILE", Path("/tmp/envy-test-secrets-does-not-exist")
        ):
            system_workflow.verify_apply_identity()

        evaluate.assert_called_once_with(refresh=True)

    def test_linux_trust_preflight_runs_before_refine(self):
        machine = Path(__file__).resolve()
        events = []
        report = SimpleNamespace(ok=True)
        with patch.object(system_workflow, "PLATFORM", "linux"), patch.object(
            system_workflow, "current_machine_file", return_value=machine
        ), patch.object(
            system_workflow,
            "ensure_nix_daemon_trust",
            side_effect=lambda **_: events.append("trust"),
        ) as trust, patch.object(
            system_workflow,
            "refine_all",
            side_effect=lambda **_: (events.append("refine"), report)[1],
        ), patch.object(system_workflow, "verify_apply_identity") as verify:
            system_workflow.refine_before_apply()

        trust.assert_called_once_with(platform="linux")
        verify.assert_called_once_with()
        self.assertEqual(events, ["trust", "refine"])

    def test_changed_paths_preserve_first_character_spaces_and_rename_destination(self):
        completed = SimpleNamespace(
            stdout=(
                " M resources/scripts/envy/main.py\0"
                "?? docs/file with spaces.md\0"
                "R  hosts/darwin/new-name.nix\0"
                "hosts/darwin/old-name.nix\0"
            ),
            returncode=0,
        )
        with patch.object(git_workflow, "run_process", return_value=completed):
            paths = git_workflow.changed_paths()
        self.assertEqual(paths, [
            "resources/scripts/envy/main.py",
            "docs/file with spaces.md",
            "hosts/darwin/new-name.nix",
        ])

    def test_push_impact_logging_uses_non_conflicting_scope_field(self):
        output = StringIO()
        with patch.object(
            main.log, "console", Console(file=output, color_system=None, width=160)
        ):
            git_workflow.show_push_impact(
                paths=["modules/cores/base.nix"],
                worktree_paths=[],
                outgoing_commits={"c1"},
                counts={"origin": 1},
                affected=["one", "two"],
                shared=True,
                branch="master",
            )
        self.assertIn("change_scope=shared", output.getvalue())

    def test_push_help_exposes_scope_guards(self):
        result = CliRunner().invoke(main.cli, ["push", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--self", result.output)
        self.assertIn("--machine-only", result.output)

    def test_branch_completion_lists_matching_local_branches(self):
        completed = SimpleNamespace(stdout="master\nfeature/test\n", returncode=0)
        with patch.object(git_workflow, "git_output", return_value=completed.stdout.strip()):
            items = main.complete_git_branches(None, "mas")
        self.assertEqual(items, ["master"])

    def test_checked_git_query_failure_cannot_be_treated_as_empty_output(self):
        failed = SimpleNamespace(returncode=7, stdout="", stderr="query failed")
        with patch.object(git_workflow, "run_process", return_value=failed):
            with self.assertRaises(typer.Exit) as raised:
                git_workflow.git_output_checked("rev-list", "HEAD")
        self.assertEqual(raised.exception.exit_code, 7)

    def test_machine_files_affect_only_their_targets(self):
        affected, shared = git_workflow.affected_machines([
            "hosts/linux/two.nix",
        ])
        self.assertFalse(shared)
        self.assertEqual(affected, ["linux/two"])

    def test_module_changes_are_conservatively_shared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("hosts/darwin/one.nix", "hosts/linux/two.nix"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{ ... }: {}\n")
            with patch.object(git_workflow, "ENVY_ROOT", root):
                affected, shared = git_workflow.affected_machines([
                    "modules/cores/base.nix",
                ])
        self.assertTrue(shared)
        self.assertEqual(affected, ["darwin/one", "linux/two"])

    def test_outgoing_impact_unions_paths_and_commits_across_remotes(self):
        outputs = {
            ("rev-list", "origin/master..HEAD"): "c2\nc1",
            ("log", "--format=", "--name-only", "origin/master..HEAD"): (
                "modules/cores/base.nix\nhosts/darwin/one.nix"
            ),
            ("rev-list", "backup/master..HEAD"): "c1",
            ("log", "--format=", "--name-only", "backup/master..HEAD"): (
                "hosts/darwin/one.nix"
            ),
        }
        with patch.object(git_workflow, "git_output_checked", side_effect=lambda *args: outputs[args]):
            paths, commits, counts = git_workflow.outgoing_impact(
                ["origin", "backup"], "master", set(),
            )
        self.assertEqual(paths, ["modules/cores/base.nix", "hosts/darwin/one.nix"])
        self.assertEqual(commits, {"c1", "c2"})
        self.assertEqual(counts, {"origin": 2, "backup": 1})

    def test_self_scope_allows_only_the_selected_machine(self):
        with patch.object(git_workflow, "current_machine_id", return_value="one"), patch.object(
            git_workflow, "platform_name", return_value="darwin"
        ):
            affected, shared = git_workflow.enforce_push_scope(
                ["hosts/darwin/one.nix"], machine_only=False, self_only=True,
            )
        self.assertEqual(affected, ["darwin/one"])
        self.assertFalse(shared)

    def test_self_scope_rejects_other_machine_and_shared_paths(self):
        with patch.object(git_workflow, "current_machine_id", return_value="one"), patch.object(
            git_workflow, "platform_name", return_value="darwin"
        ):
            with self.assertRaises(typer.Exit):
                git_workflow.enforce_push_scope(
                    ["hosts/linux/two.nix", "modules/cores/base.nix"],
                    machine_only=False, self_only=True,
                )

    def test_machine_only_allows_multiple_machine_files(self):
        affected, shared = git_workflow.enforce_push_scope(
            ["hosts/darwin/one.nix", "hosts/linux/two.nix"],
            machine_only=True,
            self_only=False,
        )
        self.assertEqual(affected, ["darwin/one", "linux/two"])
        self.assertFalse(shared)

    def test_machine_only_rejects_shared_paths(self):
        with self.assertRaises(typer.Exit):
            git_workflow.enforce_push_scope(
                ["hosts/darwin/one.nix", "modules/cores/base.nix"],
                machine_only=True,
                self_only=False,
            )

    def test_outgoing_shared_commits_require_confirmation_with_clean_worktree(self):
        with patch.object(git_workflow, "git_output_checked", return_value="master"), patch.object(
            git_workflow, "selected_remotes", return_value=["origin"]
        ), patch.object(
            git_workflow, "preflight_push_remotes", return_value=set()
        ), patch.object(
            git_workflow, "changed_paths", return_value=[]
        ), patch.object(
            git_workflow, "outgoing_impact",
            return_value=(["modules/cores/base.nix"], {"c1"}, {"origin": 1}),
        ), patch.object(git_workflow, "assert_git_secret_safety"), patch.object(
            git_workflow, "show_push_impact"
        ), patch.object(
            git_workflow, "confirm_push_scope", return_value=False
        ) as confirm, patch.object(
            git_workflow, "require_no_git_operation"
        ), patch.object(git_workflow, "sync_before_push") as synchronize:
            with self.assertRaises(typer.Abort):
                git_workflow.push(
                    msg="test", remote=None, branch="master", machine_only=False,
                    self_only=False, yes=False,
                )
        synchronize.assert_called_once_with(["origin"], "master", set(), [])
        confirm.assert_called_once()

    def test_sync_selects_the_newest_compatible_remote(self):
        ancestry = {
            ("origin/master", "HEAD"): False,
            ("HEAD", "origin/master"): True,
            ("backup/master", "origin/master"): False,
            ("origin/master", "backup/master"): True,
        }
        with patch.object(git_workflow, "git_is_ancestor", side_effect=lambda older, newer: ancestry[(older, newer)]):
            target = git_workflow.select_sync_target(["origin/master", "backup/master"])
        self.assertEqual(target, "backup/master")

    def test_sync_rejects_divergent_remote_branches(self):
        ancestry = {
            ("origin/master", "HEAD"): False,
            ("HEAD", "origin/master"): True,
            ("backup/master", "origin/master"): False,
            ("origin/master", "backup/master"): False,
        }
        with patch.object(git_workflow, "git_is_ancestor", side_effect=lambda older, newer: ancestry[(older, newer)]):
            with self.assertRaises(typer.Exit):
                git_workflow.select_sync_target(["origin/master", "backup/master"])

    def test_sync_merges_local_and_remote_divergence(self):
        with patch.object(git_workflow, "git_is_ancestor", side_effect=[False, False]), patch.object(
            git_workflow, "run_process", return_value=SimpleNamespace(returncode=0)
        ) as run_process, patch.object(git_workflow.log, "step") as step, patch.object(
            git_workflow.log, "ok"
        ):
            git_workflow.merge_sync_target("origin/master")

        run_process.assert_called_once_with(
            ["git", "merge", "--no-edit", "origin/master"],
            cwd=git_workflow.ENVY_ROOT,
            check=False,
        )
        step.assert_called_once_with("git", "merging remote history", branch="origin/master")

    def test_sync_merge_conflict_preserves_manual_recovery_instructions(self):
        output = StringIO()
        with patch.object(git_workflow, "git_is_ancestor", side_effect=[False, False]), patch.object(
            git_workflow, "run_process", return_value=SimpleNamespace(returncode=1)
        ), patch.object(
            git_workflow.log, "console", Console(file=output, force_terminal=False, width=180)
        ):
            with self.assertRaises(typer.Exit) as raised:
                git_workflow.merge_sync_target("origin/master")

        self.assertEqual(raised.exception.exit_code, 1)
        rendered = output.getvalue()
        self.assertIn("git status", rendered)
        self.assertIn("git add <path>", rendered)
        self.assertIn("git commit", rendered)
        self.assertIn("git merge --abort", rendered)
        self.assertIn("envy check --all", rendered)

    def test_sync_fast_forwards_when_local_is_ancestor(self):
        with patch.object(git_workflow, "git_is_ancestor", side_effect=[False, True]), patch.object(
            git_workflow, "run_checked_git"
        ) as run_checked:
            git_workflow.merge_sync_target("origin/master")
        run_checked.assert_called_once_with(
            ["merge", "--ff-only", "origin/master"], "fast-forward"
        )

    def test_push_preflight_leaves_remote_ahead_for_synchronization(self):
        verify = SimpleNamespace(returncode=0)
        with patch.object(git_workflow, "run_checked_git") as run_checked, patch.object(
            git_workflow, "run_process", return_value=verify
        ), patch.object(git_workflow, "git_output_checked") as query:
            new_branches = git_workflow.preflight_push_remotes(["origin"], "master")

        self.assertEqual(new_branches, set())
        run_checked.assert_called_once_with(["fetch", "origin"], "remote preflight")
        query.assert_not_called()

    def test_push_preflight_allows_a_new_remote_branch(self):
        verify = SimpleNamespace(returncode=1)
        with patch.object(git_workflow, "run_checked_git"), patch.object(
            git_workflow, "run_process", return_value=verify
        ):
            new_branches = git_workflow.preflight_push_remotes(["backup"], "master")
        self.assertEqual(new_branches, {"backup"})

    def test_push_new_remote_branch_does_not_query_a_missing_tracking_ref(self):
        def checked(*args):
            if args == ("branch", "--show-current"):
                return "master"
            raise AssertionError(f"unexpected query: {args}")

        pushed = SimpleNamespace(returncode=0)
        with patch.object(git_workflow, "git_output_checked", side_effect=checked), patch.object(
            git_workflow, "selected_remotes", return_value=["backup"]
        ), patch.object(
            git_workflow, "preflight_push_remotes", return_value={"backup"}
        ), patch.object(git_workflow, "changed_paths", return_value=[]), patch.object(
            git_workflow, "outgoing_impact", return_value=([], set(), {"backup": 0})
        ), patch.object(git_workflow, "enforce_secret_safety"), patch.object(
            git_workflow, "require_no_git_operation"
        ), patch.object(git_workflow, "sync_before_push") as synchronize, patch.object(
            git_workflow, "run_process", return_value=pushed
        ) as run:
            git_workflow.push(
                msg="test", remote=None, branch="master", machine_only=False,
                self_only=False, yes=True,
            )

        synchronize.assert_called_once_with(["backup"], "master", {"backup"}, [])
        run.assert_called_once_with(
            ["git", "push", "-u", "backup", "master"],
            cwd=git_workflow.ENVY_ROOT,
            check=False,
        )

    def test_push_rejects_an_unintentional_machine_branch(self):
        with patch.object(git_workflow, "git_output_checked", return_value="darwin-work-macbook"):
            with self.assertRaises(typer.Exit):
                git_workflow.push(
                    msg="test",
                    remote=None,
                    branch="master",
                    machine_only=False,
                    self_only=False,
                    yes=True,
                )


if __name__ == "__main__":
    unittest.main()

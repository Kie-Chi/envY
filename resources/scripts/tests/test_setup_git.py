import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy.keys import commits, prompts


class SetupGitTests(unittest.TestCase):
    def test_key_confirmation_has_no_implicit_no_default(self):
        with patch.object(prompts.Confirm, "ask", return_value=True) as ask:
            self.assertTrue(prompts.confirm("Commit managed files?"))
        ask.assert_called_once_with("Commit managed files?", console=prompts.log.console)

    def test_staging_explicit_setup_files_leaves_other_changes_unstaged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "hosts" / "darwin" / "work-mac.nix"
            unrelated = root / "modules" / "desktops" / "darwin" / "apps.nix"
            secret = root / "secrets" / "secrets.yaml"
            selected.parent.mkdir(parents=True)
            unrelated.parent.mkdir(parents=True)
            secret.parent.mkdir(parents=True)
            selected.write_text("{ }\n")
            unrelated.write_text("{ }\n")
            secret.write_text(
                "value: ENC[AES256_GCM,data:test]\n"
                "sops:\n"
                "  age:\n"
                "    - recipient: age1test\n"
                "      enc: encrypted-key\n"
                "  mac: ENC[AES256_GCM,data:test]\n"
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)

            with patch.object(commits, "ENVY_ROOT", root):
                changed = commits._stage_repo_files([selected])

            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.splitlines()

        self.assertEqual(changed, ["hosts/darwin/work-mac.nix"])
        self.assertEqual(staged, ["hosts/darwin/work-mac.nix"])

    def test_setup_commit_scopes_candidates_to_machine_and_sops_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine = root / "hosts" / "darwin" / "work-mac.nix"
            candidates = [
                machine,
                root / ".sops.yaml",
                root / "secrets" / "secrets.yaml",
                root / "secrets" / "recovery-key.age",
            ]
            with patch.object(commits, "ENVY_ROOT", root), patch.object(
                commits, "SOPS_YAML", candidates[1]
            ), patch.object(commits, "SECRETS_FILE", candidates[2]), patch.object(
                commits, "RECOVERY_KEY_FILE", candidates[3]
            ), patch.object(
                commits, "_stage_repo_files", return_value=["hosts/darwin/work-mac.nix"]
            ) as stage, patch.object(commits, "_commit_staged_files") as commit:
                (root / ".git").mkdir()

                commits.git_commit_setup_files(machine)

        stage.assert_called_once_with(candidates)
        commit.assert_called_once_with(
            ["hosts/darwin/work-mac.nix"],
            "chore(setup): update work-mac configuration (work-mac.nix)",
        )

    def test_setup_commit_skips_prompt_when_candidates_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine = root / "hosts" / "darwin" / "work-mac.nix"
            with patch.object(commits, "ENVY_ROOT", root), patch.object(
                commits, "_stage_repo_files", return_value=[]
            ), patch.object(commits, "_commit_staged_files") as commit:
                (root / ".git").mkdir()

                commits.git_commit_setup_files(machine)

        commit.assert_not_called()

    def test_key_operation_offers_commit_only_after_repository_files_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sops = root / ".sops.yaml"
            secrets = root / "secrets.yaml"
            recovery = root / "recovery-key.age"
            sops.write_text("before")
            with patch.object(commits, "SOPS_YAML", sops), patch.object(
                commits, "SECRETS_FILE", secrets
            ), patch.object(commits, "RECOVERY_KEY_FILE", recovery), patch.object(
                commits, "git_commit_sops_files"
            ) as commit:
                commits._run_key_operation("noop", lambda: None)
                commit.assert_not_called()

                commits._run_key_operation("change", lambda: sops.write_text("after"))
                commit.assert_called_once_with("change")


if __name__ == "__main__":
    unittest.main()

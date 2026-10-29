import os
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from envy import git_commit, git_safety, log, mutation
from envy.git_safety import SecretSafetyError

ENCRYPTED_SECRET = """\
token: ENC[AES256_GCM,data:fixture]
sops:
  age:
    - recipient: age1fixture
      enc: fixture
  mac: ENC[AES256_GCM,data:fixture]
"""


class ScopedCommitTests(unittest.TestCase):
    """Commit helpers must stage and commit only the paths they were given."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-scoped-commit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({
            "GIT_AUTHOR_NAME": "envy tests",
            "GIT_AUTHOR_EMAIL": "envy-tests@example.invalid",
            "GIT_COMMITTER_NAME": "envy tests",
            "GIT_COMMITTER_EMAIL": "envy-tests@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "ENVY_FLAKE_SOURCE_MIRROR": "0",
        })
        self.enterContext(patch.dict(os.environ, env, clear=True))
        self.git("init", "-q", "--initial-branch=master")
        self.write("secrets/secrets.yaml", ENCRYPTED_SECRET)
        for name in ("one", "two"):
            self.write(f"hosts/darwin/{name}.nix", "{ }\n")
        self.git("add", "--", "secrets", "hosts")
        self.git("commit", "-q", "-m", "init")

        for module in (git_commit, git_safety, mutation):
            self.enterContext(patch.object(module, "ENVY_ROOT", self.repo))
        self.enterContext(patch.object(git_safety, "SECRETS_FILE", self.repo / "secrets/secrets.yaml"))
        self.output = StringIO()
        self.enterContext(patch.object(log, "console", Console(file=self.output, force_terminal=False, width=200)))
        # Keep git's own commit summary out of the test output.
        for module in (git_commit, mutation):
            run = module.run_process
            self.enterContext(patch.object(
                module, "run_process",
                side_effect=lambda command, run=run, **kwargs: run(command, **{**kwargs, "capture": True}),
            ))

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def write(self, relative: str, text: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def staged(self) -> list[str]:
        return self.git("diff", "--cached", "--name-only").splitlines()

    def last_commit(self) -> tuple[str, list[str]]:
        files = self.git("show", "--name-only", "--format=%s", "HEAD").splitlines()
        return files[0], [name for name in files[1:] if name]

    def test_stage_reports_only_changed_paths_and_leaves_others_alone(self):
        one = self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        self.write("notes.txt", "unrelated\n")
        changed = git_commit.stage_repo_files(
            [one, one, self.repo / "hosts/darwin/two.nix", self.repo / "missing.nix"], repository=self.repo,
        )
        self.assertEqual(changed, ["hosts/darwin/one.nix"])
        self.assertEqual(self.staged(), ["hosts/darwin/one.nix"])

    def test_stage_refuses_paths_outside_the_repository(self):
        outside = self.root / "outside.nix"
        outside.write_text("{ }\n")
        with self.assertRaisesRegex(RuntimeError, "outside the repository"):
            git_commit.stage_repo_files([outside], repository=self.repo)
        self.assertEqual(self.staged(), [])

    def test_stage_refuses_plaintext_secrets_in_the_worktree_or_index(self):
        one = self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        self.write("secrets/secrets.yaml", "token: plaintext\n")
        with self.assertRaises(SecretSafetyError):
            git_commit.stage_repo_files([one], repository=self.repo)
        self.assertEqual(self.staged(), [])

        self.git("add", "--", "secrets/secrets.yaml")
        self.write("secrets/secrets.yaml", ENCRYPTED_SECRET)
        with self.assertRaisesRegex(SecretSafetyError, "staged"):
            git_commit.stage_repo_files([one], repository=self.repo)

    def test_commit_records_only_the_named_paths(self):
        one = self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        self.write("hosts/darwin/two.nix", "{ other = true; }\n")
        self.git("add", "--", "hosts/darwin/two.nix")
        changed = git_commit.stage_repo_files([one], repository=self.repo)
        prompts = []
        git_commit.commit_staged_files(
            changed, "chore: update one", repository=self.repo,
            confirm=lambda prompt: prompts.append(prompt) or True,
        )
        self.assertEqual(self.last_commit(), ("chore: update one", ["hosts/darwin/one.nix"]))
        self.assertEqual(self.staged(), ["hosts/darwin/two.nix"])
        self.assertEqual(prompts, ["Commit hosts/darwin/one.nix to git?"])

    def test_declined_commit_leaves_the_paths_staged(self):
        one = self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        changed = git_commit.stage_repo_files([one], repository=self.repo)
        git_commit.commit_staged_files(changed, "chore: update one", repository=self.repo, confirm=lambda _: False)
        self.assertEqual(self.last_commit()[0], "init")
        self.assertEqual(self.staged(), ["hosts/darwin/one.nix"])

    def test_changed_repo_paths_ignores_unchanged_missing_and_outside_paths(self):
        one = self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        new = self.write("hosts/linux/new.nix", "{ }\n")
        outside = self.root / "outside.nix"
        outside.write_text("{ }\n")
        paths = [one, one, new, self.repo / "hosts/darwin/two.nix", self.repo / "missing.nix", outside]
        self.assertEqual(mutation.changed_repo_paths(paths), ["hosts/darwin/one.nix", "hosts/linux/new.nix"])

    def offer(self, *, tty: bool, answer: bool = False, quiet: bool = False) -> None:
        stdin = SimpleNamespace(isatty=lambda: tty)
        with patch.object(mutation.sys, "stdin", stdin), \
                patch.object(mutation.typer, "confirm", return_value=answer) as confirm:
            mutation.offer_mutation_commit([self.repo / "hosts/darwin/one.nix"], "chore: mutate one", quiet=quiet)
        if not tty or quiet:
            confirm.assert_not_called()

    def test_mutation_commit_is_only_offered_on_a_terminal(self):
        self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        self.offer(tty=False, quiet=True)
        self.offer(tty=False)
        self.assertEqual(self.staged(), [])
        self.assertIn("envy push", self.output.getvalue())

        self.offer(tty=True, answer=False)
        self.assertEqual(self.last_commit()[0], "init")
        self.assertEqual(self.staged(), ["hosts/darwin/one.nix"])

    def test_accepted_mutation_commit_excludes_other_staged_paths(self):
        self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        self.write("hosts/darwin/two.nix", "{ other = true; }\n")
        self.git("add", "--", "hosts/darwin/two.nix")
        self.offer(tty=True, answer=True)
        self.assertEqual(self.last_commit(), ("chore: mutate one", ["hosts/darwin/one.nix"]))
        self.assertEqual(self.staged(), ["hosts/darwin/two.nix"])

    def test_mutation_commit_refuses_plaintext_secrets(self):
        self.write("hosts/darwin/one.nix", "{ changed = true; }\n")
        self.write("secrets/secrets.yaml", "token: plaintext\n")
        with self.assertRaises(SecretSafetyError):
            self.offer(tty=True, answer=True)
        self.assertEqual(self.staged(), [])


if __name__ == "__main__":
    unittest.main()

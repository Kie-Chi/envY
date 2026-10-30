import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy import evaluation
from envy.evaluation import manifest_selection_rows, manifest_settings


def _group(option, include=(), exclude=(), effective=()):
    entries = lambda values: [{"id": value, "name": value} for value in values]
    return {
        "optionPath": option,
        "selection": {
            "include": entries(include),
            "exclude": list(exclude),
            "effective": entries(effective),
        },
    }


class EvaluationTests(unittest.TestCase):
    def tearDown(self):
        evaluation.invalidate_machine_manifest()

    def test_settings_are_read_from_the_evaluated_manifest(self):
        values = manifest_settings({
            "settings": {
                "envy.darwin.services.mihomo.mode": "none",
                "envy.darwin.services.mihomo.tun": False,
                "envy.darwin.services.openssh.mode": "manual",
            }
        })

        self.assertEqual(values["envy.darwin.services.mihomo.mode"], "none")
        self.assertEqual(values["envy.darwin.services.mihomo.tun"], "false")
        self.assertEqual(values["envy.darwin.services.openssh.mode"], "manual")

    def test_selection_rows_include_all_three_views(self):
        manifest = {
            "schemaVersion": 2,
            "software": {"groups": {
                "nix.user.package": _group(
                    "envy.software.nix.packages",
                    ["git", "okular"], ["okular"], ["git"],
                ),
                "homebrew.system.formula": _group(
                    "envy.darwin.software.homebrew.formulae", ["gh"], [], ["gh"],
                ),
                "homebrew.system.cask": _group(
                    "envy.darwin.software.homebrew.casks",
                    ["iterm2", "uuremote"], ["uuremote"], ["iterm2"],
                ),
            }},
        }

        rows = {
            path: (include, exclude, effective)
            for path, include, exclude, effective in manifest_selection_rows(manifest)
        }

        self.assertEqual(
            rows["envy.software.nix.packages"],
            (["git", "okular"], ["okular"], ["git"]),
        )
        self.assertEqual(
            rows["envy.darwin.software.homebrew.formulae"],
            (["gh"], [], ["gh"]),
        )
        self.assertEqual(
            rows["envy.darwin.software.homebrew.casks"],
            (["iterm2", "uuremote"], ["uuremote"], ["iterm2"]),
        )

    def test_manifest_exposes_only_declared_platform_groups(self):
        linux_rows = list(manifest_selection_rows({
            "schemaVersion": 2,
            "platform": "linux",
            "software": {"groups": {
                "nix.user.package": _group(
                    "envy.software.nix.packages", ["git"], [], ["git"]
                ),
                "native.system.package": _group(
                    "envy.linux.software.native.packages", ["curl"], [], ["curl"]
                ),
            }},
        }))
        self.assertEqual(
            [row[0] for row in linux_rows],
            ["envy.software.nix.packages", "envy.linux.software.native.packages"],
        )

    def test_persistent_cache_survives_process_memoization_clear(self):
        manifest = {"id": "test-machine", "settings": {}}
        with tempfile.TemporaryDirectory() as cache_root, patch.dict(
            os.environ, {"XDG_CACHE_HOME": cache_root}, clear=False
        ), patch.object(
            evaluation, "current_machine_id", return_value="test-machine"
        ), patch.object(
            evaluation, "_repository_fingerprint", return_value="fingerprint"
        ), patch.object(
            evaluation, "_evaluate_machine_manifest", return_value=manifest
        ) as evaluate:
            os.environ.pop("ENVY_NO_CACHE", None)
            first = evaluation.machine_manifest()
            evaluation.invalidate_machine_manifest()
            second = evaluation.machine_manifest()

            self.assertEqual(first, manifest)
            self.assertEqual(second, manifest)
            evaluate.assert_called_once_with("test-machine")
            self.assertTrue(evaluation._cache_path("test-machine").exists())

    def test_non_persisting_manifest_read_never_creates_cache(self):
        manifest = {"id": "completion-machine", "settings": {}}
        with tempfile.TemporaryDirectory() as cache_root, patch.dict(
            os.environ, {"XDG_CACHE_HOME": cache_root}, clear=False
        ), patch.object(
            evaluation, "current_machine_id", return_value="completion-machine"
        ), patch.object(
            evaluation, "_repository_fingerprint", return_value="fingerprint"
        ), patch.object(
            evaluation, "_evaluate_machine_manifest", return_value=manifest
        ):
            value = evaluation.machine_manifest(write_cache=False)

            self.assertEqual(value, manifest)
            self.assertFalse(evaluation._cache_path("completion-machine").exists())

    def test_completion_reads_existing_cache_without_evaluating(self):
        manifest = {"id": "completion-machine", "settings": {}}
        with tempfile.TemporaryDirectory() as cache_root, patch.dict(
            os.environ, {"XDG_CACHE_HOME": cache_root}, clear=False
        ), patch.object(
            evaluation, "current_machine_id", return_value="completion-machine"
        ), patch.object(
            evaluation, "_evaluate_machine_manifest", side_effect=AssertionError("completion must not evaluate")
        ):
            evaluation._write_cache("completion-machine", "old-fingerprint", manifest)
            self.assertEqual(evaluation.machine_manifest(completion=True), manifest)

    def test_completion_without_cache_returns_without_running_nix(self):
        with tempfile.TemporaryDirectory() as cache_root, patch.dict(
            os.environ, {"XDG_CACHE_HOME": cache_root}, clear=False
        ), patch.object(
            evaluation, "current_machine_id", return_value="completion-machine"
        ), patch.object(
            evaluation, "_evaluate_machine_manifest", side_effect=AssertionError("completion must not evaluate")
        ):
            self.assertIsNone(evaluation.machine_manifest(completion=True))

    def test_refresh_bypasses_persistent_cache_and_replaces_it(self):
        first_manifest = {"id": "first"}
        refreshed_manifest = {"id": "refreshed"}
        with tempfile.TemporaryDirectory() as cache_root, patch.dict(
            os.environ, {"XDG_CACHE_HOME": cache_root}, clear=False
        ), patch.object(
            evaluation, "current_machine_id", return_value="test-machine"
        ), patch.object(
            evaluation, "_repository_fingerprint", return_value="fingerprint"
        ), patch.object(
            evaluation,
            "_evaluate_machine_manifest",
            side_effect=[first_manifest, refreshed_manifest],
        ) as evaluate:
            os.environ.pop("ENVY_NO_CACHE", None)
            self.assertEqual(evaluation.machine_manifest(), first_manifest)
            self.assertEqual(evaluation.machine_manifest(refresh=True), refreshed_manifest)
            self.assertEqual(evaluation.machine_manifest(), refreshed_manifest)
            self.assertEqual(evaluate.call_count, 2)


class RepositoryFingerprintTests(unittest.TestCase):
    """The manifest cache key follows visible content, not Git history."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (
            patch.object(evaluation, "ENVY_ROOT", self.root),
            patch.object(evaluation, "_nix_version", return_value=b"nix (Nix) 2.0-test\n"),
            patch.object(evaluation, "platform_name", return_value="darwin"),
            # Keep the user's global Git configuration (hooks, signing,
            # fsmonitor) away from both the fixture and the fingerprint.
            patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.git("init", "-q")
        (self.root / ".gitignore").write_text("ignored.txt\n")
        (self.root / "flake.nix").write_text("{ outputs = _: { }; }\n")
        (self.root / "tool.sh").write_text("echo tool\n")
        os.symlink("flake.nix", self.root / "link")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=self.root, check=True, capture_output=True,
        ).stdout

    def fingerprint(self, machine="test-machine"):
        value = evaluation._repository_fingerprint(machine)
        self.assertIsNotNone(value)
        return value

    def test_staging_committing_and_amending_keep_the_fingerprint(self):
        (self.root / "flake.nix").write_text("{ outputs = _: { changed = true; }; }\n")
        (self.root / "new.nix").write_text("{ }\n")
        (self.root / "tool.sh").unlink()
        changed = self.fingerprint()

        self.git("add", "-A")
        self.assertEqual(self.fingerprint(), changed)
        self.git("commit", "-qm", "change")
        self.assertEqual(self.fingerprint(), changed)
        self.git("commit", "-q", "--amend", "-m", "reworded")
        self.assertEqual(self.fingerprint(), changed)

    def test_partially_staged_changes_follow_the_worktree(self):
        (self.root / "flake.nix").write_text("staged\n")
        self.git("add", "flake.nix")
        (self.root / "flake.nix").write_text("final\n")
        mixed = self.fingerprint()

        self.git("add", "flake.nix")
        self.assertEqual(self.fingerprint(), mixed)

    def test_every_nix_visible_change_moves_the_fingerprint(self):
        seen = {self.fingerprint()}

        def changed(description):
            value = self.fingerprint()
            self.assertNotIn(value, seen, description)
            seen.add(value)

        (self.root / "flake.nix").write_text("{ outputs = _: { other = 1; }; }\n")
        changed("content")
        (self.root / "tool.sh").chmod(0o755)
        changed("executable bit")
        (self.root / "link").unlink()
        os.symlink("tool.sh", self.root / "link")
        changed("symlink target")
        (self.root / "untracked.nix").write_text("{ }\n")
        changed("untracked file")
        (self.root / "untracked.nix").write_text("{ x = 1; }\n")
        changed("untracked content")
        (self.root / "tool.sh").unlink()
        changed("deletion")
        self.assertNotEqual(self.fingerprint("other-machine"), self.fingerprint())
        with patch.object(evaluation, "_nix_version", return_value=b"nix (Nix) 3.0-test\n"):
            changed("nix version")

    def test_ignored_files_do_not_move_the_fingerprint(self):
        before = self.fingerprint()
        (self.root / "ignored.txt").write_text("local state\n")
        self.assertEqual(self.fingerprint(), before)

    def test_worktree_entries_match_git_object_ids(self):
        (self.root / "tool.sh").write_text("echo changed\n")
        (self.root / "tool.sh").chmod(0o755)
        expected = self.git("hash-object", "tool.sh").strip()

        self.assertEqual(evaluation._worktree_entry(b"tool.sh", "sha1"), b"100755 " + expected)
        self.git("add", "tool.sh")
        staged = self.git("ls-files", "--stage", "tool.sh").split(b"\t")[0]
        self.assertEqual(staged, b"100755 " + expected + b" 0")
        link = self.git("ls-files", "--stage", "link").split(b"\t")[0].rsplit(b" ", 1)[0]
        self.assertEqual(evaluation._worktree_entry(b"link", "sha1"), link)
        self.assertEqual(evaluation._worktree_entry(b"missing", "sha1"), b"")

    def test_unusual_worktree_objects_disable_the_cache(self):
        # Git never lists a FIFO, but a tracked path replaced by one must not
        # block or be cached.
        os.mkfifo(self.root / "pipe")
        self.assertIsNone(evaluation._worktree_entry(b"pipe", "sha1"))

        (self.root / "tool.sh").unlink()
        (self.root / "tool.sh").mkdir()
        self.assertIsNone(evaluation._repository_fingerprint("test-machine"))

    def test_missing_git_or_nix_disables_the_cache(self):
        with patch.object(evaluation, "_nix_version", return_value=None):
            self.assertIsNone(evaluation._repository_fingerprint("test-machine"))
        with patch.object(evaluation, "ENVY_ROOT", self.root / "not-a-repository"):
            self.assertIsNone(evaluation._repository_fingerprint("test-machine"))


if __name__ == "__main__":
    unittest.main()

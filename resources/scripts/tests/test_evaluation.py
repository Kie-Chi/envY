import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

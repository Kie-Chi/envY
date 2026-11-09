import io
import unittest
from unittest.mock import patch

from rich.console import Console

from envy import overview


def _manifest():
    return {
        "schemaVersion": 3,
        "id": "test-mac",
        "software": {"groups": {
            "nix.user.package": {
                "selection": {
                    "include": [{"id": "git"}, {"id": "jq"}],
                    "exclude": ["jq"],
                    "effective": [{"id": "git"}],
                },
            },
        }},
    }


def _payload(software):
    return {
        "machine": "test-mac",
        "platform": "darwin",
        "config": {"source": "source-fallback"},
        "software": software,
        "git": {"branch": "master", "dirty": False, "changes": 0, "upstream": None},
        "generation": {"current": {"number": 7}, "count": 1},
        "doctor": {"summary": {"ok": 1, "warn": 0, "error": 0, "info": 0, "exitCode": 0}},
    }


class OverviewGitRecommendationTests(unittest.TestCase):
    def recommendations(self, *, ahead=None, behind=None, dirty=False, upstream="origin/master"):
        payload = _payload({"available": True})
        payload["git"].update(
            upstream=upstream, ahead=ahead, behind=behind, dirty=dirty,
            changes=1 if dirty else 0,
        )
        return overview._recommendations(payload)

    def test_behind_only_recommends_sync_without_apply(self):
        recommendations = self.recommendations(ahead=0, behind=2)
        self.assertEqual([item["command"] for item in recommendations], ["envy sync --no-apply"])
        self.assertIn("2 commit(s) are behind origin/master", recommendations[0]["reason"])

    def test_diverged_recommends_sync_before_push(self):
        recommendations = self.recommendations(ahead=3, behind=2)
        self.assertEqual([item["command"] for item in recommendations], ["envy sync --no-apply"])

    def test_ahead_only_preserves_push_recommendation(self):
        recommendations = self.recommendations(ahead=3, behind=0)
        self.assertEqual([item["command"] for item in recommendations], ["envy push"])

    def test_dirty_behind_preserves_configuration_recommendations(self):
        recommendations = self.recommendations(ahead=0, behind=2, dirty=True)
        self.assertEqual(
            [item["command"] for item in recommendations],
            ["envy plan", "envy apply", "envy sync --no-apply"],
        )

    def test_unknown_counts_preserve_no_action_recommendation(self):
        for upstream in (None, "origin/master"):
            with self.subTest(upstream=upstream):
                recommendations = self.recommendations(upstream=upstream)
                self.assertEqual(recommendations[0]["label"], "No action required")
                self.assertEqual(recommendations[0]["command"], "")


class OverviewSoftwareTests(unittest.TestCase):
    def test_evaluated_manifest_reports_counts(self):
        software = overview._software_snapshot(_manifest())
        self.assertTrue(software["available"])
        self.assertEqual(software["machine"], "test-mac")
        self.assertEqual(
            (software["groups"], software["included"], software["effective"], software["excluded"]),
            (1, 2, 1, 1),
        )

    def test_unavailable_manifest_reports_unknown_counts_and_reason(self):
        with patch.object(overview, "current_machine_id", return_value="test-mac"), patch.object(
            overview, "last_manifest_error", return_value="command timed out after 30 seconds"
        ):
            software = overview._software_snapshot(None)
        self.assertFalse(software["available"])
        self.assertEqual(software["error"], "command timed out after 30 seconds")
        for key in ("groups", "included", "effective", "excluded"):
            self.assertIsNone(software[key])

    def test_unavailable_manifest_recommends_host_check_first(self):
        payload = _payload({"available": False, "error": "boom"})
        recommendations = overview._recommendations(payload)
        self.assertEqual(recommendations[0]["command"], "envy host check")
        self.assertIn("boom", recommendations[0]["reason"])

    def test_render_marks_unavailable_software_instead_of_zero(self):
        payload = _payload({
            "available": False, "error": "command timed out after 30 seconds",
            "groups": None, "included": None, "effective": None, "excluded": None,
        })
        payload["recommendations"] = overview._recommendations(payload)
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=200)
        with patch.object(overview.log, "console", console):
            overview.render(payload)
        rendered = output.getvalue()
        self.assertIn("unavailable · command timed out after 30 seconds", rendered)
        self.assertNotIn("effective", rendered)
        self.assertIn("envy host check", rendered)


if __name__ == "__main__":
    unittest.main()

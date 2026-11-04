import unittest
from unittest.mock import patch

from envy.doctor import selection
from envy.doctor.model import (
    SECTION_AUTH,
    SECTION_INSTALL,
    SECTION_PRIVACY,
    SECTION_SYSTEM,
    info,
)
from envy.schemas.apps import AppSpec


class DoctorSelectionTests(unittest.TestCase):
    def test_sections_and_apps_accept_aliases_commas_and_repeats(self):
        parsed = selection.parse_only(["auth, claude", "Visual_Studio", "app:code", "section:perms"])
        self.assertEqual(parsed.sections, {SECTION_AUTH, SECTION_PRIVACY})
        self.assertEqual(parsed.apps, ["claude-code", "vscode"])
        self.assertFalse(parsed.has_parse_errors)

    def test_unknown_and_ambiguous_tokens_are_reported(self):
        with patch.dict(selection.ALL_APP_SPECS, {"sync": AppSpec(name="Sync")}):
            parsed = selection.parse_only(["sync", "nope", "app:nope", "section:nope", "kind:auth"])
            errors = selection.selection_errors(parsed, allow_apps=True)
        self.assertEqual(parsed.ambiguous, ["sync"])
        self.assertEqual(parsed.unknown, ["nope", "app:nope", "section:nope", "kind:auth"])
        self.assertEqual([result.status for result in errors], ["error", "error"])
        self.assertIn("section:<name> or app:<name>", errors[1].hint)

    def test_app_filters_are_rejected_where_only_sections_apply(self):
        errors = selection.selection_errors(selection.parse_only(["claude"]), allow_apps=False)
        self.assertEqual(len(errors), 1)
        self.assertIn("claude-code", errors[0].message)

    def test_results_are_filtered_by_section_and_app(self):
        results = [
            info(SECTION_AUTH, "vscode account", "x", app="vscode"),
            info(SECTION_INSTALL, "claude install", "x", app="claude-code"),
            info(SECTION_SYSTEM, "TCC database", "x"),
        ]
        privacy = selection.parse_only(["perm"])
        self.assertEqual([r.name for r in selection.filter_results(results, privacy)], ["TCC database"])
        code = selection.parse_only(["code"])
        self.assertEqual([r.name for r in selection.filter_results(results, code)], ["vscode account"])
        self.assertEqual(selection.app_keys_for_selection(code), ["vscode"])
        everything = selection.parse_only(None)
        self.assertEqual(selection.filter_results(results, everything), results)
        self.assertEqual(selection.app_keys_for_selection(everything), list(selection.ALL_APP_SPECS))


if __name__ == "__main__":
    unittest.main()

"""Exercise the Homebrew removal helper against a disposable brew executable."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(os.environ.get(
    "ENVY_HOMEBREW_UNINSTALL",
    Path(__file__).resolve().parents[3] / "modules/software/darwin/uninstall-homebrew.py",
))

FAKE_BREW = r'''
import json
import os
from pathlib import Path
import sys

path = Path(__file__).with_name("state.json")
state = json.loads(path.read_text())
args = sys.argv[1:]
state["calls"].append({"args": args, "env": {key: os.environ.get(key) for key in (
    "HOMEBREW_NO_AUTOREMOVE", "HOMEBREW_NO_AUTO_UPDATE", "HOMEBREW_CASK_OPTS",
)}})
kind = args[1]
if args[0] == "list":
    if state.get("fail_list") == kind:
        path.write_text(json.dumps(state))
        sys.exit(23)
    print("\n".join(state[kind]))
elif args[0] == "uninstall":
    if state.get("fail_uninstall") == kind:
        path.write_text(json.dumps(state))
        sys.exit(24)
    assert args[2] == "--"
    for name in args[3:]:
        state[kind].remove(name)
else:
    raise AssertionError(args)
path.write_text(json.dumps(state))
'''


class HomebrewUninstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.brew = self.root / "brew"
        self.brew.write_text(f"#!{sys.executable}\n" + FAKE_BREW)
        self.brew.chmod(0o755)
        self.state_file = self.root / "state.json"
        self.declaration = self.root / "uninstall.json"
        self.state_file.write_text(json.dumps({
            "--cask": ["t3-code", "unrelated-app"],
            "--formula": ["old-tool", "old-dependency", "unrelated-tool"],
            "calls": [],
        }))

    def state(self):
        return json.loads(self.state_file.read_text())

    def update_state(self, **changes):
        self.state_file.write_text(json.dumps(self.state() | changes))

    def run_helper(self, casks=(), formulae=(), **environment):
        self.declaration.write_text(json.dumps({"casks": list(casks), "formulae": list(formulae)}))
        return subprocess.run(
            [sys.executable, str(HELPER), str(self.brew), str(self.declaration)],
            env=os.environ | environment, text=True, capture_output=True,
        )

    def removals(self):
        return [call["args"] for call in self.state()["calls"] if call["args"][0] == "uninstall"]

    def test_only_named_installed_cask_is_removed(self):
        result = self.run_helper(casks=["t3-code", "absent-app"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.removals(), [["uninstall", "--cask", "--", "t3-code"]])
        self.assertEqual(self.state()["--cask"], ["unrelated-app"])
        self.assertEqual(self.state()["--formula"], ["old-tool", "old-dependency", "unrelated-tool"])

    def test_empty_policy_does_not_invoke_brew(self):
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.state()["calls"], [])

    def test_absent_package_and_repeated_activation_are_noops(self):
        self.assertEqual(self.run_helper(casks=["t3-code"]).returncode, 0)
        self.assertEqual(self.run_helper(casks=["t3-code"]).returncode, 0)
        self.assertEqual(len(self.removals()), 1)

    def test_casks_precede_batched_formula_removals(self):
        result = self.run_helper(casks=["t3-code"], formulae=["old-tool", "old-dependency"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.removals(), [
            ["uninstall", "--cask", "--", "t3-code"],
            ["uninstall", "--formula", "--", "old-tool", "old-dependency"],
        ])
        self.assertEqual(self.state()["--formula"], ["unrelated-tool"])

    def test_same_name_in_other_ecosystem_is_preserved(self):
        self.update_state(**{"--formula": ["t3-code"]})
        self.assertEqual(self.run_helper(casks=["t3-code"]).returncode, 0)
        self.assertEqual(self.state()["--formula"], ["t3-code"])

    def test_duplicate_targets_are_removed_once(self):
        self.assertEqual(self.run_helper(casks=["t3-code", "t3-code"]).returncode, 0)
        self.assertEqual(len(self.removals()), 1)

    def test_short_names_match_tapped_packages_without_matching_prefixes(self):
        self.update_state(**{"--formula": ["vendor/tools/old-tool", "old-tool-extra"]})
        result = self.run_helper(formulae=["old-tool"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.removals(), [["uninstall", "--formula", "--", "vendor/tools/old-tool"]])
        self.assertEqual(self.state()["--formula"], ["old-tool-extra"])

    def test_qualified_name_does_not_match_another_tap(self):
        self.update_state(**{"--formula": ["other/tools/old-tool"]})
        self.assertEqual(self.run_helper(formulae=["vendor/tools/old-tool"]).returncode, 0)
        self.assertEqual(self.removals(), [])

    def test_core_qualified_names_match_short_inventory(self):
        result = self.run_helper(casks=["homebrew/cask/t3-code"], formulae=["homebrew/core/old-tool"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.removals()), 2)

    def test_listing_failure_aborts_before_any_uninstall(self):
        self.update_state(fail_list="--formula")
        result = self.run_helper(casks=["t3-code"], formulae=["old-tool"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit uninstall failed", result.stderr)
        self.assertEqual(self.removals(), [])

    def test_uninstall_failure_stops_later_removals(self):
        self.update_state(fail_uninstall="--cask")
        result = self.run_helper(casks=["t3-code"], formulae=["old-tool"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.removals(), [["uninstall", "--cask", "--", "t3-code"]])
        self.assertIn("old-tool", self.state()["--formula"])

    def test_dependency_cleanup_and_inherited_cask_flags_are_disabled(self):
        result = self.run_helper(
            casks=["t3-code"], HOMEBREW_NO_AUTOREMOVE="", HOMEBREW_CASK_OPTS="--zap --force",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.state()["calls"]:
            self.assertEqual(call["env"]["HOMEBREW_NO_AUTOREMOVE"], "1")
            self.assertEqual(call["env"]["HOMEBREW_NO_AUTO_UPDATE"], "1")
            self.assertIsNone(call["env"]["HOMEBREW_CASK_OPTS"])


if __name__ == "__main__":
    unittest.main()

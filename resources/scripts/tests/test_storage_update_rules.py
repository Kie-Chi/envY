"""Exercise the shipped update-cache rules with organize in isolated homes."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from envy.storage import lifecycle, planner
from envy.storage.model import policy_from_manifest


@unittest.skipUnless(os.environ.get("ENVY_STORAGE_RULES_FIXTURE") and shutil.which("organize"),
                     "Requires the Nix catalog fixture and organize")
class StorageUpdateRuleTests(unittest.TestCase):
    ids = ("feishu-update", "codex-sparkle", "vscode-shipit", "wechat-sparkle",
           "zotero-update-archive", "zotero-update-staging")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state = self.home / "state"
        fixture = json.loads(Path(os.environ["ENVY_STORAGE_RULES_FIXTURE"]).read_text())
        self.rules = json.loads(json.dumps(fixture["rules"]).replace(fixture["home"], str(self.home)))
        self.expected = {}
        for rule_id in self.ids:
            root = Path(self.rules[rule_id]["path"].replace("*", "fixture"))
            names = {"feishu-update": ("update.noindex", "update_downloading"),
                     "codex-sparkle": ("download.fixture",),
                     "wechat-sparkle": ("download.fixture/xWeChatMac_fixture.dmg",),
                     "vscode-shipit": ("Visual Studio Code.app",),
                     "zotero-update-archive": ("update.mar",),
                     "zotero-update-staging": ("Updated.app",)}[rule_id]
            self.expected[rule_id] = []
            for name in names:
                target = root / name
                self.expected[rule_id].append(target)
                payload = target if self.rules[rule_id]["targets"] == "files" else target / "Contents/payload"
                payload.parent.mkdir(parents=True, exist_ok=True)
                payload.write_bytes(b"isolated installer payload")
        feishu = Path(self.rules["feishu-update"]["path"])
        shipit = Path(self.rules["vscode-shipit"]["path"].replace("*", "fixture"))
        zotero = Path(self.rules["zotero-update-archive"]["path"])
        self.controls = [feishu / "darwin-updater", feishu / "updater-state/pending.json",
                         shipit / "ShipItState.plist", shipit.parent / "control-cache/state",
                         zotero / "update.status", zotero / "updater.app/Contents/payload"]
        for rule_id in ("codex-sparkle", "wechat-sparkle"):
            sparkle = Path(self.rules[rule_id]["path"]).parent
            self.controls.append(sparkle / "Launcher/updater")
        wechat = self.expected["wechat-sparkle"][0].parent
        self.controls.append(wechat / "unpacked/WeChat.app/Contents/resource.dmg")
        self.controls.extend([self.home / "Documents/report.zip",
                              self.home / "Applications/Zotero.app/Contents/payload",
                              self.home / "Applications/Visual Studio Code.app/Contents/payload"])
        for path in self.controls:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"control or installed application: retain")
        old = time.time() - 20 * 86400
        for path in sorted(self.home.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            os.utime(path, (old, old))

    def policy(self, ids=None):
        manifest = {"settings": {"envy.user.home": str(self.home)}, "storage": {
            "mode": "report", "quarantineDays": 30,
            "rules": [dict(self.rules[rule_id], id=rule_id) for rule_id in (ids or self.ids)]}}
        return policy_from_manifest(manifest, self.state, self.home / "checkout")

    def commands(self, helper=""):
        real_run = planner.run_process

        def run(argv, **kwargs):
            if Path(argv[0]).name != "pgrep":
                environment = dict(os.environ, HOME=str(self.home),
                                   XDG_CONFIG_HOME=str(self.home / ".config"))
                result = real_run(argv, env=environment, **kwargs)
                self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
                return result
            self.assertIn(argv[1], ("-f", "-x"))
            pattern = argv[2]
            matched = bool(re.search(pattern, helper) if argv[1] == "-f"
                           else re.fullmatch(pattern, Path(helper).name)) if helper else False
            return subprocess.CompletedProcess(argv, 0 if matched else 1, "12345\n" if matched else "", "")

        return patch.object(planner, "run_process", side_effect=run)

    def plan(self, ids=None, helper=""):
        with self.commands(helper):
            return planner.plan(self.policy(ids), self.state)

    def test_catalog_selects_only_old_installer_payloads(self):
        result = self.plan()
        expected = {str(path) for paths in self.expected.values() for path in paths}
        self.assertEqual({entry["path"] for entry in result["candidates"]}, expected)
        self.assertTrue(all(entry["action"] == "quarantine" for entry in result["candidates"]))
        self.assertTrue(all(Path(path).exists() for path in expected))
        self.assertTrue(all(path.exists() for path in self.controls))

    def test_recent_descendants_keep_every_staged_directory(self):
        retained = set()
        for rule_id, paths in self.expected.items():
            if self.rules[rule_id]["targets"] != "dirs":
                continue
            for path in paths:
                os.utime(path / "Contents/payload", None)
                retained.add(str(path))
        result = self.plan()
        expected = {str(path) for rule_id, paths in self.expected.items()
                    if self.rules[rule_id]["targets"] == "files" for path in paths}
        self.assertEqual({entry["path"] for entry in result["candidates"]}, expected)
        self.assertTrue(all(Path(path).exists() for path in retained))

    def test_symbolic_links_in_staged_apps_remain_protected(self):
        app = self.expected["vscode-shipit"][0]
        link = app / "Contents/Current"
        link.symlink_to("payload")
        old = time.time() - 20 * 86400
        os.utime(link, (old, old), follow_symlinks=False)
        os.utime(link.parent, (old, old))
        self.assertEqual(self.plan(["vscode-shipit"])["candidates"], [])
        self.assertTrue(link.is_symlink())
        link.unlink()
        os.utime(link.parent, (old, old))
        self.assertEqual(len(self.plan(["vscode-shipit"])["candidates"]), 1)

    def test_recent_archives_are_excluded(self):
        mar = self.expected["zotero-update-archive"][0]
        download = self.expected["feishu-update"][1]
        wechat = self.expected["wechat-sparkle"][0]
        os.utime(wechat, None)
        os.utime(mar, None)
        os.utime(download / "Contents/payload", None)
        selected = {entry["path"] for entry in self.plan()["candidates"]}
        expected = {str(path) for paths in self.expected.values() for path in paths} - {
            str(mar), str(download), str(wechat)}
        self.assertEqual(selected, expected)

    def test_background_updaters_block_each_rule_without_the_gui(self):
        helpers = {
            "feishu-update": self.home / "Library/Application Support/LarkShell/update/darwin-updater",
            "codex-sparkle": Path(self.rules["codex-sparkle"]["path"]).parent / "Launcher/task/Updater",
            "wechat-sparkle": Path(self.rules["wechat-sparkle"]["path"]).parent / "Launcher/task/Autoupdate",
            "vscode-shipit": self.home / "Applications/Visual Studio Code.app/Contents/Frameworks/ShipIt",
            "zotero-update-archive": Path(self.rules["zotero-update-archive"]["path"]) / "updater",
            "zotero-update-staging": Path(self.rules["zotero-update-staging"]["path"]) / "updater",
        }
        for rule_id, helper in helpers.items():
            with self.subTest(rule=rule_id):
                result = self.plan([rule_id], str(helper))
                self.assertEqual(len(result["candidates"]), len(self.expected[rule_id]))
                self.assertTrue(all(entry.get("blocked") for entry in result["candidates"]))
                self.assertTrue(all(path.exists() for path in self.expected[rule_id]))

    def test_updater_starting_after_plan_prevents_quarantine(self):
        policy = self.policy(["wechat-sparkle"])
        result = self.plan(["wechat-sparkle"])
        helper = Path(self.rules["wechat-sparkle"]["path"]).parent / "Launcher/task/Autoupdate"
        with self.commands(str(helper)):
            batch = lifecycle.execute(policy, self.state, result["id"])
        self.assertEqual(batch["items"], [])
        self.assertEqual(len(batch["errors"]), 1)
        self.assertIn("process is running", batch["errors"][0]["reason"])
        self.assertTrue(self.expected["wechat-sparkle"][0].exists())

    def test_missing_optional_zotero_root_is_reported(self):
        root = Path(self.rules["zotero-update-archive"]["path"])
        shutil.rmtree(root)
        result = self.plan()
        self.assertEqual(len(result["candidates"]), 5)
        skipped = {entry["rule"] for entry in result["skipped"]}
        self.assertTrue({"zotero-update-archive", "zotero-update-staging"}.issubset(skipped))

    def test_catalog_archive_uses_quarantine_and_recovery(self):
        policy = self.policy(["zotero-update-archive"])
        result = self.plan(["zotero-update-archive"])
        original = self.expected["zotero-update-archive"][0]
        data = original.read_bytes()
        with self.commands(), patch.object(lifecycle, "assert_inactive"):
            batch = lifecycle.execute(policy, self.state, result["id"])
        self.assertEqual(batch["errors"], [])
        self.assertEqual(batch["items"][0]["status"], "quarantined")
        self.assertFalse(original.exists())
        restored = self.home / "recovered"
        lifecycle.restore(policy, self.state, batch["id"], restored)
        self.assertEqual((restored / original.relative_to("/")).read_bytes(), data)
        self.assertTrue(all(path.exists() for path in self.controls))


if __name__ == "__main__":
    unittest.main()

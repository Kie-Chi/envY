"""Application storage reports use only isolated homes and projected metadata."""

import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from envy.storage import application_reports as reports
from envy.storage.measurement import ScanBudget


class ApplicationReportsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.system = self.root / "Applications"
        self.system.mkdir()
        self.base = self.root / "userbase"
        for leaf in ("T", "C", "X"):
            (self.base / leaf).mkdir(parents=True, exist_ok=True)
        self.releases = self.home / ".codex/packages/app-server-daemon/releases"
        self.releases.mkdir(parents=True)
        self.staging = self.home / "Library/Caches/com.microsoft.VSCode.ShipIt"
        self.ps_output = ""
        self.ps_code = 0
        self.getconf = {"DARWIN_USER_TEMP_DIR": "/var/folders/ab/user_hash/T/\n",
                        "DARWIN_USER_CACHE_DIR": "/private/var/folders/ab/user_hash/C/\n"}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(reports, "probe", side_effect=self.probe).start()
        patch.object(reports, "_SYSTEM_APPLICATIONS", self.system).start()
        patch.object(reports, "_validate_darwin_directory", side_effect=self.fixture_directory).start()

    def probe(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertIsInstance(kwargs["budget"], ScanBudget)
        if argv == ["ps", "-axo", "pid=,comm="]:
            return subprocess.CompletedProcess(argv, self.ps_code, self.ps_output, "")
        self.assertEqual(argv[0], "getconf")
        value = self.getconf[argv[1]]
        return subprocess.CompletedProcess(argv, 127 if value is None else 0, value or "", "")

    def fixture_directory(self, raw, leaf, uid, budget):
        # Production validation has separate virtual-filesystem tests below.
        budget.check()
        if not raw.strip().endswith("/" + leaf) and not raw.strip().endswith("/" + leaf + "/"):
            raise ValueError("fixture_invalid_path")
        return self.base / leaf

    def inspect(self, budget=None, platform="darwin"):
        return reports.inspect(self.home, budget=budget or ScanBudget(), platform=platform)

    def release(self, name):
        path = self.releases / name
        path.mkdir()
        (path / "daemon").write_bytes(b"runtime")
        return path

    def current(self, name):
        (self.releases.parent / "current").symlink_to("releases/" + name)

    def bundle(self, root, version="1.99.0"):
        contents = root / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.microsoft.VSCode", "CFBundleShortVersionString": version,
            "CFBundleVersion": "build-123", "PrivateAccount": "never report this value"}))

    def test_semantic_order_current_rollback_and_all_active_versions_are_protected(self):
        for name in ("1.8.0", "1.9.0", "1.10.0", "1.11.0-beta.2", "1.11.0-beta.10", "1.11.0"):
            self.release(name)
        self.current("1.10.0")
        self.ps_output = f"  42 {self.releases}/1.8.0/daemon\n43 {self.releases}/1.11.0/daemon\n"
        result = self.inspect()
        evidence = result["reports"]["codex"]
        self.assertEqual(evidence["current"]["version"], "1.10.0")
        self.assertEqual(evidence["rollbackVersion"], "1.9.0")
        versions = {entry["version"]: entry for entry in evidence["versions"]}
        self.assertIn("rollback", versions["1.9.0"]["protectionReasons"])
        self.assertIn("current", versions["1.10.0"]["protectionReasons"])
        for name in ("1.8.0", "1.11.0"):
            self.assertTrue(versions[name]["active"])
            self.assertIn("active", versions[name]["protectionReasons"])
        for entry in versions.values():
            self.assertTrue(entry["protected"])
            self.assertIsNone(entry["inactiveSince"])
        self.assertEqual([entry["version"] for entry in evidence["versions"]][-3:],
                         ["1.11.0-beta.2", "1.11.0-beta.10", "1.11.0"])
        self.assertTrue(all(row["action"] == "report" for row in result["items"]))
        self.assertTrue(all(row["policyEligibleBytes"] is None for row in result["items"]))

    def test_malformed_versions_and_foreign_symlink_are_never_followed(self):
        self.release("1.0.0")
        for name in ("garbage", "1.01.0", "1.0.0-01", "2.0"):
            self.release(name)
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "valuable").write_bytes(b"do not scan")
        (self.releases / "9.0.0").symlink_to(foreign)
        (self.releases.parent / "current").symlink_to(foreign)
        result = self.inspect()
        codex = result["reports"]["codex"]
        self.assertEqual(codex["status"], "unknown")
        self.assertIsNone(codex["current"]["version"])
        self.assertTrue(all(entry["protected"] for entry in codex["versions"]))
        rows = [row for row in result["items"] if row["id"].startswith("codex-daemon-")]
        self.assertEqual([row["id"] for row in rows], ["codex-daemon-1.0.0"])
        self.assertNotIn("valuable", json.dumps(result))

    def test_missing_or_nonlink_current_keeps_protection_unknown(self):
        self.release("1.0.0")
        for plain in (False, True):
            with self.subTest(plain=plain):
                if plain:
                    (self.releases.parent / "current").mkdir()
                codex = self.inspect()["reports"]["codex"]
                self.assertEqual(codex["status"], "unknown")
                self.assertIsNone(codex["current"]["version"])
                self.assertIn("protection_unknown", codex["versions"][0]["protectionReasons"])

    def test_failed_or_malformed_ps_never_claims_inactive(self):
        self.release("1.0.0")
        self.current("1.0.0")
        for code, output in ((124, ""), (125, ""), (127, ""), (0, "not an executable projection")):
            with self.subTest(code=code, output=output):
                self.ps_code, self.ps_output = code, output
                result = self.inspect()
                version = result["reports"]["codex"]["versions"][0]
                self.assertIsNone(version["active"])
                self.assertTrue(version["protected"])
                self.assertIsNone(result["reports"]["vscode"]["updaterRunning"])

    def test_partial_process_projection_keeps_known_activity_and_unknown_negatives(self):
        self.release("1.0.0")
        self.release("2.0.0")
        self.current("2.0.0")
        self.ps_output = f"0 kernel_task\n42 {self.releases}/1.0.0/daemon\n43 /tmp/ShipIt\n"
        result = self.inspect()
        versions = {entry["version"]: entry for entry in result["reports"]["codex"]["versions"]}
        self.assertTrue(versions["1.0.0"]["active"])
        self.assertIsNone(versions["2.0.0"]["active"])
        self.assertTrue(result["reports"]["vscode"]["updaterRunning"])
        self.assertIsNone(result["reports"]["vscode"]["running"])
        self.assertEqual(result["reports"]["vscode"]["processReason"], "non_absolute_process_executable")
        self.assertEqual(result["reports"]["codex"]["processReason"], "non_absolute_process_executable")
        self.assertTrue(any("non_absolute_process_executable" in warning for warning in result["warnings"]))

    def test_old_mtime_is_not_inactive_age_and_private_state_is_not_opened(self):
        release = self.release("1.0.0")
        self.current("1.0.0")
        os.utime(release, (1, 1))
        for name in ("sessions", "auth.json", "state.sqlite", "primaryruntime"):
            (self.home / ".codex" / name).write_bytes(b"private fixture")
        reader = reports.read_plist

        def only_info(path, **kwargs):
            self.assertEqual(path.name, "Info.plist")
            self.assertNotIn(".codex", path.parts)
            return reader(path, **kwargs)

        with patch.object(reports, "read_plist", side_effect=only_info):
            result = self.inspect()
        self.assertIsNone(result["reports"]["codex"]["versions"][0]["inactiveSince"])
        self.assertNotIn("private fixture", json.dumps(result))
        for name in ("sessions", "auth.json", "state.sqlite", "primaryruntime"):
            self.assertEqual((self.home / ".codex" / name).read_bytes(), b"private fixture")

    def test_new_extraction_with_old_mtime_and_equal_versions_does_not_prove_completion(self):
        staged = self.staging / "update.abc/Visual Studio Code.app"
        self.bundle(staged)
        self.bundle(self.system / "Visual Studio Code.app")
        os.utime(staged.parent, (1, 1))
        result = self.inspect()
        vscode = result["reports"]["vscode"]
        entry = vscode["staging"][0]["entries"][0]
        self.assertEqual(entry["bundle"]["version"], vscode["installed"][0]["version"])
        self.assertIsNone(entry["completion"])
        self.assertEqual(entry["completionStatus"], "unknown")
        self.assertIsNone(entry["inactiveSince"])
        self.assertNotIn("PrivateAccount", json.dumps(result))
        self.assertNotIn("never report this value", json.dumps(result))

    def test_active_updater_code_chrome_and_external_staging_remain_report_only(self):
        self.bundle(self.base / "T/ShipIt.123/Visual Studio Code.app")
        (self.base / "X/com.google.Chrome.code_sign_clone").mkdir()
        (self.base / "T/not-a-ShipIt").mkdir()
        self.ps_output = ("71 /Applications/Visual Studio Code.app/Contents/MacOS/Electron\n"
                          "72 /Applications/Visual Studio Code.app/Contents/Frameworks/ShipIt\n"
                          "73 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n")
        result = self.inspect()
        self.assertTrue(result["reports"]["vscode"]["running"])
        self.assertTrue(result["reports"]["vscode"]["updaterRunning"])
        self.assertTrue(result["reports"]["chrome"]["running"])
        self.assertIsNone(result["reports"]["vscode"]["completion"])
        self.assertIsNotNone(result["reports"]["chrome"]["codeSignClone"])
        paths = {row["path"] for row in result["items"]}
        self.assertIn(str(self.base / "T/ShipIt.123"), paths)
        self.assertNotIn(str(self.base / "T/not-a-ShipIt"), paths)
        self.assertTrue(all(row["action"] == "report" for row in result["items"]))

    def test_home_manager_installed_symlink_projects_only_bundle_metadata(self):
        target = self.root / "nix/store/vscode/Applications/Visual Studio Code.app"
        self.bundle(target, version="1.110.0")
        managed = self.home / "Applications/Home Manager Apps"
        managed.parent.mkdir()
        view = self.root / "nix/store/home-manager-applications/Applications"
        view.mkdir(parents=True)
        (view / "Visual Studio Code.app").symlink_to(target, target_is_directory=True)
        managed.symlink_to(view, target_is_directory=True)
        entry = managed / "Visual Studio Code.app"
        reader = reports.read_plist
        with patch.object(reports, "read_plist", wraps=reader) as plist_reader:
            result = self.inspect()
        installed = next(row for row in result["reports"]["vscode"]["installed"] if row["path"] == str(entry))
        self.assertEqual(installed["status"], "complete")
        self.assertEqual(installed["version"], "1.110.0")
        self.assertEqual(installed["bundleIdentifier"], "com.microsoft.VSCode")
        self.assertEqual(installed["resolvedPath"], str(target))
        self.assertTrue(any(call.args[0] == target / "Contents/Info.plist" for call in plist_reader.call_args_list))
        self.assertFalse(any(row["path"] in (str(entry), str(target)) for row in result["items"]))
        self.assertNotIn("PrivateAccount", json.dumps(result))
        self.assertEqual(reports._bundle(entry, ScanBudget())["status"], "unknown")

    def test_broken_home_manager_entry_is_absent_and_budget_exhaustion_unknown(self):
        managed = self.home / "Applications/Home Manager Apps"
        managed.parent.mkdir()
        managed.symlink_to(self.root / "missing", target_is_directory=True)
        path = str(managed / "Visual Studio Code.app")
        for budget, expected in ((ScanBudget(), "absent"), (ScanBudget(seconds=0), "unknown")):
            result = self.inspect(budget)
            installed = next(row for row in result["reports"]["vscode"]["installed"] if row["path"] == path)
            self.assertEqual(installed["status"], expected)
            self.assertIsNone(installed["version"])

    def test_namespaced_shipit_temp_is_reported_and_partial_cache_permission_preserves_entries(self):
        name = "com.microsoft.VSCode.ShipIt.b2proW3u"
        staged = self.base / "T" / name
        self.bundle(staged / "Visual Studio Code.app")
        (self.base / "T/unrelated.com.microsoft.VSCode.ShipIt.test").mkdir()
        self.bundle(self.base / "C/ShipIt.known/Visual Studio Code.app")
        children = reports._children

        def partial(path, budget):
            yield from children(path, budget)
            if path == self.base / "C":
                raise PermissionError("fixture cache access denied")

        with patch.object(reports, "_children", side_effect=partial):
            result = self.inspect()
        vscode = result["reports"]["vscode"]
        temp = next(row for row in vscode["staging"] if row["path"] == str(self.base / "T"))
        cache = next(row for row in vscode["staging"] if row["path"] == str(self.base / "C"))
        self.assertEqual([entry["name"] for entry in temp["entries"]], [name])
        self.assertEqual(temp["entries"][0]["bundle"]["version"], "1.99.0")
        self.assertEqual(cache["status"], "unknown")
        self.assertEqual(cache["reason"], "permission_denied")
        self.assertEqual([entry["name"] for entry in cache["entries"]], ["ShipIt.known"])
        row = next(row for row in result["items"] if row["path"] == str(staged))
        self.assertEqual(row["action"], "report")
        self.assertGreater(row["logicalBytes"], 0)
        self.assertIsNone(temp["entries"][0]["completion"])

    def test_incomplete_getconf_has_explicit_status_and_never_derives_x(self):
        for value in (None, "", "invalid"):
            with self.subTest(value=value):
                self.getconf["DARWIN_USER_CACHE_DIR"] = value
                result = self.inspect()
                roots = result["reports"]["darwinTemporary"]
                self.assertEqual(roots["status"], "unknown")
                self.assertIn(roots["roots"]["cache"]["status"], ("unknown", "missing", "invalid"))
                self.assertIsNone(roots["userBase"])
                self.assertIsNone(result["reports"]["chrome"]["codeSignClone"])
                self.assertFalse(any(row["path"].startswith(str(self.base)) for row in result["items"]))

    def test_mismatched_user_bases_are_not_scanned(self):
        with patch.object(reports, "_validate_darwin_directory",
                          side_effect=[self.base / "T", self.root / "foreign/C"]):
            result = self.inspect()
        self.assertEqual(result["reports"]["darwinTemporary"]["status"], "invalid")
        self.assertIsNone(result["reports"]["chrome"]["codeSignClone"])

    def test_staging_symlink_is_not_measured_or_read(self):
        self.staging.mkdir(parents=True)
        foreign = self.root / "foreign"
        foreign.mkdir()
        (self.staging / "update.foreign").symlink_to(foreign)
        result = self.inspect()
        entry = result["reports"]["vscode"]["staging"][0]["entries"][0]
        self.assertEqual(entry["status"], "unknown")
        self.assertNotIn("itemId", entry)

    def test_shared_budget_exhaustion_does_not_become_zero_or_completed(self):
        for index in range(10):
            self.release(f"1.0.{index}")
        self.current("1.0.9")
        self.staging.mkdir(parents=True)
        for index in range(10):
            (self.staging / f"update.{index}").mkdir()
        for budget in (ScanBudget(max_entries=2), ScanBudget(seconds=0)):
            with self.subTest(budget=budget):
                result = self.inspect(budget)
                self.assertEqual(result["reports"]["codex"]["status"], "unknown")
                self.assertLessEqual(budget.entries, budget.max_entries)
                self.assertEqual(result["reports"]["vscode"]["staging"][0]["reason"], "scan_budget_exhausted")
                for row in result["items"]:
                    self.assertIsNone(row["allocatedBytes"])
                    self.assertEqual(row["action"], "report")

    def test_shipit_directory_stream_stops_at_shared_entry_budget(self):
        for index in range(20):
            (self.base / "T" / f"ShipIt.{index}").mkdir()
        budget = ScanBudget(max_entries=2)
        items = []
        result = reports._staging(self.base / "T", "temp", budget, items, temp=True)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "scan_budget_exhausted")
        self.assertEqual(budget.entries, 2)
        self.assertEqual(len(result["entries"]), 2)
        self.assertTrue(all(entry["completion"] is None for entry in result["entries"]))

    def test_large_and_malformed_plist_are_unknown_without_leaking_content(self):
        staged = self.staging / "update.large/Visual Studio Code.app"
        self.bundle(staged)
        plist = staged / "Contents/Info.plist"
        for content in (b"x" * (2 * 1024**2 + 1), b"not a plist"):
            with self.subTest(size=len(content)):
                plist.write_bytes(content)
                evidence = self.inspect()["reports"]["vscode"]["staging"][0]["entries"][0]
                self.assertEqual(evidence["bundle"]["status"], "unknown")
                self.assertIsNone(evidence["bundle"]["version"])
                self.assertIsNone(evidence["completion"])

    def test_unsupported_platform_does_no_work(self):
        result = self.inspect(platform="linux")
        self.assertEqual(result["reports"]["applications"]["status"], "unsupported")
        self.assertEqual(result["items"], [])
        self.assertEqual(self.calls, [])


class DarwinDirectoryValidationTests(unittest.TestCase):
    """Exercise the production validator against a virtual, FD-bound filesystem."""

    def validate(self, raw, leaf="T", *, foreign=None, link=None, root_uid=0, changed=None):
        uid = 501
        descriptors = {}
        opened = []
        canonical = Path("/private/var/folders/ab/user_hash/T")

        def info(path):
            owner = uid if len(path.parts) >= 6 else 0
            if path == Path("/"):
                owner = root_uid
            if path == foreign:
                owner = 999
            mode = stat.S_IFLNK if path == link else stat.S_IFDIR
            return SimpleNamespace(st_uid=owner, st_mode=mode, st_dev=1, st_ino=hash(path))

        def open_fd(name, flags, dir_fd=None):
            path = Path(name) if dir_fd is None else descriptors[dir_fd] / name
            opened.append(path)
            descriptor = len(opened) + 100
            descriptors[descriptor] = path
            self.assertTrue(flags & os.O_NOFOLLOW)
            return descriptor

        def fstat(descriptor):
            path = descriptors[descriptor]
            value = info(path)
            if path == changed:
                value.st_ino += 1
            return value

        with patch.object(reports.os, "open", side_effect=open_fd), \
                patch.object(reports.os, "stat", side_effect=lambda name, *, dir_fd, follow_symlinks:
                             info(descriptors[dir_fd] / name) if not follow_symlinks else self.fail("followed link")), \
                patch.object(reports.os, "fstat", side_effect=fstat), \
                patch.object(reports.os, "close"):
            value = reports._validate_darwin_directory(raw, leaf, uid, ScanBudget())
        self.assertEqual(value, canonical.with_name(leaf))
        return opened

    def test_valid_structure_and_only_documented_var_alias(self):
        for raw in ("/private/var/folders/ab/user_hash/T", "/var/folders/ab/user_hash/T/\n"):
            opened = self.validate(raw)
            self.assertEqual(opened[-1], Path("/private/var/folders/ab/user_hash/T"))
        self.validate("/private/var/folders/ab/user_hash/C", leaf="C")

    def test_rejects_foreign_uid_at_base_leaf_or_parent(self):
        for suffix in ("", "/T"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.validate("/private/var/folders/ab/user_hash/T",
                              foreign=Path("/private/var/folders/ab/user_hash" + suffix))
        with self.assertRaises(ValueError):
            self.validate("/private/var/folders/ab/user_hash/T", foreign=Path("/private/var"))
        with self.assertRaises(ValueError):
            self.validate("/private/var/folders/ab/user_hash/T", root_uid=501)

    def test_rejects_symlinks_in_every_component_and_directory_replacement(self):
        path = Path("/private/var/folders/ab/user_hash/T")
        for component in (path, *list(path.parents)[:-1]):
            with self.subTest(component=component), self.assertRaises(ValueError):
                self.validate(str(path), link=component)
        with self.assertRaises(ValueError):
            self.validate(str(path), changed=path)

    def test_rejects_arbitrary_paths_traversal_wrong_leaf_and_multiple_lines(self):
        paths = ("/tmp/T", "relative/T", "/private/var/folders/abc/user_hash/T",
                 "/private/var/folders/ab/user_hash/../user_hash/T", "/private/var/folders/ab/user_hash/C",
                 "/private/var/folders/ab/user_hash/T/child", "/private//var/folders/ab/user_hash/T",
                 "/private/var/folders/ab/user_hash/T\n/foreign/T", "")
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(path)

    def test_missing_permission_and_budget_are_unknown_not_accepted(self):
        raw = "/private/var/folders/ab/user_hash/T"
        for error in (FileNotFoundError(), PermissionError()):
            with patch.object(reports.os, "open", side_effect=error), self.assertRaises(OSError):
                reports._validate_darwin_directory(raw, "T", 501, ScanBudget())
        with patch.object(reports.os, "open", return_value=100), \
                patch.object(reports.os, "fstat", return_value=SimpleNamespace(st_uid=0)), \
                patch.object(reports.os, "close"), self.assertRaises(OSError):
            reports._validate_darwin_directory(raw, "T", 501, ScanBudget(seconds=0))


if __name__ == "__main__":
    unittest.main()

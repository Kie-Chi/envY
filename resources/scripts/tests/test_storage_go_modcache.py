"""Whole-module-cache maintenance binds explicit native targets in fixtures."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from envy.storage import cli, go_modcache, maintenance, native_context
from envy.storage.model import StorageError, policy_from_manifest


class GoModuleCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.cwd = self.home / "project"
        self.cwd.mkdir()
        self.custom = self.home / "custom-module-downloads"

    def output(self, value):
        return subprocess.CompletedProcess([], 0, str(value) + "\n", "")

    def test_custom_cache_is_bound_and_rechecked_with_same_cwd(self):
        with patch.object(go_modcache, "probe", return_value=self.output(self.custom)) as probe:
            target = go_modcache.target(home=self.home, cwd=self.cwd)
            go_modcache.verify(target, home=self.home)
        self.assertEqual(target["path"], str(self.custom))
        self.assertEqual(go_modcache.environment(target)["GOMODCACHE"], str(self.custom))
        self.assertTrue(all(call.kwargs["cwd"] == self.cwd for call in probe.call_args_list))

    def test_target_drift_and_failed_probe_reject_execution(self):
        with patch.object(go_modcache, "probe", side_effect=[self.output(self.custom), self.output(self.home / "changed")]):
            target = go_modcache.target(home=self.home, cwd=self.cwd)
            with self.assertRaisesRegex(StorageError, "target changed"):
                go_modcache.verify(target, home=self.home)
        with patch.object(go_modcache, "probe", return_value=subprocess.CompletedProcess([], 124, "", "")):
            with self.assertRaises(StorageError):
                go_modcache.target(home=self.home)

    def test_current_project_ancestor_and_symlink_alias_are_rejected(self):
        nested = self.cwd / "subproject"
        nested.mkdir()
        alias = self.home / "cache-alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        for value in (self.cwd, alias):
            with self.subTest(value=value), patch.object(go_modcache, "probe", return_value=self.output(value)):
                with self.assertRaisesRegex(StorageError, "project"):
                    go_modcache.target(home=self.home, cwd=nested)

    def test_explicit_provider_binary_and_environment_remain_read_only_and_compatible(self):
        environment = {"PATH": "/fake/bin", "GOTOOLCHAIN": "local"}
        binary = "/fake/bin/go"
        with patch.object(go_modcache, "probe", return_value=self.output(self.custom)) as probe:
            target = go_modcache.target(home=self.home, cwd=self.cwd, env=environment, binary=binary)
            go_modcache.verify(target, home=self.home, env=environment, binary=binary)
        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertEqual(call.args[0], [binary, "env", "GOMODCACHE"])
            self.assertEqual(call.kwargs, {"cwd": self.cwd, "env": environment})

    @contextlib.contextmanager
    def native_maintenance(self):
        binary = self.home / "bin/go"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text("synthetic provider; never executed")
        binary.chmod(0o700)
        policy = policy_from_manifest({
            "settings": {"envy.user.home": str(self.home)},
            "storage": {"mode": "manual", "rules": [],
                        "maintenance": {"go-modcache": True}},
        }, self.home / "state", self.cwd)
        metadata = native_context._metadata
        values = {"GOENV": "off", "GOMOD": str(self.cwd / "go.mod"), "GOWORK": ""}
        with patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(binary.parent)}, clear=True), \
                patch.object(go_modcache.Path, "cwd", return_value=self.cwd), \
                patch.object(native_context, "_metadata", side_effect=lambda path: metadata(path)
                             if path.is_relative_to(self.home) else {"path": str(path), "exists": False}), \
                patch.object(native_context, "probe", return_value=self.output(json.dumps(values))), \
                patch.object(maintenance, "PLATFORM", "linux"), \
                patch.object(maintenance, "run_process",
                             return_value=subprocess.CompletedProcess([], 0, "", "")) as execute:
            yield policy, execute, binary

    def test_legacy_cli_rejects_manifest_protected_targets_before_cleanup(self):
        for name in (".ssh", ".config/sops", "state", "project"):
            with self.subTest(target=name):
                target = self.home / name
                target.mkdir(parents=True, exist_ok=True)
                marker = target / "synthetic-evidence"
                marker.write_text("preserve this fixture")
                output = io.StringIO()
                with self.native_maintenance() as (policy, execute, _), \
                        patch.object(go_modcache, "probe", return_value=self.output(target)), \
                        patch.object(cli, "_context", return_value=(policy, self.home / "state")), \
                        patch.object(cli.log, "console", Console(file=output, force_terminal=False)):
                    preview = maintenance.preview(policy, ["go-modcache"])
                    result = CliRunner().invoke(cli.app, ["maintain", "--go-modcache", "--yes", "--json"])
                self.assertFalse(preview["items"][0]["available"])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(json.loads(output.getvalue())["data"]["errors"], ["go-modcache"])
                execute.assert_not_called()
                self.assertEqual(marker.read_text(), "preserve this fixture")

    def test_legacy_maintenance_allows_bound_custom_module_cache(self):
        self.custom.mkdir()
        with self.native_maintenance() as (policy, execute, binary), \
                patch.object(go_modcache, "probe", return_value=self.output(self.custom)):
            preview = maintenance.preview(policy, ["go-modcache"])
            execute.assert_not_called()
            result = maintenance.maintain(policy, ["go-modcache"])
        self.assertTrue(preview["items"][0]["available"])
        self.assertEqual(result["errors"], [])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], [str(binary), "clean", "-modcache"])
        self.assertEqual(execute.call_args.kwargs["cwd"], str(self.cwd))
        self.assertEqual(execute.call_args.kwargs["env"]["GOMODCACHE"], str(self.custom))
        self.assertEqual(execute.call_args.kwargs["env"]["GOTOOLCHAIN"], "local")

    def test_legacy_maintenance_rejects_target_drift_and_symlink_aliases(self):
        self.custom.mkdir()
        with self.native_maintenance() as (policy, execute, _), \
                patch.object(go_modcache, "probe", side_effect=[
                    self.output(self.custom), self.output(self.home / "changed-cache")]):
            result = maintenance.maintain(policy, ["go-modcache"])
        self.assertEqual(result["errors"], ["go-modcache"])
        execute.assert_not_called()
        alias = self.home / "cache-alias"
        alias.symlink_to(self.custom, target_is_directory=True)
        with self.native_maintenance() as (policy, execute, _), \
                patch.object(go_modcache, "probe", return_value=self.output(alias)):
            result = maintenance.maintain(policy, ["go-modcache"])
        self.assertEqual(result["errors"], ["go-modcache"])
        execute.assert_not_called()

    def test_invalid_or_broad_targets_are_rejected(self):
        for value in ("relative", "/", self.home, self.cwd, str(self.custom) + "\n/other"):
            with self.subTest(value=value), patch.object(go_modcache, "probe", return_value=self.output(value)):
                with self.assertRaises(StorageError):
                    go_modcache.target(home=self.home, cwd=self.cwd)


if __name__ == "__main__":
    unittest.main()

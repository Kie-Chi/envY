"""Managed agent diagnostics never expose configuration values or probe logs."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from envy.doctor.app import app
from envy.doctor.checks import agents
from envy.doctor.model import SECTION_AGENTS
from envy.doctor import runner, selection


class AgentDoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-agent-doctor-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.agent = self.home / ".pi/agent"
        self.agent.mkdir(parents=True)
        self.metadata = self.agent / "envy-runtime.json"
        self.metadata.write_text("{}")
        self.source = self.home / "source"
        self.source.mkdir()
        self.extension = self.source / "extension.ts"
        self.extension.write_text("fixture")
        self.data = {
            "schemaVersion": 1,
            "pi": {"enabled": True, "version": "1.1.0", "node": "/nix/store/node/bin/node",
                   "nodeModules": "/nix/store/pi/node_modules", "probe": "/nix/store/probe/doctor.mjs"},
            "extensions": [{"name": "envy-test.ts", "source": str(self.extension), "target": ".pi/agent/extensions/envy-test.ts"}],
            "roles": [], "skills": [], "mcpServers": {},
        }
        self.probe = {"schemaVersion": 1, "status": "ok", "hostVersion": "1.1.0", "extensions": [
            {"status": "ok", "tools": ["test_tool"], "commands": ["test:command"], "guard": {"status": "unknown"}},
        ]}
        (self.agent / "extensions").mkdir()
        (self.agent / "extensions/envy-test.ts").symlink_to(self.extension)
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        self.enterContext(patch.object(agents, "app_policy", return_value=(True, "")))
        self.enterContext(patch.object(agents, "_metadata", side_effect=lambda _: copy.deepcopy(self.data)))
        self.probe_mock = self.enterContext(patch.object(agents, "_probe", side_effect=lambda _: copy.deepcopy(self.probe)))

    def test_success_and_unknown_compatibility(self):
        results = agents.run_checks()
        self.assertEqual(runner.exit_code(results, strict=True), 0)
        self.assertTrue(any(r.details.get("tools") == ["test_tool"] for r in results))
        self.assertTrue(any("unknown" in r.message for r in results))

    def test_missing_metadata_warns_and_strict_fails(self):
        self.metadata.unlink()
        results = agents.run_checks()
        self.assertEqual([r.status for r in results], ["warn"])
        self.assertEqual(runner.exit_code(results), 0)
        self.assertEqual(runner.exit_code(results, strict=True), 1)
        self.probe_mock.assert_not_called()

    def test_machine_policy_skips_without_reading_metadata(self):
        with patch.object(agents, "app_policy", return_value=(False, "excluded")), patch.object(agents, "_metadata", side_effect=AssertionError):
            self.assertEqual([r.status for r in agents.run_checks()], ["info"])
        self.data["pi"]["enabled"] = False
        self.assertEqual([r.status for r in agents.run_checks()], ["info"])
        self.probe_mock.assert_not_called()

    def test_broken_and_wrong_links_fail(self):
        link = self.agent / "extensions/envy-test.ts"
        link.unlink()
        link.symlink_to(self.home / "absent")
        self.assertEqual(runner.exit_code(agents.run_checks()), 1)
        link.unlink()
        link.write_text("fixture")
        self.assertEqual(runner.exit_code(agents.run_checks()), 1)

    def test_recursive_skill_checks_marker_source(self):
        marker = self.source / "SKILL.md"
        marker.write_text("fixture")
        skill = self.agent / "skills/read"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").symlink_to(marker)
        self.data["skills"] = [{"name": "read", "source": str(self.source), "target": ".pi/agent/skills/read", "recursive": True}]
        self.assertEqual(runner.exit_code(agents.run_checks()), 0)
        (skill / "SKILL.md").unlink()
        (skill / "SKILL.md").write_text("fixture")
        self.assertEqual(runner.exit_code(agents.run_checks()), 1)

    def test_mcp_drift_and_missing_executable_do_not_execute_or_leak(self):
        self.data["mcpServers"] = {"fixture": {"enabled": True, "command": "fixture-command", "args": ["secret-argument"]}}
        (self.agent / "mcp.json").write_text(json.dumps({"mcpServers": {
            "fixture": {"command": "fixture-command", "args": ["secret-argument"], "env": {"TOKEN": "secret-value"}},
        }}))
        with patch.object(agents.shutil, "which", return_value=None), patch.object(subprocess, "run", side_effect=AssertionError):
            results = agents.run_checks()
        self.assertEqual(runner.exit_code(results), 1)
        serialized = json.dumps([r.to_dict() for r in results])
        for secret in ("secret-argument", "secret-value", "fixture-command"):
            self.assertNotIn(secret, serialized)
        with patch.object(agents.shutil, "which", return_value="/bin/fixture"):
            self.assertEqual(runner.exit_code(agents.run_checks()), 0)

    def test_override_is_presence_only_and_info(self):
        override = self.home / ".config/envy/agent-runtime/pi.env"
        override.parent.mkdir(parents=True)
        override.write_text("secret-override")
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read")):
            results = agents.run_checks()
        self.assertTrue(any(r.name == "Pi runtime override" and r.status == "info" for r in results))

    def test_probe_failure_and_version_guard_are_safe(self):
        with patch.object(agents, "_probe", side_effect=ValueError("secret-exception")):
            results = agents.run_checks()
        self.assertEqual(runner.exit_code(results), 1)
        self.assertNotIn("secret-exception", json.dumps([r.to_dict() for r in results]))
        self.probe["extensions"][0]["guard"] = {"status": "error", "minimum": "9.0.0"}
        self.assertEqual(runner.exit_code(agents.run_checks()), 1)
        self.probe["hostVersion"] = "0.1.0"
        self.assertTrue(any(r.name == "Pi host version" and r.failed for r in agents.run_checks()))

    def test_metadata_symlink_loop_is_reported_without_exception(self):
        self.metadata.unlink()
        self.metadata.symlink_to(self.metadata.name)
        with patch.object(agents, "_metadata", side_effect=RuntimeError("secret-loop")):
            result = CliRunner().invoke(app, ["agents", "--json"])
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertEqual(payload["summary"]["error"], 1)
        self.assertNotIn("secret-loop", result.output)
        self.probe_mock.assert_not_called()

    def test_cli_strict_warning_preserves_json_envelope(self):
        self.metadata.unlink()
        result = CliRunner().invoke(app, ["agents", "--strict", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(json.loads(result.output)["summary"]["warn"], 1)

    def test_cli_json_and_selection(self):
        self.assertEqual(selection.parse_only(["section:agents"]).sections, {SECTION_AGENTS})
        result = CliRunner().invoke(app, ["agents", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["summary"]["error"], 0)
        self.assertTrue(all(r["section"] == "agents" for r in payload["results"]))
        with patch.dict(runner.CHECKS, {"agents": agents.run_checks}), patch.object(runner.config, "run_checks", side_effect=AssertionError):
            results = runner.run_sections(["agents"], ["agents"], log_progress=False)
        self.assertTrue(results)


class AgentMetadataTests(unittest.TestCase):
    def test_rejects_non_store_executables_and_metadata(self):
        for value in ("/usr/bin/node", "relative", None):
            with self.assertRaises((ValueError, OSError)):
                agents._store_path(value, executable=True)
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "metadata.json"
            file.write_text('{"schemaVersion": 1}')
            with self.assertRaises(ValueError):
                agents._metadata(file)

    def test_target_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = {"schemaVersion": 1, "pi": {"enabled": True, "version": "1.1.0", "node": "node", "probe": "doctor.mjs", "nodeModules": "modules"},
                    "extensions": [{"name": "envy-test.ts", "source": str(root), "target": ".pi/agent/extensions/../../auth.json"}], "roles": [], "skills": [], "mcpServers": {}}
            file = root / "metadata.json"
            file.write_text(json.dumps(data))
            file.chmod(0o444)
            with patch.object(agents, "_store_path", side_effect=lambda value, **_: file if value == str(file) else root / value):
                with self.assertRaisesRegex(ValueError, "target"):
                    agents._metadata(file)

    def test_env_wrapper_checks_real_server_without_executing(self):
        def available(command):
            return {"/store/bin/env": "/store/bin/env", "/store/bin/server": "/store/bin/server"}.get(command)
        with patch.object(agents.shutil, "which", side_effect=available), patch.object(subprocess, "run", side_effect=AssertionError):
            self.assertTrue(agents._executable("/store/bin/env", ["-u", "PYTHONPATH", "/store/bin/server"]))
            self.assertFalse(agents._executable("/store/bin/env", ["-u", "PYTHONPATH", "/store/bin/missing"]))
            self.assertFalse(agents._executable("/store/bin/env", ["-u"]))
            self.assertFalse(agents._executable("/store/bin/env", ["TOKEN=secret", "/store/bin/server"]))
            self.assertFalse(agents._executable("/store/bin/env", ["--unknown", "/store/bin/server"]))

    def test_subprocess_has_no_credentials_and_discards_logs(self):
        data = {"pi": {"node": "/nix/store/node/bin/node", "probe": "/nix/store/probe/doctor.mjs"}}
        completed = subprocess.CompletedProcess([], 0, '{"schemaVersion":1,"status":"ok"}', "secret-log")
        with patch.object(agents.subprocess, "run", return_value=completed) as run, patch.dict(os.environ, {"API_KEY": "secret-key"}):
            result = agents._probe(data)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("API_KEY", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.args[0], [data["pi"]["node"], data["pi"]["probe"]])
        self.assertFalse(Path(run.call_args.kwargs["cwd"]).exists())


if __name__ == "__main__":
    unittest.main()

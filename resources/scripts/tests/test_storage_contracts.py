"""Storage capability contracts use synthetic policy and never run maintenance."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.main import get_command
from typer.testing import CliRunner

from envy.storage import cli, contracts, maintenance
from envy.storage.model import Policy, StorageError


class StorageContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"storage": {"maintenance": {"nix": True, "go-modcache": True},
                                     "archive": {"repository": "secret-repository",
                                                 "passwordFile": "secret-password-path"},
                                     "protectedOverrides": ["secret-rule-path"]}}

    def invoke(self, args):
        output = io.StringIO()
        with patch.object(cli.log, "console", Console(file=output, force_terminal=False)):
            result = CliRunner().invoke(cli.app, args)
        self.assertEqual(result.output, "")
        return result, json.loads(output.getvalue())

    def test_actions_match_owner_and_opt_ins_without_probing(self):
        with patch.object(maintenance, "run_process", side_effect=AssertionError("probe")), \
                patch.object(maintenance.shutil, "which", side_effect=AssertionError("tool lookup")), \
                patch.object(maintenance.docker, "inspect", side_effect=AssertionError("Docker")):
            data = contracts.capabilities(self.manifest)
        self.assertEqual([item["id"] for item in data["actions"]], list(maintenance.ACTIONS))
        self.assertEqual(set(maintenance.ACTIONS), set(maintenance.NOTES))
        for item in data["actions"]:
            self.assertEqual(item["flag"], "--" + item["id"])
            self.assertEqual(item["policyKey"], "envy.storage.maintenance." + item["id"])
            self.assertFalse(item["policyDefault"])
            self.assertEqual(item["policyEnabled"], item["id"] in {"nix", "go-modcache"})
            self.assertTrue(item["requiresPolicyOptIn"])
            self.assertFalse(item["scheduled"])
            self.assertTrue(item["preview"]["supported"])
            self.assertFalse(item["preview"]["writes"])
            self.assertEqual(item["sideEffects"], maintenance.NOTES[item["id"]])
        self.assertFalse(any(item["policyEnabled"] for item in contracts.capabilities({})["actions"]))

    def test_cli_capabilities_is_one_secret_safe_document_without_context_or_state(self):
        with patch.object(cli, "machine_manifest", return_value=self.manifest), \
                patch.object(cli, "_context", side_effect=AssertionError("full policy")), \
                patch.object(cli, "locked", side_effect=AssertionError("state")), \
                patch.object(cli.inspection, "inspect", side_effect=AssertionError("scan")), \
                patch.object(maintenance, "preview", side_effect=AssertionError("preview")):
            result, payload = self.invoke(["capabilities", "--json"])
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["command"], "storage capabilities")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["data"]["contractVersion"], 1)
        text = json.dumps(payload)
        for secret in ("secret-repository", "secret-password-path", "secret-rule-path"):
            self.assertNotIn(secret, text)

    def test_invalid_policy_errors_are_one_document_and_do_not_echo_values(self):
        for manifest in (None, {"storage": []}, {"storage": {"maintenance": "secret-value"}},
                         {"storage": {"maintenance": {"nix": "secret-value"}}}):
            with self.subTest(manifest=manifest), patch.object(cli, "machine_manifest", return_value=manifest):
                result, payload = self.invoke(["capabilities", "--json"])
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "storage_error")
            self.assertNotIn("secret-value", json.dumps(payload))

    def test_command_and_flag_coverage_matches_registered_cli(self):
        commands = {item["id"]: item for item in contracts.capabilities({})["commands"]}
        registered = get_command(cli.app).commands
        self.assertEqual(set(commands), set(registered))
        for name, command in registered.items():
            metadata = commands[name]
            actual = {option for param in command.params for option in (*param.opts, *param.secondary_opts)
                      if option.startswith("--")}
            advertised = {option for flag in metadata["flags"] for option in flag["flag"].split("/")}
            self.assertEqual(advertised, actual, name)
            self.assertEqual(metadata["json"], "--json" in actual)
        self.assertFalse(commands["gc"]["json"])
        self.assertEqual(commands["gc"]["tui"], "CLIhandoff")
        self.assertIn("Delete old Nix generations", commands["gc"]["sideEffects"])
        self.assertTrue(commands["restore"]["preview"])
        self.assertIn("Immediately write", commands["restore"]["sideEffects"])
        self.assertIn("--preview-token", {item["flag"] for item in commands["restore"]["flags"]})
        for name in ("init", "check", "gc"):
            handoff = commands[name]["handoff"]
            self.assertEqual(handoff["argv"][:3], ["envy", "storage", name])
            self.assertIn("sideEffects", handoff)
            self.assertIn("exit", handoff)
        self.assertEqual(commands["check"]["handoff"]["backend"]["fixedArguments"], ["check", "--read-data"])
        self.assertNotIn("--read-data", commands["check"]["handoff"]["argv"])
        self.assertNotIn("--json", commands["gc"]["handoff"]["argv"])
        self.assertIn("save a plan", commands["plan"]["sideEffects"])
        self.assertIn("report", contracts.capabilities({})["modes"])
        self.assertNotIn("report", commands)
        self.assertEqual(contracts.capabilities({})["internalInvocations"][0]["tui"], "none")

    def test_frontend_empty_selection_rejects_and_flags_are_explicit(self):
        for execute in (False, True):
            with self.assertRaisesRegex(StorageError, "Select at least one"):
                contracts.maintenance_argv([], execute=execute)
        with self.assertRaisesRegex(StorageError, "Unknown"):
            contracts.maintenance_argv(["go", "secret-value"])
        self.assertEqual(contracts.maintenance_argv(["npm", "go", "npm"], execute=True),
                         ["storage", "maintain", "--npm", "--go", "--yes", "--json"])
        selection = contracts.capabilities({})["maintenanceSelection"]
        self.assertEqual(selection["frontendDefault"], [])
        self.assertEqual(selection["emptySelection"], "reject")

    def test_frontend_argv_and_legacy_defaults_dispatch_without_real_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            policy = Policy(home, "manual", 30, (), (), (), (), "", "", maintenance.ACTIONS)
            for execute in (False, True):
                for selected in ([name] for name in maintenance.ACTIONS):
                    with self.subTest(execute=execute, selected=selected), \
                            patch.object(cli, "_context", return_value=(policy, home / "state")), \
                            patch.object(maintenance, "preview", return_value={"items": [], "execute": False}) as preview, \
                            patch.object(maintenance, "maintain", return_value={"items": [], "execute": True}) as maintain:
                        argv = contracts.maintenance_argv(selected, execute=execute)
                        result, payload = self.invoke(argv[1:])
                        self.assertEqual(result.exit_code, 0, result.exception)
                        self.assertTrue(payload["ok"])
                        (maintain if execute else preview).assert_called_once_with(policy, selected)
                        (preview if execute else maintain).assert_not_called()
                with patch.object(cli, "_context", return_value=(policy, home / "state")), \
                        patch.object(maintenance, "preview", return_value={"items": []}) as preview, \
                        patch.object(maintenance, "maintain", return_value={"items": []}) as maintain:
                    result, _ = self.invoke(["maintain", "--json", *(["--yes"] if execute else [])])
                    self.assertEqual(result.exit_code, 0, result.exception)
                    (maintain if execute else preview).assert_called_once_with(policy, None)


if __name__ == "__main__":
    unittest.main()

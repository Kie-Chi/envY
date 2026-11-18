import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import typer
from rich.console import Console

from envy.config import cli, machine_nix, refine, secret_store, updates


class ConfigShowTests(unittest.TestCase):
    def setUp(self):
        secrets = patch.object(secret_store, "read_secrets_data", return_value=({}, False))
        managed = patch("envy.agent._read_values", return_value={})
        secrets.start()
        managed.start()
        self.addCleanup(secrets.stop)
        self.addCleanup(managed.stop)

    def test_show_uses_evaluated_values_without_software_policy(self):
        settings = {field.path: field.default_fn() for field in cli.MACHINE_FIELDS}
        settings["envy.user.name"] = "evaluated-user"
        manifest = {
            "schemaVersion": 2,
            "settings": settings,
            "software": {"groups": {
                "nix.user.package": {
                    "optionPath": "envy.software.nix.packages",
                    "selection": {
                        "include": [{"id": "git", "name": "git"}],
                        "exclude": [],
                        "effective": [{"id": "git", "name": "git"}],
                    },
                },
            }},
        }
        # Calling the command directly leaves unset options as OptionInfo
        # objects, so pass json_output explicitly to reach both renderers.
        for json_output in (False, True):
            output = io.StringIO()
            console = Console(file=output, force_terminal=False, width=220)
            with self.subTest(json_output=json_output), patch.object(
                cli, "machine_manifest", return_value=manifest
            ) as evaluate, patch.object(
                machine_nix, "read_machine_nix", return_value={"envy.user.name": "source-user"}
            ), patch.object(cli, "read_device_metadata", return_value={
                "machine_id": "test-mac", "sops_label": "test_mac"
            }), patch.object(secret_store, "read_secrets_yaml", return_value=({}, True)), patch.object(
                cli.log, "console", console
            ):
                cli.cmd_show(refresh=False, json_output=json_output)

                rendered = output.getvalue()
                self.assertIn("evaluated-user", rendered)
                self.assertIn("envy config (evaluated)" if not json_output else '"source": "evaluated"', rendered)
                self.assertNotIn("source-user", rendered)
                self.assertNotIn("envy.software", rendered)
                self.assertNotIn("include", rendered)
                evaluate.assert_called_once_with(refresh=False)

    def test_json_output_reports_secret_presence_without_values(self):
        settings = {field.path: field.default_fn() for field in cli.MACHINE_FIELDS}
        manifest = {"schemaVersion": 2, "settings": settings}
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch.object(cli, "machine_manifest", return_value=manifest), patch.object(
            cli, "read_device_metadata", return_value={"machine_id": "test-mac"}
        ), patch.object(
            secret_store, "read_secrets_yaml", return_value=({"llm_steps_apikey": "top-secret"}, True)
        ), patch.object(cli.log, "console", console):
            cli.cmd_show(refresh=False, json_output=True)

        rendered = output.getvalue()
        payload = json.loads(rendered)
        self.assertNotIn("top-secret", rendered)
        self.assertTrue(payload["secrets"]["llm/steps/apikey"])
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["platform"], cli.platform_name())

    def test_table_marks_secrets_set_or_empty_without_values(self):
        settings = {field.path: field.default_fn() for field in cli.MACHINE_FIELDS}
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch.object(cli, "machine_manifest", return_value={"schemaVersion": 2, "settings": settings}), patch.object(
            cli, "read_device_metadata", return_value={"machine_id": "test-mac"}
        ), patch.object(
            secret_store, "read_secrets_yaml",
            return_value=({"llm_steps_apikey": "top-secret", "home_passwd": ""}, True),
        ), patch.object(cli.log, "console", console):
            cli.cmd_show(refresh=False, json_output=False)

        rendered = output.getvalue()
        self.assertNotIn("top-secret", rendered)
        rows = {
            path: next(line for line in rendered.splitlines() if f" {path} " in line)
            for path in ("llm/steps/apikey", "home/passwd")
        }
        self.assertIn("<set>", rows["llm/steps/apikey"])
        self.assertIn("<empty>", rows["home/passwd"])
        self.assertNotIn("<set>", rows["home/passwd"])


    def test_json_output_includes_field_choices_metadata(self):
        settings = {field.path: field.default_fn() for field in cli.MACHINE_FIELDS}
        manifest = {"schemaVersion": 2, "settings": settings}
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch.object(cli, "machine_manifest", return_value=manifest), patch.object(
            cli, "read_device_metadata", return_value={"machine_id": "test-mac"}
        ), patch.object(secret_store, "read_secrets_yaml", return_value=({}, True)), patch.object(
            cli.log, "console", console
        ):
            cli.cmd_show(refresh=False, json_output=True)

        payload = json.loads(output.getvalue())
        fields = {entry["path"]: entry for entry in payload["fields"]}
        # Enumerated fields expose their choices so the frontend can render a dropdown.
        self.assertEqual(fields["envy.mirrors.mode"]["choices"], ["upstream", "china"])
        self.assertEqual(fields["envy.vscode.mode"]["choices"], ["remote", "local"])
        # Freeform fields report an empty choices list, signalling a text input.
        self.assertEqual(fields["envy.user.name"]["choices"], [])

    def test_json_output_includes_dynamic_llm_and_agent_views(self):
        settings = {field.path: field.default_fn() for field in cli.MACHINE_FIELDS}
        manifest = {
            "schemaVersion": 2,
            "settings": settings,
            "llm": {
                "credentials": {
                    "xikai": {"kind": "api-key", "secretPath": "llm/xikai/apikey"},
                },
                "endpoints": {
                    "xikai-openai": {
                        "baseUrl": "https://api.xikai.site/v1",
                        "protocol": "openai-compatible",
                    },
                },
            },
            "agents": {
                "codex": {
                    "adapter": "codex",
                    "endpoint": "xikai-openai",
                    "credential": "xikai",
                    "model": "gpt-6.1-sol",
                },
            },
        }
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch.object(cli, "machine_manifest", return_value=manifest), patch.object(
            cli, "read_device_metadata", return_value={"machine_id": "test-mac"}
        ), patch.object(machine_nix, "read_machine_nix", return_value={}), patch.object(
            secret_store, "read_secrets_yaml", return_value=({}, True)
        ), patch.object(
            secret_store, "read_secrets_data", return_value=({"llm": {"xikai": {"apikey": "secret"}}}, True)
        ), patch.object(cli.log, "console", console):
            cli.cmd_show(refresh=False, json_output=True)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["values"]["envy.llm.endpoints.xikai-openai.baseUrl"], "https://api.xikai.site/v1")
        self.assertEqual(payload["values"]["envy.agents.codex.model"], "gpt-6.1-sol")
        self.assertEqual(payload["agents"]["codex"]["credentialSet"], True)
        self.assertNotIn('"secret"', output.getvalue())

    def test_pi_moreThinking_exposes_boolean_choices_only_for_pi(self):
        agents = {
            "pi": {"adapter": "pi", "moreThinking": True},
            "custom-pi": {"adapter": "pi"},
            "codex": {"adapter": "codex", "moreThinking": False},
        }
        values = {
            f"envy.agents.{name}.{field}": value
            for name, item in agents.items() for field, value in item.items()
        }
        for evaluated in (True, False):
            with self.subTest(evaluated=evaluated):
                manifest = {"agents": agents} if evaluated else None
                output = io.StringIO()
                console = Console(file=output, force_terminal=False, width=220)
                with patch.object(cli, "machine_manifest", return_value=manifest), patch.object(
                    machine_nix, "read_machine_nix", return_value={}
                ), patch.object(cli, "read_device_metadata", return_value={}), patch.object(
                    secret_store, "read_secrets_yaml", return_value=({}, False)
                ), patch("envy.agent._read_values", return_value=values), patch.object(
                    cli.log, "console", console
                ):
                    cli.cmd_show(refresh=False, json_output=True)
                payload = json.loads(output.getvalue())
                fields = {entry["path"]: entry for entry in payload["fields"]}
                for name in ("pi", "custom-pi"):
                    key = f"envy.agents.{name}.moreThinking"
                    expected = name == "pi"
                    self.assertEqual(fields[key]["choices"], ["true", "false"])
                    self.assertEqual(payload["values"][key], str(expected).lower())
                    self.assertIs(payload["agents"][name]["moreThinking"], expected)
                key = "envy.agents.codex.moreThinking"
                self.assertNotIn(key, fields)
                self.assertNotIn(key, payload["values"])

    def test_show_hides_legacy_fields_from_source_and_cached_manifest(self):
        legacy = {
            "envy.llm.steps.url": "https://legacy.example",
            "envy.llm.steps.model": "legacy-model",
            "envy.llm.deepseek.url": "https://legacy-deepseek.example",
            "envy.llm.deepseek.model": "legacy-deepseek-model",
        }
        for manifest in (None, {"settings": legacy, "llm": {"steps": {"url": "https://legacy.example"}}}):
            for json_output in (False, True):
                with self.subTest(manifest=manifest, json=json_output):
                    output = io.StringIO()
                    console = Console(file=output, force_terminal=False, width=220)
                    with patch.object(cli, "machine_manifest", return_value=manifest), patch.object(
                        machine_nix, "read_machine_nix", return_value=legacy
                    ), patch.object(cli, "read_device_metadata", return_value={}), patch.object(
                        secret_store, "read_secrets_yaml", return_value=({}, True)
                    ), patch.object(cli.log, "console", console):
                        cli.cmd_show(refresh=False, json_output=json_output)
                    rendered = output.getvalue()
                    for path in legacy:
                        self.assertNotIn(path, rendered)
                    if json_output:
                        payload = json.loads(rendered)
                        self.assertTrue(set(legacy).isdisjoint(payload["values"]))
                        self.assertTrue(set(legacy).isdisjoint(field["path"] for field in payload["fields"]))
                        self.assertNotIn("steps", payload["llm"])

    def test_source_fallback_shows_dynamic_config_without_secret_values(self):
        values = {
            "envy.llm.endpoints.local.baseUrl": "https://local.example/v1",
            "envy.llm.endpoints.local.protocol": "openai-compatible",
            "envy.llm.credentials.local.secretPath": "llm/local/apikey",
            "envy.agents.codex.adapter": "codex",
            "envy.agents.codex.endpoint": "local",
            "envy.agents.codex.credential": "local",
            "envy.agents.codex.model": "custom-model",
            "envy.agents.pi.adapter": "pi",
            "envy.agents.pi.api": "openai-responses",
        }
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch.object(cli, "machine_manifest", return_value=None), patch.object(
            machine_nix, "read_machine_nix", return_value={}
        ), patch.object(cli, "read_device_metadata", return_value={}), patch.object(
            secret_store, "read_secrets_yaml", return_value=({}, False)
        ), patch("envy.agent._read_values", return_value=values), patch.object(
            secret_store, "read_secrets_data", return_value=({"llm": {"local": {"apikey": "hidden-key"}}}, True)
        ), patch.object(cli.log, "console", console):
            cli.cmd_show(refresh=False, json_output=True)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["source"], "source-fallback")
        self.assertEqual(payload["values"]["envy.agents.codex.model"], "custom-model")
        self.assertEqual(payload["values"]["envy.agents.pi.api"], "openai-responses")
        fields = {entry["path"]: entry for entry in payload["fields"]}
        self.assertEqual(fields["envy.agents.pi.api"]["choices"], ["openai-completions", "openai-responses", "anthropic-messages"])
        self.assertTrue(payload["llm"]["credentials"]["local"]["credentialSet"])
        self.assertNotIn("hidden-key", output.getvalue())


class ConfigMetadataTests(unittest.TestCase):
    def render(self, fields, settings, *, evaluated=True, dynamic_fields=None):
        output = io.StringIO()
        manifest = {"settings": settings} if evaluated else None
        with patch.object(cli, "MACHINE_FIELDS", fields), patch.object(
            cli, "machine_manifest", return_value=manifest
        ), patch.object(machine_nix, "read_machine_nix", return_value=settings), patch.object(
            cli, "read_device_metadata", return_value={}
        ), patch.object(secret_store, "read_secrets_yaml", return_value=({"password": "never-output"}, True)), patch.object(
            secret_store, "read_secrets_data", return_value=({}, False)
        ), patch("envy.agent.config_view", return_value={
            "values": {entry["path"]: entry["value"] for entry in dynamic_fields or []},
            "fields": dynamic_fields or [], "llm": {}, "agents": {},
        }), patch.object(cli.log, "console", Console(file=output, width=220)):
            cli.cmd_show(refresh=False, json_output=True)
        self.assertNotIn("never-output", output.getvalue())
        return json.loads(output.getvalue())

    def test_envelope_retains_all_legacy_aliases_for_both_sources(self):
        from envy.schemas.config import FieldDef

        field = FieldDef(group="TEST", dest="machine", path="envy.test", prompt="Test value")
        for evaluated in (True, False):
            with self.subTest(evaluated=evaluated):
                payload = self.render([field], {field.path: "kept"}, evaluated=evaluated)
                self.assertEqual(payload["command"], "config.show")
                self.assertTrue(payload["ok"])
                self.assertEqual(bool(payload["warnings"]), not evaluated)
                self.assertNotIn("data", payload["data"])
                self.assertEqual(payload["data"], {
                    key: value for key, value in payload.items()
                    if key not in {"command", "ok", "data", "warnings"}
                })
                self.assertEqual(payload["values"][field.path], "kept")

    def test_conditions_use_current_values_keep_inactive_fields_and_report_unknown(self):
        from envy.schemas.config import FieldDef

        field = FieldDef(
            group="TEST", dest="machine", path="envy.test.value", prompt="Dependent value",
            choices=["one", "two"], nix_type="bool", required=True,
            condition=lambda values: values.get("envy.test.mode", "off") == "on",
            default_fn=lambda: self.fail("Metadata must not evaluate defaults"),
        )
        for evaluated in (True, False):
            for dependency, state, active in (("on", "active", True), ("off", "inactive", False), (None, "unknown", None)):
                with self.subTest(evaluated=evaluated, state=state):
                    settings = {field.path: "one"}
                    if dependency is not None:
                        settings["envy.test.mode"] = dependency
                    payload = self.render([field], settings, evaluated=evaluated)
                    entry = payload["fields"][0]
                    self.assertEqual(entry["conditionState"], state)
                    self.assertIs(entry["active"], active)
                    self.assertEqual(entry["value"], "one")
                    for key, expected in {"group": "TEST", "prompt": "Dependent value", "nixType": "bool", "required": True, "choices": ["one", "two"]}.items():
                        self.assertEqual(entry[key], expected)
                    self.assertTrue(entry["activationHint"])

    def test_platform_schema_conditions_and_secret_metadata(self):
        from envy.schemas.darwin.config import MACHINE_FIELDS, SECRET_FIELDS

        tun = next(field for field in MACHINE_FIELDS if field.path.endswith(".tun"))
        for mode, state in (("none", "inactive"), ("keep", "active"), (None, "unknown")):
            with self.subTest(mode=mode), patch.object(cli, "SECRET_FIELDS", SECRET_FIELDS):
                settings = {tun.path: "false"}
                if mode is not None:
                    settings["envy.darwin.services.mihomo.mode"] = mode
                payload = self.render([tun], settings)
                self.assertEqual(payload["fields"][0]["conditionState"], state)
                secret = payload["secretFields"][0]
                self.assertEqual(secret["conditionState"], state)
                self.assertEqual(secret["path"], "proxy/url")
                self.assertIs(secret["set"], False)
                self.assertNotIn("value", secret)

    def test_dynamic_activation_hints_follow_the_actual_setter(self):
        entries = [
            {"path": "envy.agents.custom.adapter", "value": "pi", "choices": []},
            {"path": "envy.agents.custom.model", "value": "fixture-model", "choices": []},
            {"path": "envy.agentProfiles.shared.model", "value": "profile-model", "choices": []},
        ]
        fields = {entry["path"]: entry for entry in self.render([], {}, dynamic_fields=entries)["fields"]}
        adapter = fields[entries[0]["path"]]
        model = fields[entries[1]["path"]]
        self.assertEqual(adapter["changeKind"], "apply")
        self.assertIn("envy apply", adapter["activationHint"])
        self.assertEqual(model["changeKind"], "live")
        self.assertIn("newly started", model["activationHint"])
        profile = fields[entries[2]["path"]]
        self.assertEqual(profile["group"], "AGENT PROFILES")
        self.assertEqual(profile["changeKind"], "live")
        self.assertIn("newly started", profile["activationHint"])
        self.assertEqual(profile["setter"], "agent.set")
        self.assertEqual(model["setter"], "agent.set")

    def test_dynamic_fields_have_descriptions_types_choices_and_unknown_dependencies(self):
        entries = [
            {"path": "envy.agents.custom.adapter", "value": "pi", "choices": []},
            {"path": "envy.agents.custom.endpoint", "value": "local", "choices": []},
            {"path": "envy.agents.custom.contextWindow", "value": "8192", "choices": []},
            {"path": "envy.agents.custom.moreThinking", "value": "true", "choices": ["true", "false"]},
            {"path": "envy.agents.missing.api", "value": "", "choices": ["openai-responses"]},
            {"path": "envy.llm.endpoints.local.baseUrl", "value": "https://example.test", "choices": []},
        ]
        payload = self.render([], {}, dynamic_fields=entries)
        fields = {entry["path"]: entry for entry in payload["fields"]}
        self.assertIn("pi", fields[entries[0]["path"]]["choices"])
        self.assertEqual(fields[entries[1]["path"]]["choices"], ["local"])
        self.assertEqual(fields[entries[2]["path"]]["nixType"], "nullOr positiveInt")
        self.assertEqual(fields[entries[3]["path"]]["choices"], ["true", "false"])
        self.assertEqual(fields[entries[3]["path"]]["conditionState"], "active")
        self.assertEqual(fields[entries[4]["path"]]["conditionState"], "unknown")
        self.assertIsNone(fields[entries[4]["path"]]["active"])
        for entry in fields.values():
            self.assertTrue(entry["prompt"])
            self.assertTrue(entry["nixType"])
            self.assertTrue(entry["group"])


class ConfigSetTests(unittest.TestCase):
    def test_set_json_emits_result_and_suppresses_command_logs(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch.object(updates, "set_config_value") as setter, patch.object(
            cli, "offer_mutation_commit"
        ) as commit, patch.object(
            cli, "current_machine_id", return_value="test-mac"
        ), patch.object(cli.log, "console", console):
            cli.cmd_set("envy.mirrors.mode", "china", json_output=True, yes=True)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["command"], "config.set")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["path"], "envy.mirrors.mode")
        self.assertEqual(payload["data"]["value"], "china")
        setter.assert_called_once_with("envy.mirrors.mode", "china", quiet=True)
        commit.assert_called_once()

    def test_set_json_invalid_value_errors_without_committing(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=220)
        with patch.object(
            updates, "set_config_value", side_effect=typer.BadParameter("invalid value")
        ), patch.object(cli, "offer_mutation_commit") as commit, patch.object(
            cli.log, "console", console
        ):
            with self.assertRaises(typer.Exit):
                cli.cmd_set("envy.mirrors.mode", "bogus", json_output=True)

        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid-value")
        commit.assert_not_called()


class ConfigRefineTests(unittest.TestCase):
    def test_refine_offers_one_commit_only_when_it_changed_something(self):
        machine = Path("/repo/hosts/darwin/test-mac.nix")
        for changed in (True, False):
            with self.subTest(changed=changed), patch.object(
                refine, "refine_all", return_value=refine.RefineReport(changed=changed)
            ) as refine_all, patch.object(cli, "offer_mutation_commit") as commit, patch.object(
                cli, "current_machine_id", return_value="test-mac"
            ), patch.object(machine_nix, "machine_config_file", return_value=machine):
                cli.cmd_refine(prune=True)

            refine_all.assert_called_once_with(write=True, strict=True, include_secrets=True, prune=True)
            if changed:
                commit.assert_called_once_with([machine, cli.SECRETS_FILE], "chore(config): refine test-mac")
            else:
                commit.assert_not_called()

    def test_failed_refine_exits_without_offering_a_commit(self):
        report = refine.RefineReport(changed=True, errors=["home/passwd"])
        with patch.object(refine, "refine_all", return_value=report), patch.object(
            cli, "offer_mutation_commit"
        ) as commit:
            with self.assertRaises(typer.Exit) as raised:
                cli.cmd_refine(prune=False)

        self.assertEqual(raised.exception.exit_code, 1)
        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

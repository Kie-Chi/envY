import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console
from typer.main import get_command
from typer.testing import CliRunner

from envy import software
from envy.main import cli
from envy.search import providers, runner
from envy.search.model import ProviderReport, ResolveResult, SearchResult


class SoftwareCliTests(unittest.TestCase):
    @staticmethod
    def _resolved_cask(name="zotero"):
        return ResolveResult.found(SearchResult(
            source="homebrew", ecosystem="homebrew", name=name,
            kind="cask", version="1.0", ref=f"homebrew:cask/{name}",
        ))

    @staticmethod
    def _completion_manifest():
        def entry(item_id, name=None):
            return {
                "id": item_id,
                "name": name or item_id,
                "version": None,
                "ref": None,
                "parameters": {},
            }

        return {
            "schemaVersion": 2,
            "platform": "linux",
            "software": {"groups": {
                "nix.user.package": {
                    "label": "Nix packages",
                    "optionPath": "envy.software.nix.packages",
                    "ecosystem": "nix",
                    "scope": "user",
                    "kind": "package",
                    "installer": "home-manager",
                    "editable": {"include": True, "exclude": True},
                    "selection": {
                        "include": [entry("git"), entry("okular")],
                        "exclude": ["okular"],
                        "effective": [entry("git")],
                    },
                },
                "npm.user.tool": {
                    "label": "NPM tools",
                    "optionPath": "envy.software.npm.tools",
                    "ecosystem": "npm",
                    "scope": "user",
                    "kind": "tool",
                    "installer": "npm",
                    "editable": {"include": True, "exclude": True},
                    "selection": {"include": [], "exclude": [], "effective": []},
                },
            }},
        }

    @classmethod
    def _policy_manifest(cls, *, include=(), exclude=(), effective=()):
        manifest = cls._completion_manifest()
        manifest["id"] = "test-linux"
        selection = manifest["software"]["groups"]["nix.user.package"]["selection"]
        selection["include"] = [
            {
                "id": item,
                "name": item,
                "version": None,
                "ref": None,
                "parameters": {},
            }
            for item in include
        ]
        selection["exclude"] = list(exclude)
        selection["effective"] = [
            {
                "id": item,
                "name": item,
                "version": None,
                "ref": None,
                "parameters": {},
            }
            for item in effective
        ]
        return manifest

    @staticmethod
    def _cask_manifest(*, include=(), exclude=(), effective=()):
        def entries(values):
            return [
                {
                    "id": value, "name": value, "version": None,
                    "ref": f"homebrew:cask/{value}", "parameters": {},
                }
                for value in values
            ]
        return {
            "schemaVersion": 2,
            "id": "test-darwin",
            "platform": "darwin",
            "system": "aarch64-darwin",
            "software": {"groups": {
                "homebrew.system.cask": {
                    "label": "Homebrew casks",
                    "optionPath": "envy.darwin.software.homebrew.casks",
                    "ecosystem": "homebrew",
                    "scope": "system",
                    "kind": "cask",
                    "installer": "homebrew",
                    "editable": {"include": True, "exclude": True},
                    "selection": {
                        "include": entries(include),
                        "exclude": list(exclude),
                        "effective": entries(effective),
                    },
                },
            }},
        }

    def test_software_and_sw_expose_short_subcommands(self):
        cli_runner = CliRunner()
        full = cli_runner.invoke(cli, ["software", "--help"])
        short = cli_runner.invoke(cli, ["sw", "--help"])

        self.assertEqual(full.exit_code, 0)
        self.assertEqual(short.exit_code, 0)
        for command in ("ls", "st", "en", "dis", "add", "rm", "se", "audit", "why", "cache"):
            self.assertIn(command, full.stdout)
            self.assertIn(command, short.stdout)

    def test_add_and_rm_register_group_and_item_completion(self):
        commands = get_command(cli).commands["sw"].commands

        for command_name in ("add", "rm"):
            for parameter in commands[command_name].params[:2]:
                self.assertIsNotNone(parameter._custom_shell_complete)

        why = commands["why"]
        self.assertIsNotNone(next(param for param in why.params if param.name == "item")._custom_shell_complete)
        self.assertIsNotNone(next(param for param in why.params if param.name == "group")._custom_shell_complete)

    def test_exact_stable_id_wins_over_another_items_display_name(self):
        items = [
            software.SoftwareItem(
                id="alpha", name="first", version=None, ref=None,
                included=True, checked=True, managed=False, locked=False,
                stale=False, changed=False,
            ),
            software.SoftwareItem(
                id="beta", name="alpha", version=None, ref=None,
                included=True, checked=True, managed=False, locked=False,
                stale=False, changed=False,
            ),
        ]
        self.assertEqual(software._resolve_existing_item(items, "alpha").id, "alpha")

    def test_removed_config_software_and_top_level_search_are_unknown(self):
        cli_runner = CliRunner()
        self.assertEqual(cli_runner.invoke(cli, ["config", "software"]).exit_code, 2)
        self.assertEqual(cli_runner.invoke(cli, ["search", "codex"]).exit_code, 2)

    def test_add_dry_run_prints_include_plan_without_writing(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            original = "{ ... }:\n\n{\n}\n"
            machine.write_text(original)
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "resolve_exact", return_value=self._resolved_cask()
            ), patch.object(software, "_manifest_or_exit", return_value=manifest), patch.object(
                software, "machine_file", return_value=machine
            ):
                result = cli_runner.invoke(cli, [
                    "sw", "add", "homebrew.system.cask", "zotero", "--dry-run",
                ])

            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertIn("include", result.stdout)
            self.assertIn("zotero", result.stdout)
            self.assertIn("dry run", result.stdout)
            self.assertEqual(machine.read_text(), original)

    def test_add_accepts_item_with_explicit_group_option(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "resolve_exact", return_value=self._resolved_cask()
            ), patch.object(software, "_manifest_or_exit", return_value=manifest), patch.object(
                software, "machine_file", return_value=machine
            ):
                result = cli_runner.invoke(cli, [
                    "sw", "add", "zotero", "--group", "homebrew.system.cask", "--dry-run",
                ])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("homebrew.system.cask", result.stdout)

    def test_add_infers_unique_existing_group(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest(include=["zotero"], effective=["zotero"])
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "_manifest_or_exit", return_value=manifest
            ), patch.object(software, "machine_file", return_value=machine):
                result = cli_runner.invoke(cli, ["sw", "add", "zotero", "--dry-run"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("selected compatible group", result.stdout)
        self.assertIn("homebrew.system.cask", result.stdout)

    def test_add_json_dry_run_is_one_stable_frontend_document(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "resolve_exact", return_value=self._resolved_cask()
            ), patch.object(software, "_manifest_or_exit", return_value=manifest), patch.object(
                software, "machine_file", return_value=machine
            ):
                result = cli_runner.invoke(cli, [
                    "sw", "add", "homebrew.system.cask", "zotero",
                    "--dry-run", "--json",
                ])

        self.assertEqual(result.exit_code, 0, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["command"], "software.add")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["result"], "dry-run")
        self.assertEqual(payload["data"]["plan"]["group"]["id"], "homebrew.system.cask")

    def test_add_json_requires_yes_before_mutating(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            original = "{ ... }:\n\n{\n}\n"
            machine.write_text(original)
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "resolve_exact", return_value=self._resolved_cask()
            ), patch.object(software, "_manifest_or_exit", return_value=manifest), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(software, "write_and_validate_software_policy") as write:
                result = cli_runner.invoke(cli, [
                    "sw", "add", "homebrew.system.cask", "zotero", "--json",
                ])
                unchanged = machine.read_text()

        self.assertEqual(result.exit_code, 2, result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "confirmation-required")
        write.assert_not_called()
        self.assertEqual(unchanged, original)

    def test_add_json_yes_returns_applied_result_without_git_guidance_noise(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "resolve_exact", return_value=self._resolved_cask()
            ), patch.object(software, "_manifest_or_exit", return_value=manifest), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(
                software, "write_and_validate_software_policy", return_value=manifest
            ) as write, patch.object(software, "offer_mutation_commit") as offer:
                result = cli_runner.invoke(cli, [
                    "sw", "add", "homebrew.system.cask", "zotero",
                    "--yes", "--json",
                ])

        self.assertEqual(result.exit_code, 0, result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["result"], "applied")
        write.assert_called_once()
        offer.assert_called_once()
        self.assertTrue(offer.call_args.kwargs["quiet"])

    def test_add_confirmation_rejection_leaves_machine_unchanged(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            original = "{ ... }:\n\n{\n}\n"
            machine.write_text(original)
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "resolve_exact", return_value=self._resolved_cask()
            ), patch.object(software, "_manifest_or_exit", return_value=manifest), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(software, "write_and_validate_software_policy") as write:
                result = cli_runner.invoke(
                    cli,
                    ["sw", "add", "homebrew.system.cask", "zotero"],
                    input="n\n",
                )

            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertIn("cancelled", result.stdout)
            write.assert_not_called()
            self.assertEqual(machine.read_text(), original)

    def test_add_rejects_an_unverified_new_registry_item_without_writing(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            original = "{ ... }:\n\n{\n}\n"
            machine.write_text(original)
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}), patch.object(
                software, "resolve_exact", return_value=ResolveResult.not_found("not found")
            ), patch.object(
                software, "_manifest_or_exit", return_value=manifest
            ), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(software, "write_and_validate_software_policy") as write:
                result = cli_runner.invoke(cli, [
                    "sw", "add", "homebrew.system.cask", "does-not-exist", "--yes",
                ])

            self.assertEqual(result.exit_code, 1, result.stdout)
            self.assertIn("does not exist", result.stdout)
            write.assert_not_called()
            self.assertEqual(machine.read_text(), original)

    def test_add_uses_exact_index_without_contacting_registry(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest()
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tempdir}):
                from envy.search.index import RegistryIndex
                RegistryIndex().put_results([self._resolved_cask().result])
                with patch.object(software, "resolve_exact") as resolve, patch.object(
                    software, "_manifest_or_exit", return_value=manifest
                ), patch.object(software, "machine_file", return_value=machine):
                    result = cli_runner.invoke(cli, [
                        "sw", "add", "homebrew.system.cask", "zotero", "--dry-run",
                    ])

            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertIn("registry index", result.stdout)
            resolve.assert_not_called()

    def test_add_blocked_by_external_exclusion_never_writes(self):
        cli_runner = CliRunner()
        manifest = self._cask_manifest(
            include=["zotero"], exclude=["zotero"], effective=[]
        )
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            original = "{ ... }:\n\n{\n}\n"
            machine.write_text(original)
            with patch.object(software, "_manifest_or_exit", return_value=manifest), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(software, "write_and_validate_software_policy") as write:
                result = cli_runner.invoke(cli, [
                    "sw", "add", "homebrew.system.cask", "zotero", "--yes",
                ])

            self.assertEqual(result.exit_code, 1, result.stdout)
            self.assertIn("blocked", result.stdout)
            write.assert_not_called()
            self.assertEqual(machine.read_text(), original)

    def test_rm_shared_contribution_previews_and_applies_machine_exclusion(self):
        cli_runner = CliRunner()
        before = self._cask_manifest(include=["zotero"], effective=["zotero"])
        after = self._cask_manifest(
            include=["zotero"], exclude=["zotero"], effective=[]
        )
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.object(software, "_manifest_or_exit", return_value=before), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(
                software, "write_and_validate_software_policy", return_value=after
            ) as write, patch.object(software, "offer_mutation_commit"):
                result = cli_runner.invoke(cli, [
                    "sw", "rm", "homebrew.system.cask", "zotero", "--yes",
                ])

            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertIn("exclude", result.stdout)
            self.assertIn("intent applied", result.stdout)
            exclusions = write.call_args.args[1]
            self.assertEqual(exclusions["homebrew.system.cask"], ["zotero"])

    def test_software_completion_filters_groups_and_items_by_action(self):
        manifest = self._completion_manifest()
        managed = {
            "nix.user.package": ["okular"],
            "npm.user.tool": [],
        }
        context = SimpleNamespace(params={"group": "nix.user.package"})
        with patch.object(software, "machine_manifest", return_value=manifest), patch.object(
            software, "read_managed_exclusions", return_value=managed
        ):
            disable_groups = software.complete_disable_groups(None, "nix")
            enable_groups = software.complete_enable_groups(None, "nix")
            disable_items = software.complete_disable_items(context, "")
            enable_items = software.complete_enable_items(context, "")
            add_groups = software.complete_add_groups(None, "")
            remove_groups = software.complete_remove_groups(None, "")
            add_items = software.complete_add_items(context, "")
            remove_items = software.complete_remove_items(context, "")

        self.assertEqual(disable_groups, [("nix.user.package", "Nix packages")])
        self.assertEqual(enable_groups, [("nix.user.package", "Nix packages")])
        self.assertEqual([item[0] for item in disable_items], ["git"])
        self.assertEqual([item[0] for item in enable_items], ["okular"])
        self.assertEqual(
            [item[0] for item in add_groups],
            ["nix.user.package", "npm.user.tool"],
        )
        self.assertEqual(
            [item[0] for item in remove_groups],
            ["nix.user.package", "npm.user.tool"],
        )
        self.assertEqual([item[0] for item in add_items], ["okular"])
        self.assertEqual([item[0] for item in remove_items], ["git", "okular"])
        self.assertIn("remove machine exclusion", add_items[0][1])
        self.assertIn("--clean", remove_items[1][1])

    def test_smart_completion_loads_policy_once(self):
        manifest = self._completion_manifest()
        managed = {"nix.user.package": ["okular"], "npm.user.tool": []}
        with patch.object(software, "machine_manifest", return_value=manifest), patch.object(
            software, "read_managed_exclusions", return_value=managed
        ), patch.object(software, "_completion_policy", wraps=software._completion_policy) as policy:
            candidates = software.complete_remove_targets(None, "")

        self.assertEqual([item[0] for item in candidates], ["nix.user.package", "npm.user.tool", "git", "okular"])
        policy.assert_called_once_with()

    def test_add_completion_merges_only_matching_exact_index_entries(self):
        manifest = self._completion_manifest()
        context = SimpleNamespace(params={"group": "npm.user.tool"})
        managed = {"nix.user.package": ["okular"], "npm.user.tool": []}
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ", {"XDG_CACHE_HOME": root}
        ):
            from envy.search.index import RegistryIndex

            RegistryIndex().put_results([
                SearchResult(
                    source="npm", ecosystem="npm", name="prettier", kind="tool",
                    version="3.6.2", ref="npm:prettier",
                ),
                SearchResult(
                    source="npm", ecosystem="npm", name="wrong-kind", kind="package",
                    version="1.0", ref="npm:wrong-kind",
                ),
            ])
            with patch.object(software, "machine_manifest", return_value=manifest), patch.object(
                software, "read_managed_exclusions", return_value=managed
            ):
                items = software.complete_add_items(context, "pre")
                wrong_kind = software.complete_add_items(context, "wrong")

        self.assertEqual([item[0] for item in items], ["prettier"])
        self.assertIn("registry index", items[0][1])
        self.assertIn("npm:prettier", items[0][1])
        self.assertEqual(wrong_kind, [])

    def test_why_completion_aggregates_groups_and_stale_managed_policy(self):
        manifest = self._completion_manifest()
        manifest["software"]["groups"]["npm.user.tool"]["selection"] = {
            "include": [{
                "id": "git", "name": "git", "version": "1.0",
                "ref": "npm:git", "parameters": {},
            }],
            "exclude": [],
            "effective": [],
        }
        includes = []
        exclusions = {
            "nix.user.package": ["old-tool"],
            "npm.user.tool": [],
        }
        with patch.object(software, "machine_manifest", return_value=manifest), patch.object(
            software, "read_managed_policy", return_value=(includes, exclusions)
        ), patch.object(
            software, "read_managed_exclusions", return_value=exclusions
        ):
            all_items = software.complete_why_items(SimpleNamespace(params={}), "")
            grouped = software.complete_why_items(
                SimpleNamespace(params={"group": "nix.user.package"}), "old"
            )
            groups = software.complete_why_groups(None, "npm")

        by_id = dict(all_items)
        self.assertIn("nix.user.package", by_id["git"])
        self.assertIn("npm.user.tool", by_id["git"])
        self.assertEqual(grouped, [("old-tool", "nix.user.package")])
        self.assertEqual(groups, [("npm.user.tool", "NPM tools")])

    def test_search_source_completion_supports_comma_separated_values(self):
        with patch(
            "envy.search.providers.available_providers",
            return_value={"nix": object(), "npm": object(), "pypi": object()},
        ):
            items = software.complete_search_sources(None, "nix,np")
            repeated = software.complete_search_sources(
                SimpleNamespace(params={"source": ["nix"]}), "ni"
            )

        self.assertEqual(items, [("nix,npm", "npm registry")])
        self.assertEqual(repeated, [])

    def test_enable_turns_an_included_and_managed_excluded_item_effective(self):
        before = self._policy_manifest(
            include=["okular"], exclude=["okular"], effective=[]
        )
        after = self._policy_manifest(
            include=["okular"], exclude=[], effective=["okular"]
        )
        managed = {"nix.user.package": ["okular"], "npm.user.tool": []}
        with patch.object(software, "_manifest_or_exit", return_value=before), patch.object(
            software, "read_managed_exclusions", return_value=managed
        ), patch.object(
            software, "write_and_validate_exclusions", return_value=after
        ), patch.object(software, "offer_mutation_commit"), patch.object(
            software.log, "ok"
        ) as ok:
            software._change_one("nix.user.package", "okular", excluded=False)

        ok.assert_called_with(
            "software", "enabled", group="nix.user.package",
            item="okular", machine="test-linux",
        )

    def test_repeated_enable_is_idempotent(self):
        before = self._policy_manifest(
            include=["git"], exclude=[], effective=["git"]
        )
        managed = {"nix.user.package": [], "npm.user.tool": []}
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.object(software, "_manifest_or_exit", return_value=before), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(
                software, "read_managed_exclusions", return_value=managed
            ), patch.object(
                software, "write_and_validate_exclusions"
            ) as write, patch.object(software.log, "ok") as ok:
                software._change_one("nix.user.package", "git", excluded=False)

        write.assert_not_called()
        ok.assert_called_with(
            "software", "already enabled", group="nix.user.package", item="git"
        )

    def test_disable_rejects_an_item_not_contributed_by_the_machine(self):
        before = self._policy_manifest(
            include=[], exclude=["old-tool"], effective=[]
        )
        managed = {"nix.user.package": ["old-tool"], "npm.user.tool": []}
        with tempfile.TemporaryDirectory() as tempdir:
            machine = Path(tempdir) / "machine.nix"
            machine.write_text("{ ... }:\n\n{\n}\n")
            with patch.object(software, "_manifest_or_exit", return_value=before), patch.object(
                software, "machine_file", return_value=machine
            ), patch.object(
                software, "read_managed_exclusions", return_value=managed
            ), patch.object(software, "write_and_validate_exclusions") as write:
                with self.assertRaises(software.typer.Exit):
                    software._change_one(
                        "nix.user.package", "old-tool", excluded=True
                    )

        write.assert_not_called()

    def test_enable_cleans_stale_exclusion_without_claiming_installation(self):
        before = self._policy_manifest(include=[], exclude=["old-tool"], effective=[])
        after = self._policy_manifest(include=[], exclude=[], effective=[])
        managed = {"nix.user.package": ["old-tool"], "npm.user.tool": []}
        with patch.object(software, "_manifest_or_exit", return_value=before), patch.object(
            software, "read_managed_exclusions", return_value=managed
        ), patch.object(
            software, "write_and_validate_exclusions", return_value=after
        ), patch.object(software, "offer_mutation_commit"), patch.object(
            software.log, "ok"
        ) as ok, patch.object(software.log, "hint") as hint:
            software._change_one("nix.user.package", "old-tool", excluded=False)

        ok.assert_called_with(
            "software", "stale exclusion removed", group="nix.user.package",
            item="old-tool", machine="test-linux",
        )
        self.assertIn("not contributed", hint.call_args.args[0])

    def test_enable_reports_external_exclusion_that_still_wins(self):
        before = self._policy_manifest(
            include=["okular"], exclude=["okular"], effective=[]
        )
        after = self._policy_manifest(
            include=["okular"], exclude=["okular"], effective=[]
        )
        managed = {"nix.user.package": ["okular"], "npm.user.tool": []}
        with patch.object(software, "_manifest_or_exit", return_value=before), patch.object(
            software, "read_managed_exclusions", return_value=managed
        ), patch.object(
            software, "write_and_validate_exclusions", return_value=after
        ), patch.object(software, "offer_mutation_commit"), patch.object(
            software.log, "warn"
        ) as warn, patch.object(software.log, "hint"):
            software._change_one("nix.user.package", "okular", excluded=False)

        warn.assert_called_with(
            "software", "managed exclusion removed; item remains excluded by another policy",
            group="nix.user.package", item="okular", machine="test-linux",
        )

    def test_enable_reports_external_stale_exclusion_that_still_exists(self):
        before = self._policy_manifest(include=[], exclude=["old-tool"], effective=[])
        after = self._policy_manifest(include=[], exclude=["old-tool"], effective=[])
        managed = {"nix.user.package": ["old-tool"], "npm.user.tool": []}
        with patch.object(software, "_manifest_or_exit", return_value=before), patch.object(
            software, "read_managed_exclusions", return_value=managed
        ), patch.object(
            software, "write_and_validate_exclusions", return_value=after
        ), patch.object(software, "offer_mutation_commit"), patch.object(
            software.log, "warn"
        ) as warn, patch.object(software.log, "hint"):
            software._change_one("nix.user.package", "old-tool", excluded=False)

        warn.assert_called_with(
            "software",
            "managed stale exclusion removed; item remains excluded by another policy",
            group="nix.user.package", item="old-tool", machine="test-linux",
        )

    def test_search_matches_results_to_the_evaluated_manifest(self):
        def fake_provider(query, limit, timeout):
            del query, limit, timeout
            return ProviderReport("npm", [SearchResult(
                source="npm", ecosystem="npm", name="@openai/codex",
                kind="tool", version="1.0.0", ref="npm:@openai/codex",
            )])

        manifest = {
            "schemaVersion": 2,
            "software": {"groups": {
                "npm.user.tool": {
                    "ecosystem": "npm",
                    "selection": {
                        "effective": [{
                            "id": "codex", "name": "@openai/codex",
                            "ref": "npm:@openai/codex",
                        }],
                    },
                },
            }},
        }
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=180)
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ", {"XDG_CACHE_HOME": root}
        ), patch.object(
            runner, "available_providers", return_value={"npm": fake_provider}
        ), patch.object(runner, "machine_manifest", return_value=manifest), patch.object(
            runner.log, "console", console
        ):
            runner.search_and_render(
                "codex", sources=["npm"], limit=10, exact=False,
                json_output=False, refresh=True,
            )

        rendered = output.getvalue()
        self.assertIn("@openai/codex", rendered)
        self.assertIn("npm.user.tool", rendered)

    def test_provider_failures_do_not_discard_successful_results(self):
        def good(query, limit, timeout):
            del query, limit, timeout
            return ProviderReport("good", [SearchResult(
                source="good", ecosystem="test", name="codex", kind="tool"
            )])

        def bad(query, limit, timeout):
            del query, limit, timeout
            raise RuntimeError("registry unavailable")

        reports = runner._run_providers({"good": good, "bad": bad}, "codex", 10, 1)
        by_source = {report.source: report for report in reports}
        self.assertEqual(by_source["good"].results[0].name, "codex")
        self.assertIn("registry unavailable", by_source["bad"].error)

    def test_partial_search_success_still_populates_exact_index(self):
        def good(query, limit, timeout):
            del query, limit, timeout
            return ProviderReport("npm", [SearchResult(
                source="npm", ecosystem="npm", name="codex", kind="tool",
                ref="npm:codex",
            )])

        def bad(query, limit, timeout):
            del query, limit, timeout
            return ProviderReport("pypi", [], "registry unavailable")

        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ", {"XDG_CACHE_HOME": root}
        ), patch.object(
            runner, "available_providers", return_value={"npm": good, "pypi": bad}
        ), patch.object(runner, "machine_manifest", return_value={}), patch.object(
            runner.log, "console", Console(file=io.StringIO(), force_terminal=False)
        ):
            runner.search_and_render(
                "codex", sources=None, limit=10, exact=False,
                json_output=True, refresh=True,
            )
            from envy.search.index import RegistryIndex
            indexed = RegistryIndex().lookup("npm", "tool", "codex")

        self.assertIsNotNone(indexed)
        self.assertEqual(indexed.result.ref, "npm:codex")

    def test_pypi_provider_uses_exact_registry_lookup(self):
        payload = {"info": {
            "name": "ruff", "version": "1.2.3", "summary": "Python linter",
            "project_url": "https://example.test/ruff",
        }}
        with patch.object(providers, "_get_json", return_value=payload):
            report = providers.search_pypi("ruff", 10, 1)

        self.assertIsNone(report.error)
        self.assertEqual(report.results[0].ref, "pypi:ruff")
        self.assertEqual(report.results[0].version, "1.2.3")

    def test_pypi_exact_resolver_accepts_pep503_equivalent_name(self):
        payload = {"info": {
            "name": "zope.interface", "version": "7.2", "summary": "Interfaces",
        }}
        with patch.object(providers, "_get_json", return_value=payload):
            resolved = providers.resolve_pypi("zope_interface", 1)

        self.assertEqual(resolved.status, "found")
        self.assertEqual(resolved.result.name, "zope.interface")

    def test_npm_exact_resolver_returns_canonical_identity(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "name": "@openai/codex", "version": "1.2.3",
                "description": "Agent", "homepage": "https://example.test",
            }),
            stderr="",
        )
        with patch.object(providers, "run_process", return_value=response):
            resolved = providers.resolve_exact("npm", "tool", "npm:@openai/codex")

        self.assertEqual(resolved.status, "found")
        self.assertEqual(resolved.result.ref, "npm:@openai/codex")

    def test_homebrew_cask_exact_resolver_uses_token_not_display_names(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"casks": [{
                "token": "firefox", "name": ["Mozilla Firefox"],
                "version": "153.0", "desc": "Web browser",
            }]}),
            stderr="",
        )
        with patch.object(providers, "run_process", return_value=response):
            resolved = providers.resolve_homebrew("cask", "firefox", 1)

        self.assertEqual(resolved.status, "found")
        self.assertEqual(resolved.result.name, "firefox")
        self.assertEqual(resolved.result.ref, "homebrew:cask/firefox")

    def test_exact_resolver_distinguishes_not_found_from_unavailable(self):
        missing = SimpleNamespace(
            returncode=1, stdout="", stderr="npm error code E404\nnpm error package not found\nlog",
        )
        unavailable = SimpleNamespace(
            returncode=1, stdout="", stderr="network timeout\nlog",
        )
        with patch.object(providers, "run_process", return_value=missing):
            self.assertEqual(providers.resolve_npm("missing", 1).status, "not_found")
        with patch.object(providers, "run_process", return_value=unavailable):
            self.assertEqual(providers.resolve_npm("missing", 1).status, "unavailable")

    def test_native_provider_name_is_matched_to_manifest_item(self):
        result = SearchResult(
            source="pacman", ecosystem="native", name="openssh",
            kind="package", ref="native:openssh",
        )
        manifest = {
            "schemaVersion": 2,
            "software": {"groups": {
                "native.system.package": {
                    "ecosystem": "native",
                    "selection": {"effective": [{
                        "id": "openssh-server",
                        "name": "openssh-server",
                        "ref": "native:openssh-server",
                        "parameters": {"names": {"pacman": "openssh"}},
                    }]},
                },
            }},
        }

        runner._mark_managed([result], manifest)

        self.assertEqual(result.managed_group, "native.system.package")

    def test_invalid_search_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ", {"XDG_CACHE_HOME": root}
        ):
            path = runner._cache_path("codex", ["npm"], 10)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "createdAt": time.time(),
                "reports": [{
                    "source": "npm",
                    "results": [{"unexpected": "shape"}],
                    "error": None,
                }],
            }))

            self.assertIsNone(runner._read_cache("codex", ["npm"], 10))


if __name__ == "__main__":
    unittest.main()

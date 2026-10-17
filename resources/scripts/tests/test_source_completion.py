"""Completion respects declared source identity and the current cached context."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.main import get_command

from envy import software
from envy.search.index import RegistryIndex
from envy.search.model import SearchResult
from envy.sources import completion
from envy.sources.cli import app as source_app
from envy.sources.model import Source


def context(**params):
    return SimpleNamespace(params=params)


def manifest(version=3):
    groups = {}
    for group in software.COMMON_GROUPS:
        groups[group.key] = {
            "label": group.label, "optionPath": group.option,
            "ecosystem": group.ecosystem, "scope": group.scope, "kind": group.kind,
            "editable": {"include": True, "exclude": True},
            "selection": {"include": [], "exclude": [], "effective": []},
        }
    return {"schemaVersion": version, "platform": "linux", "system": "x86_64-linux",
            "software": {"groups": groups, "sources": {
                "npm:vendor": {"label": "Vendor npm", "ecosystem": "npm", "type": "registry",
                               "parameters": {"registry": "https://registry.example.org"}},
            }}}


def registry(source_id):
    return Source(source_id, source_id, "npm", "registry",
                  parameters={"registry": "https://registry.example.org"},
                  capabilities={"managedInclude": True}, context={"revision": "current"})


def row(name, source, *, fingerprint=None):
    return SearchResult("npm", "npm", name, "tool", ref="npm:" + name,
                        source_id=source.id,
                        source_fingerprint=source.fingerprint if fingerprint is None else fingerprint)


class SourceCompletionTests(unittest.TestCase):
    def test_source_parameters_register_completion(self):
        source_commands = get_command(source_app).commands
        for name in ("show", "add", "rm", "sync"):
            parameter = next(p for p in source_commands[name].params if p.name == "source_id")
            self.assertIsNotNone(parameter._custom_shell_complete)
        parameter = next(p for p in source_commands["search"].params if p.name == "ecosystem")
        self.assertIsNotNone(parameter._custom_shell_complete)
        software_commands = get_command(software.app).commands
        for name in ("add", "search", "se"):
            parameter = next(p for p in software_commands[name].params if p.name == "source_id")
            self.assertIsNotNone(parameter._custom_shell_complete)

    def test_source_ids_use_public_declarations_without_registry_probes(self):
        with patch.object(completion, "machine_manifest", return_value=manifest()) as read, patch(
                "envy.sources.catalog.catalog", side_effect=AssertionError("must not probe")):
            values = dict(completion.complete_source_ids(None, "npm:"))
            self.assertEqual(set(values), {"npm:npmjs", "npm:vendor"})
            self.assertEqual(values["npm:vendor"], "Vendor npm")
            read.assert_called_once_with(completion=True)

    def test_source_options_respect_group_provider_and_previous_selections(self):
        with patch.object(completion, "machine_manifest", return_value=manifest()):
            for params in ({"group": "npm.user.tool"},
                           {"group_or_item": "npm.user.tool"},
                           {"group": "envy.software.npm.tools"},
                           {"group_or_item": "npm:prettier"}):
                self.assertEqual(set(dict(completion.complete_add_source_ids(context(**params), ""))),
                                 {"npm:npmjs", "npm:vendor"})
            values = dict(completion.complete_search_source_ids(
                context(source=["npm,pypi"], source_id=["npm:npmjs"]), ""))
            self.assertEqual(set(values), {"npm:vendor", "pypi:pypi"})
            self.assertEqual(completion.complete_ecosystems(None, "PY"), [("pypi", "Python tools")])

    def test_manifest_errors_produce_no_source_candidates(self):
        with patch.object(completion, "machine_manifest", side_effect=OSError("unavailable")):
            self.assertEqual(completion.complete_source_ids(None, ""), [])

    def test_smart_add_honors_group_and_includes_cached_canonical_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = RegistryIndex(Path(temporary) / "index.sqlite")
            index.put_results([
                SearchResult("npm", "npm", "prettier", "tool", ref="npm:prettier"),
                SearchResult("nix", "nix", "hello", "package", ref="nix:hello"),
            ])
            with patch.object(software, "machine_manifest", return_value=manifest(2)), patch.object(
                    software, "read_managed_exclusions", return_value={}), patch.object(
                    software, "RegistryIndex", return_value=index):
                self.assertEqual(list(dict(software.complete_add_targets(context(group="npm.user.tool"), ""))),
                                 ["prettier"])
                self.assertEqual(list(dict(software.complete_add_targets(context(), "pre"))), ["npm:prettier"])
                self.assertEqual(list(dict(software.complete_add_targets(context(), "hel"))), ["nix:hello"])
                self.assertEqual(software.complete_add_targets(context(group="unknown"), ""), [])

    def test_cached_completion_filters_source_and_current_context(self):
        wanted, other = registry("npm:vendor"), registry("npm:other")
        with tempfile.TemporaryDirectory() as temporary:
            index = RegistryIndex(Path(temporary) / "index.sqlite")
            index.put_results([row("current", wanted), row("old-context", wanted, fingerprint="previous"),
                               row("other-source", other)])
            with patch.object(software, "machine_manifest", return_value=manifest()), patch.object(
                    software, "read_managed_exclusions", return_value={}), patch.object(
                    software, "RegistryIndex", return_value=index), patch(
                    "envy.sources.catalog.catalog", return_value={wanted.id: wanted, other.id: other}):
                params = context(group="npm.user.tool", source_id=wanted.id)
                self.assertEqual(list(dict(software.complete_add_targets(params, ""))), ["current"])
                self.assertEqual(list(dict(software.complete_add_items(params, ""))), ["current"])
                self.assertEqual(list(dict(software.complete_add_targets(context(source_id=wanted.id), "cur"))),
                                 ["npm:current"])

    def test_source_context_is_filtered_before_index_limit(self):
        wanted, other = registry("npm:vendor"), registry("npm:other")
        with tempfile.TemporaryDirectory() as temporary:
            index = RegistryIndex(Path(temporary) / "index.sqlite")
            index.put_results([row(f"a-{number:02}", other) for number in range(60)] +
                              [row("a-old", wanted, fingerprint="previous"), row("z-current", wanted)])
            matches = index.suggest("npm", "tool", "", limit=1,
                                    source_id=wanted.id, fingerprint=wanted.fingerprint)
            self.assertEqual([item.result.name for item in matches], ["z-current"])

    def test_missing_index_never_probes_or_creates_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = RegistryIndex(Path(temporary) / "missing" / "index.sqlite")
            with patch.object(software, "machine_manifest", return_value=manifest()), patch.object(
                    software, "read_managed_exclusions", return_value={}), patch.object(
                    software, "RegistryIndex", return_value=index), patch(
                    "envy.sources.catalog.catalog", side_effect=AssertionError("must not probe")):
                self.assertEqual(software.complete_add_targets(context(), "absent"), [])
            self.assertFalse(index.path.parent.exists())


if __name__ == "__main__":
    unittest.main()

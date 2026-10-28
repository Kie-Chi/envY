import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from envy import config
from envy.software import audit, model, planner, policy, policy_text


def _entry(item_id, name=None, version=None, ref=None):
    return {
        "id": item_id,
        "name": name or item_id,
        "version": version,
        "ref": ref,
        "parameters": {},
    }


def _group(
    option, label, ecosystem, include=(), exclude=(), effective=(),
    *, editable_include=False, kind=None,
):
    return {
        "label": label,
        "optionPath": option,
        "ecosystem": ecosystem,
        "scope": "user" if ".user." in label.casefold() else "system",
        "kind": kind or ("tool" if ecosystem in {"npm", "pypi"} else "package"),
        "installer": ecosystem,
        "editable": {"include": editable_include, "exclude": True},
        "selection": {
            "include": [_entry(item) for item in include],
            "exclude": list(exclude),
            "effective": [_entry(item) for item in effective],
        },
    }


def _manifest(platform, groups):
    return {
        "schemaVersion": 2,
        "id": f"test-{platform}",
        "platform": platform,
        "software": {"groups": groups},
    }


class SoftwarePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.machine = self.root / "test-mac.nix"
        self.original = """{ ... }:

{
  imports = [ ../default.nix ];

  # BEGIN ENVY MANAGED CONFIG
  envy.user.name = "chi";
  # END ENVY MANAGED CONFIG

  # Hand-written policy remains here.
}
"""
        self.machine.write_text(self.original)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_round_trip_writes_only_non_empty_groups(self):
        groups = model.groups_for_platform("darwin")
        values = model.empty_exclusions(groups)
        values["nix.user.package"] = ["okular", "sing-box"]
        values["homebrew.system.cask"] = ["uuremote"]

        policy.write_managed_exclusions(values, self.machine, groups=groups)
        parsed = policy.read_managed_exclusions(self.machine, groups)
        text = self.machine.read_text()

        self.assertEqual(parsed, values)
        self.assertIn(policy_text.MANAGED_START, text)
        self.assertIn('"okular"', text)
        self.assertIn('"uuremote"', text)
        self.assertIn("envy.software.nix.packages.exclude", text)
        self.assertIn("envy.darwin.software.homebrew.casks.exclude", text)
        self.assertNotIn("systemPackages.exclude", text)
        self.assertIn("# Hand-written policy remains here.", text)

    def test_managed_includes_round_trip_as_direct_option_assignments(self):
        groups = model.groups_for_platform("darwin")
        includes = [model.ManagedInclude(
            group="homebrew.system.cask",
            id="zotero",
            name="zotero",
            ref="homebrew:cask/zotero",
            parameters={},
        )]
        updated = policy_text._update_managed_policy_source(
            self.original, includes, model.empty_exclusions(groups), groups
        )
        self.machine.write_text(updated)

        self.assertEqual(policy.read_managed_policy(self.machine, groups)[0], includes)
        self.assertIn(policy_text.MANAGED_START, updated)
        self.assertIn("envy.darwin.software.homebrew.casks.include", updated)
        self.assertNotIn("envy.software.managed", updated)
        self.assertIn("# Hand-written policy remains here.", updated)

        cleaned = policy_text._update_managed_policy_source(
            updated, [], model.empty_exclusions(groups), groups
        )
        self.assertNotIn(policy_text.MANAGED_START, cleaned)
        self.assertIn("# Hand-written policy remains here.", cleaned)

    def test_setup_exclusion_write_preserves_managed_include(self):
        groups = model.groups_for_platform("darwin")
        managed_include = self._managed_cask()
        source = policy_text._update_managed_policy_source(
            self.original, [managed_include], model.empty_exclusions(groups), groups
        )
        self.machine.write_text(source)
        exclusions = model.empty_exclusions(groups)
        exclusions["homebrew.system.cask"] = ["uuremote"]

        policy.write_managed_exclusions(exclusions, self.machine, groups=groups)

        self.assertEqual(policy.read_managed_policy(self.machine, groups)[0], [managed_include])
        self.assertEqual(
            policy.read_managed_exclusions(self.machine, groups)["homebrew.system.cask"],
            ["uuremote"],
        )

    def test_legacy_exclusion_block_is_read_and_migrated_on_write(self):
        groups = model.groups_for_platform("darwin")
        legacy = (
            f"{policy_text.LEGACY_MANAGED_START}\n"
            "  envy.darwin.software.homebrew.casks.exclude = [ \"uuremote\" ];\n"
            f"{policy_text.LEGACY_MANAGED_END}"
        )
        self.machine.write_text(self.original.replace(
            "  # Hand-written policy remains here.", legacy
        ))

        values = policy.read_managed_exclusions(self.machine, groups)
        policy.write_managed_exclusions(values, self.machine, groups=groups)
        updated = self.machine.read_text()

        self.assertEqual(values["homebrew.system.cask"], ["uuremote"])
        self.assertIn(policy_text.MANAGED_START, updated)
        self.assertNotIn(policy_text.LEGACY_MANAGED_START, updated)

    def test_current_and_legacy_blocks_together_are_rejected(self):
        self.machine.write_text(self.original.replace(
            "  # Hand-written policy remains here.",
            f"{policy_text.MANAGED_START}\n{policy_text.MANAGED_END}\n\n"
            f"{policy_text.LEGACY_MANAGED_START}\n{policy_text.LEGACY_MANAGED_END}",
        ))

        with self.assertRaises(model.SoftwarePolicyError):
            policy.read_managed_exclusions(self.machine)

    def test_nix_include_is_direct_and_exposes_pkgs_in_module_header(self):
        groups = model.groups_for_platform("darwin")
        managed_include = model.ManagedInclude(
            group="nix.user.package", id="hello", name="hello",
            ref="nix:hello", parameters={"attrPath": ["hello"]},
        )

        updated = policy_text._update_managed_policy_source(
            self.original, [managed_include], model.empty_exclusions(groups), groups
        )
        self.machine.write_text(updated)

        self.assertTrue(updated.startswith("{ pkgs, ... }:"))
        self.assertIn('envy.software.nix.packages.include = [', updated)
        self.assertIn('pkgs."hello"', updated)
        self.assertIn("envy.software.nix.packages.references = builtins.fromJSON", updated)
        self.assertIn('\\"hello\\":\\"nix:hello\\"', updated)
        self.assertNotIn('"parameters"', updated)
        self.assertNotIn('"version"', updated)
        self.assertEqual(policy.read_managed_policy(self.machine, groups)[0], [managed_include])

    def test_nix_reference_mapping_must_match_the_managed_include(self):
        groups = model.groups_for_platform("darwin")
        managed_include = model.ManagedInclude(
            group="nix.user.package", id="hello", name="hello",
            ref="nix:hello", parameters={"attrPath": ["hello"]},
        )
        updated = policy_text._update_managed_policy_source(
            self.original, [managed_include], model.empty_exclusions(groups), groups
        ).replace('\\"hello\\":\\"nix:hello\\"', '\\"other\\":\\"nix:other\\"')
        self.machine.write_text(updated)

        with self.assertRaisesRegex(model.SoftwarePolicyError, "references do not match"):
            policy.read_managed_policy(self.machine, groups)

    def test_structured_include_is_assigned_directly_to_the_ecosystem(self):
        groups = model.groups_for_platform("linux")
        managed_include = model.ManagedInclude(
            group="npm.user.tool", id="codex", name="@openai/codex",
            ref="npm:@openai/codex", parameters={},
        )

        updated = policy_text._update_managed_policy_source(
            self.original, [managed_include], model.empty_exclusions(groups), groups
        )
        self.machine.write_text(updated)

        self.assertIn("envy.software.npm.tools.include = builtins.fromJSON", updated)
        self.assertNotIn("envy.software.managed", updated)
        self.assertNotIn('\\"parameters\\"', updated)
        self.assertNotIn('\\"version\\"', updated)
        self.assertEqual(policy.read_managed_policy(self.machine, groups)[0], [managed_include])

    def test_managed_includes_reject_conflicts_and_nix_interpolation(self):
        groups = model.groups_for_platform("darwin")
        base = model.ManagedInclude(
            group="homebrew.system.cask", id="zotero", name="zotero",
            ref="homebrew:cask/zotero", parameters={},
        )
        conflict = model.ManagedInclude(
            group="homebrew.system.cask", id="zotero", name="zotero-beta",
            ref="homebrew:cask/zotero-beta", parameters={},
        )
        with self.assertRaises(model.SoftwarePolicyError):
            model.normalize_managed_includes([base, conflict], groups)
        with self.assertRaises(model.SoftwarePolicyError):
            model.normalize_managed_includes([
                {"group": "homebrew.system.cask", "id": 1, "name": "bad"}
            ], groups)
        with self.assertRaises(model.SoftwarePolicyError):
            model.normalize_managed_includes([
                model.ManagedInclude(
                    group="homebrew.system.cask", id="${builtins.abort \"bad\"}",
                    name="bad", parameters={},
                )
            ], groups)

    def test_removing_last_exclusion_removes_the_whole_block(self):
        groups = model.groups_for_platform("darwin")
        values = model.empty_exclusions(groups)
        values["nix.user.package"] = ["okular"]
        policy.write_managed_exclusions(values, self.machine, groups=groups)
        policy.write_managed_exclusions(
            model.empty_exclusions(groups), self.machine, groups=groups
        )

        self.assertNotIn(policy_text.MANAGED_START, self.machine.read_text())
        self.assertIn("# Hand-written policy remains here.", self.machine.read_text())

    def test_concurrent_source_change_is_rejected(self):
        digest = policy.source_digest(self.machine.read_text())
        self.machine.write_text(self.machine.read_text().replace("chi", "other"))

        with self.assertRaises(model.ConcurrentMachineEdit):
            policy.write_managed_exclusions(
                {"nix.user.package": ["okular"]},
                self.machine,
                expected_digest=digest,
            )

    def test_failed_nix_evaluation_restores_original_source(self):
        evaluator = MagicMock(return_value=None)
        evaluator.cache_clear = MagicMock()

        with patch.object(policy, "machine_manifest", evaluator), self.assertRaises(
            model.SoftwarePolicyError
        ):
            policy.write_and_validate_exclusions(
                {"nix.user.package": ["okular"]}, self.machine
            )

        self.assertEqual(self.machine.read_text(), self.original)

    def test_successful_evaluation_keeps_the_new_policy(self):
        evaluator = MagicMock(return_value=_manifest("darwin", {
            "nix.user.package": _group(
                "envy.software.nix.packages", "Nix packages", "nix",
                ["okular"], ["okular"], [],
            ),
        }))
        evaluator.cache_clear = MagicMock()

        with patch.object(policy, "machine_manifest", evaluator):
            policy.write_and_validate_exclusions(
                {"nix.user.package": ["okular"]}, self.machine
            )

        self.assertEqual(
            policy.read_managed_exclusions(self.machine)["nix.user.package"],
            ["okular"],
        )

    def test_checkbox_state_uses_stable_ids_and_names(self):
        manifest = _manifest("linux", {
            "pypi.user.tool": {
                **_group(
                    "envy.software.pypi.tools", "Python tools", "pypi",
                    ["headroom"], ["headroom"], [],
                ),
                "selection": {
                    "include": [_entry("headroom", "headroom-ai", ref="pypi:headroom-ai")],
                    "exclude": ["headroom"],
                    "effective": [],
                },
            },
        })
        groups = model.groups_for_manifest(manifest)
        original = model.empty_exclusions(groups)
        original["pypi.user.tool"] = ["headroom"]
        current = model.normalize_exclusions(original, groups)
        model.set_excluded(current, "pypi.user.tool", "headroom", False, groups)

        item = model.build_software_items(
            manifest, current, original, groups=groups
        )["pypi.user.tool"][0]

        self.assertEqual(item.id, "headroom")
        self.assertEqual(item.name, "headroom-ai")
        self.assertTrue(item.checked)
        self.assertTrue(item.changed)

    def test_darwin_homebrew_pending_toggle_is_mapped(self):
        manifest = _manifest("darwin", {
            "homebrew.system.formula": _group(
                "envy.darwin.software.homebrew.formulae", "Homebrew formulae",
                "homebrew", ["gh"], [], ["gh"],
            ),
            "homebrew.system.cask": _group(
                "envy.darwin.software.homebrew.casks", "Homebrew casks",
                "homebrew", ["iterm2", "uuremote"], ["uuremote"], ["iterm2"],
            ),
        })
        groups = model.groups_for_manifest(manifest)
        original = model.empty_exclusions(groups)
        original["homebrew.system.cask"] = ["uuremote"]
        current = model.normalize_exclusions(original, groups)

        before = model.build_software_items(
            manifest, current, original, groups=groups
        )
        formulae = {item.name: item for item in before["homebrew.system.formula"]}
        casks = {item.name: item for item in before["homebrew.system.cask"]}

        self.assertTrue(formulae["gh"].checked)
        self.assertTrue(casks["uuremote"].included)
        self.assertFalse(casks["uuremote"].checked)
        self.assertFalse(casks["uuremote"].stale)

        model.set_excluded(
            current, "homebrew.system.cask", "uuremote", False, groups
        )
        after = {
            item.name: item
            for item in model.build_software_items(
                manifest, current, original, groups=groups
            )["homebrew.system.cask"]
        }
        self.assertTrue(after["uuremote"].checked)
        self.assertTrue(after["uuremote"].changed)

    def test_overlapping_managed_and_external_exclusions_keep_external_lock(self):
        manifest = _manifest("darwin", {
            "homebrew.system.cask": _group(
                "envy.darwin.software.homebrew.casks", "Homebrew casks",
                "homebrew", ["zotero"], ["zotero", "zotero"], [],
                editable_include=True, kind="cask",
            ),
        })
        groups = model.groups_for_manifest(manifest)
        managed = model.empty_exclusions(groups)
        managed["homebrew.system.cask"] = ["zotero"]

        item = model.build_software_items(
            manifest, managed, groups=groups
        )["homebrew.system.cask"][0]

        self.assertTrue(item.managed)
        self.assertTrue(item.locked)
        self.assertFalse(item.checked)

    def _cask_policy(self, include=(), exclude=(), effective=()):
        manifest = _manifest("darwin", {
            "homebrew.system.cask": _group(
                "envy.darwin.software.homebrew.casks", "Homebrew casks",
                "homebrew", include, exclude, effective,
                editable_include=True, kind="cask",
            ),
        })
        groups = model.groups_for_manifest(manifest, include_empty=True)
        exclusions = model.empty_exclusions(groups)
        return manifest, groups, exclusions

    @staticmethod
    def _managed_cask(item_id="zotero"):
        return model.ManagedInclude(
            group="homebrew.system.cask", id=item_id, name=item_id,
            ref=f"homebrew:cask/{item_id}", parameters={},
        )

    def test_add_absent_item_creates_managed_include(self):
        manifest, groups, exclusions = self._cask_policy()
        plan = planner.build_desired_plan(
            action="add", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[], exclusions=exclusions, clean=False,
        )
        self.assertEqual(plan.include_added, ("zotero",))
        self.assertEqual(plan.exclude_removed, ())
        self.assertTrue(plan.expected_effective)
        self.assertIsNone(plan.blocked)

    def test_add_nix_reference_resolves_stable_id_and_relative_attr_path(self):
        manifest = _manifest("darwin", {
            "nix.user.package": _group(
                "envy.software.nix.packages", "Nix packages", "nix",
                editable_include=True,
            ),
        })
        manifest["system"] = "aarch64-darwin"
        groups = model.groups_for_manifest(manifest, include_empty=True)
        exclusions = model.empty_exclusions(groups)
        result = SimpleNamespace(returncode=0, stdout='{"name":"hello","version":"1","drvPath":"/nix/store/example.drv"}', stderr="")
        with patch("envy.sources.adapters.run_process", return_value=result):
            plan = planner.build_desired_plan(
                action="add", group=groups[0],
                item_value="nix:legacyPackages.aarch64-darwin.hello",
                manifest=manifest, includes=[], exclusions=exclusions,
                clean=False,
            )

        request = plan.includes_after[0]
        self.assertEqual(request.id, "hello")
        self.assertEqual(request.parameters["attrPath"], ["hello"])

    def test_add_custom_stable_id_keeps_canonical_npm_name(self):
        manifest = _manifest("linux", {
            "npm.user.tool": _group(
                "envy.software.npm.tools", "NPM tools", "npm",
                editable_include=True,
            ),
        })
        groups = model.groups_for_manifest(manifest, include_empty=True)
        exclusions = model.empty_exclusions(groups)
        plan = planner.build_desired_plan(
            action="add", group=groups[0], item_value="codex",
            explicit_ref="npm:@openai/codex",
            manifest=manifest, includes=[], exclusions=exclusions,
            clean=False,
        )

        request = plan.includes_after[0]
        self.assertEqual(request.id, "codex")
        self.assertEqual(request.name, "@openai/codex")
        self.assertEqual(request.ref, "npm:@openai/codex")

    def test_add_removes_managed_exclusion_without_duplicate_include(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero"], ["zotero"], []
        )
        exclusions[groups[0].key] = ["zotero"]
        plan = planner.build_desired_plan(
            action="add", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[], exclusions=exclusions, clean=False,
        )
        self.assertEqual(plan.include_added, ())
        self.assertEqual(plan.exclude_removed, ("zotero",))
        self.assertTrue(plan.expected_effective)

    def test_add_is_blocked_by_external_exclusion_without_partial_cleanup(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero"], ["zotero"], []
        )
        plan = planner.build_desired_plan(
            action="add", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[], exclusions=exclusions, clean=True,
        )
        self.assertIsNotNone(plan.blocked)
        self.assertFalse(plan.changed)
        self.assertFalse(plan.expected_effective)

    def test_add_clean_removes_redundant_managed_include(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero", "zotero"], [], ["zotero"]
        )
        managed = self._managed_cask()
        plan = planner.build_desired_plan(
            action="add", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[managed], exclusions=exclusions, clean=True,
        )
        self.assertEqual(plan.include_removed, ("zotero",))
        self.assertTrue(plan.expected_included)
        self.assertTrue(plan.expected_effective)

    def test_add_clean_cancels_managed_pair_when_shared_include_remains(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero", "zotero"], ["zotero"], []
        )
        exclusions[groups[0].key] = ["zotero"]
        plan = planner.build_desired_plan(
            action="add", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[self._managed_cask()],
            exclusions=exclusions, clean=True,
        )
        self.assertEqual(plan.include_removed, ("zotero",))
        self.assertEqual(plan.exclude_removed, ("zotero",))
        self.assertTrue(plan.expected_effective)

    def test_add_stays_blocked_when_managed_and_external_exclusions_overlap(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero"], ["zotero", "zotero"], []
        )
        exclusions[groups[0].key] = ["zotero"]
        plan = planner.build_desired_plan(
            action="add", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[], exclusions=exclusions, clean=True,
        )
        self.assertIsNotNone(plan.blocked)
        self.assertFalse(plan.changed)

    def test_rm_default_adds_exclusion_and_keeps_owned_include(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero"], [], ["zotero"]
        )
        plan = planner.build_desired_plan(
            action="rm", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[self._managed_cask()],
            exclusions=exclusions, clean=False,
        )
        self.assertEqual(plan.include_removed, ())
        self.assertEqual(plan.exclude_added, ("zotero",))
        self.assertTrue(plan.expected_included)
        self.assertTrue(plan.expected_excluded)
        self.assertFalse(plan.expected_effective)

    def test_rm_resolves_owned_include_by_canonical_reference(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero"], [], ["zotero"]
        )
        plan = planner.build_desired_plan(
            action="rm", group=groups[0],
            item_value="homebrew:cask/zotero",
            manifest=manifest, includes=[self._managed_cask()],
            exclusions=exclusions, clean=False,
        )
        self.assertEqual(plan.item_id, "zotero")
        self.assertEqual(plan.include_removed, ())
        self.assertEqual(plan.exclude_added, ("zotero",))

    def test_rm_clean_removes_owned_include_and_redundant_exclusion(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero"], [], ["zotero"]
        )
        plan = planner.build_desired_plan(
            action="rm", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[self._managed_cask()],
            exclusions=exclusions, clean=True,
        )
        self.assertEqual(plan.include_removed, ("zotero",))
        self.assertEqual(plan.exclude_added, ())
        self.assertFalse(plan.expected_included)
        self.assertFalse(plan.expected_excluded)
        self.assertFalse(plan.expected_effective)

    def test_rm_masks_shared_include_and_removes_redundant_owned_copy(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero", "zotero"], [], ["zotero"]
        )
        plan = planner.build_desired_plan(
            action="rm", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[self._managed_cask()],
            exclusions=exclusions, clean=True,
        )
        self.assertEqual(plan.include_removed, ("zotero",))
        self.assertEqual(plan.exclude_added, ("zotero",))
        self.assertTrue(plan.expected_included)
        self.assertTrue(plan.expected_excluded)
        self.assertFalse(plan.expected_effective)

    def test_rm_clean_prunes_only_target_stale_exclusion(self):
        manifest, groups, exclusions = self._cask_policy(
            [], ["zotero", "other"], []
        )
        exclusions[groups[0].key] = ["zotero", "other"]
        plan = planner.build_desired_plan(
            action="rm", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[], exclusions=exclusions, clean=True,
        )
        self.assertEqual(plan.exclude_removed, ("zotero",))
        self.assertEqual(plan.exclusions_after, ("other",))
        self.assertFalse(plan.expected_effective)

    def test_rm_without_clean_preserves_stale_machine_intent(self):
        manifest, groups, exclusions = self._cask_policy([], ["zotero"], [])
        exclusions[groups[0].key] = ["zotero"]
        plan = planner.build_desired_plan(
            action="rm", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[], exclusions=exclusions, clean=False,
        )
        self.assertFalse(plan.changed)
        self.assertEqual(plan.exclusions_after, ("zotero",))

    def test_rm_without_clean_adds_stale_machine_intent(self):
        manifest, groups, exclusions = self._cask_policy()
        plan = planner.build_desired_plan(
            action="rm", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[], exclusions=exclusions, clean=False,
        )
        self.assertEqual(plan.exclude_added, ("zotero",))
        self.assertFalse(plan.expected_included)
        self.assertTrue(plan.expected_excluded)
        self.assertFalse(plan.expected_effective)

    def test_rm_clean_needs_no_managed_mask_when_external_mask_already_wins(self):
        manifest, groups, exclusions = self._cask_policy(
            ["zotero", "zotero"], ["zotero", "zotero"], []
        )
        exclusions[groups[0].key] = ["zotero"]
        plan = planner.build_desired_plan(
            action="rm", group=groups[0], item_value="zotero",
            manifest=manifest, includes=[self._managed_cask()],
            exclusions=exclusions, clean=True,
        )
        self.assertEqual(plan.include_removed, ("zotero",))
        self.assertEqual(plan.exclude_removed, ("zotero",))
        self.assertTrue(plan.expected_included)
        self.assertTrue(plan.expected_excluded)
        self.assertFalse(plan.expected_effective)

    def test_failed_intent_verification_restores_managed_block(self):
        manifest, groups, exclusions = self._cask_policy()
        managed_include = self._managed_cask()
        evaluator = MagicMock(return_value=manifest)
        evaluator.cache_clear = MagicMock()

        with patch.object(policy, "machine_manifest", evaluator), self.assertRaises(
            model.SoftwarePolicyError
        ):
            policy.write_and_validate_software_policy(
                [managed_include], exclusions,
                group_key=groups[0].key,
                item_id="zotero",
                expected_effective=True,
                path=self.machine,
                groups=groups,
            )

        self.assertEqual(self.machine.read_text(), self.original)

    def test_successful_intent_write_keeps_direct_include_and_exclude(self):
        after, groups, exclusions = self._cask_policy(
            ["zotero"], ["other"], ["zotero"]
        )
        exclusions[groups[0].key] = ["other"]
        evaluator = MagicMock(return_value=after)
        evaluator.cache_clear = MagicMock()

        with patch.object(policy, "machine_manifest", evaluator):
            result = policy.write_and_validate_software_policy(
                [self._managed_cask()], exclusions,
                group_key=groups[0].key,
                item_id="zotero",
                expected_effective=True,
                path=self.machine,
                groups=groups,
            )

        self.assertIs(result, after)
        self.assertEqual(
            policy.read_managed_policy(self.machine, groups)[0],
            [self._managed_cask()],
        )
        self.assertEqual(
            policy.read_managed_exclusions(self.machine, groups)[groups[0].key],
            ["other"],
        )

    def test_desired_state_write_rejects_concurrent_machine_change(self):
        manifest, groups, exclusions = self._cask_policy()
        stale_digest = policy.source_digest(self.machine.read_text())
        self.machine.write_text(self.original.replace("chi", "other"))

        with patch.object(policy, "machine_manifest") as evaluator, self.assertRaises(
            model.ConcurrentMachineEdit
        ):
            policy.write_and_validate_software_policy(
                [self._managed_cask()], exclusions,
                group_key=groups[0].key,
                item_id="zotero",
                expected_effective=True,
                path=self.machine,
                expected_digest=stale_digest,
                groups=groups,
            )

        evaluator.assert_not_called()

    def test_linux_manifest_groups_are_discovered_without_darwin_placeholders(self):
        manifest = _manifest("linux", {
            "nix.user.package": _group(
                "envy.software.nix.packages", "Nix packages", "nix",
                ["git", "okular"], ["okular"], ["git"],
            ),
            "native.system.package": _group(
                "envy.linux.software.native.packages", "Native packages", "native",
                ["openssh-server"], [], ["openssh-server"],
            ),
        })
        groups = model.groups_for_manifest(manifest)
        original = model.empty_exclusions(groups)
        original["nix.user.package"] = ["okular"]

        items = model.build_software_items(manifest, original, original)

        self.assertEqual(
            list(items), ["nix.user.package", "native.system.package"]
        )
        self.assertFalse(any(group.ecosystem == "homebrew" for group in groups))
        by_name = {item.name: item for item in items["nix.user.package"]}
        self.assertTrue(by_name["git"].checked)
        self.assertFalse(by_name["okular"].checked)
        self.assertFalse(by_name["okular"].stale)

    def test_invalid_managed_content_is_rejected(self):
        self.machine.write_text(
            self.original.replace(
                "  # Hand-written policy remains here.",
                f'{policy_text.MANAGED_START}\n  builtins.abort "bad";\n{policy_text.MANAGED_END}',
            )
        )
        with self.assertRaises(model.SoftwarePolicyError):
            policy.read_managed_exclusions(self.machine)

        with patch.object(config, "machine_config_file", return_value=self.machine), patch.object(
            config, "machine_manifest", return_value=None
        ):
            report = config.refine_software_policy()
        self.assertFalse(report.ok)

    def test_invalid_managed_include_content_is_rejected(self):
        self.machine.write_text(
            self.original.replace(
                "  # Hand-written policy remains here.",
                f'{policy_text.MANAGED_START}\n'
                '  envy.software.nix.packages.include = builtins.abort "bad";\n'
                f'{policy_text.MANAGED_END}',
            )
        )
        with self.assertRaises(model.SoftwarePolicyError):
            policy.read_managed_policy(self.machine)

        with patch.object(config, "machine_config_file", return_value=self.machine), patch.object(
            config, "machine_manifest", return_value=None
        ):
            report = config.refine_software_policy()
        self.assertFalse(report.ok)

    def test_config_refine_migrates_legacy_software_option_paths(self):
        self.machine.write_text(self.original.replace(
            "  # Hand-written policy remains here.",
            '  envy.packages.home.exclude = [ "okular" ];',
        ))
        with patch.object(config, "machine_config_file", return_value=self.machine), patch.object(
            config, "machine_manifest", return_value=None
        ):
            report = config.refine_software_policy(write=True)

        self.assertTrue(report.changed)
        self.assertIn("envy.software.nix.packages.exclude", self.machine.read_text())
        self.assertNotIn("envy.packages.home", self.machine.read_text())

    def test_audit_finds_machine_include_exclude_pair(self):
        manifest = _manifest("darwin", {
            "homebrew.system.cask": _group(
                "envy.darwin.software.homebrew.casks", "Homebrew casks",
                "homebrew", include=["maven"], exclude=["maven"], effective=[],
                editable_include=True, kind="cask",
            ),
        })
        groups = model.groups_for_manifest(manifest, include_empty=True)
        includes = [model.ManagedInclude(
            group="homebrew.system.cask", id="maven", name="maven",
            ref="homebrew:cask/maven",
        )]
        exclusions = {"homebrew.system.cask": ["maven"]}

        findings = audit.audit_policy(manifest, includes, exclusions, groups)
        rows = audit.explain_policy_item(
            manifest, includes, exclusions, groups, "maven",
        )

        self.assertIn("managed-include-exclude", [item.code for item in findings])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["machineInclude"])
        self.assertTrue(rows[0]["machineExclude"])
        self.assertFalse(rows[0]["effective"])


if __name__ == "__main__":
    unittest.main()

"""Source identity, trust, parsing, prerequisites, and transactional failure boundaries."""
import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from envy import software
from envy.search.index import RegistryIndex
from envy.search.model import SearchResult
from envy.search.providers import parse_native_search, resolve_native, _command_resolve_failure
from envy.sources.adapters import candidate_from_uri, resolve_nix
from envy.sources.model import Source, SourceError
from envy.sources.native.apt import effective_files, effective_options, repository_text
from envy.sources.native.backend import verified_key
from envy.sources.policy import parse_definitions, render_definitions, prerequisite
from envy.sources.shared import apply as apply_shared, preview as preview_shared
from envy.sources.artifacts import download, inspect, expected_hash


def native_source():
    return Source.from_dict("native:vendor", {
        "label": "Vendor", "ecosystem": "native", "type": "native", "platforms": ["linux"],
        "parameters": {"apt": {"uri": "https://vendor.example/packages", "suites": ["{codename}"],
                               "components": ["main"], "keyUrl": "https://vendor.example/key",
                               "keyFingerprint": "A" * 40}}
    })


class NativeParsingTests(unittest.TestCase):
    def test_exact_rejects_globs_and_options_without_running_manager(self):
        with patch("envy.search.providers.run_process") as run:
            for value in ("jq*", "--help", "jq|bash", "jq?", "jq[ab]", "jq foo"):
                self.assertEqual(resolve_native(value, 1).status, "not_found")
            run.assert_not_called()

    def test_exact_rejects_wrong_returned_name(self):
        with patch("envy.search.providers._native_manager", return_value="apt"), patch(
            "envy.search.providers.run_process", return_value=SimpleNamespace(
                returncode=0, stdout="Package: jqp\nVersion: 1\nArchitecture: amd64\n", stderr="")):
            self.assertEqual(resolve_native("jq", 1).status, "not_found")

    def test_exact_multiarch_checks_arch(self):
        with patch("envy.search.providers._native_manager", return_value="apt"), patch(
            "envy.search.providers.run_process", return_value=SimpleNamespace(
                returncode=0, stdout="Package: jq\nVersion: 1\nArchitecture: amd64\n", stderr="")):
            self.assertEqual(resolve_native("jq:arm64", 1).status, "not_found")
            self.assertEqual(resolve_native("jq:amd64", 1).status, "found")

    def test_no_packages_is_not_found_but_network_failure_is_unavailable(self):
        self.assertEqual(_command_resolve_failure("E: No packages found").status, "not_found")
        self.assertEqual(_command_resolve_failure("TLS handshake failed").status, "unavailable")

    def test_dnf4_dnf5_and_zypper_real_formats(self):
        for output in ("jq.x86_64 : JSON processor\n", "jq.x86_64    JSON processor\n"):
            self.assertEqual(parse_native_search("dnf", output)[0].name, "jq")
        rows = parse_native_search("zypper", "S | Name | Summary | Type\n--+------+---------+--------\n  | jq | JSON processor | package\n")
        self.assertEqual(rows[0].name, "jq")

    def test_pacman_multiple_description_lines(self):
        rows = parse_native_search("pacman", "extra/jq 1.7\n    JSON\n    processor\ncore/bash 5\n    shell\n")
        self.assertEqual(rows[0].description, "JSON processor")
        self.assertEqual([r.name for r in rows], ["jq", "bash"])

    def test_candidate_constraint_does_not_accept_lower_version_origin(self):
        output = "Candidate: 2\nVersion table:\n   2 500\n     500 https://other.example stable amd64 Packages\n   1 500\n     500 https://vendor.example/packages stable amd64 Packages\n"
        self.assertFalse(candidate_from_uri(output, "2", "https://vendor.example/packages"))
        self.assertTrue(candidate_from_uri(output, "1", "https://vendor.example/packages"))

    def test_repository_key_mismatch_stops_before_import_or_activation(self):
        import io
        source = native_source()
        stream = io.BytesIO(b"public key")
        stream.geturl = lambda: source.parameters["apt"]["keyUrl"]
        output = b"pub:-:4096:1:KEY:1:::::::sc:\nfpr:::::::::" + b"B" * 40 + b":\n"
        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen", return_value=stream), patch(
                "envy.sources.native.backend.command", return_value=SimpleNamespace(stdout=output)) as run:
            with self.assertRaises(SourceError):
                verified_key(source.parameters["apt"], Path(temporary) / "key.gpg")
            self.assertEqual(run.call_count, 1)



class AptSourceSetTests(unittest.TestCase):
    def test_disabled_and_commented_sources_do_not_claim_registration(self):
        from envy.sources.native.apt import configured_uris
        self.assertEqual(configured_uris("# deb https://vendor.example stable main\ndeb-src https://vendor.example stable main\n", deb822=False), set())
        self.assertEqual(configured_uris("Types: deb\nURIs: https://vendor.example\nEnabled: no\n", deb822=True), set())
        self.assertEqual(configured_uris("Types: deb\nURIs: https://vendor.example/\n https://second.example/\n", deb822=True), {"https://vendor.example", "https://second.example"})

    def test_mirror_retains_vendor_and_unknown_without_mutating_external_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/apt/sources.list.d").mkdir(parents=True)
            (root / "etc/os-release").write_text('ID=ubuntu\nVERSION_CODENAME=noble\n')
            distro = root / "etc/apt/sources.list.d/ubuntu.sources"
            distro.write_text("Types: deb\nURIs: http://archive.ubuntu.com/ubuntu https://unknown.example/ubuntu\nSuites: noble\nComponents: main\n")
            vendor = root / "etc/apt/sources.list.d/vendor.list"
            vendor.write_text("deb [signed-by=/etc/key.gpg] https://vendor.example stable main\n")
            mirror = root / "etc/apt/sources.list.d/envy-mirror.sources"
            mirror.write_text("Types: deb\nURIs: https://mirror.example/ubuntu\nSuites: noble\nComponents: main\n")
            before = {path: path.read_bytes() for path in (distro, vendor, mirror)}
            text = "\n".join(effective_files(root).values())
            self.assertNotIn("archive.ubuntu.com", text)
            self.assertIn("unknown.example", text)
            self.assertIn("vendor.example", text)
            self.assertIn("mirror.example", text)
            with effective_options(root) as options:
                self.assertIn("Dir::Etc::sourcelist=/dev/null", options)
                parts = Path(options[-1].split("=", 1)[1])
                self.assertTrue(parts.exists())
            self.assertFalse(parts.exists())
            for path, data in before.items():
                self.assertEqual(path.read_bytes(), data)

    def test_no_mirror_keeps_distro_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/apt").mkdir(parents=True)
            (root / "etc/apt/sources.list").write_text("deb http://deb.debian.org/debian stable main\n")
            self.assertIn("deb.debian.org", "\n".join(effective_files(root).values()))

    def test_structured_repository_runtime_expansion(self):
        source = native_source()
        text = repository_text(source.parameters["apt"], keyring="/etc/key.gpg",
                               release={"VERSION_CODENAME": "noble"}, architecture="amd64")
        self.assertIn("Suites: noble", text)
        self.assertIn("Signed-By: /etc/key.gpg", text)
        self.assertNotIn("{codename}", text)


class SourceContractTests(unittest.TestCase):
    def empty_manifest(self, group):
        return {"id": "fixture", "schemaVersion": 3, "platform": "linux", "system": "x86_64-linux",
                "software": {"groups": {group.key: {
                    "label": group.label, "optionPath": group.option, "ecosystem": group.ecosystem,
                    "kind": group.kind, "scope": group.scope, "editable": {"include": True, "exclude": True},
                    "selection": {"include": [], "exclude": [], "effective": []}}}}}

    def test_first_nix_add_uses_scoped_resolution_and_offline_cache(self):
        from envy.search.model import ResolveResult
        group = software.COMMON_GROUPS[0]
        manifest = self.empty_manifest(group)
        source = Source("nix:nixpkgs", "Project", "nix", "flake", parameters={"input": "nixpkgs"},
                        registered=True, ready=True, capabilities={"exactResolve": True})
        row = SearchResult("nix", "nix", "sl", "package", version="5.05", ref="nix:sl", source_id=source.id)
        with tempfile.TemporaryDirectory() as temporary:
            index = RegistryIndex(Path(temporary) / "index.sqlite")
            with patch("envy.sources.catalog.catalog", return_value={source.id: source}), patch(
                    "envy.software.RegistryIndex", return_value=index), patch(
                    "envy.sources.adapters.resolve", return_value=ResolveResult.found(row)) as resolve:
                arguments = dict(action="add", group=group, item_value="nix:sl", explicit_ref=None,
                                 manifest=manifest, includes=[], exclusions={}, refresh=False, quiet=True)
                online = software._resolve_new_include(**arguments, offline=False)
                offline = software._resolve_new_include(**arguments, offline=True)
                resolve.assert_called_once()
                self.assertEqual(online.ref, offline.ref)
                self.assertEqual(offline.source_fingerprint, source.fingerprint)
                with self.assertRaises(software.SoftwarePolicyError) as failure:
                    software._resolve_new_include(**{**arguments, "item_value": "nix:missing"}, offline=True)
                self.assertEqual(failure.exception.code, "offline-index-miss")

    def test_first_artifact_add_resolves_the_requested_definition(self):
        from envy.search.model import ResolveResult
        group = next(group for group in software.LINUX_GROUPS if group.ecosystem == "url")
        manifest = self.empty_manifest(group)
        source = Source.from_dict("url:vendor", {"ecosystem": "url", "type": "artifact", "platforms": ["linux"],
            "parameters": {"url": "https://vendor.example/app.deb", "format": "deb", "packageName": "vendor-app",
                           "hash": "a" * 64, "version": "1"}})
        source.capabilities["exactResolve"] = True
        row = SearchResult("url", "url", "vendor-app", "artifact", version="1", ref=source.parameters["url"], source_id=source.id)
        with tempfile.TemporaryDirectory() as temporary, patch("envy.sources.catalog.catalog", return_value={source.id: source}), patch(
                "envy.software.RegistryIndex", return_value=RegistryIndex(Path(temporary) / "index.sqlite")), patch(
                "envy.sources.adapters.resolve", return_value=ResolveResult.found(row)) as resolve, patch(
                "envy.software.source_for", return_value=source), patch("envy.sources.policy.source_for", return_value=source):
            resolved = software._resolve_new_include(action="add", group=group, item_value=source.id, explicit_ref=None,
                manifest=manifest, includes=[], exclusions={}, offline=False, refresh=False, quiet=True)
            self.assertEqual(resolve.call_args.args[0].id, source.id)
            plan = software.build_desired_plan(action="add", group=group, item_value=source.id, manifest=manifest,
                includes=[], exclusions=software.empty_exclusions(software.groups_for_manifest(manifest, include_empty=True)), clean=False, resolved=resolved)
            self.assertEqual(plan.includes_after[0].parameters["hash"], "a" * 64)
            self.assertEqual(plan.item_id, "vendor-app")

    def test_bound_registry_include_preserves_install_endpoint(self):
        for ecosystem, endpoint_key, group in (
                ("npm", "registry", next(g for g in software.COMMON_GROUPS if g.ecosystem == "npm")),
                ("pypi", "index", next(g for g in software.COMMON_GROUPS if g.ecosystem == "pypi"))):
            source = Source.from_dict(ecosystem + ":vendor", {"ecosystem": ecosystem, "type": "registry",
                "parameters": {endpoint_key: "https://vendor.example/packages"}})
            row = SearchResult(ecosystem, ecosystem, "tool", "tool", ref=ecosystem + ":tool", source_id=source.id)
            with patch("envy.software.source_for", return_value=source):
                include = software._include_for_spec(group, "tool", {}, resolved=row)
            self.assertEqual(include.parameters[endpoint_key], source.parameters[endpoint_key])
            self.assertEqual(include.source_id, source.id)

    def test_single_platform_source_registration_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text('{ inputs = {\n    # BEGIN ENVY MANAGED INPUTS\n    # END ENVY MANAGED INPUTS\n}; bindings = {\n        # BEGIN ENVY MANAGED SOURCE BINDINGS\n        # END ENVY MANAGED SOURCE BINDINGS\n}; }\n')
            responses = [SimpleNamespace(stdout=""), SimpleNamespace(stdout="false"), SimpleNamespace(stdout="true")]
            with patch("envy.sources.shared.run_process", side_effect=responses), patch("envy.sources.shared.check_or_exit"):
                apply_shared("extra", "github:example/packages", "add", root=root)
            self.assertIn('"extra".url', (root / "flake.nix").read_text())

    def test_source_json_failure_uses_one_envelope(self):
        from typer.testing import CliRunner
        from envy.sources.cli import app
        with patch("envy.sources.cli.machine_manifest", return_value=None):
            result = CliRunner().invoke(app, ["ls", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "manifest-unavailable")

    def test_source_parameter_types_fail_as_source_errors(self):
        for definition in (
            {"ecosystem": "nix", "type": "flake", "parameters": {"input": 1}},
            {"ecosystem": "nix", "type": "flake", "platforms": None, "parameters": {"input": "bad"}},
            {"ecosystem": "url", "type": "artifact", "parameters": {"format": "deb", "packageName": "app", "hash": "a" * 64}},
        ):
            with self.assertRaises(SourceError):
                Source.from_dict(definition["ecosystem"] + ":bad", definition)

    def test_rejects_credentials_unknown_fields_and_short_key(self):
        for changes in ({"uri": "https://user:pass@example.org"},
                        {"keyFingerprint": "ABCD"}, {"password": "secret"}):
            source = native_source().to_dict()
            source["parameters"]["apt"].update(changes)
            with self.assertRaises(SourceError):
                Source.from_dict("native:vendor", source)

    def test_fingerprint_changes_with_lock_or_repository_context(self):
        source = native_source()
        first = source.fingerprint
        source.context["release"] = "24.04"
        self.assertNotEqual(first, source.fingerprint)
        first = source.fingerprint
        source.parameters["apt"]["keyFingerprint"] = "B" * 40
        self.assertNotEqual(first, source.fingerprint)

    def test_managed_source_roundtrip_preserves_manual_content(self):
        original = "{ ... }:\n{\n  # custom\n  envy.git.name = \"Human\";\n}\n"
        raw = native_source().to_dict()
        raw = {key: raw[key] for key in ("label", "ecosystem", "type", "platforms", "scope", "parameters", "owner")}
        updated = render_definitions(original, {"native:vendor": raw})
        self.assertEqual(parse_definitions(updated), {"native:vendor": raw})
        self.assertIn('envy.git.name = "Human";', updated)
        self.assertNotIn("MANAGED SOURCES", render_definitions(updated, {}))

    def test_source_index_isolates_same_name_and_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = RegistryIndex(Path(temporary) / "v2.sqlite")
            rows = [SearchResult("npm", "npm", "tool", "tool", ref="npm:tool",
                                 source_id=sid, source_fingerprint="revision-a") for sid in ("npm:a", "npm:b")]
            index.put_results(rows)
            self.assertIsNone(index.lookup("npm", "tool", "tool"))
            self.assertEqual(index.lookup("npm", "tool", "tool", source_id="npm:a",
                                         fingerprint="revision-a").result.source_id, "npm:a")
            self.assertIsNone(index.lookup("npm", "tool", "tool", source_id="npm:a", fingerprint="revision-b"))
            index.put_miss("npm", "tool", "absent", source_id="npm:a", fingerprint="a")
            self.assertFalse(index.recently_missing("npm", "tool", "absent", source_id="npm:b", fingerprint="a"))
            index.clear_source("npm:a")
            self.assertEqual(index.stats()["entries"], 1)

    def test_legacy_index_payload_not_used_for_new_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = RegistryIndex(Path(temporary) / "v2.sqlite")
            index.put_results([SearchResult("npm", "npm", "tool", "tool", ref="npm:tool")])
            self.assertIsNone(index.lookup("npm", "tool", "tool", source_id="npm:npmjs", fingerprint="current"))

    def test_exact_result_json_preserves_legacy_fields_additively(self):
        row = SearchResult("npm", "npm", "tool", "tool", version="2", source_id="npm:a")
        encoded = row.to_dict()
        self.assertEqual(encoded["version"], "2")
        self.assertEqual(encoded["resolvedVersion"], "2")
        self.assertEqual(SearchResult.from_dict(encoded).source_id, "npm:a")

    def test_source_prerequisite_exclusion_cannot_be_removed_implicitly(self):
        source = native_source()
        source.context["manager"] = "apt"
        source.capabilities["configure"] = True
        manifest = {"platform": "linux", "software": {"groups": {}}}
        with patch("envy.sources.policy.source_for", return_value=source):
            with self.assertRaises(SourceError) as error:
                prerequisite(source.id, manifest, [], {"native.system.repository": ["vendor"]})
            self.assertEqual(error.exception.code, "source-excluded")
            dependency = prerequisite(source.id, manifest, [], {})
            self.assertEqual(dependency.group, "native.system.repository")

    def test_homebrew_builtin_taps_do_not_become_prerequisites(self):
        manifest = {"platform": "darwin", "software": {"groups": {}}}
        for tap in ("homebrew/core", "homebrew/cask"):
            source = Source.from_dict("homebrew:" + tap, {
                "label": tap, "ecosystem": "homebrew", "type": "tap",
                "platforms": ["darwin"], "scope": "system", "parameters": {"tap": tap},
            })
            with patch("envy.sources.policy.source_for", return_value=source):
                self.assertIsNone(prerequisite(source.id, manifest, [], {}))

    def test_third_party_homebrew_tap_remains_a_prerequisite(self):
        source = Source.from_dict("homebrew:vendor/tools", {
            "label": "Vendor tools", "ecosystem": "homebrew", "type": "tap",
            "platforms": ["darwin"], "scope": "system",
            "parameters": {"tap": "vendor/tools"},
        })
        manifest = {"platform": "darwin", "software": {"groups": {}}}
        with patch("envy.sources.policy.source_for", return_value=source):
            dependency = prerequisite(source.id, manifest, [], {})
        self.assertEqual(dependency.group, "homebrew.system.repository")
        self.assertEqual(dependency.id, "vendor/tools")

    def test_flake_writer_parser_keeps_input_binding(self):
        group = software.COMMON_GROUPS[0]
        include = software.ManagedInclude(group.key, "hello", "hello", ref="flake:other#default",
            parameters={"input": "other", "attrPath": ["default"]}, source_id="nix:other")
        source = software._update_managed_policy_source("{ ... }:\n{\n}\n", [include],
                                                       software.empty_exclusions((group,)), (group,))
        items, _ = software._parse_managed_policy_source(source, (group,))
        self.assertEqual(items[0], include)
        self.assertIn("softwareSourcePackages,", source)

    def test_nix_lookup_targets_project_input_and_validates_derivation(self):
        source = Source("nix:other", "Other", "nix", "flake", parameters={"input": "other"}, registered=True)
        result = SimpleNamespace(returncode=0, stdout='{"name":"hello","version":"1","drvPath":"/nix/store/hello.drv"}')
        with patch("envy.sources.adapters.run_process", return_value=result) as run:
            resolved = resolve_nix(source, {"system": "x86_64-linux"}, "flake:other#default")
        self.assertEqual(resolved.result.ref, "flake:other#default")
        self.assertIn("softwareSourcePackages.x86_64-linux.other.default", run.call_args.args[0][3])
        self.assertNotIn("nixpkgs#", " ".join(run.call_args.args[0]))

    def test_shared_registration_rolls_back_flake_and_lock_on_check_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flake = root / "flake.nix"
            original = '{ inputs = {\n    # BEGIN ENVY MANAGED INPUTS\n    # END ENVY MANAGED INPUTS\n}; bindings = {\n        # BEGIN ENVY MANAGED SOURCE BINDINGS\n        # END ENVY MANAGED SOURCE BINDINGS\n}; }\n'
            flake.write_text(original)
            lock = root / "flake.lock"
            lock.write_text("original")
            def mutate(*args, **kwargs):
                lock.write_text("new lock")
                return SimpleNamespace(stdout="true")
            with patch("envy.sources.shared.run_process", side_effect=mutate), patch(
                "envy.sources.shared.check_or_exit", side_effect=SystemExit(1)):
                with self.assertRaises(SourceError) as failure:
                    apply_shared("extra", "github:example/packages", "add", root=root)
            self.assertEqual(failure.exception.code, "shared-validation-failed")
            self.assertEqual(flake.read_text(), original)
            self.assertEqual(lock.read_text(), "original")

    def test_shared_removal_rejects_handwritten_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text('  # BEGIN ENVY MANAGED INPUTS\n  # END ENVY MANAGED INPUTS\n  # BEGIN ENVY MANAGED SOURCE BINDINGS\n  # END ENVY MANAGED SOURCE BINDINGS\n')
            with self.assertRaises(SourceError):
                preview_shared("nixpkgs", None, "rm", root)

    def test_shared_duplicate_markers_are_rejected_before_mutation(self):
        from envy.sources.shared import managed_block
        block = "# BEGIN ENVY MANAGED INPUTS\n# END ENVY MANAGED INPUTS\n"
        with self.assertRaises(SourceError):
            managed_block(block + block, "INPUTS")

    def test_package_and_repository_prerequisite_are_one_plan_without_implicit_pin(self):
        source = native_source()
        source.capabilities["configure"] = True
        source.context["manager"] = "apt"
        groups = {group.key: {
            "label": group.label, "optionPath": group.option, "ecosystem": group.ecosystem,
            "kind": group.kind, "scope": group.scope, "editable": {"include": True, "exclude": True},
            "selection": {"include": [], "exclude": [], "effective": []}
        } for group in software.LINUX_GROUPS}
        manifest = {"schemaVersion": 3, "platform": "linux", "software": {"groups": groups}}
        active = software.groups_for_manifest(manifest, include_empty=True)
        package_group = software.require_group("native.system.package", active)
        resolved = SearchResult("apt", "native", "tool", "package", version="99",
                                ref="native:tool", source_id=source.id)
        with patch("envy.sources.policy.source_for", return_value=source):
            plan = software.build_desired_plan(action="add", group=package_group, item_value="tool",
                manifest=manifest, includes=[], exclusions=software.empty_exclusions(active),
                clean=False, resolved=resolved)
        self.assertEqual({item.group for item in plan.includes_after},
                         {"native.system.package", "native.system.repository"})
        package = next(item for item in plan.includes_after if item.group == package_group.key)
        self.assertIsNone(package.version)
        self.assertEqual(plan.to_dict()["prerequisites"][0]["sourceId"], source.id)

    def test_tampered_flake_metadata_is_rejected(self):
        group = software.COMMON_GROUPS[0]
        value = '[\n# envy: {"id":"tool","ref":"flake:wrong#default","sourceId":"nix:wrong"}\nsoftwareSourcePackages."correct"."default"\n]'
        with self.assertRaises(software.SoftwarePolicyError):
            software._parse_nix_includes(value, group)

    def test_private_signed_index_disables_system_cache_and_hooks(self):
        from envy.sources.native.apt import signed_options
        source = native_source()
        def prepare(source, directory, manager):
            path = directory / "vendor.sources"
            path.write_text("Types: deb")
            return path, None
        with patch("envy.sources.native.backend.prepare", side_effect=prepare), patch(
                "subprocess.run", return_value=SimpleNamespace(returncode=0)):
            with signed_options(source) as options:
                self.assertIn("Dir::Cache::pkgcache=", options)
                self.assertIn("Dir::Cache::srcpkgcache=", options)
                conf = Path(options[options.index("-c") + 1]).read_text()
                self.assertIn("#clear APT::Update::Post-Invoke;", conf)



class ArtifactTrustTests(unittest.TestCase):
    def test_download_hash_mismatch_rejected_before_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            response = contextlib.closing(__import__("io").BytesIO(b"package"))
            # URL response must expose its effective URL to detect insecure redirects.
            stream = __import__("io").BytesIO(b"package")
            stream.geturl = lambda: "https://example.org/package.deb"
            with patch("urllib.request.urlopen", return_value=stream):
                with self.assertRaises(SourceError):
                    download({"url": "https://example.org/package.deb", "hash": "0" * 64}, Path(temporary) / "package")

    def test_metadata_identity_and_architecture_are_verified(self):
        parameters = {"format": "deb", "packageName": "expected"}
        with patch("envy.sources.artifacts.shutil.which", return_value="/usr/bin/dpkg-deb"), patch(
            "envy.sources.artifacts.subprocess.run", return_value=SimpleNamespace(stdout=b"Package: wrong\nVersion: 1\nArchitecture: amd64\n")):
            with self.assertRaises(SourceError):
                inspect(parameters, Path("/tmp/package.deb"))

    def test_sri_and_hex_hashes_are_equivalent(self):
        digest = hashlib.sha256(b"package").digest()
        import base64
        self.assertEqual(expected_hash("sha256-" + base64.b64encode(digest).decode()), digest.hex())


if __name__ == "__main__":
    unittest.main()

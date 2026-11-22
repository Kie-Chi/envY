"""All native context tests use fake providers and a temporary filesystem."""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from envy.storage import maintenance, native_context
from envy.storage.model import Policy, StorageError


class NativeContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.cwd = self.home / "project"
        self.cwd.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("go", "uv", "npm", "pip", "pip3", "brew", "docker", "nix", "nix-store", "sudo"):
            (self.bin / name).write_text("fake binary\n")
            (self.bin / name).chmod(0o700)
        self.policy = Policy(self.home, "manual", 30, (), (), (), (), "", "", maintenance.ACTIONS)
        self.cache = self.home / "cache-target"
        self.cache.mkdir()
        self.roots = {name: self.root / ("brew-" + name)
                      for name in ("prefix", "cache", "cellar", "caskroom", "repository")}
        for path in self.roots.values():
            path.mkdir()
        self.values = {"GOCACHE": str(self.cache), "GOENV": str(self.home / "go.env"),
                       "GOMOD": str(self.cwd / "go.mod"), "GOWORK": ""}
        self.calls = []
        self.nix_url_prefix = "local://"
        self.nix_require_readonly_feature = True
        self.docker_target = {"endpoint": "unix:///fake/docker.sock", "daemonId": "fake",
                              "context": "default", "source": "current-context",
                              "builder": {"name": "default", "driver": "docker", "nodes": [
                                  {"endpoint": "unix:///fake/docker.sock"}]}}
        self.module_target = {"path": str(self.cache), "resolvedPath": str(self.cache),
                              "cwd": str(self.cwd)}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.bin)}, clear=True).start()
        patch.object(native_context.Path, "cwd", return_value=self.cwd).start()
        patch.object(native_context.shutil, "which", side_effect=self.which).start()
        self.probe = patch.object(native_context, "probe", side_effect=self.query).start()
        self.docker_capture = patch.object(native_context.docker, "maintenance_target",
                                           return_value=self.docker_target).start()
        self.docker_verify = patch.object(native_context.docker, "verify_target").start()
        patch.object(native_context.go_modcache, "target", return_value=self.module_target).start()
        self.module_verify = patch.object(native_context.go_modcache, "verify").start()
        metadata = native_context._metadata
        # System config paths are metadata fakes too: tests never inspect host rc files.
        patch.object(native_context, "_metadata", side_effect=lambda path: metadata(path)
                     if path.is_relative_to(self.root) else {"path": str(path), "exists": False}).start()
        patch.object(maintenance, "PLATFORM", "linux").start()
        self.run = patch.object(maintenance, "run_process", return_value=subprocess.CompletedProcess(
            [], 0, "never printed native output", "never printed native stderr")).start()

    def which(self, name):
        path = self.bin / name
        return str(path) if path.exists() else None

    def query(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        name, args = Path(argv[0]).name, argv[1:]
        if name in {"pip", "pip3"} and args == ["cache", "--help"]:
            value = "purge"
        elif name == "go":
            value = json.dumps(self.values)
        elif name in {"uv", "pip", "pip3"} or (name == "npm" and args[-1] == "cache"):
            value = str(self.cache)
        elif name == "npm":
            value = str(self.home / (args[-1] + ".npmrc"))
        elif name == "brew":
            value = str(self.roots[args[0][2:]])
        elif name == "nix":
            if "ping" in args:
                features = args[args.index("--extra-experimental-features") + 1].split()
                if self.nix_require_readonly_feature and "read-only-local-store" not in features:
                    return subprocess.CompletedProcess(argv, 1, "", "read-only-local-store is required")
                value = json.dumps({"url": self.nix_url_prefix +
                    "?log=/nix/var/log/nix&read-only=true&real=/nix/store&root=/"
                    "&state=/nix/var/nix&store=/nix/store"})
            else:
                value = json.dumps({"store": {"value": "auto"},
                                    "keep-outputs": {"value": False},
                                    "keep-derivations": {"value": True}})
        else:
            raise AssertionError("Unexpected fake query: " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, value + "\n", "private stderr must not leak")

    def preview(self, actions):
        return maintenance.preview(self.policy, actions, bind_targets=True)

    def execute(self, actions, preview):
        return maintenance.maintain(self.policy, actions, approved_preview=preview)

    def test_cache_managers_bind_real_targets_and_rebuild_fixed_commands(self):
        for action in ("go", "uv", "npm", "pip"):
            with self.subTest(action=action):
                preview = self.preview([action])
                item = preview["items"][0]
                self.assertTrue(item["available"], item["note"])
                self.assertEqual(item["target"]["path"], str(self.cache))
                item["commands"] = [["fake-malicious", "arbitrary-delete"]]
                result = self.execute([action], preview)
                self.assertEqual(result["errors"], [])
                argv = self.run.call_args.args[0]
                self.assertEqual(argv[0], str(self.bin / action))
                self.assertNotIn("fake-malicious", argv)
                self.assertEqual(self.run.call_args.kwargs["cwd"], str(self.cwd))
                if action == "go":
                    self.assertEqual(argv[1:], ["clean", "-cache"])
                    self.assertEqual(self.run.call_args.kwargs["env"]["GOCACHE"], str(self.cache))
                else:
                    self.assertIn(str(self.cache), argv)
                self.run.reset_mock()
        self.assertTrue(all(kwargs["timeout"] <= 5 and kwargs["max_bytes"] == 131_072
                            and kwargs["budget"] for _, kwargs in self.calls))
        self.assertTrue(all(Path(argv[0]).is_absolute() for argv, _ in self.calls))

    def test_target_drift_never_executes_and_needs_new_preview(self):
        for action in ("go", "uv", "npm", "pip"):
            with self.subTest(action=action):
                preview = self.preview([action])
                before = self.cache
                self.cache = self.home / (action + "-other-cache")
                self.values["GOCACHE"] = str(self.cache)
                result = self.execute([action], preview)
                self.assertEqual(result["errors"], [action])
                self.assertIn("fresh preview", result["items"][0]["reason"])
                self.run.assert_not_called()
                self.cache = before
                self.values["GOCACHE"] = str(before)

    def test_executable_replacement_and_path_resolution_drift_are_rejected(self):
        preview = self.preview(["uv"])
        binary = self.bin / "uv"
        binary.write_text("replacement binary with a different size\n")
        self.assertEqual(self.execute(["uv"], preview)["errors"], ["uv"])
        self.run.assert_not_called()
        preview = self.preview(["uv"])
        with patch.object(native_context.shutil, "which", return_value=str(self.bin / "npm")):
            self.assertEqual(self.execute(["uv"], preview)["errors"], ["uv"])
        self.run.assert_not_called()

    def test_changed_project_configuration_environment_and_cwd_require_new_preview(self):
        config = self.cwd / "uv.toml"
        for change in ("file", "environment", "cwd"):
            with self.subTest(change=change):
                preview = self.preview(["uv"])
                if change == "file":
                    config.write_text("[tool]\n")
                elif change == "environment":
                    os.environ["UV_CONFIG_FILE"] = str(self.home / "other.toml")
                else:
                    other = self.home / "other-project"
                    other.mkdir()
                    native_context.Path.cwd.return_value = other
                self.assertEqual(self.execute(["uv"], preview)["errors"], ["uv"])
                self.run.assert_not_called()

    def test_cache_root_replacement_and_symlink_alias_are_rejected(self):
        preview = self.preview(["go"])
        self.cache.rename(self.home / "old-cache")
        self.cache.mkdir()
        self.assertEqual(self.execute(["go"], preview)["errors"], ["go"])
        self.run.assert_not_called()
        self.cache.rmdir()
        self.cache.symlink_to(self.home / "old-cache", target_is_directory=True)
        self.assertFalse(self.preview(["go"])["items"][0]["available"])

    def test_broad_home_project_and_protected_targets_are_unavailable(self):
        protected = self.home / "private"
        self.policy = replace(self.policy, protected=(protected,), mandatory=(protected,))
        for path in (Path("/"), self.home, self.cwd, self.home / ".cache", protected):
            with self.subTest(path=path):
                self.values["GOCACHE"] = str(path)
                item = self.preview(["go"])["items"][0]
                self.assertFalse(item["available"])
                self.assertIn("broad", item["reason"])
        self.run.assert_not_called()

    def test_query_errors_and_invalid_metadata_never_leak_stderr_or_env(self):
        os.environ["UV_INDEX_URL"] = "https://secret:credential@fake.invalid/"
        for code, output in ((124, ""), (125, ""), (1, ""), (0, "relative\n"),
                             (0, str(self.cache) + "\n/another")):
            with self.subTest(code=code), patch.object(native_context, "probe", return_value=
                    subprocess.CompletedProcess([], code, output, "PRIVATE_QUERY_STDERR")):
                data = self.preview(["uv"])
                self.assertFalse(data["items"][0]["available"])
                serialized = json.dumps(data)
                self.assertNotIn("PRIVATE_QUERY_STDERR", serialized)
                self.assertNotIn("secret:credential", serialized)
        self.run.assert_not_called()

    def test_bindings_store_metadata_and_selector_digests_without_config_contents(self):
        rc = self.cwd / ".npmrc"
        rc.write_text("private configuration body\n")
        os.environ["NPM_CONFIG_TOKEN"] = "private token value"
        with patch.object(Path, "read_text", side_effect=AssertionError("No rc body reads")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("No rc body reads")):
            data = self.preview(["npm"])
        self.assertTrue(data["items"][0]["available"])
        serialized = json.dumps(data)
        self.assertNotIn("private configuration body", serialized)
        self.assertNotIn("private token value", serialized)
        self.assertIn("selectorsDigest", serialized)
        self.assertIn(str(rc), serialized)

    def test_pip_isolation_is_shared_by_bounded_queries_and_execution(self):
        with patch.object(maintenance, "PLATFORM", "darwin"), patch.dict(os.environ, {
                "PYTHONPATH": "foreign", "PYTHONHOME": "foreign", "DEVELOPER_DIR": "foreign"}):
            result = self.execute(["pip"], self.preview(["pip"]))
        self.assertEqual(result["errors"], [])
        environments = [kwargs["env"] for _, kwargs in self.calls]
        environments.append(self.run.call_args.kwargs["env"])
        for environment in environments:
            for name in ("PYTHONPATH", "PYTHONHOME", "DEVELOPER_DIR"):
                self.assertNotIn(name, environment)

    def test_query_controls_disable_automatic_downloads_and_update_checks(self):
        expected = {"go": {"GOTOOLCHAIN": "local"},
                    "npm": {"npm_config_update_notifier": "false", "NPM_CONFIG_UPDATE_NOTIFIER": "false"},
                    "pip": {"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
                    "brew": native_context.BREW_ENV}
        with patch.dict(os.environ, GOTOOLCHAIN="auto", PIP_DISABLE_PIP_VERSION_CHECK="0"):
            for action, controls in expected.items():
                with self.subTest(action=action):
                    self.calls.clear()
                    result = self.execute([action], self.preview([action]))
                    self.assertEqual(result["errors"], [])
                    environments = [kwargs["env"] for _, kwargs in self.calls]
                    environments.append(self.run.call_args.kwargs["env"])
                    for environment in environments:
                        for key, value in controls.items():
                            self.assertEqual(environment[key], value)

    def test_pip3_fallback_is_identity_bound(self):
        (self.bin / "pip").unlink()
        preview = self.preview(["pip"])
        self.assertTrue(preview["items"][0]["available"])
        self.assertEqual(self.execute(["pip"], preview)["errors"], [])
        self.assertEqual(self.run.call_args.args[0][0], str(self.bin / "pip3"))

    def test_brew_binds_all_cleanup_roots_and_disables_automatic_operations(self):
        with patch.dict(os.environ, HOMEBREW_NO_AUTOREMOVE="0", HOMEBREW_NO_AUTO_UPDATE="0"):
            preview = self.preview(["brew"])
            self.assertEqual(set(preview["items"][0]["target"]),
                             {"prefix", "cache", "cellar", "caskroom", "repository", "logs", "locks"})
            result = self.execute(["brew"], preview)
        self.assertEqual(result["errors"], [])
        environment = self.run.call_args.kwargs["env"]
        for name in native_context.BREW_ENV:
            self.assertEqual(environment[name], "1")
        self.assertEqual(environment["HOMEBREW_CACHE"], str(self.roots["cache"]))
        for root in self.roots:
            with self.subTest(root=root):
                self.run.reset_mock()
                preview = self.preview(["brew"])
                before = self.roots[root]
                self.roots[root] = self.root / ("other-" + root)
                self.assertEqual(self.execute(["brew"], preview)["errors"], ["brew"])
                self.run.assert_not_called()
                self.roots[root] = before

    def test_docker_is_reverified_before_both_steps_and_preserves_first_output(self):
        preview = self.preview(["docker", "go"])
        self.docker_verify.side_effect = [None, StorageError("Docker target changed")]
        result = self.execute(["docker", "go"], preview)
        self.assertEqual(result["errors"], ["docker"])
        self.assertEqual(self.docker_verify.call_count, 2)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(result["items"][0]["output"][0]["returncode"], 0)
        self.assertIn("target changed", result["items"][0]["reason"])
        self.assertEqual(result["items"][1]["status"], "completed")
        self.assertEqual(self.run.call_args_list[0].args[0][:3],
                         [str(self.bin / "docker"), "--context", "default"])

    def test_module_cache_reuses_pinned_provider_and_requires_separate_opt_in(self):
        preview = self.preview(["go-modcache"])
        self.assertTrue(preview["items"][0]["available"])
        self.assertEqual(self.execute(["go-modcache"], preview)["errors"], [])
        self.module_verify.assert_called_once_with(self.module_target, home=self.home,
            env=native_context._environment("go-modcache", "linux"), binary=str(self.bin / "go"))
        self.assertEqual(self.run.call_args.kwargs["env"]["GOMODCACHE"], str(self.cache))
        self.assertEqual(self.run.call_args.kwargs["env"]["GOTOOLCHAIN"], "local")
        self.assertEqual(self.run.call_args.args[0][1:], ["clean", "-modcache"])
        self.policy = replace(self.policy, maintenance=("go",))
        with self.assertRaises(StorageError):
            self.execute(["go-modcache"], preview)

    def test_multicall_queries_preserve_the_verified_entrypoint(self):
        entrypoint = self.bin / "pip"
        entrypoint.unlink()
        entrypoint.symlink_to("pip-multicall")
        (self.bin / "pip-multicall").write_text("fake multicall binary\n")
        (self.bin / "pip-multicall").chmod(0o700)
        preview = self.preview(["pip"])
        self.assertTrue(preview["items"][0]["available"], preview)
        binding = preview["items"][0]["binding"]
        self.assertEqual(binding["executable"]["path"], str(entrypoint))
        self.assertEqual(binding["executable"]["resolvedPath"], str(self.bin / "pip-multicall"))
        self.assertEqual(self.execute(["pip"], preview)["errors"], [])
        self.assertEqual(self.run.call_args.args[0][0], str(entrypoint))
        self.assertTrue(all(argv[0] == str(entrypoint) for argv, _ in self.calls))

    def test_nix_multicall_execution_preserves_entrypoint_and_rejects_replacement(self):
        self.nix_require_readonly_feature = False
        self.nix_url_prefix = "local"
        entrypoint = self.bin / "nix-store"
        entrypoint.unlink()
        entrypoint.symlink_to("nix")
        def multicall(argv, **kwargs):
            code = 0 if Path(argv[0]).name == "nix-store" and "--gc" in argv else 1
            return subprocess.CompletedProcess(argv, code, "", "")
        self.run.side_effect = multicall
        with patch.object(native_context, "_path", side_effect=lambda value, *args, **kwargs: value), \
                patch.object(native_context, "_root_identity", side_effect=lambda path: {"path": path}):
            preview = self.preview(["nix"])
            self.assertTrue(preview["items"][0]["available"], preview)
            self.assertEqual(self.execute(["nix"], preview)["errors"], [])
            self.assertEqual(self.run.call_args.args[0][0], str(entrypoint))
            for replacement in ("link", "target", "saved-path"):
                with self.subTest(replacement=replacement):
                    self.run.reset_mock()
                    preview = self.preview(["nix"])
                    if replacement == "link":
                        entrypoint.unlink()
                        entrypoint.symlink_to("./nix")
                    elif replacement == "target":
                        (self.bin / "nix").write_text("changed multicall binary\n")
                    else:
                        preview["items"][0]["binding"]["executable"]["path"] = str(self.bin / "sudo")
                    self.assertEqual(self.execute(["nix"], preview)["errors"], ["nix"])
                    self.run.assert_not_called()

    def test_nix_readonly_query_enables_feature_and_accepts_only_equivalent_local_urls(self):
        with patch.object(native_context, "_path", side_effect=lambda value, *args, **kwargs: value), \
                patch.object(native_context, "_root_identity", side_effect=lambda path: {"path": path}):
            for prefix in ("local://", "local"):
                with self.subTest(prefix=prefix):
                    self.nix_url_prefix = prefix
                    self.calls.clear()
                    preview = self.preview(["nix"])
                    self.assertTrue(preview["items"][0]["available"], preview)
                    self.assertEqual(self.execute(["nix"], preview)["errors"], [])
                    for argv, _ in self.calls:
                        if "ping" in argv:
                            features = argv[argv.index("--extra-experimental-features") + 1].split()
                            self.assertEqual(set(features), {"nix-command", "read-only-local-store"})
                    self.assertNotIn("--extra-experimental-features", self.run.call_args.args[0])
            config_query = self.query
            query = "log=/nix/var/log/nix&read-only=true&real=/nix/store&root=/&state=/nix/var/nix&store=/nix/store"
            invalid = ["local://other?" + query, "local:///other?" + query,
                       "ssh://fake.invalid?" + query, "daemon?" + query,
                       "local://?" + query.replace("&root=/", ""),
                       "local://?" + query.replace("&root=/", "&root=/custom"),
                       "local://?" + query + "&unknown=1",
                       "local://?" + query + "&unknown=",
                       "local://?" + query.replace("&read-only=true", "&read-only="),
                       "local://?" + query + "#fragment"]
            for url in invalid:
                with self.subTest(url=url):
                    def unproven_query(argv, **kwargs):
                        if "ping" in argv:
                            return subprocess.CompletedProcess(argv, 0, json.dumps({"url": url}), "")
                        return config_query(argv, **kwargs)
                    self.probe.side_effect = unproven_query
                    self.assertFalse(self.preview(["nix"])["items"][0]["available"])
        self.assertTrue(all("config" not in argv or "show" in argv for argv, _ in self.calls))

    def test_nix_local_endpoint_is_explicit_and_sudo_is_never_probed(self):
        path_guard = native_context._path
        def fake_store_path(value, *args, **kwargs):
            if value in {"/nix/store", "/nix/var/nix"}:
                return value
            return path_guard(value, *args, **kwargs)
        with patch.object(native_context, "_path", side_effect=fake_store_path), \
                patch.object(native_context, "_root_identity", side_effect=lambda path: {"path": path}), \
                patch.object(maintenance, "PLATFORM", "darwin"), \
                patch.object(maintenance, "esudo") as authenticate:
            preview = self.preview(["nix"])
            self.assertTrue(preview["items"][0]["available"], preview)
            self.run.assert_not_called()
            authenticate.assert_not_called()
            self.assertFalse(any(Path(argv[0]).name == "sudo" for argv, _ in self.calls))
            self.assertEqual(self.execute(["nix"], preview)["errors"], [])
        authenticate.assert_called_once_with("-v", capture=True, cwd=self.home / "project",
                                             sudo_binary=str(self.bin / "sudo"), non_interactive=True)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[:4], [str(self.bin / "sudo"), "-n", "-H", str(self.bin / "nix-store")])
        self.assertIn("--gc", argv)
        self.assertEqual(argv[-6:], ["--option", "keep-outputs", "false",
                                     "--option", "keep-derivations", "true"])
        endpoint = argv[argv.index("--store") + 1]
        self.assertEqual(parse_qs(urlsplit(endpoint).query), {
            "root": ["/"], "store": ["/nix/store"], "real": ["/nix/store"],
            "state": ["/nix/var/nix"], "log": ["/nix/var/log/nix"]})
        self.assertFalse(any(Path(argv[0]).name == "sudo" for argv, _ in self.calls))
        self.assertTrue(all("read-only=true" in argv[-1] for argv, _ in self.calls if "ping" in argv))
        self.assertNotIn("--delete-generations", argv)
        self.assertNotIn("-d", argv)

    def test_nix_unknown_or_custom_scope_is_unavailable(self):
        with patch.dict(os.environ, NIX_STATE_DIR=str(self.root / "other-state")):
            item = self.preview(["nix"])["items"][0]
        self.assertFalse(item["available"])
        self.assertIn("Custom Nix store", item["reason"])
        self.run.assert_not_called()

    def test_nix_query_evidence_and_retention_drift_are_checked_before_cleanup(self):
        with patch.object(native_context, "_path", side_effect=lambda value, *args, **kwargs: value), \
                patch.object(native_context, "_root_identity", side_effect=lambda path: {"path": path}):
            preview = self.preview(["nix"])
            self.assertTrue(preview["items"][0]["available"])
            config_query = self.query
            def changed_config(argv, **kwargs):
                result = config_query(argv, **kwargs)
                if "config" in argv:
                    values = json.loads(result.stdout)
                    values["keep-outputs"]["value"] = True
                    result.stdout = json.dumps(values)
                return result
            self.probe.side_effect = changed_config
            result = self.execute(["nix"], preview)
            self.assertEqual(result["errors"], ["nix"])
            self.assertIn("fresh preview", result["items"][0]["reason"])
            self.run.assert_not_called()
            self.probe.side_effect = config_query
            for unproven in ("local", "local?root=%2Fother", "daemon", "ssh://private.invalid"):
                with self.subTest(unproven=unproven):
                    def unproven_query(argv, **kwargs):
                        if "ping" in argv:
                            return subprocess.CompletedProcess(argv, 0, json.dumps({"url": unproven}), "")
                        return config_query(argv, **kwargs)
                    self.probe.side_effect = unproven_query
                    item = self.preview(["nix"])["items"][0]
                    self.assertFalse(item["available"])
                    self.assertIn("did not verify", item["reason"])
            self.run.assert_not_called()

    def test_remote_nix_selection_is_unavailable_without_store_queries(self):
        for selector in ("ssh://private.invalid", "https://private.invalid", "local?root=/custom"):
            with self.subTest(selector=selector), patch.dict(os.environ, NIX_REMOTE=selector):
                self.calls.clear()
                item = self.preview(["nix"])["items"][0]
                self.assertFalse(item["available"])
                self.assertFalse(any("ping" in argv for argv, _ in self.calls))
        self.run.assert_not_called()

    def test_invalid_native_paths_are_unavailable_without_alias_resolution(self):
        for target in ("relative", "//another-host/cache", "/tmp/cache/../home", "/etc",
                       "/etc/ssh", "/nix/store/fake", str(self.cache) + "\x00invalid"):
            with self.subTest(target=target):
                self.values["GOCACHE"] = target
                self.assertFalse(self.preview(["go"])["items"][0]["available"])
        self.run.assert_not_called()

    def test_current_provider_must_prove_json_target_even_when_binding_is_tampered(self):
        preview = self.preview(["uv"])
        preview["items"][0]["binding"]["target"]["path"] = str(self.home / "arbitrary")
        result = self.execute(["uv"], preview)
        self.assertEqual(result["errors"], ["uv"])
        self.run.assert_not_called()

    def test_policy_and_approved_actionset_are_checked_before_any_probe(self):
        preview = self.preview(["go", "uv"])
        self.probe.reset_mock()
        for selected in (["go"], ["uv", "go"], ["go", "uv", "pip"], ["unknown"]):
            with self.subTest(selected=selected), self.assertRaises(StorageError):
                self.execute(selected, preview)
        self.probe.assert_not_called()
        self.run.assert_not_called()

    def test_unavailable_approved_item_is_never_refreshed_into_eligible_item(self):
        self.values["GOCACHE"] = "/"
        preview = self.preview(["go", "uv"])
        self.values["GOCACHE"] = str(self.cache)
        result = self.execute(["go", "uv"], preview)
        self.assertEqual(result["errors"], ["go"])
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0][0], str(self.bin / "uv"))
        self.assertEqual(result["items"][0]["status"], "unavailable")
        self.assertEqual(result["items"][1]["status"], "completed")

    def test_failure_preserves_prior_success_and_runs_later_actions(self):
        preview = self.preview(["go", "uv", "npm"])
        self.run.side_effect = [subprocess.CompletedProcess([], 0, "", ""),
                                subprocess.CompletedProcess([], 42, "secret stdout", "secret stderr"),
                                subprocess.CompletedProcess([], 0, "", "")]
        result = self.execute(["go", "uv", "npm"], preview)
        self.assertEqual(result["errors"], ["uv"])
        self.assertEqual([item["status"] for item in result["items"]],
                         ["completed", "failed", "completed"])
        self.assertEqual(result["items"][0]["output"][0]["returncode"], 0)
        self.assertEqual(self.run.call_count, 3)
        self.assertNotIn("secret stdout", json.dumps(result))
        self.assertNotIn("secret stderr", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

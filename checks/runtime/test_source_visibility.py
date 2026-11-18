"""Safety boundaries for the synthetic source regression driver."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("source_visibility", Path(__file__).with_name("source_visibility.py"))
visibility = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(visibility)


class SourceDriverBoundaryTests(unittest.TestCase):
    def test_fresh_environment_drops_inherited_git_nix_and_secret_controls(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "GIT_DIR": "/outside/.git", "GIT_WORK_TREE": "/outside",
            "GIT_INDEX_FILE": "/outside/index", "GIT_CONFIG_COUNT": "1",
            "NIX_REMOTE": "daemon", "NIX_CONFIG": "include /outside/nix.conf",
            "OPENAI_API_KEY": "synthetic-inherited-value", "ENVY_ROOT": "/outside",
        }):
            root = Path(temporary)
            env = visibility.isolated_environment(root, [sys.executable])
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "OPENAI_API_KEY", "ENVY_ROOT"):
                self.assertNotIn(key, env)
            self.assertEqual(env["NIX_REMOTE"], f"local?root={root / 'store'}")
            self.assertNotIn("/outside", json.dumps(env))
            self.assertTrue(Path(env["HOME"]).is_relative_to(root))
            self.assertEqual(Path(env["HOME"]).stat().st_mode & 0o777, 0o700)

    def test_invalid_input_is_unavailable_with_json_and_no_executed_commands(self):
        for args in (
            ["--timeout", "0"],
            ["--timeout", "121"],
            ["--nix", "/definitely-unavailable-source-test-nix"],
        ):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                scripts = root / "scripts"
                (scripts / "envy").mkdir(parents=True)
                (scripts / "envy/host.py").touch()
                library = root / "lib"
                library.mkdir()
                (library / "modules.nix").touch()
                (library / "default.nix").touch()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = visibility.main(["--scripts", str(scripts), "--nixpkgs-lib", str(library), *args])
                report = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["actualCaseCount"], 0)
                self.assertEqual(report["executedCommandCount"], 0)
                self.assertTrue(report["unknown"])

    def test_nix_commands_pin_private_store_and_offline_without_shell(self):
        with tempfile.TemporaryDirectory(prefix="source arguments with spaces ") as temporary:
            root = Path(temporary)
            driver = visibility.Driver(root, sys.executable, sys.executable, sys.executable, 5)
            # Run a real process to verify the exact argv/environment crossing.
            recorder = root / "record.py"
            recorder.write_text("import json,os,sys; print(json.dumps({'argv':sys.argv[1:], 'store':os.environ['NIX_REMOTE']}))")
            driver.nix = sys.executable
            with patch.object(driver, "run", wraps=driver.run) as run:
                driver.nix_run("eval", "path:/synthetic with spaces#probe")
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--store") + 1], f"local?root={root / 'store'}")
            self.assertIn("--offline", argv)
            self.assertEqual(argv[-1], "path:/synthetic with spaces#probe")
            recorded = driver.run([sys.executable, str(recorder), argv[-1]])
            self.assertEqual(recorded["returncode"], 0)
            observed = json.loads(recorded["stdout"])
            self.assertEqual(observed["argv"], [argv[-1]])
            self.assertEqual(observed["store"], f"local?root={root / 'store'}")

    def test_child_output_is_bounded_and_never_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = visibility.Driver(Path(temporary), sys.executable, sys.executable, sys.executable, 5)
            result = driver.run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 262144)"])
            self.assertEqual(result["error"], "output-limit")
            self.assertLessEqual(len(result["stdout"]), visibility.OUTPUT_LIMIT)
            with self.assertRaises(RuntimeError):
                driver.require(result)


class SyntheticHostHelperTests(unittest.TestCase):
    """Execute HOST_HELPER unchanged; every envy import is a synthetic module."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="source-guard-fake-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo, self.home = self.root / "repo", self.root / "home"
        outside = tempfile.TemporaryDirectory(prefix="source-guard-outside-", dir="/tmp")
        self.addCleanup(outside.cleanup)
        self.checkout = Path(outside.name).resolve()
        (self.repo / "resources/scripts/envy").mkdir(parents=True)
        (self.repo / "flake.nix").touch()
        self.home.mkdir()
        self.platform = "darwin" if sys.platform == "darwin" else "linux"
        (self.repo / "hosts" / self.platform).mkdir(parents=True)
        self.env = {"ENVY_ROOT": str(self.repo), "HOME": str(self.home),
                    "ENVY_MACHINE": "source-import"}
        self.cwd = self.repo
        self.metadata = Mock(return_value={"machine_id": "source-import"})
        self.mutation = Mock()
        self.no_offer = Mock()
        self.production_offer = Mock(return_value=False)
        self.modules = {name: types.ModuleType(name) for name in
                        ("envy", "envy.utils", "envy.host", "envy.config",
                         "envy.config.machine_nix", "envy.config.refine")}
        self.modules["envy"].__path__ = []
        self.modules["envy.config"].__path__ = []
        self.utils = self.modules["envy.utils"]
        self.host = self.modules["envy.host"]
        self.machine = self.modules["envy.config.machine_nix"]
        self.utils.ENVY_ROOT, self.utils.HOME_DIR = self.repo, self.home
        self.utils.DEVICE_LABEL_FILE = self.repo / ".device-label"
        self.utils.SECRETS_FILE = self.repo / "secrets/secrets.yaml"
        self.utils.read_device_metadata = self.metadata
        self.host.HOSTS_DIR = self.repo / "hosts"
        self.host.MACHINES_DIR = self.host.HOSTS_DIR / self.platform
        self.host.DEFAULT_MACHINE = self.host.HOSTS_DIR / "default.nix"
        self.host.machine_file = lambda identity: self.host.MACHINES_DIR / (identity + ".nix")

        def machine_file(identity=None):
            # Model the production no-argument lookup's unconditional metadata read.
            if identity is None:
                self.utils.read_device_metadata()
                identity = self.env["ENVY_MACHINE"]
            return self.utils.ENVY_ROOT / "hosts" / self.platform / (identity + ".nix")

        self.utils.machine_config_file = machine_file
        self.machine.machine_config_file = machine_file
        self.host.offer_mutation_commit = self.production_offer

        def fake_init(**kwargs):
            self.mutation("cmd_init")
            self.no_offer()
            self.host.offer_mutation_commit([self.host.machine_file(kwargs["machine_id"])], "Synthetic")

        self.host.cmd_init = fake_init
        self.utils.set_device_machine_id = lambda identity: self.mutation("set_device_machine_id")
        self.machine.write_mirror_overrides = lambda values: self.mutation("write_mirror_overrides")

        def fake_refine(*, write, strict):
            if write:
                self.mutation("refine_agents")
            return types.SimpleNamespace(ok=True, changed=True, errors=[])

        self.modules["envy.config.refine"].refine_agents = fake_refine
        self.namespace = {}

    def run_helper(self, mode):
        args = ["synthetic-helper", mode, str(self.repo), str(self.home), str(self.root),
                str(self.checkout), str(Path(visibility.__file__).resolve())]
        # No inherited environment or production module can enter this execution.
        with patch.dict(sys.modules, self.modules), patch.object(sys, "argv", args), \
                patch.object(os, "environ", self.env), patch.object(Path, "cwd", return_value=self.cwd), \
                contextlib.redirect_stdout(io.StringIO()):
            exec(compile(visibility.HOST_HELPER, "<synthetic-HOST_HELPER>", "exec"), self.namespace)

    def assert_rejected(self):
        for mode in ("import", "copy", "refine"):
            with self.subTest(mode=mode):
                self.namespace = {}
                for mock in (self.metadata, self.mutation, self.no_offer, self.production_offer):
                    mock.reset_mock()
                error = None
                try:
                    self.run_helper(mode)
                except RuntimeError as exc:
                    error = exc
                self.assertEqual((self.metadata.call_count, self.mutation.call_count,
                                  self.no_offer.call_count, self.production_offer.call_count),
                                 (0, 0, 0, 0), "metadata/mutation/No/production offer before rejection")
                self.assertEqual(self.namespace.get("commit_offers", []), [])
                self.assertIsNotNone(error, "unsafe request reached the end of HOST_HELPER")

    def test_external_label_symlink(self):
        self.utils.DEVICE_LABEL_FILE.symlink_to(self.checkout / "unread-label")
        self.assert_rejected()

    def test_internal_label_symlink(self):
        self.utils.DEVICE_LABEL_FILE.symlink_to(self.repo / "unread-label")
        self.assert_rejected()

    def test_internal_host_directory_alias(self):
        directory = self.repo / "other"
        directory.mkdir()
        self.host.MACHINES_DIR.rmdir()
        self.host.MACHINES_DIR.symlink_to(directory, target_is_directory=True)
        self.assert_rejected()

    def test_external_host_directory_alias(self):
        self.host.MACHINES_DIR.rmdir()
        self.host.MACHINES_DIR.symlink_to(self.checkout, target_is_directory=True)
        self.assert_rejected()

    def test_internal_host_file_alias(self):
        self.host.machine_file("source-import").symlink_to(self.repo / "other.nix")
        self.assert_rejected()

    def test_external_secret_alias(self):
        (self.repo / "secrets").symlink_to(self.checkout, target_is_directory=True)
        self.assert_rejected()

    def test_missing_marker(self):
        (self.repo / "flake.nix").unlink()
        self.assert_rejected()

    def test_missing_module_marker(self):
        (self.repo / "resources/scripts/envy").rmdir()
        self.assert_rejected()

    def test_external_flake_marker_alias(self):
        (self.checkout / "other.nix").touch()
        (self.repo / "flake.nix").unlink()
        (self.repo / "flake.nix").symlink_to(self.checkout / "other.nix")
        self.assert_rejected()

    def test_internal_flake_marker_alias(self):
        (self.repo / "other.nix").touch()
        (self.repo / "flake.nix").unlink()
        (self.repo / "flake.nix").symlink_to(self.repo / "other.nix")
        self.assert_rejected()

    def test_external_module_marker_alias(self):
        (self.repo / "resources/scripts/envy").rmdir()
        (self.repo / "resources/scripts/envy").symlink_to(self.checkout, target_is_directory=True)
        self.assert_rejected()

    def test_internal_module_marker_parent_alias(self):
        (self.repo / "resources").rename(self.repo / "other-resources")
        (self.repo / "resources").symlink_to(self.repo / "other-resources", target_is_directory=True)
        self.assert_rejected()

    def test_internal_root_alias(self):
        original = self.repo
        original.rename(self.root / "other-repo")
        original.symlink_to(self.root / "other-repo", target_is_directory=True)
        self.assert_rejected()

    def test_external_root_alias(self):
        self.repo = self.root / "root-alias"
        self.repo.symlink_to(self.checkout, target_is_directory=True)
        self.env["ENVY_ROOT"] = str(self.repo)
        self.cwd = self.repo
        self.assert_rejected()

    def test_internal_home_alias(self):
        self.home.rename(self.root / "other-home")
        self.home.symlink_to(self.root / "other-home", target_is_directory=True)
        self.assert_rejected()

    def test_external_home_alias(self):
        self.home.rmdir()
        self.home.symlink_to(self.checkout, target_is_directory=True)
        self.assert_rejected()

    def test_actual_root_alias(self):
        alias = self.root / "actual-root-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.utils.ENVY_ROOT = alias
        self.assert_rejected()

    def test_actual_home_alias(self):
        alias = self.root / "actual-home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        self.utils.HOME_DIR = alias
        self.assert_rejected()

    def test_root_fallback(self):
        self.utils.ENVY_ROOT = self.checkout
        self.assert_rejected()

    def test_home_fallback(self):
        self.utils.HOME_DIR = self.checkout
        self.assert_rejected()

    def test_foreign_cwd(self):
        self.cwd = self.checkout
        self.assert_rejected()

    def test_internal_cwd_alias(self):
        self.cwd = self.root / "cwd-alias"
        self.cwd.symlink_to(self.repo, target_is_directory=True)
        self.assert_rejected()

    def test_wrong_environment_root(self):
        self.env["ENVY_ROOT"] = str(self.checkout)
        self.assert_rejected()

    def test_wrong_environment_home(self):
        self.env["HOME"] = str(self.checkout)
        self.assert_rejected()

    def test_internal_wrong_host_role(self):
        self.host.MACHINES_DIR = self.repo / "other"
        self.assert_rejected()

    def test_internal_wrong_managed_role(self):
        self.machine.machine_config_file = lambda identity=None: self.repo / "other.nix"
        self.assert_rejected()

    def test_swapped_managed_roles(self):
        self.machine.machine_config_file = lambda identity=None: self.host.machine_file("source-copy")
        self.assert_rejected()

    def test_internal_wrong_utils_role(self):
        self.utils.machine_config_file = lambda identity=None: self.repo / "other.nix"
        self.assert_rejected()

    def test_internal_wrong_default_role(self):
        self.host.DEFAULT_MACHINE = self.repo / "other.nix"
        self.assert_rejected()

    def test_unknown_machine_selection(self):
        self.env["ENVY_MACHINE"] = "unexpected-host"
        self.assert_rejected()

    def test_only_ancestors_of_temp_root_may_be_normalized(self):
        # Model macOS /var -> /private/var without accepting aliases inside root.
        with tempfile.TemporaryDirectory(prefix="source-guard-parent-", dir="/tmp") as parent:
            alias = Path(parent) / "system-alias"
            alias.symlink_to(self.root, target_is_directory=True)
            visibility.guard_fixture_request(alias, alias / "repo", alias / "home", self.checkout,
                                             alias / "repo", self.env)
            visibility.guard_resolved_targets(alias, alias / "repo", alias / "home", self.checkout,
                                              alias / "repo", alias / "home", {
                                                  "host/import": alias / "repo/hosts" / self.platform / "source-import.nix"})


if __name__ == "__main__":
    unittest.main()

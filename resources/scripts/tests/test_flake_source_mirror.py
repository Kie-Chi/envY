import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envy import process
from envy.process import prime_flake_sources


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
SCRIPT = ROOT / "resources/scripts/flake-source-mirror.sh"


class FlakeSourceMirrorTests(unittest.TestCase):
    def fixture(self, temporary: str, *, mirror: str = "https://mirror.example/", nar_hash: str = "sha256-locked") -> Path:
        root = Path(temporary)
        (root / "resources/scripts").mkdir(parents=True)
        (root / "flake.lock").write_text(json.dumps({
            "nodes": {
                "root": {"inputs": {"demo": "demo"}},
                "demo": {"locked": {
                    "type": "github",
                    "owner": "org",
                    "repo": "demo",
                    "rev": "abc123",
                    "narHash": nar_hash,
                }},
            },
        }))
        (root / "resources/scripts/flake-source-mirror.sh").write_text(SCRIPT.read_text())
        return root

    def test_prime_prefetches_locked_github_source_with_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            calls = root / "calls"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            nix = bin_dir / "nix"
            nix.write_text(
                "#!/bin/bash\n"
                "set -eu\n"
                "printf '%s\\n' \"$*\" >> \"$ENVY_TEST_CALLS\"\n"
                "if [ \"$1\" = eval ]; then printf '%s' \"$ENVY_TEST_MIRROR\"; fi\n"
            )
            nix.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "ENVY_MIRROR": "china",
                "ENVY_FLAKE_MIRROR_URL": "https://mirror.example/",
                "ENVY_TEST_CALLS": str(calls),
                "ENVY_TEST_MIRROR": "demo\thttps://mirror.example/https://github.com/org/demo/archive/abc123.tar.gz\tsha256-locked\n",
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            # The fixture's fake nix eval emits the record directly, matching
            # the real helper's Nix expression output.
            result = subprocess.run(["bash", str(SCRIPT), str(root), "prime"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = calls.read_text().splitlines()
            self.assertTrue(any("flake prefetch" in command for command in commands))
            self.assertIn("narHash=sha256-locked", "\n".join(commands))
            self.assertEqual(len(list((root / "cache/envy/flake-sources").glob("*.ready"))), 1)

    def test_nested_locked_nodes_are_prefetched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "resources/scripts").mkdir(parents=True)
            (root / "flake.lock").write_text(json.dumps({
                "nodes": {
                    "root": {"inputs": {"parent": "parent"}},
                    "parent": {"inputs": {"child": "child"}, "locked": {
                        "type": "github", "owner": "org", "repo": "parent",
                        "rev": "parent-rev", "narHash": "sha256-parent",
                    }},
                    "child": {"locked": {
                        "type": "github", "owner": "org", "repo": "child",
                        "rev": "child-rev", "narHash": "sha256-child",
                    }},
                },
            }))
            (root / "resources/scripts/flake-source-mirror.sh").write_text(SCRIPT.read_text())
            calls = root / "calls"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            nix = bin_dir / "nix"
            nix.write_text(
                "#!/bin/bash\n"
                "set -eu\n"
                "printf '%s\\n' \"$*\" >> \"$ENVY_TEST_CALLS\"\n"
                "if [ \"$1\" = eval ]; then\n"
                "  printf '%s' \"$ENVY_TEST_RECORDS\"\n"
                "fi\n"
            )
            nix.chmod(0o755)
            records = (
                "parent\thttps://mirror.example/https://github.com/org/parent/archive/parent-rev.tar.gz\tsha256-parent\n"
                "child\thttps://mirror.example/https://github.com/org/child/archive/child-rev.tar.gz\tsha256-child\n"
            )
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "ENVY_MIRROR": "china",
                "ENVY_FLAKE_MIRROR_URL": "https://mirror.example/",
                "ENVY_TEST_CALLS": str(calls),
                "ENVY_TEST_RECORDS": records,
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), str(root), "prime"],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = calls.read_text().splitlines()
            self.assertEqual(sum("flake prefetch" in command for command in commands), 2)
            self.assertIn("narHash=sha256-parent", "\n".join(commands))
            self.assertIn("narHash=sha256-child", "\n".join(commands))

    def test_source_mirror_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            env = {**os.environ, "ENVY_MIRROR": "china", "ENVY_FLAKE_SOURCE_MIRROR": "0"}
            result = subprocess.run(
                ["bash", str(SCRIPT), str(root), "prime"],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "cache").exists())

    def test_failed_prefetch_does_not_create_success_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary, nar_hash="sha256-wrong")
            calls = root / "calls"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            nix = bin_dir / "nix"
            nix.write_text(
                "#!/bin/bash\n"
                "set -eu\n"
                "printf '%s\\n' \"$*\" >> \"$ENVY_TEST_CALLS\"\n"
                "if [ \"$1\" = eval ]; then\n"
                "  printf '%s' \"demo\thttps://mirror.example/https://github.com/org/demo/archive/abc123.tar.gz\tsha256-wrong\n\"\n"
                "  exit 0\n"
                "fi\n"
                "printf '%s\\n' 'NAR hash mismatch' >&2\n"
                "exit 1\n"
            )
            nix.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "ENVY_MIRROR": "china",
                "ENVY_FLAKE_MIRROR_URL": "https://mirror.example/",
                "ENVY_TEST_CALLS": str(calls),
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), str(root), "prime"],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("NAR hash mismatch", result.stderr)
            self.assertFalse(list((root / "cache/envy/flake-sources").glob("*.ready")))

    def test_upstream_skips_prefetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            env = {**os.environ, "ENVY_MIRROR": "upstream", "PATH": os.environ["PATH"]}
            result = subprocess.run(["bash", str(SCRIPT), str(root), "prime"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "cache").exists())

    def test_non_https_endpoint_is_not_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            env = {**os.environ, "ENVY_MIRROR": "china", "ENVY_FLAKE_MIRROR_URL": "http://mirror.example/"}
            result = subprocess.run(["bash", str(SCRIPT), str(root), "prime"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("non-HTTPS", result.stderr)

    def test_process_primes_only_once_per_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            helper = root / "resources/scripts/flake-source-mirror.sh"
            calls = root / "helper-calls"
            helper.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' called >> \"$ENVY_TEST_HELPER_CALLS\"\n"
            )
            helper.chmod(0o755)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_nix = fake_bin / "nix"
            fake_nix.write_text("#!/bin/bash\nexit 0\n")
            fake_nix.chmod(0o755)
            env = {
                **os.environ,
                "ENVY_ROOT": str(root),
                "ENVY_TEST_HELPER_CALLS": str(calls),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            process._FLAKE_SOURCE_MIRROR_PRIMED.clear()
            with patch.dict(os.environ, env, clear=True):
                process.run_process(["nix", "eval"], cwd=root, env=env, check=False)
                process.run_process(["nix", "eval"], cwd=root, env=env, check=False)
            self.assertEqual(calls.read_text().splitlines(), ["called"])

    def test_prime_retries_after_lock_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            calls = root / "helper-calls"
            helper = root / "resources/scripts/flake-source-mirror.sh"
            helper.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' called >> \"$ENVY_TEST_HELPER_CALLS\"\n"
            )
            helper.chmod(0o755)
            env = {**os.environ, "ENVY_TEST_HELPER_CALLS": str(calls)}
            process._FLAKE_SOURCE_MIRROR_PRIMED.clear()
            with patch.dict(os.environ, env, clear=True):
                prime_flake_sources(root, env)
                (root / "flake.lock").write_text((root / "flake.lock").read_text() + "\\n")
                prime_flake_sources(root, env)
            self.assertEqual(calls.read_text().splitlines(), ["called", "called"])


if __name__ == "__main__":
    unittest.main()

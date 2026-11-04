"""The repository launcher reuses a saved devShell instead of evaluating the flake."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
BASH = shutil.which("bash")

NIX_STUB = '''\
if [ "$1" = --version ]; then
    printf 'nix (Nix) %s\\n' "${ENVY_TEST_NIX_VERSION:-2.0}"
    exit 0
fi
[ "$1" = develop ] || exit 97
installable="$2"
shift 2
entry="develop $installable"
if [ "$1" = --profile ]; then
    entry="$entry --profile"
    ln -sfn "$ENVY_TEST_STORE" "$2"
    shift 2
fi
printf '%s\\n' "$entry" >> "$ENVY_TEST_LOG"
[ "$1" = --command ] || exit 98
shift
# Like the real shellHook, put a store copy of resources/scripts first.
export PYTHONPATH="/nix/store/old-scripts:${PYTHONPATH:-}"
exec "$BASH" "$@"
'''

PYTHON_STUB = '''\
if [ "$1" = -c ]; then exit 0; fi
printf 'python %s\\n' "$*" >> "$ENVY_TEST_LOG"
printf 'pythonpath %s\\n' "$PYTHONPATH" >> "$ENVY_TEST_LOG"
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "checkout"
        scripts = self.root / "resources" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / "envy", self.root / "envy")
        (self.root / "flake.nix").write_text("{ devShell = 1; }\n")
        (self.root / "flake.lock").write_text("{}\n")
        (scripts / "flake-source-mirror.sh").write_text('printf "prime\\n" >> "$ENVY_TEST_LOG"\n')
        self.bin = base / "bin"
        self.bin.mkdir()
        self.stub("nix", NIX_STUB)
        self.stub("python3", PYTHON_STUB)
        self.store = base / "store-env"
        self.store.mkdir()
        self.cache = base / "cache"
        self.log = base / "log"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("ENVY_", "PYTHON"))
        }
        self.env.update({
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(base / "home"),
            "XDG_CACHE_HOME": str(self.cache),
            "ENVY_FLAKE_SOURCE_MIRROR": "1",
            "ENVY_TEST_LOG": str(self.log),
            "ENVY_TEST_STORE": str(self.store),
            "BASH": BASH,
        })

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{BASH}\nset -eu\n{body}")
        path.chmod(0o755)

    def run_envy(self, *args):
        self.log.write_text("")
        result = subprocess.run(
            [BASH, str(self.root / "envy"), *args],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.log.read_text().splitlines()

    def saved_shells(self):
        directory = self.cache / "envy" / "devshell"
        return sorted(path.name for path in directory.iterdir()) if directory.exists() else []

    def assert_checkout_first(self, lines):
        pythonpath = next(line for line in lines if line.startswith("pythonpath "))
        self.assertTrue(
            pythonpath.removeprefix("pythonpath ").startswith(f"{self.root}/resources/scripts:"),
            pythonpath,
        )

    def test_first_run_saves_the_shell_and_later_runs_reuse_it(self):
        first = self.run_envy("status", "--json")
        self.assertEqual(first[:2], ["prime", f"develop path:{self.root} --profile"])
        self.assertIn("python -m envy status --json", first)
        self.assert_checkout_first(first)
        (key,) = self.saved_shells()

        second = self.run_envy("status")
        profile = self.cache / "envy" / "devshell" / key / "profile"
        self.assertEqual(second[0], f"develop {profile}")
        self.assertNotIn("prime", second)
        self.assertIn("python -m envy status", second)
        self.assert_checkout_first(second)

    def test_flake_or_nix_changes_replace_the_saved_shell(self):
        self.run_envy("--help")
        (first,) = self.saved_shells()

        (self.root / "flake.lock").write_text('{"nodes": {}}\n')
        self.assertEqual(self.run_envy("--help")[1], f"develop path:{self.root} --profile")
        (second,) = self.saved_shells()
        self.assertNotEqual(second, first)

        self.env["ENVY_TEST_NIX_VERSION"] = "3.0"
        self.assertEqual(self.run_envy("--help")[1], f"develop path:{self.root} --profile")
        (third,) = self.saved_shells()
        self.assertNotIn(third, {first, second})

        (self.root / "flake.nix").write_text("{ devShell = 2; }\n")
        self.assertIn("--profile", self.run_envy("--help")[1])

    def test_a_collected_profile_is_saved_again(self):
        self.run_envy("--help")
        self.store.rmdir()

        lines = self.run_envy("--help")

        self.assertEqual(lines[1], f"develop path:{self.root} --profile")

    def test_unavailable_hashing_falls_back_to_the_flake(self):
        self.stub("sha256sum", "exit 1\n")

        lines = self.run_envy("--help")

        self.assertEqual(lines[:2], ["prime", f"develop path:{self.root}"])
        self.assertEqual(self.saved_shells(), [])
        self.assert_checkout_first(lines)

    def test_devshell_reads_no_checkout_path_besides_python_sources(self):
        # The saved shell is keyed by flake.nix and flake.lock only. A new local
        # input would need to join that key in the envy launcher.
        text = (ROOT / "flake.nix").read_text()
        block = re.search(r"\n( +)mkDevShell = .*?(?=\n\1\S)", text, re.S)
        self.assertIsNotNone(block)
        self.assertEqual(
            set(re.findall(r"(?<![\w.])\./[\w./-]+", block.group(0))),
            {"./resources/scripts"},
        )


if __name__ == "__main__":
    unittest.main()

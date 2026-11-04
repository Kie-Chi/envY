import os
import re
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))


class ZshStartupTests(unittest.TestCase):
    """oh-my-zsh owns the only compinit call; each extra call costs startup time."""

    def test_shared_zsh_files_do_not_call_compinit(self):
        for path in sorted((ROOT / "files/zsh").glob("*.zsh")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                code = line.split("#", 1)[0]
                with self.subTest(file=path.name, line=number):
                    self.assertIsNone(re.search(r"\bcompinit\b", code), line)

    def test_darwin_skips_the_global_compinit(self):
        text = (ROOT / "modules/desktops/darwin/base.nix").read_text()
        self.assertRegex(text, r"(?m)^\s*programs\.zsh\.enableGlobalCompInit = false;")


if __name__ == "__main__":
    unittest.main()

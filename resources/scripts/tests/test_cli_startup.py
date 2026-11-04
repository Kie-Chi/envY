"""Startup cost guards for the envy command line."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

import envy


SCRIPTS = Path(envy.__file__).resolve().parents[1]


class CliStartupTests(unittest.TestCase):
    def test_loading_every_command_leaves_prompt_toolkit_unimported(self):
        # Every envy invocation (and each TUI request) imports envy.main.
        # prompt_toolkit alone costs about 0.1 s; only interactive key flows
        # and setup.py use it, so they import it on demand.
        code = "import sys, envy.main; print('prompt_toolkit' in sys.modules)"
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": str(SCRIPTS)},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()

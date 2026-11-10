import errno
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import tempfile
import time
import unittest
from pathlib import Path


class DeltaPilotTests(unittest.TestCase):
    def setUp(self):
        self.delta = os.environ.get("ENVY_DELTA_BIN")
        self.bat = os.environ.get("ENVY_BAT_BIN") or shutil.which("bat")
        theme = os.environ.get("ENVY_TOKYONIGHT_SOURCE")
        self.git = os.environ.get("ENVY_GIT_BIN") or shutil.which("git")
        if not all((self.delta, self.bat, theme, self.git)):
            self.skipTest("Provide locked delta, bat, git and Tokyo Night for the isolated pilot")
        self.directory = tempfile.TemporaryDirectory(prefix="envy-delta-pilot-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.work = self.home / "work"
        self.work.mkdir()
        self.environment = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_CACHE_HOME": str(self.home / "cache"), "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_STATE_HOME": str(self.home / "state"), "SHELL": "/bin/sh",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CEILING_DIRECTORIES": str(self.home),
            "PATH": os.pathsep.join([str(Path(self.git).resolve().parent), "/usr/bin", "/bin"]),
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
            "TERM": "xterm-256color", "COLORTERM": "truecolor",
        }
        themes = self.home / "config/bat/themes"
        themes.mkdir(parents=True)
        shutil.copyfile(Path(theme) / "extras/sublime/tokyonight_night.tmTheme", themes / "tokyonight_night.tmTheme")
        # An empty cwd avoids bat interpreting an existing 'cache' directory as a file.
        subprocess.run([self.bat, "cache", "--build"], cwd=self.work, env=self.environment,
                       check=True, capture_output=True, text=True, timeout=15)
        self.config = self.home / "delta.gitconfig"
        self.config.write_text((Path(theme) / "extras/delta/tokyonight_night.gitconfig").read_text() + """
[delta]
    syntax-theme = tokyonight_night
    line-numbers = true
    wrap-max-lines = unlimited
    line-numbers-zero-style = "#a9b1d6"
    line-numbers-minus-style = "#f7768e"
    line-numbers-plus-style = "#9ece6a"
""")
        fixture = os.environ.get("ENVY_HISTORY_INTEGRATION_FIXTURES")
        if fixture:
            self.config.write_text(json.loads(Path(fixture).read_text())["deltaConfig"])
        self.config.chmod(0o600)
        self.before, self.after = self.work / "before", self.work / "after"
        self.before.mkdir()
        self.after.mkdir()
        self.filename = "中文 文件.py"
        common = "# synthetic fixture\n"
        long_line = "长行 " * 70
        (self.before / self.filename).write_text(common + "value = '旧内容'\n" + f"text = '{long_line}old'\n")
        (self.after / self.filename).write_text(common + "value = '新内容'\n" + f"text = '{long_line}new'\n")
        result = subprocess.run(
            [self.git, "--no-pager", "diff", "--no-index", "--no-ext-diff", "--no-color", "--", "before", "after"],
            cwd=self.work, env=dict(self.environment, GIT_CONFIG_COUNT="1",
                                   GIT_CONFIG_KEY_0="core.quotepath", GIT_CONFIG_VALUE_0="false"),
            capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.diff = result.stdout
        self.assertTrue(self.diff.startswith(b"diff --git"))
        self.original_config = self.config.read_bytes()

    def tearDown(self):
        if hasattr(self, "original_config"):
            self.assertEqual(self.config.read_bytes(), self.original_config)
            self.assertFalse((self.home / ".gitconfig").exists())

    def arguments(self, *extra):
        # --no-gitconfig disables an explicit --config too in the locked version.
        return [self.delta, "--config", str(self.config), "--true-color=always", *extra]

    def run_delta(self, *extra, environment=None, diff=None):
        return subprocess.run(self.arguments(*extra), input=self.diff if diff is None else diff,
                              cwd=self.work, env=environment or self.environment,
                              capture_output=True, timeout=15)

    @staticmethod
    def plain(value):
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value.decode(errors="replace"))

    def test_locked_theme_and_custom_cache_are_loaded(self):
        result = self.run_delta("--show-config", diff=b"")
        self.assertEqual(result.returncode, 0)
        text = self.plain(result.stdout)
        self.assertIn("syntax-theme", text)
        self.assertIn("TokyoNight", text)
        self.assertIn('syntax "#4a272f"', text)
        self.assertIn('syntax "#243e4a"', text)
        self.assertIn('"#a9b1d6"', text)
        self.assertEqual(result.stderr, b"")

    def test_normal_diff_preserves_chinese_and_uses_night_colors(self):
        result = self.run_delta("--paging=never", "--width=120")
        self.assertEqual(result.returncode, 0)
        text = self.plain(result.stdout)
        self.assertIn("旧内容", text)
        self.assertIn("新内容", text)
        self.assertIn("中文 文件.py", text)
        self.assertIn("长行", text)
        self.assertIn("old", text)
        self.assertIn("new", text)
        self.assertIn(b"48;2;74;39;47", result.stdout)
        self.assertIn(b"48;2;36;62;74", result.stdout)
        self.assertEqual(result.stderr, b"")

    def test_narrow_side_by_side_diff_wraps_without_losing_content(self):
        result = self.run_delta("--paging=never", "--side-by-side", "--width=40")
        self.assertEqual(result.returncode, 0)
        text = self.plain(result.stdout)
        self.assertIn("长行", text)
        self.assertRegex(text, r"旧内[^\n]*\n[^\n]*容")
        self.assertRegex(text, r"新内[^\n]*\n[^\n]*容")
        self.assertIn("old", text)
        self.assertIn("new", text)
        self.assertNotIn("\ufffd", text)
        self.assertEqual(result.stderr, b"")

    def test_noninteractive_pipe_does_not_start_a_pager(self):
        environment = dict(self.environment, DELTA_PAGER="/does/not/exist")
        result = self.run_delta("--paging=auto", "--width=80", environment=environment)
        self.assertEqual(result.returncode, 0)
        self.assertIn("新内容", self.plain(result.stdout))
        self.assertEqual(result.stderr, b"")

    def test_empty_input_and_cli_failures_have_distinct_status(self):
        empty = self.run_delta("--paging=never", diff=b"")
        self.assertEqual(empty.returncode, 0)
        self.assertEqual(empty.stdout, b"")
        invalid = self.run_delta("--fixture-invalid-option")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn(b"unexpected argument", invalid.stderr)
        bad_config = self.home / "invalid.config"
        bad_config.write_text("[broken" + chr(10))
        missing = subprocess.run([self.delta, "--config", str(bad_config)],
                                 cwd=self.work, env=self.environment, input=b"",
                                 capture_output=True, timeout=10)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(b"Failed to read git config", missing.stderr)

    def test_forced_paging_uses_only_the_isolated_pager(self):
        pager = self.home / "pager.py"
        output = self.home / "pager-output"
        pager.write_text("import os,sys\nfrom pathlib import Path\ndata=sys.stdin.buffer.read()\nPath(os.environ['FIXTURE_PAGER_OUTPUT']).write_bytes(data)\nsys.stdout.buffer.write(data)\n")
        environment = dict(self.environment, DELTA_PAGER=shlex.quote(sys.executable) + " " + shlex.quote(str(pager)),
                           FIXTURE_PAGER_OUTPUT=str(output))
        diff_file = self.home / "fixture.diff"
        diff_file.write_bytes(self.diff)
        script = '"$1" --config "$2" --paging=always --true-color=always --width=80 < "$3"'
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

        def session():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(["/bin/sh", "-c", script, "envy-delta-pager", self.delta, str(self.config), str(diff_file)],
                                   cwd=self.work, env=environment, stdin=slave, stdout=slave, stderr=slave,
                                   preexec_fn=session)
        os.close(slave)
        data = bytearray()
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.05)[0]:
                    if process.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                data.extend(chunk)
            process.wait(timeout=2)
            self.assertEqual(process.returncode, 0)
            self.assertTrue(output.exists())
            self.assertIn("新内容", self.plain(output.read_bytes()))
            self.assertIn("新内容", self.plain(bytes(data)))
        finally:
            os.close(master)
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()

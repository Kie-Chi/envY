import errno
import fcntl
import json
import os
import pty
import select
import shlex
import signal
import struct
import subprocess
import termios
import tempfile
import time
import unittest
from pathlib import Path

from test_shell_prompt import ROOT, isolated_environment


class CompletionSourceTests(unittest.TestCase):
    def test_installation_stays_with_envy_and_z_is_not_duplicated(self):
        text = (ROOT / "modules/cores/shell.nix").read_text()
        self.assertIn("[ pkgs.eza pkgs.fzf pkgs.zoxide pkgs.atuin ]", text)
        self.assertIn('programs.fzf.enable = false;', text)
        self.assertIn('programs.zoxide.enable = false;', text)
        self.assertIn('fzf = "nix:fzf";', text)
        self.assertIn('zoxide = "nix:zoxide";', text)
        self.assertNotIn('"web-search" "z"', text)
        self.assertIn("lib.mkOrder 1155", text)

    def test_group_descriptions_remain_plain_completion_text(self):
        text = (ROOT / "files/zsh/fzf-preview.zsh").read_text()
        self.assertIn("zstyle ':completion:*:descriptions' format '[%d]'", text)

    def test_alacritty_uses_the_shared_theme_and_font_without_installing(self):
        text = (ROOT / "modules/desktops/linux/terminal.nix").read_text()
        self.assertIn("programs.alacritty = {", text)
        self.assertIn("package = null;", text)
        self.assertIn("/extras/alacritty/tokyonight_night.toml", text)
        self.assertIn('font.normal.family = "Maple Mono NF CN";', text)
        self.assertIn("font.size = 12.0;", text)


class CompletionRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = os.environ.get("ENVY_COMPLETION_FIXTURES")
        if not fixture:
            self.skipTest("Set ENVY_COMPLETION_FIXTURES for isolated completion runtime checks")
        self.fixture = json.loads(Path(fixture).read_text())
        for key in ("zsh", "fzf", "zoxide", "ohMyZsh", "suggestions", "syntax", "fzfTab"):
            self.assertTrue(Path(self.fixture[key]).exists(), key)

    def launch(self, arguments, home, width=80, extra=None):
        environment = isolated_environment(home)
        environment["SHELL"] = "/bin/sh"
        environment["PATH"] = os.pathsep.join(
            [str(Path(self.fixture[key]).parent) for key in ("zsh", "fzf", "zoxide")]
            + ["/usr/bin", "/bin"]
        )
        environment.update(extra or {})
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))

        def session():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(arguments, stdin=slave, stdout=slave, stderr=slave,
                                   cwd=home, env=environment, preexec_fn=session)
        os.close(slave)
        return process, master

    @staticmethod
    def read_until(master, marker, timeout=8):
        output = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.05)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
            if marker is not None and marker in output:
                break
        return output.decode(errors="replace")

    @staticmethod
    def close(process, master):
        os.close(master)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.kill()
            process.wait(timeout=2)

    def test_generated_order_and_real_tab_selection_preserve_ctrl_r_and_zoxide(self):
        fixture = self.fixture
        lines = fixture["initLines"]
        positions = [next(i for i, line in enumerate(lines) if text in line) for text in (
            "zsh-autosuggestions.zsh", "oh-my-zsh.sh", "zoxide init zsh",
            "fzf --zsh", "fzf-tab.plugin.zsh", "zsh-syntax-highlighting.zsh",
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("z", fixture["omzPlugins"])
        with tempfile.TemporaryDirectory(prefix="envy-completion-") as directory:
            home = Path(directory).resolve()
            (home / "cache").mkdir()
            (home / "custom").mkdir()
            target = home / "跳转 目录"
            target.mkdir()
            styles = home / "styles.zsh"
            styles.write_text((ROOT / "files/zsh/fzf-preview.zsh").read_text() + chr(10) + fixture["fzfFlags"])
            script = [
                "bindkey -e",
                f"export ZSH={shlex.quote(fixture['ohMyZsh'])}",
                'ZSH_CUSTOM="$HOME/custom"', 'ZSH_CACHE_DIR="$HOME/cache/omz"',
                'ZSH_COMPDUMP="$HOME/cache/compdump"', "DISABLE_AUTO_UPDATE=true",
                "ZSH_DISABLE_COMPFIX=true", "zstyle ':omz:update' mode disabled",
                "plugins=(" + " ".join(shlex.quote(name) for name in fixture["omzPlugins"]) + ")",
                "typeset -gi FIXTURE_COMPINIT=0",
                'TRAPDEBUG() { [[ "$ZSH_DEBUG_CMD" != "compinit -"* ]] || (( ++FIXTURE_COMPINIT )); return 0; }',
            ]
            for line in lines:
                if "fzf-tab.plugin.zsh" in line:
                    script.append(f"source {shlex.quote(str(styles))}")
                script.append(line)
            script += [
                f"z {shlex.quote(str(target))}",
                'print -r -- "DIRECTORY=$PWD"',
                'print -r -- "COMPINIT=$FIXTURE_COMPINIT"',
                "bindkey -M emacs '^I'", "bindkey -M emacs '^R'", "whence -w z zi",
                "_fixture_complete() { local -a values=('alpha:中文候选' 'beta:second option'); _describe -t fixture-items 'Fixture entries' values; }",
                "fixture-cmd() { print -r -- 'ENVY_SELECTED:'$1; }",
                "compdef _fixture_complete fixture-cmd", "PROMPT='FIXTURE> '",
                "print -r -- ENVY_COMPLETION_READY",
            ]
            rc = home / "fixture.zsh"
            rc.write_text(chr(10).join(script))
            process, master = self.launch([fixture["zsh"], "-dfi"], home, extra={"FZF_DEFAULT_OPTS": fixture["fzfOptions"]})
            try:
                os.write(master, f"source {shlex.quote(str(rc))}".encode() + b"\r")
                startup = self.read_until(master, b"ENVY_COMPLETION_READY")
                self.assertIn("COMPINIT=1", startup)
                self.assertIn("fzf-tab-complete", startup)
                self.assertIn("fzf-history-widget", startup)
                self.assertIn("z: function", startup)
                self.assertIn("zi: function", startup)
                self.assertIn(str(target), startup)
                self.assertNotIn("command not found:", startup)
                os.write(master, b"fixture-cmd \t")
                menu = self.read_until(master, b"beta")
                self.assertIn("alpha", menu)
                self.assertIn("beta", menu)
                os.write(master, b"\r")
                time.sleep(0.15)
                os.write(master, b"\r")
                accepted = self.read_until(master, b"ENVY_SELECTED:alpha")
                self.assertIn("ENVY_SELECTED:alpha", accepted)
                self.assertNotIn("command not found:", accepted)
                os.write(master, b"exit\r")
                self.read_until(master, None, timeout=3)
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)
            finally:
                self.close(process, master)

    def test_preview_switches_between_wide_side_and_narrow_bottom_panels(self):
        window = next(value for value in shlex.split(self.fixture["fzfFlags"]) if value.startswith("--preview-window="))
        for width in (80, 120):
            with self.subTest(width=width), tempfile.TemporaryDirectory(prefix="envy-preview-layout-") as directory:
                home = Path(directory).resolve()
                sizes = home / "sizes"
                script = home / "preview.sh"
                script.write_text('printf "%s %s" "$FZF_PREVIEW_COLUMNS" "$FZF_PREVIEW_LINES" > "$SIZES"' + chr(10) + 'printf "synthetic preview"')
                process, master = self.launch(
                    [self.fixture["fzf"], window, "--border=rounded", "--preview", "sh " + shlex.quote(str(script))],
                    home, width, {"FZF_DEFAULT_COMMAND": "printf 'one\ntwo\n'", "SIZES": str(sizes)},
                )
                try:
                    deadline = time.monotonic() + 5
                    values = []
                    while time.monotonic() < deadline:
                        if sizes.exists():
                            values = sizes.read_text().split()
                            if len(values) == 2:
                                break
                        self.read_until(master, b"synthetic preview", timeout=0.05)
                    self.assertEqual(len(values), 2)
                    columns, rows = map(int, values)
                    if width == 80:
                        self.assertGreaterEqual(columns, width - 8)
                        self.assertLessEqual(rows, 12)
                    else:
                        self.assertLess(columns, 80)
                        self.assertGreater(rows, 12)
                    os.write(master, b"\x03")
                    self.read_until(master, None, timeout=3)
                    process.wait(timeout=3)
                    self.assertEqual(process.returncode, 130)
                finally:
                    self.close(process, master)

    def test_preview_fallback_handles_chinese_spaces_and_leading_dashes(self):
        with tempfile.TemporaryDirectory(prefix="envy-preview-content-") as directory:
            home = Path(directory).resolve()
            file = home / "-中文 文件.txt"
            file.write_text(chr(10).join(f"line {number}" for number in range(170)) + chr(10))
            env = isolated_environment(home)
            result = subprocess.run(
                [self.fixture["zsh"], "-dfc", 'source "$1"; zstyle -s \':fzf-tab:complete:*:*\' fzf-preview preview; realpath="$2"; eval "$preview"', "envy-preview", str(ROOT / "files/zsh/fzf-preview.zsh"), str(file)],
                cwd=home, env=env, check=True, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(len(result.stdout.splitlines()), 160)
            self.assertIn("line 159", result.stdout)
            result = subprocess.run(
                [self.fixture["zsh"], "-dfc", 'source "$1"; zstyle -s \':fzf-tab:complete:*:*\' fzf-preview preview; realpath="$2"; eval "$preview"', "envy-preview", str(ROOT / "files/zsh/fzf-preview.zsh"), str(home)],
                cwd=home, env=env, check=True, capture_output=True, text=True, timeout=10,
            )
            self.assertIn(file.name, result.stdout)

    def test_tty_cat_falls_back_when_bat_is_excluded(self):
        with tempfile.TemporaryDirectory(prefix="envy-cat-fallback-") as directory:
            home = Path(directory).resolve()
            file = home / "-中文 文件.txt"
            file.write_text("ENVY_CAT_FALLBACK_OK" + chr(10))
            script = 'compdef() { :; }; source "$1"; cat -- "$2"'
            process, master = self.launch([self.fixture["zsh"], "-dfc", script, "envy-cat", str(ROOT / "files/zsh/func.zsh"), str(file)], home)
            try:
                output = self.read_until(master, b"ENVY_CAT_FALLBACK_OK")
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)
                self.assertIn("ENVY_CAT_FALLBACK_OK", output)
                self.assertNotIn("command not found:", output)
            finally:
                self.close(process, master)


if __name__ == "__main__":
    unittest.main()

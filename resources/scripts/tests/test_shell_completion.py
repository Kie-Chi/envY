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
        environment["TMPDIR"] = str(home)
        environment["TMPPREFIX"] = str(home / "zsh")
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

    def test_real_tab_keeps_candidates_visible_in_dynamic_height_menus(self):
        tmux = shutil.which("tmux")
        self.assertIsNotNone(tmux, "Completion runtime checks require tmux")
        for count in (6, 12, 30):
            for width in (60, 80, 120):
                for height in (12, 24):
                    with self.subTest(count=count, width=width, height=height):
                        self.check_visible_candidates(tmux, count, width, height)

    def check_visible_candidates(self, tmux, count, width, height):
        with tempfile.TemporaryDirectory(prefix="envy-tab-layout-") as directory:
            home = Path(directory).resolve()
            environment = isolated_environment(home)
            environment["SHELL"] = self.fixture["zsh"]
            environment["TMPDIR"] = str(home)
            environment["TMPPREFIX"] = str(home / "zsh")
            environment["PATH"] = os.pathsep.join(
                [str(Path(self.fixture[key]).parent) for key in ("zsh", "fzf")]
                + ["/usr/bin", "/bin"]
            )
            socket = str(home / "tmux.sock")

            def run(*arguments):
                return subprocess.run(
                    [tmux, "-S", socket, "-f", os.devnull, *arguments],
                    env=environment, cwd=home, check=True, capture_output=True,
                    text=True, timeout=5,
                ).stdout

            def screen_until(predicate):
                deadline = time.monotonic() + 8
                screen = ""
                while time.monotonic() < deadline:
                    screen = run("capture-pane", "-p", "-t", "fixture:0.0")
                    if predicate(screen):
                        return screen
                    time.sleep(0.05)
                self.fail("Timed out waiting for completion screen:\n" + screen)

            candidates = " ".join(
                f"{'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'[number]}-option-{number:02d}"
                for number in range(count)
            )
            rc = home / "fixture.zsh"
            rc.write_text(chr(10).join([
                "export PATH=" + shlex.quote(environment["PATH"]),
                "bindkey -e", 'autoload -Uz compinit; compinit -d "$HOME/compdump"',
                "source " + shlex.quote(self.fixture["fzfTab"]),
                "source " + shlex.quote(str(ROOT / "files/zsh/fzf-preview.zsh")),
                self.fixture["fzfFlags"],
                "_fixture_complete() { local -a values=(" + candidates + "); "
                "_describe -t fixture-items 'Fixture entries' values; }",
                "fixture-cmd() { :; }", "compdef _fixture_complete fixture-cmd",
                # Synthetic preview exercises the layout without reading host files.
                "zstyle ':fzf-tab:complete:*:*' fzf-preview 'print PREVIEW_OK'",
                "PROMPT='READY> '",
            ]))
            try:
                run("new-session", "-d", "-s", "fixture", "-x", str(width), "-y", str(height),
                    shlex.quote(self.fixture["zsh"]) + " -dfi")
                run("send-keys", "-t", "fixture:0.0", "source " + shlex.quote(str(rc)), "Enter")
                screen_until(lambda screen: "READY>" in screen)
                run("send-keys", "-t", "fixture:0.0", "fixture-cmd ", "Tab")
                # Capture only the current pane, never scrollback or raw output history.
                expected = min(count, 3 if height == 12 else 10)
                screen = screen_until(lambda screen: len(set(re.findall(r"option-\d{2}", screen))) >= expected)
                visible = set(re.findall(r"option-\d{2}", screen))
                self.assertGreaterEqual(len(visible), expected, screen)
                screen = screen_until(lambda screen: "PREVIEW_OK" in screen)
                self.assertGreaterEqual(len(set(re.findall(r"option-\d{2}", screen))), expected, screen)
                preview_line = next(line for line in screen.splitlines() if "PREVIEW_OK" in line)
                self.assertGreater(preview_line.index("PREVIEW_OK"), width // 3, screen)
            finally:
                try:
                    # Let fzf finish its temporary-file writes before removing HOME.
                    closed = subprocess.run(
                        [tmux, "-S", socket, "send-keys", "-t", "fixture:0.0", "Escape"],
                        env=environment, capture_output=True, timeout=5, check=False,
                    )
                    if closed.returncode == 0:
                        screen_until(lambda screen: not re.search(r"option-\d{2}", screen))
                finally:
                    subprocess.run([tmux, "-S", socket, "kill-server"], env=environment,
                                   capture_output=True, timeout=5, check=False)

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

    def test_real_systemctl_completion_expands_system_and_user_service_names(self):
        with tempfile.TemporaryDirectory(prefix="envy-systemctl-tab-") as directory:
            home = Path(directory).resolve()
            binary = home / "bin"
            binary.mkdir()
            systemctl = binary / "systemctl"
            systemctl.write_text(
                "#!" + self.fixture["zsh"] + "\n"
                'print -r -- "$*" >> "$HOME/systemctl-calls"\n'
                'case " $* " in\n'
                '  *" list-units "*|*" list-unit-files "*) '
                "print -r -- 'envy-fixture.service loaded active running' ;;\n"
                '  *" show "*) print -rl -- "Id=envy-fixture.service" "CanStop=yes" ;;\n'
                '  *) print -u2 UNEXPECTED_SYSTEMCTL_EXECUTION; exit 99 ;;\nesac\n'
            )
            systemctl.chmod(0o755)
            path = os.pathsep.join([str(binary), str(Path(self.fixture["zsh"]).parent), "/usr/bin"])
            rc = home / "fixture.zsh"
            rc.write_text("\n".join([
                "export PATH=" + shlex.quote(path),
                "bindkey -e",
                "fpath=(" + shlex.quote(self.fixture["systemctlCompletion"]) + " $fpath)",
                "autoload -Uz compinit; compinit -D -i",
                "source " + shlex.quote(str(ROOT / "files/zsh/func.zsh")),
                'sudo() { print -u2 UNEXPECTED_SUDO_EXECUTION; return 99; }',
                '_fixture_inspect() { print -r -- "BUFFER=$BUFFER"; zle reset-prompt; }',
                "zle -N _fixture_inspect; bindkey '^X' _fixture_inspect",
                "PROMPT='SERVICE> '", "print -r -- ENVY_SERVICE_READY",
            ]))
            process, master = self.launch([self.fixture["zsh"], "-dfi"], home, extra={"PATH": path})
            try:
                os.write(master, ("source " + shlex.quote(str(rc)) + "\r").encode())
                startup = self.read_until(master, b"SERVICE> ")
                self.assertNotIn("command not found", startup)
                for wrapper, action in (("sc", "st"), ("usc", "st"), ("sc", "stop")):
                    command = wrapper + " " + action
                    os.write(master, (command + " envy-fixt\t").encode())
                    completed = self.read_until(master, b"envy-fixture.service")
                    self.assertIn("envy-fixture.service", completed)
                    os.write(master, b"\x18")
                    inspected = self.read_until(master, b"BUFFER=" + command.encode() + b" envy-fixture.service")
                    self.assertIn("BUFFER=" + command + " envy-fixture.service", inspected)
                    self.assertNotIn("function definition file not found", completed + inspected)
                    self.assertNotIn("UNEXPECTED_SYSTEMCTL_EXECUTION", completed + inspected)
                    self.assertNotIn("UNEXPECTED_SUDO_EXECUTION", completed + inspected)
                    os.write(master, b"\x15")
                queries = (home / "systemctl-calls").read_text().splitlines()
                self.assertTrue(any("--user" in query for query in queries))
                self.assertTrue(any("--system" in query for query in queries))
                self.assertTrue(any(" show " in query for query in queries))
                os.write(master, b"exit\r")
                self.read_until(master, None, timeout=2)
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)
            finally:
                self.close(process, master)

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

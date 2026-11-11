import json
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))


class TerminalThemeConfigTests(unittest.TestCase):
    def test_bat_theme_does_not_install_through_home_manager(self):
        text = (ROOT / "modules/cores/base.nix").read_text()
        self.assertNotIn("programs.bat", text)
        self.assertIn("\"bat/config\" = lib.mkIf batSelected", text)
        self.assertIn("themeCacheNeeded = batSelected || builtins.elem \"delta\" selectedPackages;", text)
        self.assertIn("home.activation.batCache = lib.mkIf themeCacheNeeded", text)
        self.assertIn("--theme=tokyonight_night", text)
        self.assertIn("/extras/sublime/tokyonight_night.tmTheme", text)
        self.assertIn("config.xdg.configHome", text)
        self.assertIn("config.xdg.cacheHome", text)
        self.assertIn('cd "${pkgs.emptyDirectory}"', text)

    def test_btop_keeps_envy_package_ownership(self):
        text = (ROOT / "modules/cores/base.nix").read_text()
        self.assertRegex(text, r"programs\.btop = \{\s*enable = btopSelected;\s*package = null;")
        self.assertIn('color_theme = "tokyonight_night";', text)
        self.assertIn("theme_background = false;", text)
        self.assertIn("/extras/btop/tokyonight_night.theme", text)

    def test_tmux_adapts_colors_without_replacing_status_formats(self):
        text = (ROOT / "modules/cores/tmux.nix").read_text()
        self.assertIn("${themeColors}", text)
        self.assertNotIn("/extras/tmux/", text)
        self.assertIn('tmux_conf_theme_status_left=" #S | #{pretty_pane_current_path} "', text)
        self.assertIn('tmux_conf_theme_status_right=" #{prefix}#{mouse}#{synchronized} | %F %R "', text)
        self.assertIn("set -g prefix2 None #!important", text)
        self.assertIn("unbind -n C-l #!important", text)

    def test_vscode_local_profile_has_the_matching_theme_extension(self):
        text = (ROOT / "modules/devps/vscode.nix").read_text()
        self.assertIn("profiles.default = lib.mkIf useLocalConfig", text)
        self.assertIn('"workbench.colorTheme" = "Tokyo Night";', text)
        self.assertIn("enkia.tokyo-night", text)
        self.assertNotIn('"workbench.colorTheme" = "Tokyo Night Storm";', text)

    def test_dashboard_uses_shared_palette_and_respects_btop_selection(self):
        text = (ROOT / "modules/desktops/darwin/terminal.nix").read_text()
        self.assertIn("] ++ lib.optional btopSelected ({", text)
        self.assertRegex(text, r'Name = "BtopDashboard";[\s\S]+?\} // terminalColors\);')
        self.assertIn('"Transparency" = 0.0;', text)
        self.assertIn('"Blur" = false;', text)


class ThemeReadabilityTests(unittest.TestCase):
    @staticmethod
    def contrast(first, second):
        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        darker, lighter = sorted((luminance(first), luminance(second)))
        return (lighter + 0.05) / (darker + 0.05)

    def test_fzf_info_meets_reference_text_contrast(self):
        text = (ROOT / "modules/cores/tokyo-night.nix").read_text()
        finder = re.search(r"fzf = \{(.*?)\n  \};", text, re.S).group(1)
        background = re.search(r'bg = "(#[0-9a-f]{6})";', finder).group(1)
        info = re.search(r'info = "(#[0-9a-f]{6})";', finder).group(1)
        self.assertGreaterEqual(self.contrast(info, background), 4.5)

    def test_default_terminal_foreground_meets_reference_text_contrast(self):
        text = (ROOT / "modules/cores/tokyo-night.nix").read_text()
        background = re.search(r'background = "(#[0-9a-f]{6})";', text).group(1)
        foreground = re.search(r'foreground = "(#[0-9a-f]{6})";', text).group(1)
        self.assertGreaterEqual(self.contrast(foreground, background), 4.5)


class BatThemeRuntimeTests(unittest.TestCase):
    def test_locked_theme_loads_in_an_isolated_cache(self):
        source = os.environ.get("ENVY_TOKYONIGHT_SOURCE")
        binary = os.environ.get("ENVY_BAT_BIN") or shutil.which("bat")
        if not source or not binary:
            self.skipTest("Set ENVY_TOKYONIGHT_SOURCE and provide bat for the runtime theme check")
        theme = Path(source) / "extras/sublime/tokyonight_night.tmTheme"
        self.assertTrue(theme.is_file(), theme)
        with tempfile.TemporaryDirectory(prefix="envy-bat-theme-") as directory:
            root = Path(directory)
            config = root / "config/bat"
            (config / "themes").mkdir(parents=True)
            (root / "home").mkdir()
            shutil.copyfile(theme, config / "themes" / theme.name)
            (config / "config").write_text("--theme=tokyonight_night\n")
            env = {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
            }
            cache = subprocess.run(
                [binary, "cache", "--build"], cwd=root, env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(cache.returncode, 0, cache.stderr)
            themes = subprocess.run(
                [binary, "--list-themes"], cwd=root, env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(themes.returncode, 0, themes.stderr)
            self.assertIn("tokyonight_night", themes.stdout)
            preview = subprocess.run(
                [binary, "--color=always", "--paging=never", "--style=plain", "--language=python"],
                input='print("中文 theme")\n', cwd=root, env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("\x1b[", preview.stdout)
            plain = re.sub(r"\x1b\[[0-9;]*m", "", preview.stdout)
            self.assertIn('print("中文 theme")', plain)


class TmuxThemeRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = os.environ.get("ENVY_TERMINAL_THEME_FIXTURES")
        self.binary = shutil.which("tmux")
        if not fixture or not self.binary:
            message = "Provide generated terminal fixtures and tmux"
            if os.environ.get("ENVY_REQUIRE_TMUX_TESTS") == "1":
                self.fail(message)
            self.skipTest(message)
        self.configurations = json.loads(Path(fixture).read_text())["selectedCases"]
        self.assertTrue(self.configurations, "No selected tmux configurations were tested")
        self.shell = shutil.which("sh")
        self.assertIsNotNone(self.shell)

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(message)

    @contextmanager
    def tmux(self, configuration, terminal_env=None):
        with tempfile.TemporaryDirectory(prefix="envy-tmux-theme-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = Path(configuration["tmuxSource"])
            self.assertTrue(source.is_file(), source)
            shutil.copyfile(source, home / ".tmux.conf")
            (home / ".tmux.conf.local").write_text(configuration["tmuxConfig"])
            # Always intercept native copying, including on Darwin test hosts.
            binaries = root / "bin"
            binaries.mkdir()
            copier = binaries / "pbcopy"
            copier.write_text(f"#!{self.shell}\ncat > {shlex.quote(str(root / 'copied'))}\n")
            copier.chmod(0o755)
            env = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                "SHELL": self.shell,
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
                **(terminal_env or {}),
            }
            command = [self.binary, "-S", str(root / "socket")]

            def invoke(*arguments, check=True):
                result = subprocess.run(
                    [*command, *arguments], cwd=root, env=env,
                    capture_output=True, text=True, timeout=15,
                )
                if check:
                    self.assertEqual(result.returncode, 0, result.stderr)
                return result

            try:
                started = invoke("-f", str(home / ".tmux.conf"), "new-session", "-d", "-s", "theme-test", self.shell)
                self.assertEqual(started.stderr, "")
                yield root, invoke, env
            finally:
                invoke("kill-server", check=False)

    @staticmethod
    def option(invoke, name):
        return invoke("show-options", "-gv", name).stdout.strip()

    def test_generated_colors_preserve_status_and_key_behavior(self):
        for name, configuration in self.configurations.items():
            with self.subTest(machine=name), self.tmux(configuration) as (root, invoke, _):
                self.wait_for(lambda: "#1a1b26" in self.option(invoke, "status-style"), "Theme was not applied")
                self.assertIn("#a9b1d6", self.option(invoke, "status-style"))
                self.assertIn("#7aa2f7", self.option(invoke, "pane-active-border-style"))
                self.assertIn("#S", self.option(invoke, "status-left"))
                self.assertIn("pane_current_path", self.option(invoke, "status-left"))
                self.assertIn("client_prefix", self.option(invoke, "status-right"))
                self.assertIn("%F %R", self.option(invoke, "status-right"))
                self.assertEqual(self.option(invoke, "prefix2"), "None")
                self.assertEqual(self.option(invoke, "default-terminal"), "tmux-256color")
                self.assertEqual(self.option(invoke, "set-clipboard"), "on")
                self.assertEqual(self.option(invoke, "allow-passthrough"), "on")
                for key in ("C-a", "C-l"):
                    self.assertNotEqual(invoke("list-keys", "-T", "root", key, check=False).returncode, 0)
                for key in ("C-S-H", "C-S-L"):
                    self.assertIn("swap-window", invoke("list-keys", "-T", "prefix", key).stdout)

                invoke("send-keys", "-l", "printf '\\nenvy-copy-marker\\n'")
                invoke("send-keys", "Enter")
                self.wait_for(
                    lambda: "envy-copy-marker" in invoke("capture-pane", "-p").stdout,
                    "Copy marker did not reach the pane",
                )
                invoke("copy-mode")
                for action in ("history-top", "begin-selection", "history-bottom", "end-of-line"):
                    invoke("send-keys", "-X", action)
                binding = shlex.split(invoke("list-keys", "-T", "copy-mode-vi", "y").stdout)
                action = binding[binding.index("send-keys") + 1:]
                self.assertIn("copy-pipe-and-cancel", action)
                self.assertIn("pbcopy", action)
                invoke("send-keys", *action)
                copied = root / "copied"
                self.wait_for(lambda: copied.exists() and "envy-copy-marker" in copied.read_text(), "Copy action did not reach the isolated clipboard")

    def test_extended_keys_are_stable_across_startup_and_reload(self):
        environments = ({"TERM_PROGRAM": "iTerm.app"}, {"TERM": "alacritty", "TERM_PROGRAM": "Alacritty"}, {})
        for name, configuration in self.configurations.items():
            for environment in environments:
                with self.subTest(machine=name, terminal=environment), self.tmux(configuration, environment) as (root, invoke, _):
                    for reload in (False, True):
                        if reload:
                            invoke("set-option", "-ag", "update-environment", "ENVY_TEST_CONTEXT")
                            invoke("source-file", str(root / "home/.tmux.conf"))
                            self.assertIn("ENVY_TEST_CONTEXT", self.option(invoke, "update-environment").splitlines())
                        self.assertEqual(self.option(invoke, "extended-keys"), "on")
                        self.assertEqual(self.option(invoke, "extended-keys-format"), "csi-u")
                        variables = self.option(invoke, "update-environment").splitlines()
                        for variable in ("DISPLAY", "SSH_AUTH_SOCK", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
                            self.assertEqual(variables.count(variable), 1)

    def test_application_receives_modified_enter_after_requesting_extended_keys(self):
        probe_text = r'''
import os
import sys
import tty

tty.setraw(0)
sys.stdout.write("\x1b[>4;2m\x1b]2;" + sys.argv[2] + "\x07")
sys.stdout.flush()
with open(sys.argv[1], "ab", buffering=0) as output:
    while True:
        data = os.read(0, 1024)
        if not data:
            break
        output.write(data)
'''
        for name, configuration in self.configurations.items():
            with self.subTest(machine=name), self.tmux(configuration, {"TERM_PROGRAM": "Alacritty"}) as (root, invoke, _):
                probe = root / "keys.py"
                probe.write_text(probe_text)
                for mode, expected in ((None, b"\x1b[13;2u\x1b[13;3u\r"), ("off", b"\r\x1b\r\r")):
                    if mode:
                        invoke("set-option", "-s", "extended-keys", mode)
                    received = root / ("configured.keys" if mode is None else "off.keys")
                    title = "envy-keys-" + (mode or "configured")
                    command = shlex.join([sys.executable, str(probe), str(received), title])
                    invoke("respawn-pane", "-k", command)
                    self.wait_for(lambda: invoke("display-message", "-p", "#{pane_title}").stdout.strip() == title, "Key request was not parsed")
                    invoke("send-keys", "S-Enter", "M-Enter", "Enter")
                    self.wait_for(lambda: received.exists() and received.stat().st_size >= len(expected), "Expected key bytes did not reach the application")
                    self.assertEqual(received.read_bytes(), expected)

    def test_wayland_environment_refreshes_on_attach(self):
        for name, configuration in self.configurations.items():
            with self.subTest(machine=name), self.tmux(configuration, {"WAYLAND_DISPLAY": "wayland-old", "XDG_SESSION_TYPE": "x11"}) as (root, invoke, env):
                env.update(WAYLAND_DISPLAY="wayland-new", XDG_SESSION_TYPE="wayland")
                master, slave = pty.openpty()
                termios.tcsetwinsize(slave, (24, 80))
                client = None
                try:
                    client = subprocess.Popen(
                        [self.binary, "-S", str(root / "socket"), "attach-session", "-t", "theme-test"],
                        cwd=root, env=env, stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                        start_new_session=True,
                    )

                    def attached():
                        if select.select([master], [], [], 0)[0]:
                            os.read(master, 65536)
                        return bool(invoke("list-clients", "-F", "#{client_name}").stdout.strip())

                    self.wait_for(attached, "Isolated terminal client did not attach")
                    self.assertEqual(invoke("show-environment", "-t", "theme-test", "WAYLAND_DISPLAY").stdout.strip(), "WAYLAND_DISPLAY=wayland-new")
                    self.assertEqual(invoke("show-environment", "-t", "theme-test", "XDG_SESSION_TYPE").stdout.strip(), "XDG_SESSION_TYPE=wayland")
                finally:
                    if client is not None:
                        invoke("detach-client", "-s", "theme-test", check=False)
                        try:
                            client.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            client.kill()
                            client.communicate(timeout=5)
                    os.close(slave)
                    os.close(master)

    def test_amux_preserves_session_management_with_the_shared_key_policy(self):
        configuration = {"tmuxSource": str(ROOT / "modules/agents/amux/image/tmux.conf"), "tmuxConfig": ""}
        with self.tmux(configuration) as (_, invoke, _):
            self.assertEqual(self.option(invoke, "escape-time"), "10")
            self.assertEqual(self.option(invoke, "extended-keys"), "on")
            self.assertEqual(self.option(invoke, "extended-keys-format"), "csi-u")
            self.assertEqual(self.option(invoke, "prefix"), "C-b")
            self.assertEqual(self.option(invoke, "window-size"), "latest")
            for key, status in (("s", 12), ("(", 11), (")", 10)):
                self.assertIn(f"exit {status}", invoke("list-keys", "-T", "prefix", key).stdout)
            self.assertIn("detach-client", invoke("list-keys", "-T", "root", "User0").stdout)

if __name__ == "__main__":
    unittest.main()

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
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
    def test_generated_colors_preserve_status_and_key_behavior(self):
        fixture = os.environ.get("ENVY_TERMINAL_THEME_FIXTURES")
        source = os.environ.get("ENVY_OHMY_TMUX_SOURCE")
        binary = shutil.which("tmux")
        if not fixture or not source or not binary:
            self.skipTest("Provide generated theme fixtures, ENVY_OHMY_TMUX_SOURCE and tmux")
        configuration = next(iter(json.loads(Path(fixture).read_text())["selectedCases"].values()))
        with tempfile.TemporaryDirectory(prefix="envy-tmux-theme-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            shutil.copyfile(Path(source) / ".tmux.conf", home / ".tmux.conf")
            (home / ".tmux.conf.local").write_text(configuration["tmuxConfig"])
            env = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SHELL": "/bin/sh",
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
            }
            command = [binary, "-S", str(root / "socket")]

            def invoke(*arguments):
                return subprocess.run(
                    [*command, *arguments], cwd=root, env=env,
                    capture_output=True, text=True, timeout=15,
                )

            def option(name):
                result = invoke("show-options", "-gv", name)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            try:
                started = invoke("-f", str(home / ".tmux.conf"), "new-session", "-d", "-s", "theme-test", "/bin/sh")
                self.assertEqual(started.returncode, 0, started.stderr)
                # Oh My Tmux applies its generated colors in a background job.
                for _ in range(50):
                    if "#1a1b26" in option("status-style"):
                        break
                    time.sleep(0.1)
                self.assertIn("#1a1b26", option("status-style"))
                self.assertIn("#a9b1d6", option("status-style"))
                self.assertIn("#7aa2f7", option("pane-active-border-style"))
                self.assertIn("#S", option("status-left"))
                self.assertIn("pane_current_path", option("status-left"))
                self.assertIn("client_prefix", option("status-right"))
                self.assertIn("%F %R", option("status-right"))
                self.assertEqual(option("prefix2"), "None")
                self.assertEqual(option("default-terminal"), "tmux-256color")
                self.assertEqual(option("set-clipboard"), "on")
                self.assertEqual(option("allow-passthrough"), "on")
                root_binding = invoke("list-keys", "-T", "root", "C-l")
                self.assertNotEqual(root_binding.returncode, 0)
            finally:
                invoke("kill-server")


if __name__ == "__main__":
    unittest.main()

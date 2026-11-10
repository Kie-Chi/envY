import errno
import json
import pty
import os
import select
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


class HistoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture = os.environ.get("ENVY_HISTORY_INTEGRATION_FIXTURES")
        if not fixture:
            self.skipTest("Provide evaluated history integration fixtures")
        self.fixture = json.loads(Path(fixture).read_text())

    def test_single_install_owner_and_exclusions_remove_integrations(self):
        selected, excluded = self.fixture["selected"], self.fixture["none"]
        for name in ("delta", "atuin"):
            self.assertEqual(selected["counts"][name], 1)
            self.assertEqual(excluded["counts"][name], 0)
            self.assertTrue(selected[name + "Config"])
            self.assertFalse(excluded[name + "Config"])
        self.assertEqual(set(selected["pager"]), {"diff", "show", "log"})
        self.assertTrue(all(command.endswith("/bin/delta") for command in selected["pager"].values()))
        self.assertEqual(excluded["pager"], {})
        self.assertNotIn("/bin/atuin init zsh", excluded["initContent"])
        self.assertIn("/bin/fzf --zsh", excluded["initContent"])
        for case in ("selected", "none", "withoutBat", "withoutFzf"):
            self.assertFalse(self.fixture[case]["service"])
            self.assertFalse(self.fixture[case]["daemon"])

    def test_generated_widget_order_and_fzf_independence(self):
        init = self.fixture["selected"]["initContent"]
        tokens = ("zsh-autosuggestions.zsh", "/bin/fzf --zsh", "/bin/atuin init zsh --disable-up-arrow",
                  "/share/fzf-tab/fzf-tab.plugin.zsh", "zsh-syntax-highlighting.zsh")
        positions = [init.index(token) for token in tokens]
        self.assertEqual(positions, sorted(positions))
        without_fzf = self.fixture["withoutFzf"]["initContent"]
        self.assertIn("/bin/atuin init zsh --disable-up-arrow", without_fzf)
        self.assertNotIn("/bin/fzf --zsh", without_fzf)
        self.assertNotIn("/share/fzf-tab/", without_fzf)
        self.assertNotRegex(init, r"atuin (import|login|register|sync|daemon)")

    def test_shared_theme_cache_does_not_install_excluded_bat(self):
        case = self.fixture["withoutBat"]
        self.assertEqual(case["counts"]["bat"], 0)
        self.assertFalse(case["batConfig"])
        self.assertTrue(case["themeCache"])
        delta = os.environ.get("ENVY_DELTA_BIN")
        if not delta:
            self.skipTest("Provide locked delta for the shared cache runtime check")
        with tempfile.TemporaryDirectory(prefix="envy-delta-cache-") as directory:
            root = Path(directory).resolve()
            config = root / "config"
            theme = config / "bat/themes/tokyonight_night.tmTheme"
            theme.parent.mkdir(parents=True)
            shutil.copyfile(self.fixture["theme"], theme)
            cache = root / "cache"
            script = self.fixture["cacheScript"].replace(self.fixture["configHome"], str(config))
            script = script.replace(self.fixture["cacheHome"], str(cache))
            environment = {
                "HOME": str(root), "XDG_CONFIG_HOME": str(config), "XDG_CACHE_HOME": str(cache),
                "PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "LANG": "en_US.UTF-8",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            }
            result = subprocess.run(["/bin/bash", "-e", "-c", 'run() { "$@"; }; verboseEcho() { :; }; ' + script],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((cache / "bat/themes.bin").is_file())
            self.assertFalse((config / "bat/config").exists())
            delta_config = root / "delta-config"
            delta_config.write_text(self.fixture["deltaConfig"])
            result = subprocess.run([delta, "--config", str(delta_config), "--show-config"],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"syntax-theme\s*=\s*TokyoNight")
            git = os.environ.get("ENVY_GIT_BIN") or shutil.which("git")
            self.assertIsNotNone(git)
            (root / "before.py").write_text("value = '旧内容'\n")
            (root / "after.py").write_text("value = '新内容'\n")
            # libgit2 does not consume Git's GIT_CONFIG_COUNT include directives.
            # Use private files to exercise the same include/pager declarations as Home Manager.
            global_config = root / ".gitconfig"
            global_config.write_text('[include]\n    path = ' + json.dumps(str(delta_config))
                                     + '\n[pager]\n' + ''.join('    ' + key + ' = ' + json.dumps(value) + '\n'
                                                                  for key, value in self.fixture["selected"]["pager"].items()))
            environment.update(COLORTERM="truecolor", DELTA_PAGER="/bin/cat",
                               GIT_CONFIG_GLOBAL=str(global_config))
            master, slave = pty.openpty()
            process = subprocess.Popen([git, "diff", "--no-index", "--", "before.py", "after.py"],
                                       cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            output = bytearray()
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if not select.select([master], [], [], 0.1)[0]:
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
                    output.extend(chunk)
                process.wait(timeout=2)
                self.assertEqual(process.returncode, 1)
                self.assertIn("新内容", output.decode())
                self.assertIn(b"48;2;74;39;47", output)
                self.assertIn(b"48;2;36;62;74", output)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                os.close(master)


if __name__ == "__main__":
    unittest.main()

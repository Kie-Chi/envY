import errno
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

import test_shell_completion as completion


FIXTURES = Path(__file__).resolve().parent / "fixtures"
SESSION_A = "11111111111141118111111111111111"
SESSION_B = "22222222222242228222222222222222"


class AtuinPilotTests(unittest.TestCase):
    def setUp(self):
        self.atuin = os.environ.get("ENVY_ATUIN_BIN")
        if not self.atuin:
            self.skipTest("Set ENVY_ATUIN_BIN to the locked package for the isolated pilot")
        if sys.platform != "darwin" or not shutil.which("cc"):
            self.skipTest("This pilot's network/file sandbox is verified on macOS only")
        self.directory = tempfile.TemporaryDirectory(prefix="envy-atuin-pilot-")
        self.home = Path(self.directory.name).resolve()
        self.addCleanup(self.cleanup_fixture)
        self.config_dir = self.home / "config/atuin"
        self.config_dir.mkdir(parents=True)
        self.data_dir = self.home / "data/atuin"
        self.work_a, self.work_b = self.home / "目录 A", self.home / "目录 B"
        self.work_a.mkdir()
        self.work_b.mkdir()
        self.network_audit = self.home / "network.log"
        self.library = self.home / "network-guard.dylib"
        self.launcher = self.home / "guard-launcher"
        self.probe = self.home / "network-probe"
        self.environment = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"), "XDG_CACHE_HOME": str(self.home / "cache"),
            "XDG_STATE_HOME": str(self.home / "state"), "TMPDIR": str(self.home),
            "ATUIN_CONFIG_DIR": str(self.config_dir), "ATUIN_SESSION": SESSION_A,
            "USER": "fixture-user", "LOGNAME": "fixture-user", "SHELL": "/bin/zsh",
            "PATH": os.pathsep.join([str(Path(self.atuin).parent), "/usr/bin", "/bin"]),
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TERM": "xterm-256color",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "ENVY_GUARD_LIBRARY": str(self.library), "ENVY_NETWORK_AUDIT": str(self.network_audit),
            "ATUIN_TMUX_POPUP": "false",
        }
        compiler_environment = {key: self.environment[key] for key in ("HOME", "TMPDIR", "PATH", "LANG")}
        for source, target, flags in (
            ("atuin_network_guard.c", self.library, ["-dynamiclib"]),
            ("atuin_guard_launcher.c", self.launcher, []),
            ("atuin_network_probe.c", self.probe, []),
        ):
            subprocess.run(["/usr/bin/cc", *flags, "-Wall", "-Wextra", "-Werror", str(FIXTURES / source), "-o", str(target)],
                           cwd=self.home, env=compiler_environment, check=True, capture_output=True, timeout=30)
        self.profile = '(version 1) (allow default) (deny network*) (deny file-read* file-write* (subpath ' + json.dumps(str(Path.home())) + '))'
        values = {
            "data_dir": str(self.data_dir), "db_path": str(self.data_dir / "history.db"),
            "record_store_path": str(self.data_dir / "records.db"), "key_path": str(self.data_dir / "key"),
            "session_path": str(self.data_dir / "session"), "auto_sync": False, "update_check": False,
            "enter_accept": False, "filter_mode": "global", "style": "compact", "inline_height": 10,
            "exit_mode": "return-original", "prefers_reduced_motion": True,
            "network_connect_timeout": 1, "network_timeout": 1,
        }
        text = "\n".join(key + " = " + json.dumps(value) for key, value in values.items())
        text += '\n[sync]\nrecords = false\n[daemon]\nenabled = false\nautostart = false\n[logs]\nenabled = false\n[theme]\nname = "envy-night"\ndebug = false\n'
        self.config = self.config_dir / "config.toml"
        self.config.write_text(text)
        self.config.chmod(0o600)
        themes = self.config_dir / "themes"
        themes.mkdir()
        (themes / "envy-night.toml").write_text('''[theme]
name = "envy-night"
[colors]
Base = "@reset"
Annotation = "@reset"
Muted = "@reset"
AlertInfo = "@cyan"
AlertWarn = "@yellow"
AlertError = "@red"
Guidance = "@green"
Important = "@blue"
Title = "@magenta"
''')
        fixture = os.environ.get("ENVY_HISTORY_INTEGRATION_FIXTURES")
        self.integration_fixture = json.loads(Path(fixture).read_text()) if fixture else None
        if self.integration_fixture:
            managed = {"auto_sync", "update_check", "enter_accept", "exit_mode"}
            private = "\n".join(key + " = " + json.dumps(value) for key, value in values.items()
                                if key not in managed)
            self.config.write_text(private + "\n" + self.integration_fixture["atuinConfig"])
            (themes / "envy-night.toml").write_text(self.integration_fixture["atuinTheme"])
        self.original_config = self.config.read_bytes()
        self.rows = [
            ("printf '试点 中文 🌙'", self.work_a, SESSION_A, 0),
            ("printf '合成 带空格'", self.work_b, SESSION_A, 7),
            ("echo '会话 二'", self.work_a, SESSION_B, 0),
            ("false # fixture-status", self.work_b, SESSION_B, 130),
        ]
        for command, cwd, session, status in self.rows:
            environment = dict(self.environment, ATUIN_SESSION=session)
            result = self.run_cli("history", "start", command, cwd=cwd, environment=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            identifier = result.stdout.strip()
            self.assertEqual(len(identifier), 32)
            result = self.run_cli("history", "end", identifier, "--exit", str(status), "--duration", "1000000",
                                  cwd=cwd, environment=environment)
            self.assertEqual(result.returncode, 0, result.stderr)

    def cleanup_fixture(self):
        self.directory.cleanup()
        self.assertFalse(self.home.exists())

    def tearDown(self):
        if hasattr(self, "original_config"):
            self.assertEqual(self.config.read_bytes(), self.original_config)
            self.assertFalse((self.data_dir / "session").exists())
            self.assertFalse((self.data_dir / "atuin.sock").exists())
            self.assertFalse((self.data_dir / "atuin-daemon.pid").exists())
            attempts = self.network_audit.read_text() if self.network_audit.exists() else ""
            self.assertEqual(attempts, "", "Atuin attempted network access")

    def isolated_arguments(self, executable, *arguments):
        return ["/usr/bin/sandbox-exec", "-p", self.profile, str(self.launcher), str(executable), *arguments]

    def run_cli(self, *arguments, cwd=None, environment=None):
        return subprocess.run(self.isolated_arguments(self.atuin, *arguments), cwd=cwd or self.work_a,
                              env=environment or self.environment, capture_output=True, text=True, timeout=15)

    def commands(self, *arguments, **kwargs):
        result = self.run_cli(*arguments, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return set(result.stdout.splitlines())

    def test_settings_and_data_paths_are_private_and_offline(self):
        for key, expected in {"auto_sync": "false", "update_check": "false", "daemon.enabled": "false",
                              "daemon.autostart": "false", "sync.records": "false", "enter_accept": "false"}.items():
            result = self.run_cli("config", "get", key)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        for key in ("data_dir", "db_path", "record_store_path", "key_path", "session_path"):
            result = self.run_cli("config", "get", key)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(Path(result.stdout.strip()).is_relative_to(self.home))
        self.assertTrue((self.data_dir / "history.db").is_file())

    def test_directory_and_session_filters_use_only_synthetic_history(self):
        self.assertEqual(self.commands("search", "--cwd", str(self.work_a), "--format", "{command}"),
                         {self.rows[0][0], self.rows[2][0]})
        self.assertEqual(self.commands("history", "list", "--session", "--format", "{command}"),
                         {self.rows[0][0], self.rows[1][0]})
        environment = dict(self.environment, ATUIN_SESSION=SESSION_B)
        self.assertEqual(self.commands("history", "list", "--session", "--format", "{command}", environment=environment),
                         {self.rows[2][0], self.rows[3][0]})

    def test_exit_status_chinese_space_and_cli_errors(self):
        self.assertEqual(self.commands("search", "--exit", "7", "--format", "{exit}|{command}"),
                         {"7|" + self.rows[1][0]})
        self.assertEqual(self.commands("search", "--exit", "130", "--format", "{exit}|{command}"),
                         {"130|" + self.rows[3][0]})
        self.assertEqual(self.commands("search", "--format", "{command}", "试点"), {self.rows[0][0]})
        invalid = self.run_cli("--fixture-invalid-option")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("unexpected argument", invalid.stderr)

    def test_guard_denies_and_records_probes_and_is_loaded_by_atuin(self):
        audit = self.home / "positive-audit.log"
        environment = dict(self.environment, ENVY_NETWORK_AUDIT=str(audit))
        result = subprocess.run(self.isolated_arguments(self.probe), cwd=self.home, env=environment,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(audit.read_text().splitlines(), ["socket", "connect", "getaddrinfo"])
        loaded = self.home / "loaded-library.log"
        result = self.run_cli("--version", environment=dict(self.environment, ENVY_GUARD_LOAD_AUDIT=str(loaded)))
        self.assertEqual(result.returncode, 0)
        self.assertIn("18.15.2", result.stdout)
        self.assertEqual(loaded.read_text().splitlines(), ["atuin"])

    def test_generated_hooks_capture_only_new_synthetic_commands(self):
        if not self.integration_fixture:
            self.skipTest("Provide evaluated integration for the real shell capture test")
        zsh = os.environ.get("ENVY_ZSH_BIN") or shutil.which("zsh")
        if not zsh:
            self.skipTest("Provide Zsh for shell history capture")
        pattern = (r"if \[\[ \$options\[zle\] = on \]\]; then\n"
                   r"\s*typeset -i _envy_had_suggest_strategy=.*?"
                   r"unset _envy_had_suggest_strategy _envy_suggest_strategy\n\s*fi")
        match = re.search(pattern, self.integration_fixture["selected"]["initContent"], re.S)
        self.assertIsNotNone(match)
        script = self.home / "capture.zsh"
        script.write_text("bindkey -e\nHISTFILE=''\n" + match.group()
                          + "\nPS1='_CAPTURE> ' RPROMPT=''\n")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

        def session():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(self.isolated_arguments(zsh, "-dfi"), cwd=self.work_a,
                                   env=self.environment, stdin=slave, stdout=slave, stderr=slave,
                                   preexec_fn=session)
        os.close(slave)
        command = "(print -r -- ENVY_CAPTURE_ERROR; exit 7)"
        try:
            os.write(master, f"source {shlex.quote(str(script))}\n".encode())
            self.assertIn("_CAPTURE>", self.read_terminal(master, b"_CAPTURE> "))
            os.write(master, (command + "\n").encode())
            self.read_terminal(master, b"_CAPTURE> ")
            for _ in range(30):
                result = self.run_cli("search", "--exit", "7", "--format", "{command}", "ENVY_CAPTURE_ERROR")
                self.assertEqual(result.returncode, 0, result.stderr)
                if command in result.stdout.splitlines():
                    break
                time.sleep(0.1)
            self.assertIn(command, result.stdout.splitlines())
            self.assertFalse((self.home / ".zsh_history").exists())
            os.write(master, b"exit 0\n")
            self.read_terminal(master, None, timeout=1)
            process.wait(timeout=2)
            self.assertEqual(process.returncode, 0)
        finally:
            completion.CompletionRuntimeTests.close(process, master)

    @staticmethod
    def read_terminal(master, marker, timeout=8):
        output = bytearray()
        answered = 0
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
            # Ratatui's inline viewport needs a terminal cursor-position reply.
            requests = output.count(b"\x1b[6n")
            if requests > answered:
                os.write(master, b"\x1b[24;1R" * (requests - answered))
                answered = requests
            if marker is not None and marker in output:
                break
        return output.decode(errors="replace")

    def test_isolated_ctrl_r_selection_escape_and_fzf_binding_restore(self):
        zsh = os.environ.get("ENVY_ZSH_BIN") or shutil.which("zsh")
        fzf = os.environ.get("ENVY_FZF_BIN")
        if not zsh or not fzf:
            self.skipTest("Provide locked Zsh and fzf for the isolated Ctrl-R test")
        init = self.run_cli("init", "zsh", "--disable-up-arrow")
        self.assertEqual(init.returncode, 0, init.stderr)
        native_init = self.home / "atuin-init.zsh"
        if self.integration_fixture:
            pattern = (r"if \[\[ \$options\[zle\] = on \]\]; then\n"
                       r"\s*typeset -i _envy_had_suggest_strategy=.*?"
                       r"unset _envy_had_suggest_strategy _envy_suggest_strategy\n\s*fi")
            match = re.search(pattern, self.integration_fixture["selected"]["initContent"], re.S)
            self.assertIsNotNone(match, "The generated Atuin integration block is missing")
            native_init.write_text(match.group())
        else:
            native_init.write_text(init.stdout)
        script = self.home / "shell-fixture.zsh"
        script.write_text("\n".join([
            "bindkey -e", "HISTFILE=''", "HISTSIZE=100", "SAVEHIST=0",
            f"source {shlex.quote(str(Path(fzf).parent.parent / 'share/fzf/key-bindings.zsh'))}",
            "_fixture_baseline=$(bindkey '^R')", "_fixture_up=$(bindkey '^[[A')",
            "_fixture_down=$(bindkey '^[[B')",
            "typeset -a ZSH_AUTOSUGGEST_STRATEGY=(history fixture-strategy)",
            "_fixture_suggestions=${(j: :)ZSH_AUTOSUGGEST_STRATEGY}",
            f"source {shlex.quote(str(native_init))}",
            # History capture is tested separately; avoid asynchronous hook writes during UI cleanup.
            "add-zsh-hook -d preexec _atuin_preexec", "add-zsh-hook -d precmd _atuin_precmd",
            "fixture-report() { print -r -- _BUFFER:$BUFFER; zle reset-prompt; }",
            "zle -N fixture-report", "bindkey '^X' fixture-report",
            "PS1='ENVY_ATUIN> '", "RPROMPT=''",
            "print -r -- _BASELINE:$_fixture_baseline", "print -r -- _PILOT:$(bindkey '^R')",
            "[[ $_fixture_up == $(bindkey '^[[A') ]] && print _UP_UNCHANGED",
            "[[ $_fixture_down == $(bindkey '^[[B') ]] && print _DOWN_UNCHANGED",
            "[[ $_fixture_suggestions == \"${(j: :)ZSH_AUTOSUGGEST_STRATEGY}\" ]] && print _SUGGESTIONS_UNCHANGED",
            "print _READY",
        ]))
        environment = dict(self.environment, PATH=os.pathsep.join([str(Path(self.atuin).parent), str(Path(fzf).parent),
                                                                  str(Path(zsh).parent), "/usr/bin", "/bin"]))
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

        def session():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(self.isolated_arguments(zsh, "-dfi"), cwd=self.work_a, env=environment,
                                   stdin=slave, stdout=slave, stderr=slave, preexec_fn=session)
        os.close(slave)
        read = self.read_terminal
        try:
            os.write(master, f"source {shlex.quote(str(script))}\n".encode())
            ready = read(master, b"ENVY_ATUIN> ")
            self.assertIn("fzf-history-widget", ready)
            self.assertIn("atuin-search", ready)
            self.assertIn("_UP_UNCHANGED", ready)
            self.assertIn("_DOWN_UNCHANGED", ready)
            if self.integration_fixture:
                self.assertIn("_SUGGESTIONS_UNCHANGED", ready)
            os.write(master, b"printf\x12")
            opened = read(master, b"18.15.2")
            self.assertIn("18.15.2", opened)
            os.write(master, b"\x1b")
            read(master, b"ENVY_ATUIN>")
            os.write(master, b"\x18")
            restored = read(master, b"_BUFFER:printf")
            self.assertIn("_BUFFER:printf", restored)
            os.write(master, b"\x15\x12")
            read(master, b"18.15.2")
            os.write(master, "试点".encode())
            read(master, "试点".encode())
            os.write(master, b"\t")
            read(master, b"ENVY_ATUIN>")
            os.write(master, b"\x18")
            selected = read(master, ("_BUFFER:" + self.rows[0][0]).encode())
            self.assertIn("_BUFFER:" + self.rows[0][0], selected)
            os.write(master, b"\x15\x12")
            read(master, b"18.15.2")
            os.write(master, "试点".encode())
            read(master, "试点".encode())
            os.write(master, b"\r")
            read(master, b"ENVY_ATUIN>")
            os.write(master, b"\x18")
            self.assertIn("_BUFFER:" + self.rows[0][0],
                          read(master, ("_BUFFER:" + self.rows[0][0]).encode()))
            os.write(master, b"\x15bindkey '^R' fzf-history-widget; [[ $_fixture_baseline == $(bindkey '^R') ]] && print _BINDING_'RESTORED'\n")
            self.assertIn("_BINDING_RESTORED", read(master, b"_BINDING_RESTORED"))
            os.write(master, b"print _NORMAL_'COMMAND_AFTER_UI'\n")
            self.assertIn("_NORMAL_COMMAND_AFTER_UI", read(master, b"_NORMAL_COMMAND_AFTER_UI"))
            os.write(master, b"exit 0\n")
            read(master, None, timeout=1)
            process.wait(timeout=2)
            self.assertEqual(process.returncode, 0)
        finally:
            completion.CompletionRuntimeTests.close(process, master)


if __name__ == "__main__":
    unittest.main()

import errno
import fcntl
import hashlib
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

ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
CONFIG = Path(os.environ.get("ENVY_P10K_CONFIG", ROOT / "files/zsh/p10k.zsh"))
ZSH = os.environ.get("ENVY_ZSH_BIN") or shutil.which("zsh")
GIT = os.environ.get("ENVY_GIT_BIN") or shutil.which("git")


def isolated_environment(home):
    paths = [str(Path(binary).resolve().parent) for binary in (ZSH, GIT) if binary]
    return {
        "HOME": str(home), "ZDOTDIR": str(home), "HISTFILE": "",
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"), "XDG_STATE_HOME": str(home / "state"),
        "PATH": os.pathsep.join(paths + ["/usr/bin", "/bin"]),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TERM": "xterm-256color",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GITSTATUS_AUTO_INSTALL": "0",
    }


class PromptSourceTests(unittest.TestCase):
    def test_original_configuration_is_restored_without_palette_substitution(self):
        text = (ROOT / "modules/cores/shell.nix").read_text()
        self.assertIn('home.file.".p10k.zsh".source = ../../files/zsh/p10k.zsh;', text)
        self.assertNotIn("pkgs.replaceVars", text)
        self.assertNotIn("programs.starship", text)
        self.assertEqual(hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
                         "387190b170afff685c3a4a0f79325cdd1e7ace5d0a38ef4dca44d8719857ba61")

@unittest.skipUnless(ZSH, "Provide zsh for isolated prompt configuration tests")
class PromptConfigTests(unittest.TestCase):
    def run_zsh(self, command):
        with tempfile.TemporaryDirectory(prefix="envy-p10k-config-") as directory:
            home = Path(directory).resolve()
            return subprocess.run(
                [ZSH, "-dfc", 'source "$1"' + chr(10) + command, "envy-prompt-config", str(CONFIG)],
                cwd=home, env=isolated_environment(home), check=True,
                capture_output=True, text=True, timeout=10,
            ).stdout

    def test_layout_keeps_environment_and_command_feedback(self):
        command = "print -r -- \"LEFT=${(j: :)POWERLEVEL9K_LEFT_PROMPT_ELEMENTS}\"\nprint -r -- \"RIGHT=${(j: :)POWERLEVEL9K_RIGHT_PROMPT_ELEMENTS}\"\nprint -r -- \"MODE=$POWERLEVEL9K_MODE\"\nprint -r -- \"TRANSIENT=$POWERLEVEL9K_TRANSIENT_PROMPT\"\nprint -r -- \"INSTANT=$POWERLEVEL9K_INSTANT_PROMPT\"\nprint -r -- \"COLOR=$POWERLEVEL9K_DIR_FOREGROUND\"\nprint -r -- \"STATUS=$POWERLEVEL9K_STATUS_ERROR_FOREGROUND\"\nprint -r -- \"TIME=$POWERLEVEL9K_TIME_FORMAT\"\nfor name in ${(k)parameters[(I)POWERLEVEL9K_*_BACKGROUND]}; do\n  [[ -z ${(P)name} ]] || print -r -- \"OPAQUE=$name\"\ndone"
        values = dict(line.split("=", 1) for line in self.run_zsh(command).splitlines())
        self.assertEqual(values["LEFT"].split(), ["dir", "vcs"])
        right = values["RIGHT"].split()
        self.assertNotIn("newline", right)
        self.assertEqual(right[:3], ["status", "command_execution_time", "background_jobs"])
        for segment in ("direnv", "virtualenv", "anaconda", "kubecontext", "terraform", "context", "nix_shell"):
            self.assertIn(segment, right)
        self.assertEqual(values["MODE"], "compatible")
        self.assertEqual(values["TRANSIENT"], "always")
        self.assertEqual(values["INSTANT"], "verbose")
        self.assertEqual(values["TIME"], "%D{%I:%M:%S %p}")
        self.assertIn("OPAQUE", values)
        self.assertEqual(values["COLOR"], "254")
        self.assertEqual(values["STATUS"], "3")
        self.assertNotRegex(CONFIG.read_text(), r"@prompt_[a-z]+@")

    def formatter(self, **values):
        assignments = [f"typeset -g {name}={shlex.quote(str(value))}" for name, value in values.items()]
        command = chr(10).join(assignments + ["my_git_formatter", 'print -r -- "$my_git_format"'])
        return self.run_zsh(command).strip()

    def test_git_counts_and_special_states_are_preserved(self):
        raw = self.formatter(
            VCS_STATUS_LOCAL_BRANCH="fixture-中文", VCS_STATUS_COMMIT_SUMMARY="synthetic wip change",
            VCS_STATUS_COMMITS_AHEAD=2, VCS_STATUS_COMMITS_BEHIND=3,
            VCS_STATUS_PUSH_COMMITS_AHEAD=1, VCS_STATUS_PUSH_COMMITS_BEHIND=4,
            VCS_STATUS_STASHES=5, VCS_STATUS_ACTION="merge", VCS_STATUS_NUM_CONFLICTED=2,
            VCS_STATUS_NUM_STAGED=4, VCS_STATUS_NUM_UNSTAGED=7, VCS_STATUS_NUM_UNTRACKED=6,
            VCS_STATUS_HAS_UNSTAGED=-1,
        )
        plain = re.sub(r"%F[{][^}]*[}]|%[0-9]+F", "", raw)
        for token in ("fixture-中文", "wip", "⇣3⇡2", "⇠4⇢1", "*5", "merge", "~2", "+4", "!7", "?6", "─"):
            self.assertIn(token, plain)

    def test_git_labels_escape_prompt_sequences(self):
        raw = self.formatter(VCS_STATUS_LOCAL_BRANCH="feature/中文%p")
        self.assertIn("feature/中文%%p", raw)
        self.assertNotIn("feature/中文%p", raw)
        raw = self.formatter(VCS_STATUS_TAG="release%p")
        self.assertIn("release%%p", raw)

    def test_detached_commit_loading_and_long_branch_are_preserved(self):
        self.assertIn("abcdef01", self.formatter(VCS_STATUS_COMMIT="abcdef0123456789"))
        self.assertEqual(self.formatter(P9K_CONTENT="loading"), "loading")
        branch = "feature/" + "中文" * 30
        raw = self.formatter(VCS_STATUS_LOCAL_BRANCH=branch)
        self.assertIn(branch[:12], raw)
        self.assertIn("…", raw)
        self.assertIn(branch[-12:], raw)


@unittest.skipUnless(ZSH and GIT, "Provide zsh and git for isolated prompt rendering")
class PromptRuntimeTests(unittest.TestCase):
    # Match the accepted-line and preexec inputs consumed by the locked P10k.
    SCRIPT = r"""
source "$1"
source "$2"
typeset -gi COLUMNS=$3 LINES=24
for attempt in {1..3}; do
  _p9k__line_finished=
  _p9k_preexec1 "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__"
  _p9k_preexec2 "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__"
  __FIXTURE_COMMAND__
  _p9k_precmd
  sleep 0.1
done
_p9k__line_finished=
_p9k_preexec1 "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__"
_p9k_preexec2 "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__" "__FIXTURE_COMMAND__"
if [[ "$4" == duration ]]; then
  _p9k__timer_start=$((EPOCHREALTIME - 5))
fi
__FIXTURE_COMMAND__
_p9k_precmd
_fixture_rprompt="${(e)RPROMPT}"
_fixture_prompt="${(e)PROMPT}"
_fixture_transient="${(e)_p9k_transient_prompt}"
print -r -- ENVY_PACKET_PROMPT_BEGIN
print -r -- "${(%)_fixture_prompt}"
print -r -- ENVY_PACKET_PROMPT_END
print -r -- ENVY_PACKET_RPROMPT_BEGIN
print -r -- "${(%)_fixture_rprompt}"
print -r -- ENVY_PACKET_RPROMPT_END
print -r -- ENVY_PACKET_TRANSIENT_BEGIN
print -r -- "${(%)_fixture_transient}"
print -r -- ENVY_PACKET_TRANSIENT_END
_p9k_prompt_length "$_fixture_prompt"
print -r -- ENVY_PACKET_LENGTH_BEGIN
print -r -- "$_p9k__ret"
print -r -- ENVY_PACKET_LENGTH_END
if (( $+functions[gitstatus_stop] )); then
  gitstatus_stop POWERLEVEL9K
fi
"""

    def setUp(self):
        root = os.environ.get("ENVY_P10K_SOURCE")
        if not root or not os.environ.get("ENVY_P10K_CONFIG"):
            self.skipTest("Set ENVY_P10K_SOURCE and ENVY_P10K_CONFIG for generated prompt rendering")
        self.theme = Path(root) / "share/zsh-powerlevel10k/powerlevel10k.zsh-theme"
        self.assertTrue(self.theme.is_file())

    def capture(self, width, state, long_directory=False):
        commands = {"ok": ":", "error": "(exit 7)", "signal": "(exit 130)", "pipeline": "false | true", "duration": ":"}
        script = self.SCRIPT.replace("__FIXTURE_COMMAND__", commands[state])
        with tempfile.TemporaryDirectory(prefix="envy-p10k-render-") as directory:
            home = Path(directory).resolve()
            label = "特别长的中文目录" * 4 if long_directory else "合成项目"
            work = home / "目录 带空格" / label
            work.mkdir(parents=True)
            environment = isolated_environment(home)
            environment.update(IN_NIX_SHELL="pure", VIRTUAL_ENV=str(home / "test-env"))
            subprocess.run([GIT, "init", "-q", "--initial-branch=fixture-中文", str(work)], env=environment, check=True, capture_output=True)
            (work / "中文 文件.txt").write_text("synthetic contents" + chr(10))
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))

            def session():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            process = subprocess.Popen(
                [ZSH, "-dfi", "-c", script, "envy-prompt-render", str(CONFIG), str(self.theme), str(width), state],
                stdin=slave, stdout=slave, stderr=slave, env=environment, cwd=work, preexec_fn=session,
            )
            os.close(slave)
            output = bytearray()
            deadline = time.monotonic() + 12
            try:
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if ready:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        output.extend(chunk)
                    elif process.poll() is not None:
                        break
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                os.close(master)
            self.assertEqual(process.returncode, 0)
            self.assertFalse((home / ".zsh_history").exists())
            text = output.decode(errors="replace")
            self.assertNotIn("command not found:", text)
            self.assertNotIn("failed to initialize gitstatus", text)
            packets = {}
            for key in ("PROMPT", "RPROMPT", "TRANSIENT", "LENGTH"):
                prefix, suffix = f"ENVY_PACKET_{key}_BEGIN", f"ENVY_PACKET_{key}_END"
                self.assertIn(prefix, text)
                self.assertIn(suffix, text)
                packets[key] = text.split(prefix, 1)[1].split(suffix, 1)[0].strip(chr(13) + chr(10))
            return packets

    @staticmethod
    def plain(text):
        return re.sub(re.escape(chr(27) + "[") + "[0-9;:]*[A-Za-z]", "", text)

    def test_wide_prompt_keeps_git_environment_feedback_and_transient(self):
        for state in ("ok", "error", "signal", "duration"):
            with self.subTest(state=state):
                result = self.capture(80, state)
                prompt = self.plain(result["PROMPT"])
                self.assertIn("合成项目", prompt)
                self.assertIn("fixture-中文 ?1", prompt)
                self.assertEqual(len(prompt.splitlines()), 1)
                self.assertGreater(int(result["LENGTH"]), 2)
                transient = self.plain(result["TRANSIENT"])
                self.assertEqual(transient.strip(), "❯")
                self.assertNotIn("fixture-中文", transient)
                feedback = self.plain(result["RPROMPT"])
                self.assertIn("test-env", feedback)
                self.assertIn("pure nix", feedback)
                if state == "error":
                    self.assertIn("7", feedback)
                elif state == "signal":
                    self.assertIn("INT", feedback)
                elif state == "duration":
                    self.assertIn("5s", feedback)
                else:
                    self.assertIn("✔", feedback)

    def test_narrow_original_prompt_preserves_long_chinese_directory_and_git(self):
        for state in ("ok", "error"):
            with self.subTest(state=state):
                result = self.capture(40, state, long_directory=True)
                prompt = self.plain(result["PROMPT"])
                self.assertIn("特别长的中文目录", prompt)
                self.assertIn("fixture-中文 ?1", prompt)
                self.assertEqual(len(prompt.splitlines()), 1)
                self.assertGreater(int(result["LENGTH"]), 2)
                if state == "error":
                    self.assertIn("7", self.plain(result["RPROMPT"]))

    def test_a_partial_pipeline_failure_keeps_its_codes(self):
        result = self.capture(80, "pipeline")
        feedback = self.plain(result["RPROMPT"])
        self.assertIn("1|0", feedback)
        self.assertIn("✔", feedback)


if __name__ == "__main__":
    unittest.main()

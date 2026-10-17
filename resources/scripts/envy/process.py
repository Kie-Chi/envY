"""One subprocess boundary with consistent diagnostics and failure semantics."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from envy import log


_FLAKE_SOURCE_MIRROR_PRIMED: set[tuple[str, str]] = set()


def prime_flake_sources(
    cwd: Path | str | None = None, env: Mapping[str, str] | None = None,
) -> None:
    """Prime the current checkout's locked Flake sources through its mirror."""
    effective_env = dict(env) if env is not None else os.environ.copy()
    if effective_env.get("ENVY_FLAKE_SOURCE_MIRROR", "1").strip().casefold() in {
        "0", "false", "no", "off",
    }:
        return
    checkout = Path(cwd or effective_env.get("ENVY_ROOT", Path.cwd())).expanduser()
    lock_file = checkout / "flake.lock"
    helper = checkout / "resources" / "scripts" / "flake-source-mirror.sh"
    if not lock_file.is_file() or not helper.is_file():
        return
    key = str(checkout.resolve())
    try:
        lock_signature = hashlib.sha256(lock_file.read_bytes()).hexdigest()
    except OSError:
        return
    state_key = (key, lock_signature)
    if state_key in _FLAKE_SOURCE_MIRROR_PRIMED:
        return
    result = subprocess.run(
        ["bash", str(helper), str(checkout), "prime"],
        cwd=str(checkout),
        env=effective_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log.debug(
            "mirror", "flake source prefetch helper failed; using locked URLs",
            checkout=key, exit_code=result.returncode,
        )
    elif result.stderr and log.is_debug():
        log.debug("mirror", "flake source prefetch completed", checkout=key)
    _FLAKE_SOURCE_MIRROR_PRIMED.add(state_key)


def _maybe_prime_flake_sources(
    command: Sequence[str], cwd: Path | str | None, env: Mapping[str, str] | None,
) -> None:
    """Prime locked Flake sources once before a Nix Flake command."""
    effective_env = dict(env) if env is not None else os.environ.copy()
    nix_index = next(
        (index for index, part in enumerate(command) if Path(str(part)).name == "nix"),
        None,
    )
    if nix_index is None:
        return
    nix_commands = {"build", "develop", "eval", "flake", "run", "search"}
    if not any(str(part) in nix_commands for part in command[nix_index + 1:]):
        return
    prime_flake_sources(cwd, effective_env)


class CommandError(subprocess.CalledProcessError):
    """A subprocess failure safe to render once at the CLI boundary."""

    def __init__(self, result: subprocess.CompletedProcess[str]):
        super().__init__(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )


def command_display(command: Sequence[str]) -> str:
    return shlex.join(_redacted_command(command))


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname += f":{parsed.port}"
    if parsed.username or parsed.password:
        hostname = f"<redacted>@{hostname}"
    query = "<redacted>" if parsed.query else ""
    return urlunsplit((parsed.scheme, hostname, parsed.path, query, ""))


def _redacted_command(command: Sequence[str]) -> list[str]:
    """Keep diagnostics useful without echoing common secret-bearing arguments."""
    sensitive = re.compile(r"(?:pass(?:word)?|token|secret|api[-_]?key|credential)", re.I)
    result: list[str] = []
    redact_next = False
    for raw in command:
        value = str(raw)
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        if value.startswith("-") and "=" in value:
            option, option_value = value.split("=", 1)
            if sensitive.search(option):
                result.append(f"{option}=<redacted>")
                continue
            value = f"{option}={_redact_url(option_value)}"
        elif value.startswith("-") and sensitive.search(value):
            result.append(value)
            redact_next = True
            continue
        else:
            value = _redact_url(value)
        result.append(value)
    return result


def _activity_label(command: Sequence[str]) -> str:
    names = [Path(str(part)).name for part in command]
    if not names:
        return "external command"
    executable = names[0]
    index = 1
    if executable == "sudo":
        while index < len(names) and names[index].startswith("-"):
            index += 1
        if index < len(names):
            executable = names[index]
            index += 1
    subcommand = next(
        (part for part in names[index:] if not part.startswith("-")),
        "",
    )
    return " ".join(part for part in (executable, subcommand) if part)[:80]


class _ActivityReporter:
    """Emit a low-noise heartbeat while a foreground command is silent."""

    def __init__(self, label: str):
        self.label = label
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.announced = False
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        enabled = os.environ.get("ENVY_ACTIVITY", "1").strip().casefold()
        if (
            enabled in {"0", "false", "no", "off"}
            or log.is_completion_invocation()
            or not sys.stderr.isatty()
            or threading.current_thread() is not threading.main_thread()
        ):
            return
        delay = 3.0 if log.is_debug() else 5.0
        interval = 5.0 if log.is_debug() else 10.0

        def report() -> None:
            if self.stop_event.wait(delay):
                return
            while not self.stop_event.is_set():
                self.announced = True
                elapsed = int(time.monotonic() - self.started)
                log.activity(
                    "command",
                    "still running; Ctrl-C cancels",
                    task=self.label,
                    elapsed=f"{elapsed}s",
                )
                if self.stop_event.wait(interval):
                    return

        self.thread = threading.Thread(target=report, name="envy-activity", daemon=True)
        self.thread.start()

    def stop(self) -> float:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=0.2)
        return time.monotonic() - self.started


NOTIFY_THRESHOLD_SECONDS = 30.0
_NOTIFY_ENV = "ENVY_NOTIFY"


def _notify_enabled() -> bool:
    """Gate desktop notifications: opt-out env, interactive TTY, non-JSON only."""
    value = os.environ.get(_NOTIFY_ENV, "1").strip().casefold()
    if value in {"0", "false", "no", "off"}:
        return False
    if not sys.stderr.isatty():
        return False
    # Never notify in machine-readable mode. Scanning argv is reliable here
    # because every --json flag in this CLI is a boolean long option.
    if "--json" in sys.argv:
        return False
    return True


def _notify_command(title: str, body: str) -> list[str] | None:
    """Return the platform notification command, or None when unsupported."""
    if sys.platform == "darwin":
        script = f"display notification {shlex.quote(body)} with title {shlex.quote(title)}"
        return ["osascript", "-e", script]
    if sys.platform.startswith("linux"):
        binary = shutil.which("notify-send")
        if binary is None:
            return None
        return [binary, "--app-name=envy", title, body]
    return None


def _fire_completion_notification(label: str, duration: float, exit_code: int) -> None:
    """Fire a detached, best-effort desktop notification; never raise."""
    status = "completed" if exit_code == 0 else f"failed (exit {exit_code})"
    body = f"{label} {status} in {duration:.0f}s"
    command = _notify_command("envy", body)
    if command is None:
        return
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def run_process(
    command: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_data: str | None = None,
    capture: bool = False,
    check: bool = True,
    timeout: int | float | None = None,
    activity: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in command]
    _maybe_prime_flake_sources(argv, cwd, env)
    displayed = command_display(argv)
    reporter = _ActivityReporter(activity or _activity_label(argv))
    log.debug(
        "cmd", "start", command=displayed, cwd=str(cwd or Path.cwd()),
        capture=str(capture).lower(), timeout=timeout if timeout is not None else "none",
    )
    reporter.start()
    try:
        try:
            result = subprocess.run(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                env=dict(env) if env is not None else None,
                input=stdin_data,
                text=True,
                capture_output=capture,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            result = subprocess.CompletedProcess(argv, 127, "" if capture else None, str(exc))
        except subprocess.TimeoutExpired as exc:
            stderr = f"command timed out after {timeout} seconds"
            result = subprocess.CompletedProcess(argv, 124, exc.stdout, stderr)
    finally:
        duration = reporter.stop()

    if reporter.announced:
        log.activity(
            "command", "finished", task=reporter.label,
            elapsed=f"{duration:.1f}s", exit_code=result.returncode,
        )
        if _notify_enabled() and duration >= NOTIFY_THRESHOLD_SECONDS:
            _fire_completion_notification(reporter.label, duration, result.returncode)

    if log.is_debug():
        log.debug(
            "cmd", "finish", command=displayed,
            elapsed=f"{duration:.3f}s", exit_code=result.returncode,
            stdout_chars=len(result.stdout or ""), stderr_chars=len(result.stderr or ""),
        )
    if check and result.returncode != 0:
        raise CommandError(result)
    return result


_FAILURE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"attribute '[^']+' missing", re.I),
        "A configuration attribute is missing. Run: envy host check <machine> to "
        "locate the failing target, or envy config refine to normalize machine values.",
    ),
    (
        re.compile(r"infinite recursion encountered", re.I),
        "Infinite recursion in the Nix evaluation. Run: envy config check to inspect "
        "evaluated values, or compare against a working machine with envy host diff.",
    ),
    (
        re.compile(r"hash mismatch", re.I),
        "A fixed-output hash changed. Run: envy update inputs <name> to refresh "
        "flake.lock, then envy apply.",
    ),
    (
        re.compile(r"builder for '[^']+' failed|build of '[^']+' failed", re.I),
        "A Nix build failed. Run: envy doctor to check toolchain health, then retry "
        "envy apply.",
    ),
    (
        re.compile(r"syntax error", re.I),
        "Nix syntax error in a machine or module file. Run: envy config check to "
        "localize it, or envy edit to open the machine file.",
    ),
    (
        re.compile(r"no matching (?:creation rules|keys)", re.I),
        "SOPS cannot decrypt or re-encrypt secrets with the local age key. Run: "
        "envy key repair, or envy setup to reconfigure the recovery key.",
    ),
    (
        re.compile(r"Permission denied \(publickey\)", re.I),
        "Git SSH authentication failed. Inspect remotes with envy git remote -v and "
        "confirm your SSH key is loaded (ssh-add -l).",
    ),
    (
        re.compile(r"non-fast-forward|failed to push some refs", re.I),
        "Git push was rejected because the branch is behind. Run: envy sync first, "
        "then retry envy push.",
    ),
    (
        re.compile(r"diverged|divergent branches", re.I),
        "Local and remote branches have diverged. Run: envy sync to reconcile before "
        "pushing.",
    ),
]


def translate_failure(command: Sequence[str], stderr: str) -> str | None:
    """Map a known Nix/git/sops failure to one friendly next-step hint."""
    if not stderr:
        return None
    for pattern, hint in _FAILURE_PATTERNS:
        if pattern.search(stderr):
            return hint
    return None


def render_command_error(error: CommandError) -> None:
    command = error.cmd if isinstance(error.cmd, list) else [str(error.cmd)]
    log.error("command", "external command failed", exit_code=error.returncode)
    log.hint(command_display(command))
    stderr = str(error.stderr or "")
    if stderr:
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        if log.is_debug():
            selected = lines[-12:]
        else:
            important = [
                line for line in lines
                if re.search(r"\b(?:error|failed|failure|denied|timed out)\b", line, re.I)
            ]
            selected = [*(important[-2:]), *(lines[-2:])]
        for line in dict.fromkeys(selected):
            log.hint(line[:500])
    translation = translate_failure(command, stderr)
    if translation is not None:
        log.hint(translation)


def environment_with(base: Mapping[str, str] | None = None, **values: str) -> dict[str, str]:
    result = dict(base or os.environ)
    result.update(values)
    return result

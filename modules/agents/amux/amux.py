#!/usr/bin/env python3
"""amux: run Claude Code or Codex in per-session Docker containers.

Each session is one container whose main process holds the agent in a private
tmux server, so detaching leaves the agent running. Keys are tmux's own. The
host client only proxies the terminal to paste host clipboard images, keep
newlines intact in bracketed paste, switch between containers, and detach
cleanly when the host terminal goes away.
"""

from __future__ import annotations

import argparse
import csv
import errno
import fcntl
import hashlib
import io
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

LABEL = "dev.envy.amux"
IMAGE_LABEL = "dev.envy.amux.image"
IMAGE_REPOSITORY = "envy-amux"
CONTAINER_PREFIX = "amux-"
HOME_VOLUME = "amux-home"
PASTE_MOUNT = "/amux/paste"
SSH_AGENT_MOUNT = "/run/amux/ssh-auth.sock"
TMUX = ["tmux", "-L", "amux"]
AGENTS = ("claude", "codex")

# tmux.conf binds these keys to "detach-client -E 'exit N'".
EXIT_NEXT, EXIT_PREV, EXIT_CHOOSE = 10, 11, 12
# tmux user-keys[0], bound to detach-client inside the container.
DETACH_KEY = b"\x1b[9999~"
CTRL_V = {b"\x16", b"\x1b[118;5u", b"\x1b[27;5;118~"}
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
CSI_RE = re.compile(rb"\x1b\[[0-9;:<=>?]*[ -/]*[@-~]")
CSI_PREFIX_RE = re.compile(rb"\x1b\[[0-9;:<=>?]*[ -/]*")
ATTACH_ENV = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LC_TERMINAL", "LC_TERMINAL_VERSION")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
PASTE_RETENTION = 7 * 24 * 3600

# Lists each tmux client with the host owner recorded by "docker exec -e".
CLIENTS_SCRIPT = r"""tmux -L amux list-clients -F '#{client_pid} #{client_name}' 2>/dev/null |
while read -r pid name; do
  owner=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^AMUX_OWNER=//p')
  printf '%s %s\n' "$name" "${owner:--}"
done"""


class AmuxError(Exception):
    pass


# --- configuration and Docker helpers -------------------------------------


def load_config() -> dict:
    path = os.environ.get("AMUX_CONFIG")
    if not path:
        raise AmuxError("AMUX_CONFIG is not set; run amux through the envY wrapper")
    return json.loads(Path(path).read_text())


def docker(*args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, env=env)
    except FileNotFoundError:
        raise AmuxError("docker is not installed or not on PATH") from None
    if check and result.returncode != 0:
        raise AmuxError(result.stderr.strip() or f"docker {args[0]} failed")
    return result


def container_name(name: str) -> str:
    return CONTAINER_PREFIX + name


@dataclass
class Session:
    name: str
    agent: str
    workdir: str
    created: str


def list_sessions() -> list[Session]:
    """Running sessions, oldest first."""
    fmt = "\t".join([
        "{{.Names}}",
        '{{.Label "dev.envy.amux.agent"}}',
        '{{.Label "dev.envy.amux.workdir"}}',
        "{{.RunningFor}}",
    ])
    out = docker("ps", "--filter", f"label={LABEL}=1", "--format", fmt).stdout
    sessions = []
    for line in reversed(out.splitlines()):
        fields = line.split("\t")
        if len(fields) == 4 and fields[0].startswith(CONTAINER_PREFIX):
            sessions.append(Session(fields[0][len(CONTAINER_PREFIX):], *fields[1:]))
    return sessions


def resolve_target(target: str | None, sessions: list[Session]) -> str:
    if target:
        if target not in {session.name for session in sessions}:
            raise AmuxError(f"can't find session: {target}")
        return target
    if not sessions:
        raise AmuxError("no sessions")
    return sessions[-1].name


def default_name(workdir: str, taken: Iterable[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(workdir).name).lstrip("._-") or "session"
    taken = set(taken)
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


# --- image ----------------------------------------------------------------


def container_user(name: str) -> str:
    """A Debian-valid user name; the name itself has no effect on access."""
    candidate = re.sub(r"[^a-z0-9_-]", "-", name.lower())
    return candidate if re.match(r"[a-z_][a-z0-9_-]{0,31}$", candidate) else "amux"


def build_args(cfg: dict) -> dict[str, str]:
    return {
        **cfg["image"]["buildArgs"],
        "USER_NAME": container_user(cfg["user"]),
        "USER_UID": str(os.getuid()),
        "USER_GID": str(os.getgid()),
        "USER_HOME": cfg["home"],
    }


def image_tag(context: str, args: dict[str, str]) -> str:
    payload = json.dumps({"context": context, "args": args}, sort_keys=True)
    return f"{IMAGE_REPOSITORY}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def ensure_image(cfg: dict) -> str:
    context = cfg["image"]["context"]
    args = build_args(cfg)
    tag = image_tag(context, args)
    if docker("image", "inspect", tag, check=False).returncode == 0:
        return tag
    print(f"amux: building {tag}; this happens once per agent update", file=sys.stderr)
    command = ["docker", "build", "--tag", tag]
    for key, value in sorted(args.items()):
        command += ["--build-arg", f"{key}={value}"]
    if subprocess.run([*command, context]).returncode != 0:
        raise AmuxError("image build failed")
    # Images still used by running sessions refuse removal and are kept.
    old = docker("images", "--filter", f"label={IMAGE_LABEL}=1", "--format", "{{.Repository}}:{{.Tag}}", check=False)
    for image in old.stdout.split():
        if image != tag:
            docker("image", "rm", image, check=False)
    return tag


# --- mounts ---------------------------------------------------------------


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    readonly: bool = False
    volume: bool = False

    def args(self) -> list[str]:
        fields = [f"type={'volume' if self.volume else 'bind'}", f"source={self.source}", f"target={self.target}"]
        if self.readonly:
            fields.append("readonly")
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(fields)
        return ["--mount", buffer.getvalue()]


@dataclass(frozen=True)
class GitLayout:
    top: str
    gitdir: str
    common: str


def within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_shareable(path: str, home: str) -> None:
    if within(home, path):
        raise AmuxError(f"refusing to share {path}: it contains the home directory")


def git_layout(path: str, git: str) -> GitLayout | None:
    result = subprocess.run(
        [git, "-C", path, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir"],
        capture_output=True, text=True,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 3:
        return None
    return GitLayout(*(os.path.realpath(line) for line in lines))


def parse_add_dir(value: str) -> tuple[str, bool]:
    path, writable = value, False
    if value.endswith(":rw") or value.endswith(":ro"):
        path, writable = value[:-3], value.endswith(":rw")
    if not os.path.isdir(path):
        raise AmuxError(f"not a directory: {path}")
    return os.path.realpath(path), writable


def plan_mounts(
    *,
    root: str,
    home: str,
    layout: GitLayout | None,
    add_dirs: list[tuple[str, bool]],
    skills: str | None,
    paste_dir: str,
    ssh_agent: str | None,
) -> list[Mount]:
    """Same-path mounts for the session; Docker mounts parents before children."""
    for path in [root, *(path for path, _ in add_dirs)]:
        check_shareable(path, home)
    mounts = [Mount(root, root)]

    def writable(path: str) -> bool:
        return any(within(path, m.target) and not m.readonly for m in mounts)

    if layout:
        for path in (layout.common, layout.gitdir):
            if not writable(path):
                mounts.append(Mount(path, path))
        # Keep host-executed git configuration out of the agent's reach.
        protected = [os.path.join(layout.common, "config"), os.path.join(layout.common, "hooks")]
        protected += [os.path.join(d, "config.worktree") for d in {layout.common, layout.gitdir}]
        mounts += [Mount(p, p, readonly=True) for p in sorted(set(protected)) if os.path.exists(p)]
    targets = {m.target for m in mounts}
    for path, rw in add_dirs:
        if path not in targets:
            mounts.append(Mount(path, path, readonly=not rw))
            targets.add(path)
    mounts.append(Mount("/nix/store", "/nix/store", readonly=True))
    if skills and os.path.isdir(skills):
        mounts.append(Mount(skills, skills, readonly=True))
    mounts.append(Mount(paste_dir, PASTE_MOUNT, readonly=True))
    mounts.append(Mount(HOME_VOLUME, home, volume=True))
    if ssh_agent:
        mounts.append(Mount(ssh_agent, SSH_AGENT_MOUNT))
    return mounts


def host_ssh_agent() -> str | None:
    if not os.environ.get("SSH_AUTH_SOCK"):
        return None
    if sys.platform == "darwin":
        # OrbStack and Docker Desktop forward the macOS agent at this path.
        return "/run/host-services/ssh-auth.sock"
    sock = os.environ["SSH_AUTH_SOCK"]
    return sock if os.path.exists(sock) else None


# --- agent command --------------------------------------------------------


def load_env(files: list[str], names: list[str]) -> dict[str, str]:
    """Source the agent's credential files in a clean shell; keep only names."""
    script = 'for f in "$@"; do if [ -f "$f" ]; then . "$f"; fi; done; env -0'
    result = subprocess.run(
        ["/bin/bash", "-c", script, "amux-env", *files],
        capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")},
    )
    if result.returncode != 0:
        raise AmuxError("could not read agent credentials: " + result.stderr.decode(errors="replace").strip())
    values = dict(item.split("=", 1) for item in result.stdout.decode().split("\0") if "=" in item)
    return {name: values[name] for name in names if values.get(name)}


def codex_provider_args(env: dict[str, str]) -> list[str]:
    """Point Codex at the envY endpoint without persisting the key in auth.json."""
    args = []
    if env.get("OPENAI_BASE_URL"):
        args += [
            "-c", 'model_provider="envy"',
            "-c", 'model_providers.envy.name="envy"',
            "-c", f"model_providers.envy.base_url={json.dumps(env['OPENAI_BASE_URL'])}",
            "-c", 'model_providers.envy.env_key="OPENAI_API_KEY"',
            "-c", 'model_providers.envy.wire_api="responses"',
        ]
    if env.get("OPENAI_MODEL"):
        args += ["-m", env["OPENAI_MODEL"]]
    return args


def agent_argv(spec: dict, agent: str, env: dict[str, str], skip: bool,
               add_dirs: list[tuple[str, bool]], extra: list[str]) -> list[str]:
    argv = [spec["command"], *(spec["skipArgs"] if skip else spec.get("promptArgs", [])), *spec["args"]]
    if agent == "codex":
        argv += codex_provider_args(env)
    if spec.get("addDirFlag"):
        for path, _ in add_dirs:
            argv += [spec["addDirFlag"], path]
    return argv + extra


# --- terminal proxy -------------------------------------------------------


def tokens(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split input into key tokens; also return a trailing incomplete escape."""
    out, i = [], 0
    while i < len(buf):
        if buf[i] != 0x1B:
            j = buf.find(b"\x1b", i)
            j = len(buf) if j < 0 else j
            out.extend(buf[k:k + 1] for k in range(i, j))
            i = j
            continue
        if i + 1 == len(buf):
            return out, buf[i:]
        nxt = buf[i + 1:i + 2]
        if nxt == b"[":
            match = CSI_RE.match(buf, i)
            if match:
                out.append(match.group())
                i = match.end()
            elif CSI_PREFIX_RE.fullmatch(buf, i):
                return out, buf[i:]
            else:
                out.append(buf[i:i + 2])
                i += 2
        elif nxt == b"O":
            if i + 2 == len(buf):
                return out, buf[i:]
            out.append(buf[i:i + 3])
            i += 3
        else:
            out.append(buf[i:i + 2])
            i += 2
    return out, b""


def clipboard_image(dest: str) -> bool:
    """Write the host clipboard image to dest as PNG when one is present."""
    custom = os.environ.get("AMUX_CLIPBOARD_CMD")
    if custom:
        command = ["/bin/sh", "-c", custom]
    elif sys.platform == "darwin":
        command = ["osascript",
                   "-e", f'set f to open for access POSIX file "{dest}" with write permission',
                   "-e", "try", "-e", "write (the clipboard as «class PNGf») to f",
                   "-e", "on error", "-e", "close access f", "-e", "error number -1", "-e", "end try",
                   "-e", "close access f"]
    elif os.environ.get("WAYLAND_DISPLAY"):
        command = ["wl-paste", "--no-newline", "--type", "image/png"]
    else:
        command = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    try:
        with open(dest, "wb") as handle:
            stdout = subprocess.DEVNULL if command[0] == "osascript" else handle
            ok = subprocess.run(command, stdout=stdout, stderr=subprocess.DEVNULL).returncode == 0
    except FileNotFoundError:
        return False
    return ok and os.path.getsize(dest) > 0


def save_clipboard_image(paste_dir: str) -> str | None:
    """Save the clipboard image and return its path inside the container."""
    os.makedirs(paste_dir, mode=0o700, exist_ok=True)
    now = time.time()
    for entry in os.scandir(paste_dir):
        if entry.is_file() and now - entry.stat().st_mtime > PASTE_RETENTION:
            os.unlink(entry.path)
    name = time.strftime("clip-%Y%m%d-%H%M%S-") + f"{os.getpid()}-{int(now * 1000) % 1000:03d}.png"
    dest = os.path.join(paste_dir, name)
    old_umask = os.umask(0o077)
    try:
        if clipboard_image(dest):
            return f"{PASTE_MOUNT}/{name}"
    finally:
        os.umask(old_umask)
    if os.path.exists(dest):
        os.unlink(dest)
    return None


class InputFilter:
    """Rewrite host keyboard input before it reaches the session."""

    def __init__(self, paste_image: Callable[[], str | None]):
        self.paste_image = paste_image
        self.pending = b""
        self.in_paste = False
        self.after_cr = False

    def feed(self, data: bytes) -> bytes:
        toks, self.pending = tokens(self.pending + data)
        out = bytearray()
        for tok in toks:
            if self.in_paste:
                if tok == PASTE_END:
                    self.in_paste = False
                    out += tok
                elif tok == b"\n":
                    # tmux extended keys turn a pasted LF into C-j; agents
                    # expect CR line breaks inside bracketed paste.
                    if not self.after_cr:
                        out += b"\r"
                else:
                    out += tok
                self.after_cr = tok == b"\r"
            elif tok == PASTE_START:
                self.in_paste = True
                out += tok
            elif tok in CTRL_V and (path := self.paste_image()):
                out += PASTE_START + path.encode() + PASTE_END
            else:
                out += tok
        return bytes(out)

    def flush(self) -> bytes:
        data, self.pending = self.pending, b""
        return data


def winsize(fd: int) -> tuple[int, int]:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        if rows and cols:
            return rows, cols
    except OSError:
        pass
    return 40, 120


def set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def process_start(pid: int) -> str | None:
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    start = result.stdout.strip()
    return "_".join(start.split()) if result.returncode == 0 and start else None


def owner_token() -> str:
    return f"{os.getpid()}:{process_start(os.getpid()) or '-'}"


def stale_clients(listing: str, alive: Callable[[int, str], bool]) -> list[str]:
    """Clients whose recorded host owner process no longer exists."""
    stale = []
    for line in listing.splitlines():
        name, _, owner = line.partition(" ")
        pid, sep, start = owner.partition(":")
        if name and sep and pid.isdigit() and not alive(int(pid), start):
            stale.append(name)
    return stale


def owner_alive(pid: int, start: str) -> bool:
    return process_start(pid) == start


def reap(container: str) -> int:
    """Detach clients orphaned by killed host processes; return live clients."""
    result = docker("exec", container, "sh", "-c", CLIENTS_SCRIPT, check=False)
    if result.returncode != 0:
        return 0
    stale = stale_clients(result.stdout, owner_alive)
    for client in stale:
        docker("exec", container, *TMUX, "detach-client", "-t", client, check=False)
    return len(result.stdout.splitlines()) - len(stale)


class Attach:
    """Proxy one tmux client running in the container through a local pty."""

    def __init__(self, container: str, paste_dir: str):
        self.container = container
        self.input = InputFilter(lambda: save_clipboard_image(paste_dir))
        self.hangup = False

    def argv(self) -> list[str]:
        env = ["-e", f"AMUX_OWNER={owner_token()}"]
        for name in ATTACH_ENV:
            if os.environ.get(name):
                env += ["-e", f"{name}={os.environ[name]}"]
        return ["docker", "exec", "-it", *env, self.container, *TMUX, "attach", "-t", "main"]

    def run(self) -> int:
        stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
        argv = self.argv()
        pid, master = pty.fork()
        if pid == 0:
            os.execvp(argv[0], argv)
        set_winsize(master, *winsize(stdin))

        def on_winch(*_):
            set_winsize(master, *winsize(stdin))

        def on_hangup(*_):
            # docker exec leaves the in-container client running when the CLI
            # dies, so detach through tmux before this process goes away.
            self.hangup = True
            try:
                os.write(master, DETACH_KEY)
            except OSError:
                pass

        old = termios.tcgetattr(stdin) if os.isatty(stdin) else None
        handlers = {sig: signal.signal(sig, handler) for sig, handler in (
            (signal.SIGWINCH, on_winch), (signal.SIGHUP, on_hangup), (signal.SIGTERM, on_hangup))}
        if old:
            tty.setraw(stdin)
        stdin_open = True
        try:
            while True:
                watch = [master, stdin] if stdin_open else [master]
                try:
                    ready, _, _ = select.select(watch, [], [], 0.05 if self.input.pending else None)
                except InterruptedError:
                    continue
                if not ready:
                    os.write(master, self.input.flush())
                    continue
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        data = b""
                    if not data:
                        break
                    try:
                        os.write(stdout, data)
                    except OSError:
                        pass
                if stdin_open and stdin in ready:
                    try:
                        data = os.read(stdin, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        stdin_open = False
                        self.hangup = True
                        os.write(master, DETACH_KEY)
                        continue
                    out = self.input.feed(data)
                    if out:
                        os.write(master, out)
        finally:
            if old:
                termios.tcsetattr(stdin, termios.TCSAFLUSH, old)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            os.close(master)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)


def choose(sessions: list[Session], current: str) -> str | None:
    """Full-screen session picker for C-b s."""
    fd, out = sys.stdin.fileno(), sys.stdout.fileno()
    index = next((i for i, s in enumerate(sessions) if s.name == current), 0)
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        os.write(out, b"\x1b[?1049h\x1b[?25l")
        while True:
            lines = ["amux sessions: j/k or arrows to move, Enter to attach, q to cancel", ""]
            for i, s in enumerate(sessions):
                mark = "\x1b[7m" if i == index else ""
                lines.append(f"{mark}({i}) {s.name}: {s.agent} {s.workdir} (created {s.created})\x1b[0m")
            os.write(out, ("\x1b[H\x1b[2J" + "\r\n".join(lines)).encode())
            key = os.read(fd, 32)
            if key in (b"\r", b"\n"):
                return sessions[index].name
            if key in (b"q", b"\x1b", b"\x03"):
                return None
            if key in (b"k", b"\x1b[A", b"\x1bOA"):
                index = (index - 1) % len(sessions)
            elif key in (b"j", b"\x1b[B", b"\x1bOB"):
                index = (index + 1) % len(sessions)
            elif key.isdigit() and int(key) < len(sessions):
                return sessions[int(key)].name
    finally:
        os.write(out, b"\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)


def next_session(code: int, current: str, sessions: list[Session], pick: Callable[[], str | None]) -> str | None:
    """Session to attach after a switching key, or None when none are left."""
    names = [s.name for s in sessions]
    if not names:
        return None
    if code == EXIT_CHOOSE:
        return pick() or (current if current in names else names[-1])
    if current not in names:
        return names[-1]
    step = 1 if code == EXIT_NEXT else -1
    return names[(names.index(current) + step) % len(names)]


def attach_loop(name: str, cfg: dict) -> int:
    paste_dir = os.path.join(cfg["home"], ".cache", "amux", "paste")
    while True:
        container = container_name(name)
        reap(container)
        attach = Attach(container, paste_dir)
        code = attach.run()
        if attach.hangup or code not in (EXIT_NEXT, EXIT_PREV, EXIT_CHOOSE):
            return code
        sessions = list_sessions()
        name = next_session(code, name, sessions, lambda: choose(sessions, name))
        if name is None:
            return 0


# --- commands -------------------------------------------------------------


def cmd_new(args: argparse.Namespace, cfg: dict) -> int:
    agent = args.agent
    spec = cfg["agents"][agent]
    home = os.path.realpath(cfg["home"])
    workdir = os.path.realpath(args.c or os.getcwd())
    if not os.path.isdir(workdir):
        raise AmuxError(f"not a directory: {workdir}")
    sessions = list_sessions()
    taken = {s.name for s in sessions}
    name = args.s or default_name(workdir, taken)
    if not NAME_RE.fullmatch(name):
        raise AmuxError(f"invalid session name: {name}")
    if name in taken:
        raise AmuxError(f"duplicate session: {name}")

    layout = git_layout(workdir, cfg["git"])
    root = layout.top if layout else workdir
    add_dirs = [parse_add_dir(value) for value in args.add_dir]
    for path in [root, *(path for path, _ in add_dirs)]:
        check_shareable(path, home)
    if layout:
        # A missing hooks directory could otherwise be created by the agent.
        os.makedirs(os.path.join(layout.common, "hooks"), exist_ok=True)
    paste_dir = os.path.join(home, ".cache", "amux", "paste")
    os.makedirs(paste_dir, mode=0o700, exist_ok=True)
    mounts = plan_mounts(
        root=root, home=home, layout=layout, add_dirs=add_dirs,
        skills=os.path.join(home, spec["skills"]) if spec.get("skills") else None,
        paste_dir=paste_dir, ssh_agent=host_ssh_agent(),
    )
    env = load_env(spec["envFiles"], spec["env"])
    skip = cfg["skipPermissions"] and not args.no_skip_permissions
    argv = agent_argv(spec, agent, env, skip, add_dirs, args.agent_args)

    image = ensure_image(cfg)
    rows, cols = winsize(sys.stdin.fileno())
    container = container_name(name)
    run = ["run", "--detach", "--rm", "--init", "--name", container,
           "--network", "host", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--label", f"{LABEL}=1", "--label", f"{LABEL}.agent={agent}", "--label", f"{LABEL}.workdir={workdir}",
           "--workdir", workdir,
           "--env", f"AMUX_ROWS={rows}", "--env", f"AMUX_COLS={cols}",
           "--env", f"GIT_CONFIG_GLOBAL={cfg['gitConfig']}"]
    if any(m.target == SSH_AGENT_MOUNT for m in mounts):
        run += ["--env", f"SSH_AUTH_SOCK={SSH_AGENT_MOUNT}"]
    for variable in env:
        run += ["--env", variable]
    for mount in mounts:
        run += mount.args()
    docker(*run, image, *argv, env={**os.environ, **env})

    for _ in range(100):
        if docker("exec", container, *TMUX, "has-session", "-t", "main", check=False).returncode == 0:
            break
        if not docker("ps", "--quiet", "--filter", f"name=^{container}$").stdout.strip():
            raise AmuxError(f"session {name} exited during startup")
        time.sleep(0.1)
    else:
        raise AmuxError(f"session {name} did not start")
    if args.d:
        print(name)
        return 0
    return attach_loop(name, cfg)


def cmd_attach(args: argparse.Namespace, cfg: dict) -> int:
    return attach_loop(resolve_target(args.t, list_sessions()), cfg)


def cmd_ls(args: argparse.Namespace, cfg: dict) -> int:
    sessions = list_sessions()
    if not sessions:
        print("no sessions", file=sys.stderr)
        return 1
    for s in sessions:
        attached = " (attached)" if reap(container_name(s.name)) > 0 else ""
        print(f"{s.name}: {s.agent} {s.workdir} (created {s.created}){attached}")
    return 0


def cmd_kill(args: argparse.Namespace, cfg: dict) -> int:
    name = resolve_target(args.t, list_sessions())
    docker("rm", "--force", container_name(name))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="amux", description="Run coding agents in detachable Docker sessions.")
    commands = root.add_subparsers(dest="command")

    new = commands.add_parser("new-session", aliases=["new"], help="start a session (default command)")
    new.add_argument("-s", metavar="NAME", help="session name (default: working directory name)")
    new.add_argument("-d", action="store_true", help="do not attach")
    new.add_argument("-c", metavar="DIR", help="working directory (default: current directory)")
    new.add_argument("--add-dir", action="append", default=[], metavar="PATH[:rw]",
                     help="also share PATH, read-only unless suffixed with :rw")
    new.add_argument("--no-skip-permissions", action="store_true",
                     help="keep the agent's permission prompts for this session")
    new.add_argument("agent", nargs="?", choices=AGENTS, default="claude")
    new.set_defaults(func=cmd_new)

    attach = commands.add_parser("attach-session", aliases=["attach", "a", "at"], help="attach to a session")
    attach.add_argument("-t", metavar="NAME", help="target session (default: newest)")
    attach.set_defaults(func=cmd_attach)

    ls = commands.add_parser("list-sessions", aliases=["ls"], help="list sessions")
    ls.set_defaults(func=cmd_ls)

    kill = commands.add_parser("kill-session", aliases=["kill"], help="stop a session and its agent")
    kill.add_argument("-t", metavar="NAME", help="target session (default: newest)")
    kill.set_defaults(func=cmd_kill)
    return root


def parse(argv: list[str]) -> argparse.Namespace:
    agent_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, agent_args = argv[:split], argv[split + 1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")) or argv[0] in AGENTS:
        argv = ["new", *argv]
    args = parser().parse_args(argv)
    if agent_args and args.command not in ("new", "new-session"):
        parser().error("agent arguments after -- require the new command")
    args.agent_args = agent_args
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    try:
        return args.func(args, load_config())
    except AmuxError as exc:
        print(f"amux: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

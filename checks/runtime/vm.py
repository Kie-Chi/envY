#!/usr/bin/env python3
"""Explicit offline Linux boot probe; never installs Nix or applies envY."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import errno
import json
import math
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time

RECEIPT_PREFIX = "ENVY_RUNTIME_BOOT_RECEIPT "
MAX_TIMEOUT = 600
MAX_OUTPUT = 8 * 1024 * 1024
DISK_SIZE = 40 * 1024**3


@dataclass(frozen=True)
class BootConfig:
    workdir: str
    base: str | None = None
    sha256: str | None = None
    qemu: str | None = None
    qemu_img: str | None = None
    mkisofs: str | None = None
    darwin_ps: str | None = None
    timeout: float = MAX_TIMEOUT
    output_limit: int = MAX_OUTPUT


class ProbeError(Exception):
    """A fixed, non-secret failure reason safe for the JSON report."""


def safe_path(value: str) -> Path:
    # QEMU has its own option parsers. Reject punctuation even with shell=False.
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise ProbeError("unsafe-path")
    if ".." in value.split("/"):
        raise ProbeError("unsafe-path")
    return Path(value)


def trusted_darwin_ps(value: str | None) -> str | None:
    """Bind the candidate to the independently supplied packaged ps identity."""
    if sys.platform != 'darwin':
        return None
    if value is None:
        raise ProbeError('darwin-ps-unavailable')
    # Nix wrappers overwrite this public binding before exec, independently of argv.
    expected = os.environ.get('ENVY_RUNTIME_DARWIN_PS')
    if expected is None:
        raise ProbeError('darwin-ps-identity-unavailable')
    candidate = _immutable_darwin_ps_path(value)
    if candidate != _immutable_darwin_ps_path(expected):
        raise ProbeError('darwin-ps-identity-mismatch')
    return candidate


def _immutable_darwin_ps_path(value: str) -> str:
    """Resolve checked store-only symlinks to an unprivileged regular executable."""
    store = Path('/nix/store')

    def store_path(path):
        if (not isinstance(path, str) or not re.fullmatch(
                r'/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._?=-]+(?:/[A-Za-z0-9+._?=-]+)*', path)
                or any(part in ('.', '..') for part in path.split('/'))):
            raise ProbeError('untrusted-darwin-ps')
        return Path(path)

    try:
        path = store_path(value)
        # The store itself is shared/sticky; every package component is immutable.
        for parent in (Path('/nix'), store):
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or info.st_mode & stat.S_IWOTH
                    or (info.st_mode & stat.S_IWGRP and not info.st_mode & stat.S_ISVTX)):
                raise ProbeError('untrusted-darwin-ps')
        for _ in range(41):
            current = store
            parts = path.relative_to(store).parts
            for index, part in enumerate(parts):
                current = current / part
                info = current.lstat()
                if info.st_uid != 0:
                    raise ProbeError('untrusted-darwin-ps')
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(current)
                    if not os.path.isabs(target):
                        target = str(current.parent / target)
                    # Check each hop before following it, including package symlinks.
                    path = store_path(os.path.normpath(target))
                    path = path.joinpath(*parts[index + 1:])
                    break
                if info.st_mode & (0o222 | stat.S_ISUID | stat.S_ISGID):
                    raise ProbeError('untrusted-darwin-ps')
                if index < len(parts) - 1:
                    if not stat.S_ISDIR(info.st_mode):
                        raise ProbeError('untrusted-darwin-ps')
                elif stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                    return str(current)
                else:
                    raise ProbeError('untrusted-darwin-ps')
            else:
                raise ProbeError('untrusted-darwin-ps')
        raise ProbeError('untrusted-darwin-ps')
    except (OSError, ValueError):
        raise ProbeError('untrusted-darwin-ps') from None


@contextmanager
def preflight(config: BootConfig, deadline: float):
    # Keep the exact hashed inode open until every owned child has exited.
    if any(value is None for value in (config.base, config.sha256, config.qemu, config.qemu_img, config.mkisofs)):
        raise ProbeError("missing-arguments")
    trusted_darwin_ps(config.darwin_ps)
    if (isinstance(config.timeout, bool) or not isinstance(config.timeout, (int, float))
            or not math.isfinite(config.timeout) or not 0 < config.timeout <= MAX_TIMEOUT):
        raise ProbeError("invalid-timeout")
    if (type(config.output_limit) is not int or not 1024 <= config.output_limit <= MAX_OUTPUT):
        raise ProbeError("invalid-output-limit")
    if not isinstance(config.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", config.sha256):
        raise ProbeError("invalid-sha256")
    base = safe_path(config.base)
    work = safe_path(config.workdir)
    tools = [safe_path(v) for v in (config.qemu, config.qemu_img, config.mkisofs)]
    # A new leaf only: no recursive mkdir, reuse, cleanup, or deletion of input paths.
    if os.path.lexists(work) or not work.parent.is_dir():
        raise ProbeError("workdir-not-new")
    work = safe_path(str(work.parent.resolve() / work.name))
    if os.path.lexists(work):
        raise ProbeError("workdir-not-new")
    if base.is_symlink() or base.resolve() != base:
        raise ProbeError("base-not-immutable-regular-file")
    # O_NONBLOCK avoids a FIFO replacement blocking before fstat can reject it.
    descriptor = os.open(base, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222
                or not 0 < info.st_size <= DISK_SIZE):
            raise ProbeError("base-not-immutable-regular-file")
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise ProbeError("timeout")
            digest.update(chunk)
        if digest.hexdigest() != config.sha256:
            raise ProbeError("sha256-mismatch")
        stream.seek(0)
        resolved_tools = []
        for tool in tools:
            resolved = safe_path(str(tool.resolve()))
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                raise ProbeError("tool-unavailable")
            resolved_tools.append(str(resolved))
        yield stream.fileno(), work, resolved_tools


@contextmanager
def exclusive_file(path, *, directory_fd=None):
    """Create only a new regular inode; the containing namespace is untrusted."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, "w+b") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ProbeError("output-not-regular")
        os.fchmod(stream.fileno(), 0o600)
        yield stream


@contextmanager
def bound_file(directory_fd, leaf, *, anonymous=False):
    """Bind a readonly handle to our new writer before any body is written/read."""
    with exclusive_file(leaf, directory_fd=directory_fd) as writer:
        readonly = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=directory_fd)
        try:
            a, b = os.fstat(writer.fileno()), os.fstat(readonly)
            if (not stat.S_ISREG(b.st_mode) or (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino)):
                raise ProbeError("seed-binding-changed")
            if anonymous:
                os.unlink(leaf, dir_fd=directory_fd)
            yield writer, readonly
        finally:
            os.close(readonly)


@contextmanager
def payload_grafts(directory_fd, payloads, deadline):
    """Only explicit unlinked readonly regular FDs are passed to mkisofs."""
    with ExitStack() as stack:
        grafts, descriptors = [], []
        for leaf, content in payloads:
            writer, readonly = stack.enter_context(bound_file(
                directory_fd, ".seed-" + secrets.token_hex(16), anonymous=True))
            if isinstance(content, bytes):
                writer.write(content)
            else:
                while chunk := content.read(1024 * 1024):
                    if time.monotonic() >= deadline:
                        raise ProbeError("timeout")
                    writer.write(chunk)
            writer.flush()
            grafts.append(leaf + "=/dev/fd/" + str(readonly))
            descriptors.append(readonly)
        yield grafts, tuple(descriptors)


@contextmanager
def pinned_work(work: Path):
    # This pins only a namespace, not its origin or ownership. Never chdir into
    # it or expose its directory FD to a native process.
    descriptor = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor, work
    finally:
        os.close(descriptor)


def private_file(path: Path, content: str) -> None:
    with exclusive_file(path) as stream:
        stream.write(content.encode())


def native_tmp() -> str:
    path = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX):
        raise ProbeError("unsafe-system-tmp")
    return str(path)


def seed_data(token: str) -> tuple[str, str]:
    # All shell text is fixed, except the internally generated hex nonce.
    script = f"""#!/bin/sh
arch=$(uname -m)
nix_present=false
if command -v nix >/dev/null 2>&1; then nix_present=true; fi
printf '\\n{RECEIPT_PREFIX}{{\"token\":\"{token}\",\"arch\":\"%s\",\"nixPresent\":%s}}\\n' "$arch" "$nix_present" > /dev/ttyS0
sync
poweroff
"""
    # JSON strings are YAML scalars; no host values enter guest commands.
    user_data = """#cloud-config
users:
  - name: envy-runtime
    homedir: /home/envy-runtime
    lock_passwd: true
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    shell: /bin/sh
ssh_pwauth: false
disable_root: true
package_update: false
package_upgrade: false
write_files:
  - path: /usr/local/sbin/envy-runtime-boot-probe
    owner: root:root
    permissions: '0700'
    content: """ + json.dumps(script) + """
runcmd:
  - [ /usr/local/sbin/envy-runtime-boot-probe ]
"""
    metadata = json.dumps({"instance-id": "envy-runtime-" + token, "local-hostname": "envy-runtime"}) + "\n"
    return user_data, metadata


def wait_flags() -> int:
    """Refuse native spawn unless the platform supports non-reaping observation."""
    names = ('P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT',
             'CLD_EXITED', 'CLD_KILLED', 'CLD_DUMPED')
    if not callable(getattr(os, 'waitid', None)) or any(
            type(getattr(os, name, None)) is not int or getattr(os, name) <= 0 for name in names):
        raise ProbeError('waitid-unavailable')
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    try:
        # Our own PID cannot be our child. ECHILD proves flags reached waitid.
        os.waitid(os.P_PID, os.getpid(), flags)
    except OSError as exc:
        if exc.errno != errno.ECHILD and not isinstance(exc, ChildProcessError):
            raise ProbeError('waitid-unavailable') from None
    except (NotImplementedError, ValueError):
        raise ProbeError('waitid-unavailable') from None
    else:
        raise ProbeError('waitid-unavailable')
    return flags


class _ProcessLease:
    """Reserve an owned Popen leader until its group is stopped, then reap it."""

    def __init__(self, process, flags, *, deadline=None, darwin_ps=None):
        self.process, self.flags, self.deadline = process, flags, deadline
        self.darwin_ps = darwin_ps
        self.can_stop, self.stopped = True, False
        self.code = None
        self.exit_observation = None
        self.cleanup_evidence = None

    def observe(self):
        if not self.can_stop or self.process.returncode is not None:
            self.can_stop = False
            raise ProbeError('process-lease-lost')
        try:
            observed = os.waitid(os.P_PID, self.process.pid, self.flags)
            if observed is None:
                if self.exit_observation is not None:
                    raise ProbeError('process-exit-observation-changed')
                return None
            if (type(getattr(observed, 'si_pid', None)) is not int or observed.si_pid != self.process.pid
                    or type(getattr(observed, 'si_code', None)) is not int
                    or type(getattr(observed, 'si_status', None)) is not int):
                raise ProbeError('invalid-waitid-result')
            identity = (observed.si_pid, observed.si_code, observed.si_status)
            if self.exit_observation is not None and identity != self.exit_observation:
                raise ProbeError('process-exit-observation-changed')
            if observed.si_code == os.CLD_EXITED and 0 <= observed.si_status <= 255:
                self.code = observed.si_status
            elif observed.si_code in (os.CLD_KILLED, os.CLD_DUMPED) and 0 < observed.si_status <= 255:
                self.code = -observed.si_status
            else:
                raise ProbeError('invalid-waitid-result')
            self.exit_observation = identity
        except BaseException:
            self.can_stop = False
            raise
        return self.code

    def _darwin_zombies(self):
        """Query only this reserved group after Darwin rejects its SIGKILL."""
        if sys.platform != 'darwin' or not self.can_stop or self.exit_observation is None:
            raise ProbeError('process-lease-lost')
        before = self.exit_observation
        if self.observe() is None or before is None or self.exit_observation != before:
            raise ProbeError('process-lease-lost')
        tool = trusted_darwin_ps(self.darwin_ps)
        tool_dir = str(Path(tool).parent)
        # Reserve a separate second for kill/reap, bounding this query by two.
        deadline = time.monotonic() + 1
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        query, selector, reaped = None, None, False
        raw = bytearray()
        primary = None
        try:
            if time.monotonic() >= deadline:
                raise ProbeError('group-query-timeout')
            selector = selectors.DefaultSelector()
            query = subprocess.Popen(
                [tool, '-g', str(self.process.pid), '-o', 'pid=,pgid=,state='],
                shell=False, close_fds=True, pass_fds=(), cwd=tool_dir,
                env={'HOME': tool_dir, 'TMPDIR': native_tmp(), 'PATH': tool_dir, 'LC_ALL': 'C', 'TZ': 'UTC'},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            selector.register(query.stdout, selectors.EVENT_READ, True)
            selector.register(query.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError('group-query-timeout')
                for key, _ in selector.select(timeout=min(0.05, remaining)):
                    chunk = os.read(key.fileobj.fileno(), min(8192, 8193 - len(raw)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if not key.data:
                        raise ProbeError('group-query-stderr')
                    raw.extend(chunk)
                    if len(raw) > 8192 or raw.count(b'\n') > 128:
                        raise ProbeError('group-query-limit')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError('group-query-timeout')
            code = query.wait(timeout=remaining)
            reaped = True
            if code != 0 or time.monotonic() >= deadline:
                raise ProbeError('group-query-failed')
            rows = raw.split(b'\n')
            if rows[-1] == b'':
                rows.pop()
            if not 1 <= len(rows) <= 128:
                raise ProbeError('group-query-shape')
            seen = set()
            for row in rows:
                match = re.fullmatch(rb'[ \t]*([1-9][0-9]*)[ \t]+([1-9][0-9]*)[ \t]+Z[ \t]*', row)
                if match is None:
                    raise ProbeError('group-query-shape')
                pid, group = (int(value) for value in match.groups())
                if not 0 < pid < 2**31 or group != self.process.pid or pid in seen:
                    raise ProbeError('group-query-shape')
                seen.add(pid)
            if self.process.pid not in seen:
                raise ProbeError('group-query-missing-leader')
            evidence = {'kind': 'darwin-owned-group-metadata', 'querySuccess': True,
                        'allObservedMembersZombie': True, 'count': len(seen)}
        except BaseException as exc:
            primary = exc
            raise
        finally:
            raw.clear()
            cleanup_error = None
            actions = []
            if query is not None:
                if not reaped:
                    # Fixed trusted ps is a single process. Never signal its PG.
                    actions.extend((query.kill, lambda: query.wait(timeout=1)))
                actions.extend(pipe.close for pipe in (query.stdout, query.stderr) if pipe is not None)
            if selector is not None:
                actions.append(selector.close)
            for action in actions:
                try:
                    action()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if primary is None and cleanup_error is not None:
                raise cleanup_error
        if self.observe() is None or self.exit_observation != before:
            raise ProbeError('process-lease-lost')
        return evidence

    def stop(self):
        if self.stopped:
            return
        self.observe()
        self.stopped = True
        # Only the still-unreaped own leader reserves this process-group ID.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            if exc.errno != errno.EPERM or sys.platform != 'darwin' or self.code is None:
                raise
            try:
                evidence = self._darwin_zombies()
            except (OSError, ProbeError, ValueError, subprocess.SubprocessError):
                # EPERM remains the first error unless positive metadata and
                # both non-reaping observations prove the narrow exception.
                raise exc from None
            self.cleanup_evidence = evidence

    def reap(self):
        # No signal may follow even a failing wait.
        self.can_stop = False
        code = self.process.wait(timeout=3)
        if self.code is not None and code != self.code:
            raise ProbeError('process-exit-code-changed')
        return code


class Runner:
    """Bounded output and one owned session per child, including preparation tools."""

    def __init__(self, root: Path, tools: list[str], deadline: float, limit: int, *, directory_fd: int, darwin_ps: str | None = None):
        self.root, self.deadline, self.limit = root, deadline, limit
        self.darwin_ps = trusted_darwin_ps(darwin_ps)
        self.directory_fd = directory_fd
        self.cwd = str(Path(tools[0]).parent)
        self.commands: list[dict] = []
        self.used = 0
        self.env = {
            "HOME": self.cwd, "TMPDIR": native_tmp(),
            "XDG_CONFIG_HOME": self.cwd, "XDG_CACHE_HOME": self.cwd,
            "PATH": os.pathsep.join(dict.fromkeys(str(Path(p).parent) for p in tools)),
            "LC_ALL": "C", "TZ": "UTC", "USER": "envy-runtime", "LOGNAME": "envy-runtime",
        }

    @staticmethod
    def stop(lease: _ProcessLease) -> None:
        if not isinstance(lease, _ProcessLease):
            raise ProbeError('process-lease-required')
        lease.stop()

    def run(self, stage: str, argv: list[str], *, capture: bool = False,
            pass_fds: tuple[int, ...] = ()) -> bytes:
        if time.monotonic() >= self.deadline:
            raise ProbeError("timeout")
        flags = wait_flags()
        record = {"stage": stage, "argv": argv, "pid": None, "returncode": None,
                  "stdout": str(self.root / (stage + ".stdout.log")),
                  "stderr": str(self.root / (stage + ".stderr.log"))}
        self.commands.append(record)
        output = bytearray()
        process, lease, selector = None, None, None
        contexts = []
        primary = None
        try:
            selector = selectors.DefaultSelector()
            streams = []
            for suffix in ('.stdout.log', '.stderr.log'):
                context = exclusive_file(stage + suffix, directory_fd=self.directory_fd)
                streams.append(context.__enter__())
                contexts.append(context)
            stdout, stderr = streams
            process = subprocess.Popen(argv, cwd=self.cwd, env=self.env, shell=False,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True,
                                       close_fds=True, pass_fds=pass_fds)
            lease = _ProcessLease(process, flags, deadline=self.deadline, darwin_ps=self.darwin_ps)
            record["pid"] = process.pid
            selector.register(process.stdout, selectors.EVENT_READ, (stdout, capture))
            selector.register(process.stderr, selectors.EVENT_READ, (stderr, False))
            while selector.get_map() or lease.code is None:
                if time.monotonic() >= self.deadline:
                    raise ProbeError("timeout")
                if lease.code is None and lease.observe() is not None:
                    record['returncode'] = lease.code
                    # Stop descendants even when they already closed stdio.
                    self.stop(lease)
                for key, _ in selector.select(timeout=min(0.05, max(0, self.deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    allowed = max(0, self.limit - self.used)
                    bounded = chunk[:allowed]
                    key.data[0].write(bounded)
                    if key.data[1]:
                        output.extend(bounded)
                    self.used += len(bounded)
                    if len(chunk) > allowed:
                        raise ProbeError("output-limit")
            if lease.code != 0:
                raise ProbeError("tool-failed:" + stage)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_error = None
            actions = []
            if lease is not None:
                actions.extend((lambda: self.stop(lease), lease.reap))
            if process is not None:
                actions.extend(pipe.close for pipe in (process.stdout, process.stderr) if pipe is not None)
            if selector is not None:
                actions.append(selector.close)
            actions.extend(lambda context=context: context.__exit__(None, None, None)
                           for context in reversed(contexts))
            for action in actions:
                try:
                    action()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if lease is not None:
                if lease.cleanup_evidence is not None:
                    record['cleanupEvidence'] = lease.cleanup_evidence
                # An invalid/lost lease never fabricates a successful exit code.
                if lease.code is not None:
                    record['returncode'] = lease.code
                elif lease.stopped:
                    record['returncode'] = process.returncode
            if primary is None and cleanup_error is not None:
                raise cleanup_error
        return bytes(output)


def validate_image_info(raw: bytes) -> None:
    try:
        info = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProbeError("invalid-image-info") from None
    if not isinstance(info, dict) or info.get("format") != "qcow2":
        raise ProbeError("base-not-qcow2")
    # External backing/data files and encrypted images are outside this probe.
    def external(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (("backing" in key or key in {"data-file", "data-file-raw", "encrypted", "encrypt"})
                        and item not in (None, False, "", [])):
                    return True
                if external(item):
                    return True
        return isinstance(value, list) and any(external(item) for item in value)
    if external(info):
        raise ProbeError("base-has-external-or-encrypted-storage")
    size = info.get("virtual-size")
    if type(size) is not int or not 0 < size <= DISK_SIZE:
        raise ProbeError("invalid-virtual-size")


def qemu_argv(qemu: str, base_fd: int, overlay_fd: int, seed_fd: int) -> list[str]:
    # QEMU registers the inherited O_RDONLY descriptor before opening blockdevs.
    # Its file driver duplicates /dev/fdset/1 with matching access flags.
    base_node = {"driver": "qcow2", "read-only": True,
                 "file": {"driver": "file", "filename": "/dev/fdset/1", "read-only": True}}
    disk = {"driver": "qcow2", "node-name": "runtime-disk", "backing": base_node,
            "file": {"driver": "file", "filename": "/dev/fdset/2"}}
    seed = {"driver": "raw", "node-name": "runtime-seed", "read-only": True,
            "file": {"driver": "file", "filename": "/dev/fdset/3", "read-only": True}}
    return [qemu, "-add-fd", f"fd={base_fd},set=1,opaque=verified-base",
            "-add-fd", f"fd={overlay_fd},set=2,opaque=owned-overlay",
            "-add-fd", f"fd={seed_fd},set=3,opaque=owned-seed",
            "-no-user-config", "-nodefaults", "-machine", "pc,accel=tcg", "-cpu", "max",
            "-smp", "2", "-m", "4096", "-display", "none", "-monitor", "none", "-nic", "none",
            "-audiodev", "none,id=runtime-audio", "-parallel", "none", "-serial", "stdio", "-no-reboot",
            "-boot", "order=c,menu=off", "-blockdev", json.dumps(disk, separators=(",", ":")),
            "-device", "virtio-blk-pci,drive=runtime-disk", "-blockdev", json.dumps(seed, separators=(",", ":")),
            "-device", "ide-cd,drive=runtime-seed"]


def receipt_from(raw: bytes, token: str) -> dict:
    receipts = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith(RECEIPT_PREFIX):
            try:
                receipts.append(json.loads(line[len(RECEIPT_PREFIX):]))
            except ValueError:
                raise ProbeError("invalid-guest-receipt") from None
    expected = {"token": token, "arch": "x86_64", "nixPresent": False}
    if len(receipts) != 1 or receipts[0] != expected:
        raise ProbeError("missing-or-mismatched-guest-receipt")
    # JSON false must be a boolean, not an integer comparing equal to False.
    if receipts[0]["nixPresent"] is not False:
        raise ProbeError("missing-or-mismatched-guest-receipt")
    return receipts[0]


def boot_probe(config: BootConfig) -> dict:
    """Return a JSON-safe result. Only an explicit call can launch a guest."""
    report = {"schemaVersion": 1, "command": "boot-probe", "status": "unavailable", "ok": False,
              "evidence": "linux-vm-boot-only", "noNixInstallYet": True, "workdir": None,
              "receipt": None, "commands": [], "error": None}
    runner = None
    try:
        # Validate timeout before arithmetic as this is also a public Python API.
        if (isinstance(config.timeout, bool) or not isinstance(config.timeout, (int, float))
                or not math.isfinite(config.timeout) or not 0 < config.timeout <= MAX_TIMEOUT):
            raise ProbeError("invalid-timeout")
        deadline = time.monotonic() + config.timeout
        with preflight(config, deadline) as (base_fd, work, tools):
            # qemu-img opens this inherited descriptor, never the caller's path.
            base_path = f"/dev/fd/{base_fd}"
            base_fds = (base_fd,)
            work.mkdir(mode=0o700, exist_ok=False)
            report.update(status="failure", workdir=str(work))
            with pinned_work(work) as (directory_fd, _), \
                    exclusive_file("overlay.qcow2", directory_fd=directory_fd) as overlay, \
                    bound_file(directory_fd, "seed.iso") as (iso_writer, iso_readonly):
                runner = Runner(work, tools, deadline, config.output_limit, directory_fd=directory_fd,
                                darwin_ps=config.darwin_ps)
                report["commands"] = runner.commands
                qemu, qemu_img, mkisofs = tools
                validate_image_info(runner.run(
                    "image-info", [qemu_img, "info", "--output=json", "-f", "qcow2", base_path],
                    capture=True, pass_fds=base_fds))
                token = secrets.token_hex(32)
                user_data, metadata = seed_data(token)
                overlay_fd, iso_fd = overlay.fileno(), iso_writer.fileno()
                overlay_path = f"/dev/fd/{overlay_fd}"
                disk_fds = (base_fd, overlay_fd)
                runner.run("overlay-create", [qemu_img, "create", "-f", "qcow2", "-F", "qcow2", "-b",
                                              base_path, overlay_path], pass_fds=disk_fds)
                runner.run("overlay-resize", [qemu_img, "resize", "-f", "qcow2", overlay_path, "40G"],
                           pass_fds=disk_fds)
                payloads = (("user-data", user_data.encode()), ("meta-data", metadata.encode()),
                            ("network-config", b"version: 2\nethernets: {}\n"))
                with payload_grafts(directory_fd, payloads, deadline) as (grafts, descriptors):
                    runner.run("seed-create", [mkisofs, "-quiet", "-output", f"/dev/fd/{iso_fd}", "-volid", "cidata",
                                               "-joliet", "-rock", "-file-mode", "0444", "-dir-mode", "0555", "-uid", "0", "-gid", "0",
                                               "-follow-links", "-graft-points", *grafts], pass_fds=(iso_fd, *descriptors))
                raw = runner.run("qemu", qemu_argv(qemu, base_fd, overlay_fd, iso_readonly), capture=True,
                                 pass_fds=(base_fd, overlay_fd, iso_readonly))
                report["receipt"] = receipt_from(raw, token)
                report.update(status="passed", ok=True)
    except KeyboardInterrupt:
        report.update(status="failure", error="cancelled")
    except ProbeError as exc:
        report["error"] = str(exc)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never copy arbitrary tool stderr, inherited environment, or OS messages into JSON.
        report["error"] = "host-io-or-process-error"
    return report


class _DarwinPsArgument(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error('duplicate-darwin-ps')
        setattr(namespace, self.dest, value)


class JsonParser(argparse.ArgumentParser):
    def add_darwin_ps(self):
        self.add_argument('--darwin-ps', action=_DarwinPsArgument, help=argparse.SUPPRESS)

    def error(self, message):
        raise ProbeError("invalid-arguments")


def main(argv: list[str] | None = None) -> int:
    parser = JsonParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    boot = sub.add_parser("boot-probe")
    boot.add_argument("--workdir", required=True)
    for name in ("base", "sha256", "qemu", "qemu-img", "mkisofs"):
        boot.add_argument("--" + name)
    boot.add_darwin_ps()
    boot.add_argument("--timeout", type=float, default=MAX_TIMEOUT)
    boot.add_argument("--output-limit", type=int, default=MAX_OUTPUT)
    previous_handlers = {}

    def cancel(signum, frame):
        raise KeyboardInterrupt

    try:
        # Install only at the CLI boundary; restore the caller's handlers on return.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, cancel)
        args = vars(parser.parse_args(argv))
        args.pop("command")
        result = boot_probe(BootConfig(**args))
    except (ProbeError, KeyboardInterrupt) as exc:
        result = {"schemaVersion": 1, "command": "boot-probe", "status": "unavailable", "ok": False,
                  "evidence": "linux-vm-boot-only", "noNixInstallYet": True, "workdir": None,
                  "receipt": None, "commands": [],
                  "error": "cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)}
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

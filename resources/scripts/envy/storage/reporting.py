"""Bounded read-only probes shared by storage report providers."""

from __future__ import annotations

import os
import plistlib
import selectors
import signal
import subprocess
import time
from pathlib import Path

from envy.storage.measurement import ScanBudget, open_directory
from envy.storage.model import StorageError


def probe(argv, *, budget: ScanBudget | None = None, timeout: float = 5,
          max_bytes: int = 1_048_576, env=None, cwd=None) -> subprocess.CompletedProcess:
    """Capture bounded metadata output with a deadline, including slow producers."""
    deadline = min(time.monotonic() + timeout, budget.deadline if budget else float("inf"))
    if time.monotonic() >= deadline:
        return subprocess.CompletedProcess(argv, 124, "", "probe deadline exhausted")
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                                   start_new_session=True)
    except OSError:
        return subprocess.CompletedProcess(argv, 127, "", "probe could not start")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    code = None
    aborted = False
    selector = selectors.DefaultSelector()
    try:
        for label in buffers:
            stream = getattr(process, label)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                code, aborted = 124, True
                break
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), min(65_536, max_bytes + 1))
                if chunk:
                    buffers[key.data].extend(chunk)
                    if sum(map(len, buffers.values())) > max_bytes:
                        code, aborted = 125, True
                        break
                else:
                    selector.unregister(key.fileobj)
            if code is not None:
                break
        if code is None:
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                code, aborted = 124, True
    finally:
        if process.poll() is None or aborted:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if aborted:
        return subprocess.CompletedProcess(argv, code, "", "probe deadline exhausted" if code == 124 else "probe output limit exceeded")
    return subprocess.CompletedProcess(argv, code, buffers["stdout"].decode("utf-8", "replace"),
                                       buffers["stderr"].decode("utf-8", "replace"))


def read_plist(path: Path, budget: ScanBudget | None = None) -> dict:
    """Read a small application Info.plist without following path components."""
    if budget:
        budget.check()
    parent = open_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "rb") as handle:
        import stat

        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024**2:
            raise StorageError("Application metadata exceeds the supported file boundary")
        data = handle.read(2 * 1024**2 + 1)
    if budget:
        budget.check()
    if len(data) > 2 * 1024**2:
        raise StorageError("Application metadata exceeds the size limit")
    value = plistlib.loads(data)
    if not isinstance(value, dict):
        raise StorageError("Application metadata must be a dictionary")
    return value

"""Command execution probes."""

from dataclasses import dataclass
import os
import selectors
import shutil
import signal
import subprocess
import time


def run(cmd: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout=_output_text(exc.stdout),
            stderr=_output_text(exc.stderr) or f"timed out after {timeout}s",
        )


def exists(name: str) -> bool:
    return shutil.which(name) is not None


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


@dataclass(frozen=True)
class BoundedRead:
    """A bounded read result; stderr and exception text never leave the probe."""

    stdout: str | None
    reason: str
    returncode: int | None = None


BOUNDED_CLEANUP_GRACE = 0.25


def _collect_bounded(process, selector, deadline: float, byte_limit: int) -> BoundedRead:
    output = bytearray()
    total = 0
    for pipe in (process.stdout, process.stderr):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ)
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return BoundedRead(None, "timeout")
        for key, _ in selector.select(remaining):
            chunk = os.read(key.fd, min(4096, byte_limit - total + 1))
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            total += len(chunk)
            if total > byte_limit:
                return BoundedRead(None, "output-limit")
            if key.fileobj is process.stdout:
                output.extend(chunk)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return BoundedRead(None, "timeout")
    returncode = process.wait(timeout=remaining)
    if returncode:
        return BoundedRead(None, "read-unavailable", returncode)
    try:
        return BoundedRead(output.decode("utf-8", errors="strict"), "read", returncode)
    except UnicodeError:
        return BoundedRead(None, "invalid-encoding", returncode)


def _reap_bounded(process, deadline: float) -> bool:
    confirmed = True
    # Also terminate descendants holding pipes after the direct child exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        confirmed = False
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                confirmed = False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        reaped = process.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return confirmed and type(reaped) is int


def run_bounded(cmd: list[str], *, timeout: float = 2, byte_limit: int = 8192) -> BoundedRead:
    """Bound collection by timeout and cleanup by a separate 0.25-second grace."""
    if timeout <= 0 or byte_limit <= 0:
        raise ValueError("positive read limits are required")
    process = None
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    cleanup_confirmed = True
    try:
        process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env={**os.environ, "LC_ALL": "C"},
        )
        result = _collect_bounded(process, selector, deadline, byte_limit)
    except FileNotFoundError:
        result = BoundedRead(None, "missing-tool")
    except PermissionError:
        result = BoundedRead(None, "permission-denied")
    except subprocess.TimeoutExpired:
        result = BoundedRead(None, "timeout")
    except OSError:
        result = BoundedRead(None, "probe-error")
    finally:
        cleanup_deadline = time.monotonic() + BOUNDED_CLEANUP_GRACE
        try:
            selector.close()
        except OSError:
            cleanup_confirmed = False
        if process is not None:
            cleanup_confirmed = _reap_bounded(process, cleanup_deadline) and cleanup_confirmed
    if not cleanup_confirmed:
        return BoundedRead(None, "cleanup-unconfirmed")
    return result

"""Small recoverable file transactions for multi-file envY workflows."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock, get_ident
from collections.abc import Iterable

from envy.secure_io import atomic_write_bytes


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    existed: bool
    data: bytes
    mode: int

    @classmethod
    def capture(cls, path: Path) -> "FileSnapshot":
        if not path.exists():
            return cls(path=path, existed=False, data=b"", mode=0o600)
        return cls(
            path=path,
            existed=True,
            data=path.read_bytes(),
            mode=path.stat().st_mode & 0o777,
        )

    def restore(self) -> None:
        if self.existed:
            atomic_write_bytes(self.path, self.data, mode=self.mode)
        elif self.path.exists():
            self.path.unlink()


_lock_state: dict[Path, tuple[object, int, int]] = {}
_thread_locks: dict[Path, RLock] = {}
_lock_guard = RLock()


@contextmanager
def transaction_lock(path: Path):
    """Serialize mutations across processes and re-enter safely in one thread."""
    lock_path = path.expanduser().resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_guard:
        thread_lock = _thread_locks.setdefault(lock_path, RLock())

    thread_lock.acquire()
    owner = get_ident()
    registered = False
    try:
        with _lock_guard:
            state = _lock_state.get(lock_path)
            if state is None:
                handle = lock_path.open("a+")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    handle.close()
                    raise
                _lock_state[lock_path] = (handle, 1, owner)
            else:
                handle, depth, state_owner = state
                if state_owner != owner:
                    raise RuntimeError("transaction lock owner mismatch")
                _lock_state[lock_path] = (handle, depth + 1, owner)
            registered = True
        yield
    finally:
        if registered:
            with _lock_guard:
                handle, depth, state_owner = _lock_state[lock_path]
                if depth > 1:
                    _lock_state[lock_path] = (handle, depth - 1, state_owner)
                else:
                    del _lock_state[lock_path]
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
        thread_lock.release()


class FileTransaction:
    """Restore captured paths unless commit() is reached without an exception."""

    def __init__(self, paths: list[Path], *, missing_paths: Iterable[Path] = ()):
        forced_missing = {path.expanduser().resolve() for path in missing_paths}
        self.snapshots = [
            FileSnapshot.capture(path) if path.expanduser().resolve() not in forced_missing
            else FileSnapshot(path=path, existed=False, data=b"", mode=0o600)
            for path in paths
        ]
        self._committed = False

    def __enter__(self) -> "FileTransaction":
        return self

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        errors: list[OSError] = []
        for snapshot in reversed(self.snapshots):
            try:
                snapshot.restore()
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(f"failed to restore {len(errors)} transaction file(s)") from errors[0]

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None or not self._committed:
            self.rollback()
        return False

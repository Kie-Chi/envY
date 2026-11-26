"""Atomic filesystem helpers for envY-managed public and sensitive files."""

from __future__ import annotations

import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


def ensure_private_directory(path: Path) -> None:
    """Create a private directory and remove group/other access if it exists."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync so a completed replace survives a crash."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int | None = None,
    private_parent: bool = False,
    pre_replace: Callable[[], None] | None = None,
) -> None:
    """Write atomically; an optional guard runs after fsync, immediately before replace."""
    if private_parent:
        ensure_private_directory(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    target_mode = mode
    if target_mode is None:
        try:
            target_mode = path.stat().st_mode & 0o777
        except OSError:
            target_mode = 0o644

    # Keep the temporary's directory reachable for cleanup if a guard detects a rename.
    parent_descriptor = (
        os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY) if pre_replace is not None else None
    )
    descriptor = -1
    temporary: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        if parent_descriptor is None:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=f".{path.name}.envy-"
            )
            temporary = Path(temporary_name)
        else:
            bound_parent = os.fstat(parent_descriptor)
            current_parent = path.parent.stat()
            if (current_parent.st_dev, current_parent.st_ino) != (bound_parent.st_dev, bound_parent.st_ino):
                raise OSError("temporary directory changed during write")
            # Create relative to the bound directory, never a newly substituted parent.
            for _ in range(100):
                name = f".{path.name}.envy-{secrets.token_hex(8)}"
                try:
                    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL,
                                         0o600, dir_fd=parent_descriptor)
                except FileExistsError:
                    continue
                temporary = path.parent / name
                break
            else:
                raise FileExistsError("cannot create a unique temporary file")
        info = os.fstat(descriptor)
        temporary_identity = (info.st_dev, info.st_ino)
        if parent_descriptor is not None:
            parent_entry = os.stat(temporary.name, dir_fd=parent_descriptor, follow_symlinks=False)
            current_parent = path.parent.stat()
            if ((parent_entry.st_dev, parent_entry.st_ino) != temporary_identity
                    or (current_parent.st_dev, current_parent.st_ino) != (bound_parent.st_dev, bound_parent.st_ino)):
                raise OSError("temporary directory changed during write")
        os.fchmod(descriptor, target_mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if pre_replace is not None:
            pre_replace()
        if parent_descriptor is None:
            os.replace(temporary, path)
        else:
            os.replace(temporary.name, path, src_dir_fd=parent_descriptor)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            if temporary is not None:
                try:
                    info = os.stat(
                        temporary.name if parent_descriptor is not None else temporary,
                        dir_fd=parent_descriptor, follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    if (info.st_dev, info.st_ino) == temporary_identity:
                        os.unlink(temporary.name if parent_descriptor is not None else temporary,
                                  dir_fd=parent_descriptor)
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int | None = None,
    private_parent: bool = False,
    pre_replace: Callable[[], None] | None = None,
) -> None:
    atomic_write_bytes(
        path,
        text.encode("utf-8"),
        mode=mode,
        private_parent=private_parent,
        pre_replace=pre_replace,
    )


def secure_copy(source: Path, destination: Path) -> None:
    """Copy sensitive bytes without ever creating a broadly readable target."""
    atomic_write_bytes(destination, source.read_bytes(), mode=0o600)


@contextmanager
def secure_temporary_path(
    directory: Path,
    *,
    prefix: str = ".envy-",
    suffix: str = "",
) -> Iterator[Path]:
    """Yield a same-filesystem 0600 temporary path and always remove it."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=str(directory), prefix=prefix, suffix=suffix)
    path = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        yield path
    finally:
        if path.exists():
            path.unlink()


def replace_prepared_file(source: Path, destination: Path, *, mode: int = 0o644) -> None:
    """Durably replace destination with an already prepared same-directory file."""
    source.chmod(mode)
    with source.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(source, destination)
    _fsync_directory(destination.parent)

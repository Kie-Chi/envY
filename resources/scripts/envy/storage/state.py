"""Private durable plans and lifecycle records, serialized by a user lock."""

from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from pathlib import Path

from envy.secure_io import atomic_write_text, ensure_private_directory
from envy.storage.model import StorageError, no_symlinks


def read_record(root: Path, kind: str, identifier: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise StorageError("Invalid storage record ID")
    path = root / kind / f"{identifier}.json"
    no_symlinks(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise StorageError("Storage record is missing or unreadable") from exc
    if not isinstance(data, dict) or data.get("id") != identifier:
        raise StorageError("Invalid storage record")
    return data


def write_record(root: Path, kind: str, record: dict) -> None:
    path = root / kind / f"{record['id']}.json"
    no_symlinks(path)
    atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                      mode=0o600, private_parent=True)


def records(root: Path, kind: str) -> list[dict]:
    return [read_record(root, kind, path.stem) for path in sorted((root / kind).glob("*.json"))]


@contextmanager
def locked(root: Path):
    no_symlinks(root)
    ensure_private_directory(root)
    path = root / "lock"
    no_symlinks(path)
    with path.open("a") as handle:
        path.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StorageError("Another storage operation is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

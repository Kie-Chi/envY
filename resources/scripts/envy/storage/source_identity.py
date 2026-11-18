"""Fingerprint only shipped, non-secret implementation sources."""

from __future__ import annotations

import hashlib
from pathlib import Path


def fingerprint() -> str:
    package = Path(__file__).parent
    paths = sorted(package.glob("*.py"))
    paths.extend(package.parent / name for name in
                 ("process.py", "secure_io.py", "transaction.py", "jsonio.py"))
    digest = hashlib.sha256(b"envy-storage-operations-v1\0")
    for path in paths:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()

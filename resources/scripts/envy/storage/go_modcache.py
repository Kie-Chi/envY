"""Bind explicit whole-module-cache maintenance to a verified Go target."""

from __future__ import annotations

import os
from pathlib import Path

from envy.storage.model import StorageError
from envy.storage.reporting import probe


def target(*, home: Path, cwd: Path | None = None, env: dict[str, str] | None = None,
           binary: str = "go") -> dict:
    cwd = cwd or Path.cwd()
    kwargs = {"cwd": cwd}
    if env is not None:
        kwargs["env"] = env
    result = probe([binary, "env", "GOMODCACHE"], **kwargs)
    if result.returncode != 0:
        raise StorageError("Go module cache target could not be verified")
    value = result.stdout.strip()
    path = Path(value)
    if (not value or not path.is_absolute() or ".." in path.parts
            or len(value.splitlines()) != 1):
        raise StorageError("Go module cache must be one absolute directory")
    resolved = path.resolve()
    if (resolved == home.resolve() or resolved in home.resolve().parents
            or resolved == cwd.resolve() or resolved in cwd.resolve().parents):
        raise StorageError("Go module cache cannot be the home, project or filesystem root")
    return {"path": str(path), "resolvedPath": str(resolved), "cwd": str(cwd)}


def verify(selected: dict, *, home: Path, env: dict[str, str] | None = None,
           binary: str = "go") -> None:
    if target(home=home, cwd=Path(selected["cwd"]), env=env, binary=binary) != selected:
        raise StorageError("Go module cache target changed; generate a fresh preview")


def environment(selected: dict) -> dict[str, str]:
    return dict(os.environ, GOMODCACHE=selected["resolvedPath"])

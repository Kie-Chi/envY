"""Merge declarations into writable application JSON/YAML/TOML configuration files."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

import tomllib

import tomli_w
import yaml

from envy.secure_io import atomic_write_text


def merge(base: Any, declared: Any) -> Any:
    """Recursively merge objects; declarations replace arrays and scalar values."""
    if isinstance(base, dict) and isinstance(declared, dict):
        result = deepcopy(base)
        for key, value in declared.items():
            result[key] = merge(base.get(key), value)
        return result
    return deepcopy(declared)


def contains(needle: Any, haystack: Any) -> bool:
    """Object subset comparison; arrays are exact, ordered structural values."""
    if isinstance(needle, dict) and isinstance(haystack, dict):
        return all(key in haystack and contains(value, haystack[key]) for key, value in needle.items())
    if isinstance(needle, list) and isinstance(haystack, list):
        return len(needle) == len(haystack) and all(
            contains(left, right) for left, right in zip(needle, haystack)
        )
    return equal(needle, haystack)


def equal(left: Any, right: Any) -> bool:
    """Structural equality with distinct boolean/numeric types and unordered keys."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


CONFLICT_MESSAGE = "Configuration changed concurrently. Close the application and retry."
CONCURRENCY_NOTICE = (
    "Optimistic checks do not lock the application; a writer can still race after the final check. "
    "Close the application before merging."
)
COMMAND = "structured-config.merge"


class ConflictError(ValueError):
    """The configuration or its path changed since it was read."""

    def __init__(self) -> None:
        super().__init__(CONFLICT_MESSAGE)


def _identity(info: os.stat_result, *, directory: bool = False) -> tuple[int, ...]:
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    # Directory timestamps change when our own temporary file is created.
    return identity if directory else identity + (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class _Parent:
    path: Path
    entry: tuple[int, ...] | None
    resolved: tuple[int, ...] | None = None
    link: str | None = None


def _parent(path: Path) -> _Parent:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return _Parent(path, None)
    resolved = path.stat()
    if not stat.S_ISDIR(resolved.st_mode):
        raise ValueError("configuration parent must be a directory")
    return _Parent(path, _identity(entry, directory=True), _identity(resolved, directory=True),
                   os.readlink(path) if stat.S_ISLNK(entry.st_mode) else None)


def _parents(target: Path) -> tuple[_Parent, ...]:
    return tuple(_parent(path) for path in reversed(target.parents))


@dataclass(frozen=True, repr=False)
class _Snapshot:
    parents: tuple[_Parent, ...]
    entry: tuple[int, ...] | None
    resolved: tuple[int, ...] | None = None
    link: str | None = None
    content: bytes = b""


def _capture(target: Path) -> _Snapshot:
    """Read only regular files, verifying identities and metadata around the read."""
    parents = _parents(target)
    try:
        entry = target.lstat()
    except FileNotFoundError:
        if _parents(target) != parents:
            raise ConflictError()
        # Detect a file appearing during the missing-file observation.
        if os.path.lexists(target):
            raise ConflictError()
        return _Snapshot(parents, None)
    is_link = stat.S_ISLNK(entry.st_mode)
    if not is_link and not stat.S_ISREG(entry.st_mode):
        raise ValueError("configuration must be a regular file or a valid file link")
    link = os.readlink(target) if is_link else None
    try:
        resolved = target.stat()
    except FileNotFoundError:
        if not is_link:
            raise ConflictError() from None
        try:
            unchanged_link = _identity(target.lstat()) == _identity(entry) and os.readlink(target) == link
        except OSError:
            unchanged_link = False
        if not unchanged_link:
            raise ConflictError() from None
        raise ValueError("dangling configuration link") from None
    if not stat.S_ISREG(resolved.st_mode):
        raise ValueError("configuration must resolve to a regular file")
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ConflictError() from None
    with os.fdopen(descriptor, "rb") as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(resolved):
            raise ConflictError()
        content = handle.read()
        if _identity(os.fstat(handle.fileno())) != _identity(resolved):
            raise ConflictError()
    try:
        unchanged = (
            _identity(target.lstat()) == _identity(entry)
            and _identity(target.stat()) == _identity(resolved)
            and (os.readlink(target) if is_link else None) == link
            and _parents(target) == parents
        )
    except OSError:
        raise ConflictError() from None
    if not unchanged:
        raise ConflictError()
    return _Snapshot(parents, _identity(entry), _identity(resolved), link, content)


def _changes(before: dict[str, Any], after: dict[str, Any], path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Describe final structural differences without retaining either value."""
    changes = []
    changed_keys = [key for key in before.keys() | after.keys()
                    if key not in before or key not in after or not equal(before[key], after[key])]
    for key in sorted(changed_keys, key=lambda key: (type(key).__name__, str(key))):
        current = (*path, key)
        if key not in before:
            if isinstance(after[key], dict) and after[key]:
                changes.extend(_changes({}, after[key], current))
            else:
                changes.append({"path": list(current), "operation": "add"})
        elif key not in after:
            # A removed object is one managed path; no user values or list members leak.
            changes.append({"path": list(current), "operation": "remove"})
        elif isinstance(before[key], dict) and isinstance(after[key], dict):
            changes.extend(_changes(before[key], after[key], current))
        elif not equal(before[key], after[key]):
            changes.append({"path": list(current), "operation": "change"})
    return changes


@dataclass(repr=False)
class PreparedUpdate:
    """Prepared private content plus a value-free preview and optimistic apply operation."""

    target: Path
    format: str
    _snapshot: _Snapshot = field(repr=False)
    _rendered: str = field(repr=False)
    _changes: list[dict[str, Any]] = field(repr=False)
    _mode: int
    _write: bool
    _chmod: bool

    def preview(self) -> dict[str, Any]:
        operations = []
        if self._write:
            operations.append("create" if self._snapshot.entry is None else "write")
        if self._snapshot.link is not None:
            operations.append("replace_symlink")
        if self._chmod:
            operations.append("chmod")
        return {"format": self.format, "changed": bool(operations),
                "changes": deepcopy(self._changes), "operations": operations}

    def _check(self) -> None:
        try:
            current = _capture(self.target)
        except (OSError, ValueError):
            raise ConflictError() from None
        if current != self._snapshot:
            raise ConflictError()

    def _create_parents(self) -> None:
        # Preserve every originally existing ancestor's baseline, updating only dirs we create.
        for index, parent in enumerate(self._snapshot.parents):
            if parent.entry is not None:
                continue
            self._check()
            try:
                parent.path.mkdir()
                created = _parent(parent.path)
            except OSError:
                raise ConflictError() from None
            parents = list(self._snapshot.parents)
            parents[index] = created
            self._snapshot = replace(self._snapshot, parents=tuple(parents))
            self._check()

    def apply(self) -> bool:
        """Return legacy content-write status; checks are optimistic, not application locks."""
        self._check()
        if not self._write:
            if self._chmod:
                try:
                    descriptor = os.open(self.target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                except OSError:
                    raise ConflictError() from None
                try:
                    if _identity(os.fstat(descriptor)) != self._snapshot.entry:
                        raise ConflictError()
                    self._check()
                    os.fchmod(descriptor, self._mode)
                finally:
                    os.close(descriptor)
            return False
        self._create_parents()
        try:
            atomic_write_text(self.target, self._rendered, mode=self._mode, pre_replace=self._check)
        except OSError:
            # Directory binding checks can fail before the pre-replace guard is reached.
            self._check()
            raise
        return True


def prepare_update(
    target: Path, declared: dict[str, Any], *, format: str = "json",
    managed_key_prefixes: dict[str, str] | None = None,
    managed_keys: list[str] | None = None,
) -> PreparedUpdate:
    """Prepare a merge and preview without creating directories, changing modes or writing."""
    if format not in {"json", "yaml", "toml"} or not isinstance(declared, dict):
        raise ValueError("expected an object and json, yaml or toml format")
    if managed_key_prefixes is not None and not isinstance(managed_key_prefixes, dict):
        raise ValueError("managed key prefixes must be an object")
    if managed_keys is not None and (not isinstance(managed_keys, list) or not all(isinstance(key, str) for key in managed_keys)):
        raise ValueError("managed keys must be a list of strings")
    # Keep symlinks and '..' for filesystem traversal and ancestor drift checks.
    target = Path(target)
    if not target.is_absolute():
        target = Path.cwd() / target
    snapshot = _capture(target)
    mode = (snapshot.resolved[2] & 0o777) | 0o600 if snapshot.resolved is not None else 0o600
    try:
        text = snapshot.content.decode("utf-8")
        existing = (
            json.loads(text) if format == "json" else
            tomllib.loads(text) if format == "toml" else yaml.safe_load(text)
        ) if text.strip() else {}
    except (ValueError, yaml.YAMLError):
        raise ValueError("cannot parse configuration") from None
    if existing is None and format == "yaml":
        existing = {}
    if not isinstance(existing, dict):
        raise ValueError("configuration root must be an object")
    base = deepcopy(existing)
    for key in managed_keys or []:
        base.pop(key, None)
    for section, prefix in (managed_key_prefixes or {}).items():
        if not isinstance(prefix, str) or not prefix or not isinstance(declared.get(section), dict):
            raise ValueError("managed key prefixes require a non-empty namespace and a declared object")
        members = base.get(section, {})
        if not isinstance(members, dict):
            raise ValueError("managed configuration section must be an object")
        base[section] = {key: value for key, value in members.items()
                         if not isinstance(key, str) or (key != prefix and not key.startswith(prefix + "-"))}
    updated = merge(base, declared)
    try:
        changes = _changes(existing, updated)
        rendered = (
            json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            if format == "json" else tomli_w.dumps(updated) if format == "toml"
            else yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
        )
    except (ValueError, TypeError, yaml.YAMLError):
        raise ValueError("cannot render configuration") from None
    write = snapshot.entry is None or snapshot.link is not None or not equal(existing, updated)
    chmod = snapshot.resolved is not None and (snapshot.resolved[2] & 0o777) != mode
    return PreparedUpdate(target, format, snapshot, rendered, changes, mode, write, chmod)


def update_file(
    target: Path, declared: dict[str, Any], *, format: str = "json",
    managed_key_prefixes: dict[str, str] | None = None,
    managed_keys: list[str] | None = None,
) -> bool:
    """Keep the activation API while checking for concurrent application writes."""
    return prepare_update(target, declared, format=format,
                          managed_key_prefixes=managed_key_prefixes, managed_keys=managed_keys).apply()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default errors can echo a secret argument or invalid JSON body.
        raise ValueError("invalid merge arguments")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    json_output = "--json" in argv
    parser = _Parser(description=__doc__, epilog=CONCURRENCY_NOTICE)
    parser.add_argument("--format", choices=["json", "yaml", "toml"], default="json")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--managed-key-prefixes", type=json.loads, default={})
    parser.add_argument("--managed-keys", type=json.loads, default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    code = "invalid_arguments"
    try:
        args = parser.parse_args(argv)
        code = "invalid_configuration"
        prepared = prepare_update(args.target, json.loads(args.source.read_text()), format=args.format,
                                  managed_key_prefixes=args.managed_key_prefixes, managed_keys=args.managed_keys)
        preview = prepared.preview()
        if not args.dry_run:
            prepared.apply()
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        if isinstance(error, ConflictError):
            code, message = "conflict", CONFLICT_MESSAGE
        elif isinstance(error, OSError):
            code, message = "io_error", "Cannot access configuration; original retained."
        else:
            message = "Cannot merge configuration; original retained."
        if json_output:
            from envy import jsonio
            jsonio.emit_error(COMMAND, message, code=code)
        else:
            print(message, file=sys.stderr)
        return 1
    if args.json:
        from envy import jsonio
        jsonio.emit(COMMAND, data=preview, warnings=[CONCURRENCY_NOTICE])
    elif args.dry_run:
        print(json.dumps(preview, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

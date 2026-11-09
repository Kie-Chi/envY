"""Validate machine-owned storage policy and filesystem boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


class StorageError(ValueError):
    """A storage operation could not meet its required conditions."""


@dataclass(frozen=True)
class Rule:
    id: str
    path: str
    action: str
    extensions: tuple[str, ...] = ()
    minAgeDays: int = 30
    minSizeBytes: int = 0


@dataclass(frozen=True)
class Policy:
    home: Path
    mode: str
    quarantine_days: int
    rules: tuple[Rule, ...]
    protected: tuple[Path, ...]
    repository: str
    password_file: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()


def absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StorageError("Expected a nonempty absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise StorageError("Storage paths must be absolute and cannot contain '..'")
    return path


def no_symlinks(path: Path) -> None:
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise StorageError(f"Symbolic link in storage path: {part}")


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _integer(value: object, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise StorageError(f"{name} must be an integer >= {minimum}")
    return value


def policy_from_manifest(manifest: dict, state: Path, checkout: Path) -> Policy:
    raw = manifest.get("storage", {})
    if not isinstance(raw, dict):
        raise StorageError("Invalid manifest storage policy")
    home = absolute_path(manifest.get("settings", {}).get("envy.user.home", str(Path.home())))
    mode = raw.get("mode", "manual")
    if mode not in {"manual", "report", "auto"}:
        raise StorageError("Storage mode must be manual, report, or auto")
    days = _integer(raw.get("quarantineDays", 30), "quarantineDays", 1)
    archive = raw.get("archive", {})
    if not isinstance(archive, dict):
        raise StorageError("Invalid storage archive policy")
    repository = archive.get("repository", "")
    password_file = archive.get("passwordFile", "")
    if not isinstance(repository, str) or not isinstance(password_file, str):
        raise StorageError("Archive repository and passwordFile must be strings")
    if repository and not (
        re.fullmatch(r"sftp:[A-Za-z0-9_.@-]+:/[^\s\x00]*", repository)
        or repository.startswith("/")
    ):
        raise StorageError("Archive repository must be sftp:user@host:/path or an absolute mounted path")
    if repository.startswith("/"):
        absolute_path(repository)
    if password_file:
        absolute_path(password_file)
    protected = [state, checkout, home / ".ssh", home / ".gnupg", home / ".config/sops",
                 home / "Library/Keychains", home / "Library/Mail", home / "Library/Messages",
                 home / "Library/Application Support", home / "Library/Containers",
                 home / "Library/Group Containers", home / ".local/share"]
    if repository.startswith("/"):
        protected.append(absolute_path(repository))
    if password_file:
        protected.append(absolute_path(password_file))
    extra = raw.get("protectedPaths", [])
    if not isinstance(extra, list):
        raise StorageError("protectedPaths must be a list")
    protected.extend(absolute_path(path) for path in extra)
    entries = raw.get("rules", [])
    if not isinstance(entries, list):
        raise StorageError("Storage rules must be a list")
    rules = []
    ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise StorageError("Every storage rule must be an attribute set")
        rule_id = entry.get("id", "")
        if not isinstance(rule_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", rule_id) or rule_id in ids:
            raise StorageError("Storage rule IDs must be unique lowercase identifiers")
        ids.add(rule_id)
        root = absolute_path(entry.get("path"))
        if root == home or not inside(root, home):
            raise StorageError("Storage rule roots must be directories below the managed home")
        action = entry.get("action")
        if action not in {"quarantine", "archive"}:
            raise StorageError(f"Unsupported action in rule {rule_id}")
        if action == "archive" and (not repository or not password_file):
            raise StorageError("Archive rules require a repository and password file")
        extensions = entry.get("extensions", [])
        if not isinstance(extensions, list) or any(
            not isinstance(ext, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", ext)
            for ext in extensions
        ):
            raise StorageError("Extensions must be plain suffixes without dots or wildcards")
        rules.append(Rule(rule_id, str(root), action, tuple(extensions),
                          _integer(entry.get("minAgeDays", 30), "minAgeDays", 1),
                          _integer(entry.get("minSizeBytes", 0), "minSizeBytes", 0)))
    return Policy(home, mode, days, tuple(rules), tuple(protected), repository, password_file)


def validate_file(path: Path, rule: Rule, policy: Policy) -> os.stat_result:
    root = Path(rule.path)
    if path == root or not inside(path, root):
        raise StorageError("Candidate escaped its configured root")
    no_symlinks(path)
    if any(inside(path, protected) for protected in policy.protected):
        raise StorageError("Protected storage path")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StorageError("Only regular files with one hard link are eligible")
    if info.st_uid != os.getuid():
        raise StorageError("Candidate is not owned by the current user")
    return info


def signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

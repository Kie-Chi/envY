"""Validate machine-owned storage policy and filesystem boundaries."""

from __future__ import annotations

import glob
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
    targets: str = "files"
    depth: int | None = None
    names: tuple[str, ...] = ()
    recursive_age: bool = False
    processes: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    min_age_days: int = 30
    min_size_bytes: int = 0


@dataclass(frozen=True)
class Policy:
    home: Path
    mode: str
    quarantine_days: int
    rules: tuple[Rule, ...]
    protected: tuple[Path, ...]
    mandatory: tuple[Path, ...]
    overrides: tuple[Path, ...]
    repository: str
    password_file: str
    maintenance: tuple[str, ...] = ()

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


def expand(pattern: str) -> list[Path]:
    """Expand shell wildcards here; organize does not expand location paths."""
    path = absolute_path(pattern)
    if not any(char in pattern for char in "*?["):
        return [path]
    return sorted(Path(match) for match in glob.glob(pattern, recursive=False))


def no_symlinks(path: Path) -> None:
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise StorageError(f"Symbolic link in storage path: {part}")


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _integer(value: int, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise StorageError(f"{name} must be an integer greater than or equal to {minimum}")
    return value


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise StorageError(f"{name} must be a list of nonempty strings")
    return value


def _validate_override(pattern: str, home: Path, general: list[Path]) -> Path:
    path = absolute_path(pattern)
    if path == home or not inside(path, home):
        raise StorageError("Protected overrides must be directories below the managed home")
    matching = [root for root in general if inside(path, root) and path != root]
    if not matching:
        raise StorageError("Protected overrides must name a directory below protected application data")
    relative = path.relative_to(max(matching, key=lambda root: len(root.parts))).parts
    if len(relative) < 2 or relative[-1] == "*":
        raise StorageError("Protected overrides must name a specific cache or update directory")
    return path


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
    if repository and not (re.fullmatch(r"sftp:[A-Za-z0-9_.@-]+:/[^\s\x00]*", repository)
                           or repository.startswith("/")):
        raise StorageError("Archive repository must be sftp:user@host:/path or an absolute mounted path")
    if repository.startswith("/"):
        absolute_path(repository)
    if password_file:
        absolute_path(password_file)
    general = [
        home / "Library/Application Support",
        home / "Library/Containers",
        home / "Library/Group Containers",
        home / ".local/share",
    ]
    mandatory = [
        state,
        checkout,
        home / ".ssh",
        home / ".gnupg",
        home / ".config/sops",
        home / "Library/Keychains",
        home / "Library/Mail",
        home / "Library/Messages",
    ]
    if repository.startswith("/"):
        mandatory.append(absolute_path(repository))
    if password_file:
        mandatory.append(absolute_path(password_file))
    extra = [absolute_path(path) for path in _strings(raw.get("protectedPaths", []), "protectedPaths")]
    overrides: list[Path] = []
    for pattern in _strings(raw.get("protectedOverrides", []), "protectedOverrides"):
        _validate_override(pattern, home, general)
        overrides.extend(expand(pattern))
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
        targets = entry.get("targets", "files")
        if targets not in {"files", "dirs"}:
            raise StorageError(f"Unsupported targets in rule {rule_id}")
        if action == "archive" and targets == "dirs":
            raise StorageError("Archive actions currently support regular files only")
        extensions = _strings(entry.get("extensions", []), "extensions")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", ext) for ext in extensions):
            raise StorageError("Extensions must be plain suffixes without dots or wildcards")
        if targets == "dirs" and extensions:
            raise StorageError("Directory rules cannot filter by extension")
        depth = entry.get("depth")
        if depth is not None:
            depth = _integer(depth, "depth", 1)
        names = _strings(entry.get("names", []), "names")
        if any("/" in name for name in names):
            raise StorageError("Rule names must be plain entry names")
        recursive_age = entry.get("recursiveAge", False)
        if type(recursive_age) is not bool:
            raise StorageError("recursiveAge must be a boolean")
        if recursive_age and targets != "dirs":
            raise StorageError("recursiveAge requires directory targets")
        processes = _strings(entry.get("processes", []), "processes")
        rules.append(Rule(rule_id, str(root), action, targets, depth, tuple(names),
                          recursive_age, tuple(processes), tuple(extensions),
                          _integer(entry.get("minAgeDays", 30), "minAgeDays", 1),
                          _integer(entry.get("minSizeBytes", 0), "minSizeBytes", 0)))
    maintenance_raw = raw.get("maintenance", {})
    if not isinstance(maintenance_raw, dict):
        raise StorageError("Invalid storage maintenance policy")
    maintenance = []
    for name in ("nix", "docker", "go"):
        value = maintenance_raw.get(name, False)
        if not isinstance(value, bool):
            raise StorageError("Storage maintenance flags must be booleans")
        if value:
            maintenance.append(name)
    protected = tuple([state, checkout, *general, *mandatory[2:], *extra])
    return Policy(home, mode, days, tuple(rules), protected, tuple(mandatory),
                  tuple(overrides), repository, password_file, tuple(maintenance))


def is_protected(path: Path, policy: Policy) -> bool:
    for root in policy.protected:
        if not inside(path, root):
            continue
        if root in policy.mandatory:
            return True
        if any(inside(path, override) for override in policy.overrides):
            continue
        return True
    return False


def tree_digest(path: Path) -> str:
    """Hash every descendant so a cached directory is frozen as a whole."""
    entries = []
    for current, directories, files in os.walk(path, followlinks=False):
        directories.sort()
        files.sort()
        for name in [*directories, *files]:
            entry = Path(current) / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise StorageError("Directory contains a symbolic link")
            if stat.S_ISDIR(info.st_mode):
                kind = "d"
            elif stat.S_ISREG(info.st_mode):
                kind = "f"
            else:
                raise StorageError("Directory contains an unsupported file type")
            entries.append([str(entry.relative_to(path)), kind, info.st_size, info.st_mtime_ns])
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def entry_bytes(path: Path, targets: str) -> int:
    if targets == "files":
        return path.lstat().st_size
    total = 0
    for current, _directories, files in os.walk(path, followlinks=False):
        total += sum((Path(current) / name).lstat().st_blocks * 512 for name in files)
    return total


def newest_mtime(path: Path) -> float:
    newest = path.lstat().st_mtime
    for current, _directories, files in os.walk(path, followlinks=False):
        newest = max(newest, Path(current).lstat().st_mtime)
        for name in files:
            newest = max(newest, (Path(current) / name).lstat().st_mtime)
    return newest


def validate_entry(path: Path, root: Path, rule: Rule, policy: Policy) -> os.stat_result:
    if path == root or not inside(path, root):
        raise StorageError("Candidate escaped its configured root")
    no_symlinks(path)
    if is_protected(path, policy):
        raise StorageError("Protected storage path")
    info = path.lstat()
    if rule.targets == "files":
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StorageError("Only regular files with one hard link are eligible")
    elif not stat.S_ISDIR(info.st_mode):
        raise StorageError("Directory rule matched a non-directory entry")
    if info.st_uid != os.getuid():
        raise StorageError("Candidate is not owned by the current user")
    return info


def signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

"""Validate machine-owned storage policy and filesystem boundaries."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


MODEL_VERSION = 2


class StorageError(ValueError):
    """A storage operation could not meet its required conditions."""

    def __init__(self, message: str, *, code: str = "storage_error"):
        super().__init__(message)
        self.code = code


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
    protected_overrides: tuple[str, ...] = ()
    optional_root: bool = False
    source: str = "machine"
    overridden_fields: tuple[str, ...] = ()


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
    excluded: tuple[str, ...] = ()
    extra_protected: tuple[Path, ...] = ()
    mount_source: str = ""
    machine_id: str = ""
    platform: str = ""

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps({"modelVersion": MODEL_VERSION, **asdict(self)}, sort_keys=True, default=str).encode()).hexdigest()


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
    roots = [Path(path.anchor)]
    for component in path.parts[1:]:
        following = []
        for root in roots:
            try:
                if any(char in component for char in "*?["):
                    no_symlinks(root)
                    with os.scandir(root) as entries:
                        following.extend(root / entry.name for entry in entries
                                         if fnmatch.fnmatchcase(entry.name, component)
                                         and (not entry.name.startswith(".") or component.startswith(".")))
                else:
                    candidate = root / component
                    candidate.lstat()
                    following.append(candidate)
            except (FileNotFoundError, NotADirectoryError):
                continue
        roots = following
    return sorted(roots)


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
    if len(relative) < 2 or any(char in relative[-1] for char in "*?["):
        raise StorageError("Protected overrides must name a specific cache or update directory")
    return path


def _expand_overrides(pattern: str, home: Path, general: list[Path]) -> list[Path]:
    _validate_override(pattern, home, general)
    return [_validate_override(str(path), home, general) for path in expand(pattern)]


def policy_from_manifest(manifest: dict, state: Path, checkout: Path) -> Policy:
    raw = manifest.get("storage", {})
    if not isinstance(raw, dict):
        raise StorageError("Invalid manifest storage policy")
    home = absolute_path(manifest.get("settings", {}).get("envy.user.home", str(Path.home())))
    mode = raw.get("mode", "manual")
    if not isinstance(mode, str) or mode not in {"manual", "report", "auto"}:
        raise StorageError("Storage mode must be manual, report, or auto")
    days = _integer(raw.get("quarantineDays", 30), "quarantineDays", 1)
    archive = raw.get("archive", {})
    if not isinstance(archive, dict):
        raise StorageError("Invalid storage archive policy")
    repository = archive.get("repository", "")
    password_file = archive.get("passwordFile", "")
    mount_source = archive.get("mountSource", "")
    if not all(isinstance(value, str) for value in (repository, password_file, mount_source)):
        raise StorageError("Archive repository, passwordFile and mountSource must be strings")
    if mount_source and (not repository.startswith("/") or mount_source != mount_source.strip()
                         or re.search(r"[\x00-\x1f\x7f]", mount_source)
                         or re.search(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^/]*:[^/]*@", mount_source)):
        raise StorageError("Archive mountSource must be a non-secret mount identity for an absolute repository")
    if repository and not (re.fullmatch(r"sftp:[A-Za-z0-9_.@-]+:/[^\s\x00]*", repository)
                           or repository.startswith("/")):
        raise StorageError("Archive repository must be sftp:user@host:/path or an absolute mounted path")
    if repository.startswith("/"):
        absolute_path(repository)
        if not mount_source:
            raise StorageError("An absolute archive repository requires mountSource")
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
        home / "Library/Application Support/sops",
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
        overrides.extend(_expand_overrides(pattern, home, general))
    entries = raw.get("rules", [])
    excluded = []
    if isinstance(entries, dict):
        if set(entries) - {"include", "exclude", "effective"}:
            raise StorageError("Unknown storage rule selection fields")
        excluded = _strings(entries.get("exclude", []), "rules.exclude")
        if (len(set(excluded)) != len(excluded)
                or any(not re.fullmatch(r"[a-z][a-z0-9-]*", item) for item in excluded)):
            raise StorageError("Excluded rule IDs must be unique lowercase identifiers")
        includes = entries.get("include", {})
        if (not isinstance(includes, dict)
                or any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", key)
                       or not isinstance(value, dict) for key, value in includes.items())):
            raise StorageError("rules.include must contain merged rule definitions")
        entries = entries.get("effective")
    if not isinstance(entries, list):
        raise StorageError("Storage rules must be a list or contain an effective list")
    rules = []
    ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise StorageError("Every storage rule must be an attribute set")
        rule_id = entry.get("id", "")
        if not isinstance(rule_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", rule_id) or rule_id in ids:
            raise StorageError("Storage rule IDs must be unique lowercase identifiers")
        ids.add(rule_id)
        if rule_id in excluded:
            raise StorageError("Excluded rule appears in effective rules: " + rule_id)
        allowed = {"id", "path", "action", "targets", "depth", "names", "recursiveAge",
                   "processes", "extensions", "minAgeDays", "minSizeBytes", "protectedOverrides",
                   "optionalRoot", "source", "overriddenFields"}
        if set(entry) - allowed:
            raise StorageError("Unknown fields in rule " + rule_id)
        source = entry.get("source", "machine")
        if not isinstance(source, str) or source not in {"machine", "default:common", "default:darwin", "default:linux"}:
            raise StorageError("Invalid rule source: " + rule_id)
        optional_root = entry.get("optionalRoot", False)
        if type(optional_root) is not bool:
            raise StorageError("optionalRoot must be a boolean")
        overridden = _strings(entry.get("overriddenFields", []), "overriddenFields")
        if set(overridden) - (allowed - {"id", "source", "overriddenFields"}):
            raise StorageError("Invalid overriddenFields in rule " + rule_id)
        rule_overrides = _strings(entry.get("protectedOverrides", []), "protectedOverrides")
        for pattern in rule_overrides:
            overrides.extend(_expand_overrides(pattern, home, general))
        root = absolute_path(entry.get("path"))
        if root == home or not inside(root, home):
            raise StorageError("Storage rule roots must be directories below the managed home")
        action = entry.get("action")
        if not isinstance(action, str) or action not in {"quarantine", "archive"}:
            raise StorageError(f"Unsupported action in rule {rule_id}")
        if action == "archive" and (not repository or not password_file):
            raise StorageError("Archive rules require a repository and password file")
        targets = entry.get("targets", "files")
        if not isinstance(targets, str) or targets not in {"files", "dirs"}:
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
                          _integer(entry.get("minSizeBytes", 0), "minSizeBytes", 0),
                          tuple(rule_overrides), optional_root, source, tuple(overridden)))
    maintenance_raw = raw.get("maintenance", {})
    if not isinstance(maintenance_raw, dict):
        raise StorageError("Invalid storage maintenance policy")
    maintenance = []
    for name in ("nix", "docker", "go", "go-modcache", "uv", "npm", "pip", "brew"):
        value = maintenance_raw.get(name, False)
        if not isinstance(value, bool):
            raise StorageError("Storage maintenance flags must be booleans")
        if value:
            maintenance.append(name)
    protected = tuple([state, checkout, *general, *mandatory[2:], *extra])
    return Policy(home, mode, days, tuple(rules), protected, tuple(mandatory),
                  tuple(overrides), repository, password_file, tuple(maintenance), tuple(excluded), tuple(extra), mount_source,
                  manifest.get("id", ""), manifest.get("platform", ""))


def is_protected(path: Path, policy: Policy) -> bool:
    for root in policy.protected:
        if inside(root, path):
            return True
        if not inside(path, root):
            continue
        if root in policy.mandatory or root in policy.extra_protected:
            return True
        if any(inside(path, override) for override in policy.overrides):
            continue
        return True
    return False


def rule_details(rule: Rule) -> dict:
    return {"id": rule.id, "path": rule.path, "action": rule.action, "targets": rule.targets,
            "depth": rule.depth, "names": list(rule.names), "recursiveAge": rule.recursive_age,
            "processes": list(rule.processes), "extensions": list(rule.extensions),
            "minAgeDays": rule.min_age_days, "minSizeBytes": rule.min_size_bytes,
            "protectedOverrides": list(rule.protected_overrides), "optionalRoot": rule.optional_root,
            "source": rule.source, "overriddenFields": list(rule.overridden_fields)}


def _walk_error(error: OSError) -> None:
    raise error


def tree_digest(path: Path) -> str:
    """Hash every descendant so a cached directory is frozen as a whole."""
    entries = []
    for current, directories, files in os.walk(path, followlinks=False, onerror=_walk_error):
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
            if info.st_uid != os.getuid() or (kind == "f" and info.st_nlink != 1):
                raise StorageError("Directory contains entries with unsafe ownership or hard links")
            entries.append([str(entry.relative_to(path)), kind, info.st_size, info.st_mtime_ns])
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def entry_bytes(path: Path, targets: str) -> int:
    if targets == "files":
        return path.lstat().st_size
    total = 0
    for current, _directories, files in os.walk(path, followlinks=False, onerror=_walk_error):
        total += sum((Path(current) / name).lstat().st_blocks * 512 for name in files)
    return total


def newest_mtime(path: Path) -> float:
    newest = path.lstat().st_mtime
    for current, _directories, files in os.walk(path, followlinks=False, onerror=_walk_error):
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
    if rule.targets == "dirs" and any(inside(protected, path) and is_protected(protected, policy)
                                      for protected in policy.protected):
        raise StorageError("Directory contains a protected storage path")
    if info.st_uid != os.getuid():
        raise StorageError("Candidate is not owned by the current user")
    return info


def signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

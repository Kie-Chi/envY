"""Evaluate and cache the selected Nix machine for envY's read-only views."""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from envy import log
from envy.process import run_process
from envy.utils import ENVY_ROOT, current_machine_id, machine_manifest_attr, platform_name


CACHE_SCHEMA = 3
_CACHE_ENV = "ENVY_NO_CACHE"
_COMMAND_TIMEOUT = 5
_last_manifest_error: str | None = None


def _cache_disabled() -> bool:
    value = os.environ.get(_CACHE_ENV, "").strip().casefold()
    return value not in {"", "0", "false", "no", "off"}


def _cache_path(machine_id: str) -> Path:
    root = os.environ.get("XDG_CACHE_HOME", "").strip()
    cache_root = Path(root).expanduser() if root else Path.home() / ".cache"
    return cache_root / "envy" / "manifests" / platform_name() / f"{machine_id}.json"


def _command_bytes(command: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            command,
            cwd=str(ENVY_ROOT),
            capture_output=True,
            timeout=_COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _nix_version() -> bytes | None:
    return _command_bytes(["nix", "--version"])


def _worktree_entry(raw_path: bytes, hash_name: str) -> bytes | None:
    """Return the Git mode and object ID this worktree path would be staged as.

    An absent path yields an empty entry. Objects Nix may read differently
    from Git (directories, sockets, devices) yield None, so the caller does
    not cache that snapshot.
    """
    path = ENVY_ROOT / os.fsdecode(raw_path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b""
    except OSError:
        return None
    digest = hashlib.new(hash_name)
    try:
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(os.fsencode(path))
            digest.update(b"blob %d\0" % len(target))
            digest.update(target)
            mode = b"120000"
        elif stat.S_ISREG(metadata.st_mode):
            # Never follow a symlink or block on a FIFO swapped in after lstat.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_ino != metadata.st_ino:
                    return None
                digest.update(b"blob %d\0" % opened.st_size)
                size = 0
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != opened.st_size:
                return None
            mode = b"100755" if opened.st_mode & stat.S_IXUSR else b"100644"
        else:
            return None
    except OSError:
        return None
    return mode + b" " + digest.hexdigest().encode()


def _tree_digest(index: bytes, modified: bytes, untracked: bytes) -> str | None:
    """Hash every visible path as Git would stage it, regardless of history.

    Unchanged index entries contribute their recorded mode and blob ID, so no
    file is read twice. Paths that differ from the index, are unmerged, or are
    untracked are hashed from the worktree with Git's object format. The
    result therefore stays the same when a change is staged or committed.
    """
    entries: dict[bytes, bytes] = {}
    worktree = {path for path in modified.split(b"\0") if path}
    worktree.update(path for path in untracked.split(b"\0") if path)
    hash_name = "sha1"
    for record in index.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            return None
        mode, object_id, stage = fields
        hash_name = "sha256" if len(object_id) == 64 else "sha1"
        if stage != b"0":
            worktree.add(path)
        else:
            entries[path] = mode + b" " + object_id
    for path in worktree:
        entry = _worktree_entry(path, hash_name)
        if entry is None:
            return None
        if entry:
            entries[path] = entry
        else:
            entries.pop(path, None)
    digest = hashlib.sha256()
    for path in sorted(entries):
        digest.update(path + b"\0" + entries[path] + b"\0")
    return digest.hexdigest()


def _repository_fingerprint(machine_id: str) -> str | None:
    """Return a content fingerprint of the repository files Nix can see.

    Only paths, Git file modes, and contents count. HEAD and the staged state
    do not, so a commit, amend, or `git add` of unchanged content keeps the
    cached manifest valid.
    """
    started = time.monotonic()
    index = _command_bytes(["git", "ls-files", "--stage", "-z"])
    modified = _command_bytes([
        "git", "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--",
    ])
    untracked = _command_bytes(["git", "ls-files", "--others", "--exclude-standard", "-z"])
    nix_version = _nix_version()
    tree = (
        _tree_digest(index, modified, untracked)
        if None not in (index, modified, untracked)
        else None
    )
    if tree is None or nix_version is None:
        log.debug(
            "eval", "repository fingerprint unavailable",
            machine=machine_id, elapsed=f"{time.monotonic() - started:.3f}s",
        )
        return None

    inputs = {
        "schema": CACHE_SCHEMA,
        "machine": machine_id,
        "platform": platform_name(),
        "tree": tree,
        "nix": nix_version.decode(errors="replace").strip(),
    }
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = _digest(encoded)
    log.debug(
        "eval", "repository fingerprint ready", machine=machine_id,
        fingerprint=fingerprint[:12], elapsed=f"{time.monotonic() - started:.3f}s",
    )
    return fingerprint


def _read_cache(machine_id: str, fingerprint: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_cache_path(machine_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    manifest = value.get("manifest")
    if (
        value.get("schema") != CACHE_SCHEMA
        or value.get("machine") != machine_id
        or value.get("fingerprint") != fingerprint
        or not isinstance(manifest, dict)
    ):
        return None
    return manifest


def _write_cache(machine_id: str, fingerprint: str, manifest: dict[str, Any]) -> None:
    """Atomically store a successful evaluation; cache failures are non-fatal."""
    path = _cache_path(machine_id)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "schema": CACHE_SCHEMA,
            "machine": machine_id,
            "fingerprint": fingerprint,
            "created_at": int(time.time()),
            "manifest": manifest,
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_latest_cache(machine_id: str) -> dict[str, Any] | None:
    """Read a structurally valid manifest cache without running Git or Nix.

    Shell completion must remain fast and side-effect free. A stale manifest is
    acceptable for suggestions; evaluating the machine to refresh it is not.
    """
    try:
        value = json.loads(_cache_path(machine_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA:
        return None
    if value.get("machine") != machine_id:
        return None
    manifest = value.get("manifest")
    return manifest if isinstance(manifest, dict) else None


def informative_nix_error(stderr: str, *, fallback: str = "nix eval exited without stderr") -> str:
    """Explain explicit envY diagnostics; preserve unrecognized stderr verbatim."""
    from envy.error import parse_nix_error, summary

    diagnostic = parse_nix_error(stderr or "")
    if diagnostic is not None:
        return summary([diagnostic])
    return stderr if (stderr or "").strip() else fallback


def _evaluate_machine_manifest(machine_id: str) -> dict[str, Any] | None:
    global _last_manifest_error
    _last_manifest_error = None
    attr = machine_manifest_attr(machine_id)
    started = time.monotonic()
    log.debug("eval", "Nix manifest evaluation started", machine=machine_id, attr=attr)
    result = run_process(
        ["nix", "eval", "--impure", attr, "--json"],
        cwd=ENVY_ROOT, capture=True, timeout=30, check=False,
        activity=f"evaluate machine {machine_id}",
    )
    if result.returncode != 0:
        _last_manifest_error = informative_nix_error(result.stderr or "")
        log.debug(
            "eval", "Nix manifest evaluation failed", machine=machine_id,
            exit_code=result.returncode, detail=_last_manifest_error,
            elapsed=f"{time.monotonic() - started:.3f}s",
        )
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _last_manifest_error = f"invalid Nix manifest JSON: {exc}"
        log.debug(
            "eval", "Nix manifest JSON invalid", machine=machine_id,
            reason=str(exc), elapsed=f"{time.monotonic() - started:.3f}s",
        )
        return None
    log.debug(
        "eval", "Nix manifest evaluation completed", machine=machine_id,
        elapsed=f"{time.monotonic() - started:.3f}s",
    )
    return value if isinstance(value, dict) else None


def last_manifest_error() -> str | None:
    """Return the latest Nix manifest evaluation diagnostic without truncation."""
    return _last_manifest_error


def _load_machine_manifest(
    *, read_cache: bool, write_cache: bool, allow_evaluation: bool = True,
) -> dict[str, Any] | None:
    machine_id = current_machine_id()
    if not allow_evaluation:
        return _read_latest_cache(machine_id)

    fingerprint = _repository_fingerprint(machine_id) if (read_cache or write_cache) else None
    if read_cache and fingerprint is not None:
        cached = _read_cache(machine_id, fingerprint)
        if cached is not None:
            log.debug(
                "eval", "manifest cache hit", machine=machine_id,
                cache=str(_cache_path(machine_id)), fingerprint=fingerprint[:12],
            )
            return cached

    log.debug(
        "eval", "manifest cache miss", machine=machine_id,
        cache=str(_cache_path(machine_id)),
        fingerprint=fingerprint[:12] if fingerprint else "unavailable",
    )

    manifest = _evaluate_machine_manifest(machine_id)
    if manifest is None or not write_cache or fingerprint is None:
        return manifest

    # Never associate an evaluation with a source snapshot that changed while
    # Nix was running. A later invocation will evaluate the new snapshot.
    if _repository_fingerprint(machine_id) == fingerprint:
        _write_cache(machine_id, fingerprint, manifest)
        log.debug(
            "eval", "manifest cache updated", machine=machine_id,
            cache=str(_cache_path(machine_id)), fingerprint=fingerprint[:12],
        )
    return manifest


@lru_cache(maxsize=1)
def _memoized_machine_manifest() -> dict[str, Any] | None:
    return _load_machine_manifest(read_cache=True, write_cache=True)


def invalidate_machine_manifest() -> None:
    """Clear the current process' memoized manifest after a source write."""
    _memoized_machine_manifest.cache_clear()


def machine_manifest(
    *,
    refresh: bool = False,
    write_cache: bool = True,
    completion: bool = False,
) -> dict[str, Any] | None:
    """Return the manifest, optionally using only the existing cache for completion."""
    if completion:
        return _load_machine_manifest(
            read_cache=False, write_cache=False, allow_evaluation=False,
        )
    if not write_cache:
        return _load_machine_manifest(
            read_cache=not refresh and not _cache_disabled(),
            write_cache=False,
        )
    if refresh:
        invalidate_machine_manifest()
        return _load_machine_manifest(read_cache=False, write_cache=True)
    if _cache_disabled():
        invalidate_machine_manifest()
        return _load_machine_manifest(read_cache=False, write_cache=False)
    return _memoized_machine_manifest()


def manifest_settings(manifest: dict[str, Any] | None) -> dict[str, str]:
    """Normalize evaluated scalar settings for the config editor/view."""
    if not manifest or not isinstance(manifest.get("settings"), dict):
        return {}
    values: dict[str, str] = {}
    for path, value in manifest["settings"].items():
        if isinstance(value, bool):
            values[str(path)] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            values[str(path)] = str(value)
    return values


def manifest_selection_rows(
    manifest: dict[str, Any] | None,
) -> Iterator[tuple[str, list[str], list[str], list[str]]]:
    """Yield option path plus evaluated software selection IDs."""
    for group in manifest_software_groups(manifest).values():
        path = group.get("optionPath")
        selection = group.get("selection")
        if not isinstance(path, str) or not isinstance(selection, dict):
            continue
        yield (
            path,
            _entry_ids(selection.get("include")),
            _string_list(selection.get("exclude")),
            _entry_ids(selection.get("effective")),
        )


def manifest_software_groups(
    manifest: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return validated manifest v2 software groups keyed by canonical ID."""
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") not in (2, 3):
        return {}
    software = manifest.get("software")
    groups = software.get("groups") if isinstance(software, dict) else None
    if not isinstance(groups, dict):
        return {}
    return {
        str(group_id): group
        for group_id, group in groups.items()
        if isinstance(group, dict)
    }


def _entry_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
        elif isinstance(item, str):
            ids.append(item)
    return ids


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]

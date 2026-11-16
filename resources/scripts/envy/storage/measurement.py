"""Bounded, metadata-only storage measurements without following links."""

from __future__ import annotations

import errno
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path


class ScanLimit(OSError):
    """The cooperative scan budget was exhausted."""


@dataclass
class ScanBudget:
    max_entries: int = 1_000_000
    seconds: float = 30
    max_depth: int = 64
    entries: int = 0
    deadline: float = field(init=False)

    def __post_init__(self):
        self.deadline = time.monotonic() + self.seconds

    def check(self, *, entry: bool = False):
        if time.monotonic() >= self.deadline or self.entries >= self.max_entries:
            raise ScanLimit("scan_budget_exhausted")
        if entry:
            self.entries += 1


def open_directory(path: Path) -> int:
    """Bind each absolute path component; reject intermediate symlinks too."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Inspection paths must be absolute without '..'")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise OSError(errno.ELOOP, "symlink_skipped")
            child = os.open(component, flags, dir_fd=descriptor)
            after = os.fstat(child)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(child)
                raise OSError(errno.ESTALE, "directory_changed")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _reason(error: OSError) -> str:
    if isinstance(error, ScanLimit):
        return "scan_budget_exhausted"
    return {errno.EACCES: "permission_denied", errno.EPERM: "permission_denied",
            errno.ENOENT: "entry_disappeared", errno.ELOOP: "symlink_skipped",
            errno.ENOTDIR: "not_directory", errno.ESTALE: "directory_changed"}.get(
                error.errno, "filesystem_error")


def measure(path: Path, budget: ScanBudget) -> dict:
    """Return complete estimates or explicit unknowns and observed lower bounds."""
    result = {"logicalBytes": None, "allocatedBytes": None, "observedLogicalBytes": 0,
              "observedAllocatedBytes": 0, "status": "unknown", "reasons": [],
              "symlinksSkipped": 0, "hardlinksDeduplicated": 0,
              "allocationIsEstimate": True}
    logical = allocated = 0
    seen: set[tuple[int, int]] = set()
    reasons: set[str] = set()
    stack = []
    try:
        budget.check()
        descriptor = open_directory(path)
    except FileNotFoundError:
        # Distinguish an initially absent path from a missing child during scan.
        result.update(status="absent", logicalBytes=0, allocatedBytes=0)
        return result
    except OSError as exc:
        reason = _reason(exc)
        result.update(status="skipped" if reason == "symlink_skipped" else "unknown",
                      reasons=[reason])
        return result
    try:
        root = os.fstat(descriptor)
        device = root.st_dev
        seen.add((device, root.st_ino))
        allocated += root.st_blocks * 512
        stack.append((descriptor, os.scandir(descriptor)))
        descriptor = None
        while stack:
            budget.check()
            parent, entries = stack[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                os.close(parent)
                stack.pop()
                continue
            except OSError as exc:
                reasons.add(_reason(exc))
                entries.close()
                os.close(parent)
                stack.pop()
                continue
            budget.check(entry=True)
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    result["symlinksSkipped"] += 1
                    continue
                if info.st_dev != device:
                    reasons.add("mount_skipped")
                    continue
                identity = info.st_dev, info.st_ino
                if identity in seen:
                    result["hardlinksDeduplicated"] += 1
                    continue
                seen.add(identity)
                if stat.S_ISREG(info.st_mode):
                    logical += info.st_size
                    allocated += info.st_blocks * 512
                elif stat.S_ISDIR(info.st_mode):
                    if len(stack) >= budget.max_depth:
                        reasons.add("depth_limit")
                        continue
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != identity:
                            raise OSError(errno.ESTALE, "directory_changed")
                        allocated += opened.st_blocks * 512
                        iterator = os.scandir(child)
                    except BaseException:
                        os.close(child)
                        raise
                    stack.append((child, iterator))
                else:
                    reasons.add("special_file_skipped")
            except OSError as exc:
                reasons.add(_reason(exc))
    except OSError as exc:
        reasons.add(_reason(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for descriptor, iterator in reversed(stack):
            iterator.close()
            os.close(descriptor)
    result.update(observedLogicalBytes=logical, observedAllocatedBytes=allocated,
                  reasons=sorted(reasons), status="partial" if reasons else "complete")
    if not reasons:
        result.update(logicalBytes=logical, allocatedBytes=allocated)
    return result

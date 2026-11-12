"""Restic archive backend with streamed content verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from envy.process import run_process
from envy.secure_io import secure_temporary_path
from envy.storage.model import Policy, StorageError, absolute_path, no_symlinks


_SFTP_SSH_OPTIONS = (
    "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=8",
    "-oUpdateHostKeys=no",
    "-oPreferredAuthentications=publickey", "-oPasswordAuthentication=no",
    "-oKbdInteractiveAuthentication=no",
)


def _mount_unescape(value: str) -> str:
    return re.sub(r"\\(040|011|012|134)", lambda match: chr(int(match[1], 8)), value)


def _mount_source(mount: Path) -> str:
    """Read the OS mount table and fail closed on missing or ambiguous identity."""
    sources = []
    try:
        if sys.platform == "darwin":
            result = run_process(["/sbin/mount"], capture=True, check=False, timeout=10)
            if result.returncode:
                raise StorageError("Cannot verify archive mount source")
            pattern = r"(.+) on " + re.escape(str(mount)) + r" \([^()\n]+\)"
            for line in (result.stdout or "").splitlines():
                match = re.fullmatch(pattern, _mount_unescape(line))
                if match:
                    sources.append(match[1])
        elif sys.platform.startswith("linux"):
            for line in Path("/proc/self/mountinfo").read_text().splitlines():
                fields = line.split()
                if "-" not in fields:
                    raise StorageError("Cannot verify archive mount source")
                separator = fields.index("-")
                if separator < 6 or len(fields) < separator + 4:
                    raise StorageError("Cannot verify archive mount source")
                if _mount_unescape(fields[4]) == str(mount):
                    sources.append(_mount_unescape(fields[separator + 2]))
        else:
            raise StorageError("Cannot verify archive mount source on this platform")
    except (OSError, UnicodeError) as exc:
        raise StorageError("Cannot read archive mount identity") from exc
    if len(sources) != 1 or not sources[0]:
        raise StorageError("Cannot uniquely verify archive mount source")
    return sources[0]


class Archive:
    def __init__(self, policy: Policy, state: Path):
        self.policy = policy
        self.state = state

    def environment(self) -> dict[str, str]:
        if not self.policy.repository or not self.policy.password_file:
            raise StorageError("Configure the archive repository and sops password first")
        # sops-nix publishes secrets through managed symlink directories.
        try:
            password = Path(self.policy.password_file).resolve(strict=True)
            info = password.stat()
        except (OSError, RuntimeError) as exc:
            raise StorageError("Archive password file is unavailable") from exc
        if (not password.is_file() or info.st_mode & 0o077
                or info.st_uid not in {0, os.getuid()}):
            raise StorageError("Archive password file must be private")
        if self.policy.repository.startswith("/"):
            if not self.policy.mount_source:
                raise StorageError("Configure archive mountSource for the mounted repository")
            repository = absolute_path(self.policy.repository)
            no_symlinks(repository)
            mount = next((parent for parent in [repository, *repository.parents]
                          if parent.is_mount()), None)
            if mount is None or mount == Path("/") or str(mount).startswith("/System/"):
                raise StorageError("Archive destination is not on an available mounted volume")
            if _mount_source(mount) != self.policy.mount_source:
                raise StorageError("Archive mounted volume source does not match mountSource")
        env = {key: value for key, value in os.environ.items() if not key.startswith("RESTIC_")}
        env.update(RESTIC_REPOSITORY=self.policy.repository,
                   RESTIC_PASSWORD_FILE=self.policy.password_file,
                   RESTIC_CACHE_DIR=str(self.state / "restic-cache"))
        return env

    def _argv(self, args: list[str]) -> list[str]:
        options = ["-o", "sftp.args=" + " ".join(_SFTP_SSH_OPTIONS)] if self.policy.repository.startswith("sftp:") else []
        return ["restic", *options, *args]

    def command(self, args: list[str], *, timeout: int = 3600) -> str:
        result = run_process(self._argv(args), env=self.environment(),
                             capture=True, check=False, timeout=timeout)
        if result.returncode:
            raise StorageError(f"restic {args[0]} failed (exit {result.returncode}); local files retained")
        return result.stdout or ""

    def identity(self) -> str:
        try:
            value = json.loads(self.command(["cat", "config"], timeout=60))["id"]
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError("Invalid repository ID")
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise StorageError("Cannot verify restic repository identity") from exc

    def backup(self, paths: list[str], batch_id: str) -> str:
        with secure_temporary_path(self.state, suffix=".paths") as listing:
            listing.write_bytes(b"\x00".join(os.fsencode(path) for path in paths) + b"\x00")
            output = self.command(["backup", "--json", "--compression", "auto",
                                   "--tag", "envy-archive", "--tag", f"envy-run:{batch_id}",
                                   "--files-from-raw", str(listing)])
        try:
            entries = [json.loads(line) for line in output.splitlines() if line.strip()]
            if any(not isinstance(entry, dict) for entry in entries):
                raise ValueError("Invalid backup progress record")
            summaries = [entry for entry in entries if entry.get("message_type") == "summary"]
            snapshot = summaries[-1]["snapshot_id"]
            if not isinstance(snapshot, str) or len(snapshot) != 64:
                raise ValueError("Invalid snapshot ID")
            return snapshot
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise StorageError("restic did not return a complete snapshot") from exc

    def content_hash(self, snapshot: str, path: str) -> str:
        # Dump to a streaming hash rather than restoring a second large local copy.
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(self._argv(["dump", snapshot, path]), env=self.environment(),
                                  stdout=subprocess.PIPE, stderr=errors, start_new_session=True) as process:
                def terminate():
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                timer = threading.Timer(3600, terminate)
                timer.daemon = True
                timer.start()
                try:
                    digest = hashlib.file_digest(process.stdout, "sha256").hexdigest()
                    code = process.wait()
                finally:
                    timer.cancel()
                if code:
                    raise StorageError("Archive content verification failed; local file retained")
                return digest

    def restore(self, snapshot: str, target: Path, paths: list[str]) -> None:
        args = ["restore", snapshot, "--target", str(target), "--verify"]
        for path in paths:
            pattern = "".join("\\" + char if char in "\\*?[]" else char for char in path)
            args.extend(["--include", pattern])
        self.command(args)

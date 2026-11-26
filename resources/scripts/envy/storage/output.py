"""Live, sanitized native output with private bounded durable tails."""

from __future__ import annotations

import codecs
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import BinaryIO, Callable

from envy.secure_io import atomic_write_bytes, ensure_private_directory
from envy.storage.model import StorageError, no_symlinks

_STREAM_LIMIT = 1024 * 1024
_LINE_LIMIT = 16 * 1024
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s<>\"']+")
_CREDENTIAL = re.compile(
    r"(?im)([\"']?(?:[\w.-]{0,64}[_-])?(?:password|passwd|pwd|token|secret|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|credential[s]?|_?auth(?:[_-]?token)?|"
    r"authorization|proxy-authorization|"
    r"cookie|set-cookie)[\"']?\s*[:=]\s*).*"
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+\S+")


def sanitize_output(text: str) -> str:
    """Remove terminal controls and common credentials before displaying text."""
    text = _ANSI.sub("", text).replace("\r", " ")
    text = "".join(char for char in text
                   if char == "\n" or not unicodedata.category(char).startswith("C"))

    def url(match: re.Match[str]) -> str:
        value = match.group()
        scheme, rest = value.split("://", 1)
        authority, slash, path = rest.partition("/")
        # Userinfo can contain query punctuation, so remove it first.
        authority = re.sub(r"^.*@", "[redacted]@", authority)
        value = scheme + "://" + authority + slash + path
        value = re.sub(r"[?#].*", "?[redacted]", value)
        return value

    text = _URL.sub(url, text)
    text = _CREDENTIAL.sub(r"\1[redacted]", text)
    return _BEARER.sub(r"\1 [redacted]", text)


class _Lines:
    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.text = ""
        self.size = 0
        self.omitted = False

    def feed(self, data: bytes, *, final: bool = False) -> None:
        parts = data.split(b"\n")
        for index, part in enumerate(parts):
            self.size += len(part)
            if self.size > _LINE_LIMIT:
                self.omitted = True
                self.text = ""
            elif not self.omitted:
                self.text += self.decoder.decode(part)
            if index < len(parts) - 1:
                self._line()
        if final and (self.size or self.omitted):
            self._line()

    def _line(self) -> None:
        if self.omitted:
            text = "[overlong output line omitted]"
        else:
            text = sanitize_output(self.text + self.decoder.decode(b"", final=True))
        self.emit(text + "\n")
        self.decoder.reset()
        self.text = ""
        self.size = 0
        self.omitted = False


class NativeOutput:
    """Capture through run_process's binary stdout/stderr arguments.

    Construction validates and creates log files before the caller runs a command.
    finish() drains output but leaves raw captures seekable until context exit.
    Call raise_if_failed() after finish() to report safe error categories explicitly;
    failures are retained in metadata and never raised implicitly at context exit.
    Callbacks run on the reader thread and must return promptly. The producer must
    have stopped before finish().
    """

    def __init__(
        self, directory: Path | None, action: str, step: int,
        on_output: Callable[[str, str, str], None] | None = None,
    ):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", action):
            raise ValueError("Invalid native output action")
        if type(step) is not int or step < 0:
            raise ValueError("Invalid native output step")
        self.action = action
        self.on_output = on_output
        self.metadata: dict = {"truncatedStreams": []}
        self._failures: dict[str, BaseException] = {}
        self._paths: dict[str, Path] = {}
        self._tails = {channel: bytearray() for channel in ("stdout", "stderr")}
        self._stop = threading.Event()
        self._drained = threading.Event()
        self._finished = False
        self._callback_failed = False
        if directory is not None:
            no_symlinks(directory)
            ensure_private_directory(directory)
            for channel in self._tails:
                path = directory / f"{step:03d}-{action}-{channel}.log"
                no_symlinks(path)
                self._write(path, b"")
                self._paths[channel] = path
                self.metadata[channel + "Path"] = str(path)
        self.stdout: BinaryIO = tempfile.TemporaryFile(mode="w+b", buffering=0)
        try:
            self.stderr: BinaryIO = tempfile.TemporaryFile(mode="w+b", buffering=0)
            self._thread = threading.Thread(
                target=self._read, name=f"envy-output-{action}-{step}", daemon=True,
            )
            self._thread.start()
        except BaseException:
            self.stdout.close()
            if hasattr(self, "stderr"):
                self.stderr.close()
            raise

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        no_symlinks(path)
        atomic_write_bytes(path, data, mode=0o600, private_parent=True,
                           pre_replace=lambda: no_symlinks(path))

    def _failure(self, category: str, exc: BaseException) -> None:
        # Retain the cause for diagnostics, but never serialize its text or chain it.
        self._failures.setdefault(category, exc)
        self.metadata["outputErrors"] = sorted(self._failures)
        if category.endswith("Write"):
            self.metadata["logError"] = "Native output log could not be saved"

    def _emit(self, channel: str, text: str) -> None:
        tail = self._tails[channel]
        tail.extend(text.encode("utf-8"))
        if len(tail) > _STREAM_LIMIT:
            del tail[:len(tail) - _STREAM_LIMIT]
            # A byte cap may land inside a multibyte code point.
            while tail and tail[0] & 0xC0 == 0x80:
                del tail[0]
            if channel not in self.metadata["truncatedStreams"]:
                self.metadata["truncatedStreams"].append(channel)
        if self.on_output is not None and not self._callback_failed:
            try:
                self.on_output(self.action, channel, text)
            except BaseException as exc:
                self._callback_failed = True
                self._failure("callback", exc)

    def _read(self) -> None:
        streams = {"stdout": self.stdout, "stderr": self.stderr}
        offsets = dict.fromkeys(streams, 0)
        lines = {channel: _Lines(lambda text, channel=channel: self._emit(channel, text))
                 for channel in streams}
        failed = set()
        try:
            while True:
                stopping = self._stop.is_set()
                read_any = False
                for channel, handle in streams.items():
                    if channel in failed:
                        continue
                    try:
                        data = os.pread(handle.fileno(), 65536, offsets[channel])
                        if data:
                            offsets[channel] += len(data)
                            lines[channel].feed(data)
                            read_any = True
                    except BaseException as exc:
                        failed.add(channel)
                        self._failure(channel + "Read", exc)
                if stopping and not read_any:
                    break
                if not read_any:
                    self._stop.wait(0.02)
            for channel in streams:
                lines[channel].feed(b"", final=True)
        except BaseException as exc:
            self._failure("reader", exc)
        finally:
            self._drained.set()

    def finish(self) -> None:
        """Idempotently stop, drain, join and persist sanitized tails."""
        if self._finished:
            return
        interrupted: BaseException | None = None
        try:
            for channel in self._tails:
                try:
                    getattr(self, channel).flush()
                except BaseException as exc:
                    self._failure(channel + "Flush", exc)
        finally:
            self._stop.set()
            # Never close captures while the reader could still use them.
            try:
                self._thread.join()
            except BaseException as exc:
                # An interrupted Thread.join may mark the thread stopped before
                # its target returns. Wait for the reader's own completion signal.
                interrupted = exc
                self._drained.wait()
                self._thread.join()
        self._finished = True
        for channel, path in self._paths.items():
            try:
                self._write(path, bytes(self._tails[channel]))
            except BaseException as exc:
                self._failure(channel + "Write", exc)
        if interrupted is not None:
            raise interrupted

    def tail(self, channel: str) -> bytes:
        """Return only already-sanitized bytes after draining the producer."""
        if not self._finished:
            raise RuntimeError("Native output must be finished before reading its tail")
        return bytes(self._tails[channel])

    def raise_if_failed(self) -> None:
        """Report fixed categories, without secret-bearing exception messages."""
        if not self._finished:
            raise RuntimeError("Native output must be finished before checking failures")
        if self._failures:
            raise StorageError("Native output failed: " + ", ".join(sorted(self._failures))) from None

    def __enter__(self) -> NativeOutput:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.finish()
        finally:
            self.stdout.close()
            self.stderr.close()

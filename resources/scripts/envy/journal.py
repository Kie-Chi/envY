"""Persistent operation journal for state-changing envy commands.

Records a compact, append-only JSONL history of apply/sync/push/update/rollback/
clean operations so the CLI and TUI can answer "what did I run, when, and did it
succeed" — something Nix generations alone do not capture.

Storage lives under the persistent state directory (see ``utils.state_dir``), not
the cache, so it survives cache clears and ``envy clean``.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
import functools
import fcntl
import json
import os
import stat
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import typer
from rich.table import Table

from envy import log
from envy.process import CommandError
from envy.secure_io import ensure_private_directory
from envy.utils import current_machine_id, platform_name, state_dir

JOURNAL_SCHEMA_VERSION = 1


def journal_path() -> Path:
    return state_dir() / "journal.jsonl"


def append(record: dict[str, Any]) -> bool:
    """Append one JSONL record durably.

    Writers hold an advisory lock across the complete encoded record and fsync.
    I/O failures never break the wrapped operation and return False.
    """
    path = journal_path()
    try:
        ensure_private_directory(path.parent)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return False
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError as exc:
        log.debug("journal", "append failed", error=str(exc))
        return False


def read(
    *,
    limit: int | None = None,
    failed: bool = False,
    operation: str | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return journal records newest-first, after optional filtering."""
    path = journal_path()
    records: list[dict[str, Any]] = []
    try:
        if not os.path.lexists(path):
            return []
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("journal must be a regular file")
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("journal", "skipped malformed line")
                    continue
                if (not isinstance(record, dict) or not isinstance(record.get("operationId", ""), str)
                        or not isinstance(record.get("operation", ""), str)
                        or not isinstance(record.get("result", ""), str)):
                    continue
                records.append(record)
    except (OSError, UnicodeError) as exc:
        log.debug("journal", "read failed", error=str(exc))
        if strict:
            raise RuntimeError("Cannot read operation journal") from exc
        return []
    grouped: dict[str, dict[str, Any]] = {}
    observed_starts: set[str] = set()
    pending_terminals: dict[str, str] = {}
    legacy = []
    for record in records:
        operation_id = record.get("operationId")
        if not operation_id:
            legacy.append(record)
            continue
        event = record.get("event")
        if event == "terminal-confirmed":
            # A matching later confirmation is emitted only after terminal fsync succeeds.
            # Its readable presence proves that earlier sync, even if its own sync failed.
            if (operation_id in pending_terminals
                    and record.get("terminalId") == pending_terminals[operation_id]):
                grouped[operation_id]["journalPersisted"] = True
            continue
        entry = grouped.setdefault(operation_id, {"stages": [], "result": "interrupted", "terminal": None,
                                                  "journalPersisted": False})
        public = {key: value for key, value in record.items()
                  if key not in {"terminalId", "priorEventsPersisted", "journalPersisted"}}
        if event == "stage":
            stage = record.get("stage")
            if isinstance(stage, dict):
                entry["stages"].append(stage)
            entry.update({key: value for key, value in public.items()
                          if key not in {"stage", "stages", "terminal", "result"}})
        else:
            entry.update(public)
            if event == "started":
                observed_starts.add(operation_id)
                pending_terminals.pop(operation_id, None)
                entry.update(result="interrupted", terminal=None, journalPersisted=False)
            if event == "terminal":
                entry.update(terminal=record.get("result"), journalPersisted=False)
                pending_terminals.pop(operation_id, None)
                terminal_id = record.get("terminalId")
                if (operation_id in observed_starts and record.get("priorEventsPersisted") is True
                        and isinstance(terminal_id, str) and terminal_id):
                    pending_terminals[operation_id] = terminal_id
    records = legacy + list(grouped.values())
    for entry in records:
        entry["retry"] = retry_policy(entry)
    records.reverse()
    records.sort(key=lambda row: str(row.get("timestamp", "")), reverse=True)
    if failed:
        records = [row for row in records if row.get("result") in {"fail", "partial", "interrupted"}]
    if operation is not None:
        records = [row for row in records if row.get("operation") == operation]
    if limit is not None:
        return records[:limit]
    return records


@dataclass
class _Receipt:
    operation: str
    detail: dict
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="microseconds"))
    machine: str = field(default_factory=lambda: current_machine_id())
    platform: str = field(default_factory=lambda: platform_name())
    evidence: dict = field(default_factory=dict)
    persisted: bool = True
    activation_started: bool = False
    verification_incomplete: bool = False

    def record(self, event: str) -> dict:
        return {"schemaVersion": JOURNAL_SCHEMA_VERSION, "operationId": self.operation_id, "startedAt": self.started_at,
                "event": event, "timestamp": datetime.now().astimezone().isoformat(timespec="microseconds"),
                "operation": self.operation, "machine": self.machine, "platform": self.platform,
                "detail": self.detail, **self.evidence}


_active_receipt: ContextVar[_Receipt | None] = ContextVar("envy_operation_receipt", default=None)


def operation_stage(name: str, status: str, **evidence: Any) -> bool:
    """Append a workflow stage; return persistence evidence."""
    receipt = _active_receipt.get()
    if receipt is None:
        return False
    if name == "activate" and status == "started":
        receipt.activation_started = True
    if name == "verify" and status in {"unavailable", "unknown", "failed"}:
        receipt.verification_incomplete = True
    receipt.evidence.update(evidence)
    record = receipt.record("stage")
    record["stage"] = {"name": name, "status": status}
    receipt.persisted = append(record) and receipt.persisted
    return receipt.persisted


def retry_policy(entry: dict) -> dict:
    step = "verify-generation"
    if entry.get("operation") not in {"apply", "bootstrap", "sync"}:
        reason = "This operation has no safe retry step"
    elif entry.get("terminal") != "ok" or entry.get("result") != "ok":
        reason = "A complete successful terminal is required for retry"
    elif entry.get("journalPersisted") is not True:
        reason = "Operation receipt is not fully persisted"
    elif not entry.get("operationId") or not isinstance(entry.get("source"), dict) or not entry["source"].get("verified"):
        reason = "Verified frozen source evidence is unavailable"
    elif not entry.get("after") or not entry.get("binding"):
        reason = "No verified generation target is recorded"
    elif entry.get("machine") != current_machine_id() or entry.get("platform") != platform_name():
        reason = "Operation host or platform does not match"
    elif entry.get("conflict"):
        reason = "A recorded runtime conflict requires manual resolution"
    else:
        return {"allowed": True, "step": step, "reason": "Only read-only generation verification is repeatable",
                "requiresRevalidation": True}
    return {"allowed": False, "step": step, "reason": reason}


def baseline_for_generation(generation: Any) -> dict | None:
    if generation is None:
        return None
    expected = generation.to_dict()
    for entry in read():
        binding = entry.get("binding")
        if (entry.get("terminal") == "ok" and entry.get("result") == "ok" and entry.get("journalPersisted") is True
                and isinstance(entry.get("source"), dict) and entry["source"].get("verified") is True
                and isinstance(binding, dict) and binding.get("host") == current_machine_id()
                and binding.get("platform") == platform_name()
                and binding.get("profile") == str(generation.path.parent / generation.path.name.rsplit("-", 2)[0])
                and entry["source"].get("outputPath") == expected.get("target")
                and all(binding.get(key) == expected.get(key) for key in ("number", "path", "target"))
                and isinstance(entry.get("semanticBaseline"), dict)):
            return entry["semanticBaseline"]
    return None


def verify_retry(operation_id: str, step: str) -> dict:
    """Revalidate fixed read-only work; never execute journal strings."""
    if step != "verify-generation":
        raise ValueError("Unsafe retry step: only verify-generation is allowed")
    entry = next((row for row in read() if row.get("operationId") == operation_id), None)
    if entry is None:
        raise ValueError("Operation receipt was not found")
    policy = retry_policy(entry)
    if not policy["allowed"]:
        raise ValueError(policy["reason"])
    if entry.get("machine") != current_machine_id() or entry.get("platform") != platform_name():
        raise ValueError("Operation host or platform does not match")
    from envy.provenance import reverify_source
    from envy.workflows.generations import current_generation

    source = entry["source"]
    expected = entry["binding"]
    if (not isinstance(expected, dict) or expected.get("host") != current_machine_id()
            or expected.get("platform") != platform_name()):
        raise ValueError("Operation host or platform does not match")
    if not reverify_source(source, expected.get("target")):
        raise ValueError("Frozen source evidence could not be reverified")
    current = current_generation()
    if (current is None or expected.get("profile") != str(current.path.parent / current.path.name.rsplit("-", 2)[0])
            or any(current.to_dict().get(key) != expected.get(key) for key in ("number", "path", "target"))):
        raise ValueError("Active profile or generation target changed")
    return {"operationId": operation_id, "step": step, "status": "verified", "retry": policy,
            "generation": current.to_dict(), "scope": "Generation identity only; external effects remain unknown"}


@contextlib.contextmanager
def _span(operation: str, detail: dict[str, Any]) -> Iterator[None]:
    """Time a wrapped operation and record its outcome, re-raising everything."""
    receipt = _Receipt(operation, detail)
    token = _active_receipt.set(receipt)
    receipt.persisted = append(receipt.record("started"))
    started = time.monotonic()
    result = "fail"
    exit_code = 1
    try:
        yield
        result = "ok"
        exit_code = 0
    except typer.Exit as exc:
        code = getattr(exc, "exit_code", getattr(exc, "code", 0)) or 0
        result = "ok" if code == 0 else "fail"
        exit_code = code
        raise
    except typer.Abort:
        result = "fail"
        exit_code = 1
        raise
    except CommandError as exc:
        result = "fail"
        exit_code = exc.returncode
        raise
    except BaseException:
        result = "fail"
        exit_code = 1
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        if (result == "fail" and receipt.activation_started) or (result == "ok" and receipt.verification_incomplete):
            result = "partial"
        record = receipt.record("terminal")
        terminal_id = str(uuid.uuid4())
        record.update(durationMs=duration_ms, result=result, exitCode=exit_code,
                      journalPersisted=False, priorEventsPersisted=receipt.persisted, terminalId=terminal_id)
        try:
            saved = append(record)
            if saved and receipt.persisted:
                # Never publish confirmation before the terminal's fsync has succeeded.
                # These private fields stay on disk; read() derives the public persistence flag.
                confirmed = append({"schemaVersion": JOURNAL_SCHEMA_VERSION, "event": "terminal-confirmed",
                                    "operationId": receipt.operation_id, "terminalId": terminal_id})
                if not confirmed:
                    log.warn("journal", "terminal was persisted; confirmation durability is unknown; baseline requires readable confirmation")
            else:
                log.warn("journal", "operation receipt was not fully persisted; semantic baseline is unbound")
        finally:
            _active_receipt.reset(token)


def record_operation(
    operation: str,
    detail: Callable[..., dict[str, Any]] | None = None,
    skip: Callable[..., bool] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a workflow entry point to record a journal entry for each run.

    ``detail`` receives the wrapped call's args/kwargs and returns a small dict;
    keep it defensive (absorb ``**kwargs``) so signature changes never raise at
    call time. ``skip`` receives the same args and, when it returns True, runs
    the wrapped function without journaling — used to exclude read-only paths
    (e.g. ``rollback --dry-run`` / ``rollback list``).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                if skip is not None and skip(*args, **kwargs):
                    return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - skip must never break the op
                log.debug("journal", "skip callback failed", error=str(exc))
            try:
                detail_dict = detail(*args, **kwargs) if detail is not None else {}
            except Exception as exc:  # noqa: BLE001 - detail must never break the op
                log.debug("journal", "detail callback failed", error=str(exc))
                detail_dict = {}
            with _span(operation, detail_dict):
                return func(*args, **kwargs)

        return wrapper

    return decorator


# ==========================================
# RENDERING (envy log)
# ==========================================


def _format_detail(detail: Any) -> str:
    if not isinstance(detail, dict) or not detail:
        return ""
    return " ".join(f"{key}={value}" for key, value in detail.items())


def _duration_text(duration_ms: Any) -> str:
    try:
        seconds = int(duration_ms) / 1000.0
    except (TypeError, ValueError):
        return "—"
    return f"{seconds:.1f}s"


def snapshot(
    *,
    limit: int | None = None,
    failed: bool = False,
    operation: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": JOURNAL_SCHEMA_VERSION,
        "reportVersion": 1,
        "machine": current_machine_id(),
        "platform": platform_name(),
        "entries": read(limit=limit, failed=failed, operation=operation, strict=True),
    }


def render(payload: dict[str, Any]) -> None:
    entries = payload.get("entries") or []
    if not entries:
        log.warn("journal", "no operations recorded yet")
        return
    table = Table(title="envY operation journal", title_style="cyan", expand=False)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Operation", style="cyan", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    table.add_column("Duration", justify="right", no_wrap=True)
    table.add_column("Machine", no_wrap=True)
    table.add_column("Detail")
    for entry in entries:
        result = str(entry.get("result", ""))
        result_cell = (
            "[green]ok[/green]" if result == "ok" else f"[red]{result or 'fail'}[/red]"
        )
        table.add_row(
            str(entry.get("timestamp", "")),
            str(entry.get("operation", "")),
            result_cell,
            _duration_text(entry.get("durationMs")),
            str(entry.get("machine", "")),
            _format_detail(entry.get("detail")),
        )
    log.console.print(table)


def show_log(
    *,
    json_output: bool = False,
    limit: int | None = None,
    failed: bool = False,
    operation: str | None = None,
) -> None:
    from envy.behavior import emit_report

    try:
        payload = snapshot(limit=limit, failed=failed, operation=operation)
    except (RuntimeError, OSError, ValueError) as exc:
        if json_output:
            emit_report("log", {"entries": [], "status": "unavailable"}, ok=False, error=str(exc))
        else:
            log.error("journal", str(exc))
        raise typer.Exit(1) from exc
    if json_output:
        emit_report("log", payload)
    else:
        render(payload)

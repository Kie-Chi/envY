"""Small, stable JSON protocol helpers for envY frontends."""

from __future__ import annotations

import json
from typing import Any

from envy import log


JSON_SCHEMA_VERSION = 1


def write_document(payload: Any) -> None:
    """Write JSON without terminal styling, wrapping, or markup interpretation."""
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2), file=log.console.file)


def emit(
    command: str,
    *,
    data: Any = None,
    ok: bool = True,
    error: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Emit one parseable document for a non-interactive frontend."""
    payload: dict[str, Any] = {
        "schemaVersion": JSON_SCHEMA_VERSION,
        "command": command,
        "ok": ok,
        "data": data,
        "warnings": warnings or [],
    }
    if error is not None:
        payload["error"] = error
    write_document(payload)


def emit_error(command: str, message: str, *, code: str = "error") -> None:
    emit(command, ok=False, error={"code": code, "message": message})

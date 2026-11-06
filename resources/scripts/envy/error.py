"""Structured errors for envY-owned managed configuration blocks.

The config modules only detect problems; wording, codes, and reporting live
here so every command describes the same failure the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from envy import log
from envy.errno import ErrorCode


@dataclass(frozen=True)
class ConfigError:
    """One problem found in a managed block."""

    code: ErrorCode
    path: str
    message: str
    hint: str | None = None
    line: int | None = None


def managed_block_missing(path: str, *, hint: str) -> ConfigError:
    return ConfigError(ErrorCode.MANAGED_BLOCK_MISSING, path, "managed block is missing", hint)


def managed_block_markers(path: str, *, starts: int, ends: int) -> ConfigError:
    return ConfigError(
        ErrorCode.MANAGED_BLOCK_MARKERS,
        path,
        f"managed block markers are malformed (found {starts} begin and {ends} end marker(s))",
        "Keep exactly one BEGIN/END marker pair around the block.",
    )


def syntax(path: str, reason: str, *, line: int | None = None) -> ConfigError:
    return ConfigError(
        ErrorCode.MANAGED_BLOCK_SYNTAX,
        path,
        f"cannot parse the assignment: {reason}",
        "Use a literal envY can rewrite: quoted strings, numbers, booleans, lists, or builtins.fromJSON.",
        line,
    )


def duplicate(path: str, *, line: int | None = None) -> ConfigError:
    return ConfigError(
        ErrorCode.DUPLICATE_ASSIGNMENT,
        path,
        "assigned more than once",
        "Keep exactly one assignment per field.",
        line,
    )


def unsupported_object(path: str, *, hint: str | None = None) -> ConfigError:
    return ConfigError(
        ErrorCode.UNSUPPORTED_OBJECT,
        path,
        "whole-attribute-set assignment is not supported in the managed block",
        hint or "Write one assignment per field instead of a nested attribute set.",
    )


def unknown_field(path: str, allowed: Sequence[str]) -> ConfigError:
    return ConfigError(
        ErrorCode.UNKNOWN_FIELD,
        path,
        "unknown field for this configuration path",
        "Allowed fields: " + ", ".join(allowed),
    )


def invalid_type(path: str, expected: str) -> ConfigError:
    return ConfigError(ErrorCode.INVALID_TYPE, path, f"expected {expected}")


def invalid_value(path: str, message: str) -> ConfigError:
    return ConfigError(ErrorCode.INVALID_VALUE, path, message)


def invalid_choice(path: str, allowed: Sequence[str]) -> ConfigError:
    return ConfigError(
        ErrorCode.INVALID_CHOICE,
        path,
        "value is not one of the allowed choices",
        "Allowed values: " + ", ".join(allowed),
    )


def missing_required(path: str, field: str | None = None, *, hint: str | None = None) -> ConfigError:
    detail = f"missing required field: {field}" if field else "required field is not set"
    return ConfigError(ErrorCode.MISSING_REQUIRED, path, detail, hint)


def unknown_reference(path: str, kind: str, *, declared: str) -> ConfigError:
    return ConfigError(
        ErrorCode.UNKNOWN_REFERENCE,
        path,
        f"references an unknown {kind}",
        f"Declare it under {declared} first.",
    )


def not_found(kind: str, name: str, *, hint: str | None = None) -> ConfigError:
    """Report an unknown CLI target such as an agent, profile, or credential."""
    return ConfigError(ErrorCode.NOT_FOUND, "", f"unknown {kind}: {name}", hint)


def already_exists(kind: str, name: str, *, hint: str | None = None) -> ConfigError:
    return ConfigError(ErrorCode.ALREADY_EXISTS, "", f"{kind} already exists: {name}", hint)


def invalid_name(kind: str, name: str) -> ConfigError:
    return ConfigError(
        ErrorCode.INVALID_ARGUMENT,
        "",
        f"invalid {kind} name: {name}",
        "Names start with a letter or digit and use letters, digits, '_' or '-'.",
    )


def invalid_argument(message: str, *, hint: str | None = None) -> ConfigError:
    return ConfigError(ErrorCode.INVALID_ARGUMENT, "", message, hint)


def must_be_one_of(label: str, allowed: Sequence[str]) -> ConfigError:
    return ConfigError(
        ErrorCode.INVALID_CHOICE, "", f"{label} must be one of: {', '.join(allowed)}"
    )


def positive_integer(label: str) -> ConfigError:
    return ConfigError(ErrorCode.INVALID_ARGUMENT, "", f"{label} must be positive")


def invalid_path(path: str, *, expected: str, line: int | None = None) -> ConfigError:
    return ConfigError(
        ErrorCode.INVALID_PATH,
        path,
        "invalid managed field path",
        f"Expected {expected}.",
        line,
    )


def report(scope: str, errors: Iterable[ConfigError]) -> list[str]:
    """Log each problem with its code, line, and a deduplicated hint.

    Returns the plain paths so callers can keep their existing report shape.
    """
    paths: list[str] = []
    hints: list[str] = []
    for item in errors:
        values: dict[str, Any] = {}
        if item.path:
            values["field"] = item.path
        values["code"] = item.code.value
        if item.line is not None:
            values["line"] = item.line
        log.error(scope, item.message, **values)
        paths.append(item.path)
        if item.hint and item.hint not in hints:
            hints.append(item.hint)
    for hint in hints:
        log.hint(hint)
    return paths


def summary(errors: Iterable[ConfigError], *, prefix: str = "") -> str:
    """Return one actionable line for typer.BadParameter, hints included."""
    parts = []
    for item in errors:
        detail = f"{item.path}: {item.message}" if item.path else item.message
        if item.hint:
            detail += f" ({item.hint})"
        parts.append(detail)
    body = "; ".join(parts)
    return f"{prefix}: {body}" if prefix else body

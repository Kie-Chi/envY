"""Stable error codes for envY-owned managed configuration blocks.

The module name is deliberate: Python 3 resolves imports absolutely, so the
standard library `errno` module stays reachable and nothing here shadows it.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """One machine-readable identity per managed configuration problem."""

    MANAGED_BLOCK_MISSING = "managed-block-missing"
    MANAGED_BLOCK_MARKERS = "managed-block-markers"
    MANAGED_BLOCK_SYNTAX = "managed-block-syntax"
    DUPLICATE_ASSIGNMENT = "duplicate-assignment"
    UNSUPPORTED_OBJECT = "unsupported-object"
    UNKNOWN_FIELD = "unknown-field"
    INVALID_TYPE = "invalid-type"
    INVALID_VALUE = "invalid-value"
    INVALID_CHOICE = "invalid-choice"
    MISSING_REQUIRED = "missing-required"
    UNKNOWN_REFERENCE = "unknown-reference"
    INVALID_PATH = "invalid-path"
    NOT_FOUND = "not-found"
    ALREADY_EXISTS = "already-exists"
    INVALID_ARGUMENT = "invalid-argument"

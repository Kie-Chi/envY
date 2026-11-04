"""Merge declarations into writable application JSON/YAML/TOML configuration files."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any

import tomllib

import tomli_w
import yaml

from envy.secure_io import atomic_write_text


def merge(base: Any, declared: Any) -> Any:
    """Recursively merge objects; declarations replace arrays and scalar values."""
    if isinstance(base, dict) and isinstance(declared, dict):
        result = deepcopy(base)
        for key, value in declared.items():
            result[key] = merge(base.get(key), value)
        return result
    return deepcopy(declared)


def contains(needle: Any, haystack: Any) -> bool:
    """Object subset comparison; arrays are exact, ordered structural values."""
    if isinstance(needle, dict) and isinstance(haystack, dict):
        return all(key in haystack and contains(value, haystack[key]) for key, value in needle.items())
    if isinstance(needle, list) and isinstance(haystack, list):
        return len(needle) == len(haystack) and all(
            contains(left, right) for left, right in zip(needle, haystack)
        )
    return equal(needle, haystack)


def equal(left: Any, right: Any) -> bool:
    """Structural equality with distinct boolean/numeric types and unordered keys."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


def update_file(
    target: Path, declared: dict[str, Any], *, format: str = "json",
    managed_key_prefixes: dict[str, str] | None = None,
) -> bool:
    """Atomically replace store links, preserve runtime fields and owner writability."""
    if format not in {"json", "yaml", "toml"} or not isinstance(declared, dict):
        raise ValueError("expected an object and json, yaml or toml format")
    if managed_key_prefixes is not None and not isinstance(managed_key_prefixes, dict):
        raise ValueError("managed key prefixes must be an object")
    try:
        text = target.read_text()
        mode = (target.stat().st_mode & 0o777) | 0o600
    except FileNotFoundError:
        if target.is_symlink():
            raise ValueError(f"dangling configuration link: {target}") from None
        text = ""
        mode = 0o600
    try:
        existing = (
            json.loads(text)
            if format == "json"
            else tomllib.loads(text)
            if format == "toml"
            else yaml.safe_load(text)
        ) if text.strip() else {}
    except (ValueError, tomllib.TOMLDecodeError, yaml.YAMLError):
        raise ValueError(f"cannot parse configuration: {target}") from None
    if existing is None and format == "yaml":
        existing = {}
    if not isinstance(existing, dict):
        raise ValueError(f"configuration root must be an object: {target}")
    base = deepcopy(existing)
    for section, prefix in (managed_key_prefixes or {}).items():
        if not isinstance(prefix, str) or not prefix or not isinstance(declared.get(section), dict):
            raise ValueError("managed key prefixes require a non-empty namespace and a declared object")
        members = base.get(section, {})
        if not isinstance(members, dict):
            raise ValueError("managed configuration section must be an object")
        base[section] = {key: value for key, value in members.items()
                         if key != prefix and not key.startswith(prefix + "-")}
    updated = merge(base, declared)
    if target.exists() and not target.is_symlink() and equal(existing, updated):
        if target.stat().st_mode & 0o777 != mode:
            target.chmod(mode)
        return False
    rendered = (
        json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if format == "json"
        else tomli_w.dumps(updated)
        if format == "toml"
        else yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
    )
    atomic_write_text(target, rendered, mode=mode)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["json", "yaml", "toml"], default="json")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--managed-key-prefixes", type=json.loads, default={})
    args = parser.parse_args()
    try:
        update_file(args.target, json.loads(args.source.read_text()), format=args.format,
                    managed_key_prefixes=args.managed_key_prefixes)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        print(f"envY could not merge configuration; original retained: {args.target}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

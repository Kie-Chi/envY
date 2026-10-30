"""Managed config and mirror-override blocks of the selected machine file."""

import re
from pathlib import Path

from envy.evaluation import invalidate_machine_manifest
from envy.schemas.config import MACHINE_FIELDS, OBSOLETE_MACHINE_KEYS
from envy.secure_io import atomic_write_text
from envy.utils import (
    current_machine_id,
    machine_config_file as versioned_machine_file,
    set_device_machine_id,
)


MANAGED_START = "  # BEGIN ENVY MANAGED CONFIG"
MANAGED_END = "  # END ENVY MANAGED CONFIG"
MIRROR_MANAGED_START = "  # BEGIN ENVY MANAGED MIRROR OVERRIDES"
MIRROR_MANAGED_END = "  # END ENVY MANAGED MIRROR OVERRIDES"


def _escape_nix_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _unescape_nix_string(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _read_nix_assignments(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    string_pattern = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"((?:\\.|[^"])*)"\s*;')
    bool_pattern = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*(true|false)\s*;')
    for line in path.read_text().splitlines():
        string_match = string_pattern.match(line)
        if string_match:
            values[string_match.group(1)] = _unescape_nix_string(string_match.group(2))
            continue
        bool_match = bool_pattern.match(line)
        if bool_match:
            values[bool_match.group(1)] = bool_match.group(2)
    return values


def machine_config_file(machine_id: str | None = None) -> Path:
    return versioned_machine_file(machine_id)


def read_machine_nix(machine_id: str | None = None) -> dict:
    return _read_nix_assignments(machine_config_file(machine_id))


def _render_machine_block(values: dict) -> str:
    groups = sorted(set(f.group for f in MACHINE_FIELDS))
    lines = [MANAGED_START + "\n", "  # `envy config` updates only this block. Other machine policy stays intact.\n"]
    for group in groups:
        lines.append(f"\n  # --- {group} CONFIG ---\n")
        for f in MACHINE_FIELDS:
            if f.group != group:
                continue
            raw = str(values.get(f.path, f.default_fn()))
            if f.nix_type == "bool":
                if raw.lower() not in {"true", "false"}:
                    raise ValueError(f"{f.path} must be true or false")
                val = raw.lower()
                lines.append(f"  {f.path} = {val};\n")
            else:
                lines.append(f"  {f.path} = \"{_escape_nix_string(raw)}\";\n")
    lines.append(MANAGED_END)
    return "".join(lines)


def write_machine_nix(values: dict, machine_id: str | None = None) -> None:
    selected = machine_id or current_machine_id()
    path = machine_config_file(selected)
    if not path.exists():
        raise FileNotFoundError(f"machine configuration is missing: {path}")

    text = path.read_text()
    block = _render_machine_block(values)
    managed_pattern = re.compile(
        rf"(?ms)^{re.escape(MANAGED_START)}$.*?^{re.escape(MANAGED_END)}$"
    )
    if managed_pattern.search(text):
        updated = managed_pattern.sub(block, text, count=1)
    else:
        # Move flat assignments for managed fields into the controlled block
        # while leaving imports and all other machine policy untouched.
        managed_paths = {field.path for field in MACHINE_FIELDS} | set(OBSOLETE_MACHINE_KEYS)
        assignment = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=.*;\s*$")
        kept_lines = []
        for line in text.splitlines(keepends=True):
            match = assignment.match(line)
            if not match or match.group(1) not in managed_paths:
                kept_lines.append(line)
        text = "".join(kept_lines)
        closing = text.rfind("\n}")
        if closing < 0:
            raise ValueError(f"cannot locate the top-level closing brace in {path}")
        updated = text[:closing].rstrip() + "\n\n" + block + "\n" + text[closing:]

    atomic_write_text(path, updated if updated.endswith("\n") else updated + "\n")
    set_device_machine_id(selected)


def _mirror_managed_pattern() -> re.Pattern[str]:
    return re.compile(
        rf"(?ms)^{re.escape(MIRROR_MANAGED_START)}$.*?^{re.escape(MIRROR_MANAGED_END)}$"
    )


def read_mirror_overrides(machine_id: str | None = None) -> dict[str, str]:
    """Read envY-owned mirror assignments without interpreting hand policy."""
    path = machine_config_file(machine_id)
    if not path.exists():
        return {}
    text = path.read_text()
    match = _mirror_managed_pattern().search(text)
    if match is None:
        return {}
    assignments = re.compile(
        r'^\s*(envy\.mirrors\.overrides\.[A-Za-z0-9_.-]+)\s*=\s*"((?:\\.|[^"])*)"\s*;\s*$'
    )
    values: dict[str, str] = {}
    for line in match.group(0).splitlines():
        found = assignments.match(line)
        if found:
            values[found.group(1)] = _unescape_nix_string(found.group(2))
    return values


def _render_mirror_override_block(values: dict[str, str]) -> str:
    lines = [
        MIRROR_MANAGED_START + "\n",
        "  # `envy mirror` owns this block. Other machine policy stays intact.\n",
    ]
    for path, value in sorted(values.items()):
        if not re.fullmatch(r"envy\.mirrors\.overrides\.[A-Za-z0-9_.-]+", path):
            raise ValueError(f"invalid mirror override path: {path}")
        lines.append(f'  {path} = "{_escape_nix_string(value)}";\n')
    lines.append(MIRROR_MANAGED_END)
    return "".join(lines)


def write_mirror_overrides(
    values: dict[str, str], machine_id: str | None = None,
) -> None:
    """Atomically replace only the envY-generated mirror override block."""
    selected = machine_id or current_machine_id()
    path = machine_config_file(selected)
    if not path.exists():
        raise FileNotFoundError(f"machine configuration is missing: {path}")

    text = path.read_text()
    block = _render_mirror_override_block(values)
    pattern = _mirror_managed_pattern()
    if pattern.search(text):
        updated = pattern.sub(block, text, count=1)
    else:
        anchor = "  # END ENVY MANAGED CONFIG"
        anchor_index = text.find(anchor)
        if anchor_index >= 0:
            insert_at = text.find("\n", anchor_index)
            insert_at = len(text) if insert_at < 0 else insert_at + 1
        else:
            insert_at = text.rfind("\n}")
            if insert_at < 0:
                raise ValueError(f"cannot locate the top-level closing brace in {path}")
            insert_at += 1
        prefix = text[:insert_at].rstrip()
        suffix = text[insert_at:].lstrip("\n")
        updated = prefix + "\n\n" + block + "\n\n" + suffix

    atomic_write_text(path, updated if updated.endswith("\n") else updated + "\n")
    set_device_machine_id(selected)
    invalidate_machine_manifest()

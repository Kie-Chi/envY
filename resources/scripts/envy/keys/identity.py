"""Device-local sops label resolution backed by `.device-label`."""

from __future__ import annotations

import re

from envy.keys.storage import (
    current_device_public_key,
    read_sops_yaml_keys,
    validate_device_label,
)
from envy.utils import (
    device_metadata_is_toml,
    read_device_metadata,
    run_cmd,
    write_device_metadata,
)


def sanitize_label(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def get_sops_label() -> str:
    metadata = read_device_metadata()
    label = metadata.get("sops_label") or metadata.get("machine_id")
    if label:
        return sanitize_label(label)
    hostname = run_cmd(["hostname", "-s"], check=False, capture=True)
    return sanitize_label(hostname) if hostname else "unknown"


def set_sops_label(label: str) -> None:
    normalized = sanitize_label(label) or "unknown"
    validate_device_label(normalized)
    write_device_metadata(sops_label=normalized)


def ensure_sops_label(*, public_key: str | None = None, allow_new: bool = True) -> str:
    """Resolve a label without allowing stale metadata to overwrite another key."""
    stored = read_device_metadata().get("sops_label", "")
    keys = read_sops_yaml_keys()
    public_key = public_key or current_device_public_key()

    matching_labels = []
    if public_key:
        matching_labels = [
            label for label, value in keys.items()
            if label not in {"recovery", "recovery_new"}
            and not label.startswith("envy_rotation_old_")
            and value == public_key
        ]
        if len(matching_labels) > 1:
            raise ValueError("local age key is registered under multiple labels")

    if matching_labels:
        label = matching_labels[0]
        if stored != label or not device_metadata_is_toml():
            set_sops_label(label)
        return label

    if stored:
        label = sanitize_label(stored)
        validate_device_label(label)
        if label in keys:
            if public_key and keys[label] != public_key:
                raise ValueError(f"device sops label '{label}' does not match the local age key")
            if not public_key:
                return label
        else:
            raise ValueError("stored device sops label is not registered in .sops.yaml")

    if not allow_new:
        raise ValueError("local age key is not registered in .sops.yaml")
    label = get_sops_label()
    validate_device_label(label)
    set_sops_label(label)
    return label

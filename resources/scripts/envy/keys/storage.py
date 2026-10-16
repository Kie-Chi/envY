"""Atomic storage for .sops.yaml recipients and the local age private key."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from envy.secure_io import atomic_write_text, ensure_private_directory
from envy.utils import AGE_KEY_DIR, AGE_KEY_FILE, SOPS_YAML, backup_sensitive_file, run_cmd


_LABEL_PATTERN = re.compile(r"[a-z0-9_]+")
_PRIVATE_KEY_PATTERN = re.compile(r"^AGE-SECRET-KEY-[A-Z0-9]+$")
_PUBLIC_KEY_PATTERN = re.compile(r"^age1[0-9a-z]+$")
ROTATION_PREFIX = "envy_rotation_old_"


def validate_device_label(label: str) -> str:
    if (
        not _LABEL_PATTERN.fullmatch(label)
        or label in {"recovery", "recovery_new"}
        or label.startswith(ROTATION_PREFIX)
    ):
        raise ValueError("invalid or reserved device sops label")
    return label


@dataclass
class PreparedDeviceKey:
    """A private age identity staged outside the active key file."""

    path: Path
    public_key: str

    @property
    def private_key(self) -> str:
        return self.path.read_text().strip()

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _validate_keys(keys: dict[str, str]) -> None:
    seen_public: set[str] = set()
    for label, public_key in keys.items():
        if not isinstance(label, str) or not _LABEL_PATTERN.fullmatch(label):
            raise ValueError(f"invalid sops key label: {label!r}")
        if not isinstance(public_key, str) or not _PUBLIC_KEY_PATTERN.fullmatch(public_key):
            raise ValueError(f"invalid age public key for label {label!r}")
        if public_key in seen_public:
            raise ValueError(f"duplicate age public key for label {label!r}")
        seen_public.add(public_key)


def read_sops_yaml_keys() -> dict[str, str]:
    """Read managed recipients while ignoring commented-out YAML lines."""
    if not SOPS_YAML.exists():
        return {}
    text = SOPS_YAML.read_text()
    try:
        document = yaml.safe_load(text) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot parse {SOPS_YAML}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError(f"invalid keys section in {SOPS_YAML}")

    keys: dict[str, str] = {}
    # Parser events retain anchor names, unlike safe_load/compose. Limit them
    # to the root keys sequence; commented recipients are never YAML events.
    depth = 0
    expect_keys = False
    in_keys = False
    for event in yaml.parse(text):
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if expect_keys:
                if not isinstance(event, yaml.SequenceStartEvent):
                    raise ValueError("managed keys must be a YAML sequence")
                in_keys, expect_keys = True, False
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            if in_keys and depth == 2:
                in_keys = False
            depth -= 1
        elif isinstance(event, yaml.ScalarEvent):
            if depth == 1:
                expect_keys = event.value == "keys"
            elif in_keys:
                if depth != 2 or not event.anchor or event.anchor in keys:
                    raise ValueError("managed recipients require unique YAML anchors")
                keys[event.anchor] = event.value
        elif in_keys and isinstance(event, yaml.AliasEvent):
            raise ValueError("managed recipients must declare anchors, not aliases")
    document_keys = document["keys"]
    if any(not isinstance(value, str) for value in document_keys):
        raise ValueError(f"managed keys section contains unsupported entries in {SOPS_YAML}")
    if len(keys) != len(document_keys) or set(keys.values()) != set(document_keys):
        raise ValueError(f"managed keys section contains unsupported entries in {SOPS_YAML}")
    _validate_keys(keys)
    return keys


def write_sops_yaml_keys(keys: dict[str, str]) -> None:
    _validate_keys(keys)
    backup_sensitive_file(SOPS_YAML)
    lines = ["keys:\n", "  # Device keys - managed by envy key commands\n"]
    lines.extend(f"  - &{label} {public_key}\n" for label, public_key in keys.items())
    lines.extend([
        "\ncreation_rules:\n",
        "  - path_regex: secrets/secrets\\.yaml$\n",
        "    key_groups:\n",
        "      - age:\n",
    ])
    lines.extend(f"          - *{label}\n" for label in keys)
    atomic_write_text(SOPS_YAML, "".join(lines), mode=0o644)


def private_key_lines(content: str) -> list[str]:
    """Return normalized age identities while discarding age-keygen comments."""
    identities = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not identities or any(not _PRIVATE_KEY_PATTERN.fullmatch(identity) for identity in identities):
        raise ValueError("age private key content is missing a valid AGE-SECRET-KEY")
    return list(dict.fromkeys(identities))


def store_device_age_key(content: str) -> None:
    normalized = "\n".join(private_key_lines(content))
    ensure_private_directory(AGE_KEY_DIR)
    atomic_write_text(
        AGE_KEY_FILE,
        normalized + "\n",
        mode=0o600,
        private_parent=True,
    )


def prepare_device_age_key(content: str) -> PreparedDeviceKey:
    """Stage an age identity in a private temporary file without activating it."""
    normalized = "\n".join(private_key_lines(content)) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=".envy-prepared-key-", suffix=".txt")
    path = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        public_keys = _public_keys_for_file(path)
        if not public_keys:
            raise RuntimeError("prepared age key did not produce a public key")
        return PreparedDeviceKey(path=path, public_key=public_keys[-1])
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def prepare_generated_device_age_key() -> PreparedDeviceKey:
    return prepare_device_age_key(run_cmd(["age-keygen"], capture=True) or "")


def commit_prepared_device_age_key(prepared: PreparedDeviceKey) -> str:
    store_device_age_key(prepared.private_key)
    return prepared.public_key


def _public_keys_for_file(path: Path) -> list[str]:
    output = run_cmd(["age-keygen", "-y", str(path)], capture=True) or ""
    return list(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))


def device_public_keys(path: Path | None = None) -> list[str]:
    """Return every public recipient represented by a local identity file."""
    identity_path = path or AGE_KEY_FILE
    if not identity_path.exists():
        return []
    try:
        return _public_keys_for_file(identity_path)
    except subprocess.CalledProcessError:
        return []


def generate_device_age_key() -> str:
    generated = run_cmd(["age-keygen"], capture=True)
    store_device_age_key(generated)
    public_keys = device_public_keys()
    if not public_keys:
        raise RuntimeError("age-keygen did not produce a usable public key")
    return public_keys[-1]


def current_device_public_key(path: Path | None = None) -> str | None:
    """Return the active identity, represented by the last local key entry."""
    public_keys = device_public_keys(path)
    return public_keys[-1] if public_keys else None

"""Nested secret paths and the sops-encrypted secrets.yaml."""

import json
import os
import subprocess
from pathlib import Path

import yaml

from envy import log
from envy.schemas.config import SECRET_FIELDS
from envy.secure_io import atomic_write_text, replace_prepared_file, secure_temporary_path
from envy.utils import SECRETS_DIR, SECRETS_FILE, is_sops_encrypted, run_cmd


_MISSING = object()


def _lookup_nested(data: dict, path: str) -> tuple[object, str | None, type | None]:
    """Return a raw YAML value and any non-mapping path component."""
    current: object = data
    traversed: list[str] = []
    for part in path.split("/"):
        if not isinstance(current, dict):
            return _MISSING, "/".join(traversed), type(current)
        if part not in current:
            return _MISSING, None, None
        traversed.append(part)
        current = current[part]
    return current, None, None


def _get_nested_raw(data: dict, path: str, default=_MISSING):
    value, _, _ = _lookup_nested(data, path)
    return default if value is _MISSING else value


def _get_nested(data: dict, path: str, default=_MISSING):
    value = _get_nested_raw(data, path, default)
    if value is default or value is _MISSING:
        return default
    return "" if value is None else str(value)


def _secret_type_name(value: object) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _secret_scalar_text(value: object) -> str | None:
    """Return a string form for scalar YAML values, or None for structures."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _set_nested(data: dict, path: str, value: str) -> None:
    cur = data
    parts = path.split("/")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _unset_nested(data: dict, path: str) -> bool:
    cur = data
    parts = path.split("/")
    parents = []
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return False
        parents.append((cur, part))
        cur = cur[part]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return False
    del cur[parts[-1]]
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
    return True


def _sops_index(yaml_path: str) -> str:
    return "".join(f"[{json.dumps(part)}]" for part in yaml_path.split("/"))


def read_secrets_yaml(*, age_key_file: Path | None = None) -> tuple[dict, bool]:
    values = {}
    data, ok = read_secrets_data(age_key_file=age_key_file)
    if data is None:
        return values, False
    if not ok and is_sops_encrypted(SECRETS_FILE):
        return values, False
    for f in SECRET_FIELDS:
        if f.yaml_path:
            value = _get_nested(data, f.yaml_path, "")
            values[f.path] = "" if value is _MISSING else value
    return values, ok


def read_secrets_data(*, age_key_file: Path | None = None) -> tuple[dict | None, bool]:
    if not SECRETS_FILE.exists():
        return {}, False
    try:
        plain = run_cmd(["sops", "--decrypt", str(SECRETS_FILE)], age_key_file=age_key_file)
        return yaml.safe_load(plain) or {}, True
    except subprocess.CalledProcessError as exc:
        log.debug("secrets", "sops decrypt failed", stderr=(exc.stderr or "").strip())
        try:
            return yaml.safe_load(SECRETS_FILE.read_text()) or {}, False
        except Exception:
            return None, False


def write_secrets_yaml(values: dict, *, replace: bool = False) -> None:
    """Update managed secret paths while preserving unknown data by default."""
    data: dict = {}
    if SECRETS_FILE.exists() and not replace:
        existing, decrypt_ok = read_secrets_data()
        if existing is None or not decrypt_ok:
            raise RuntimeError("cannot safely merge secrets without decrypting secrets.yaml")
        data = existing
    for f in SECRET_FIELDS:
        if f.yaml_path:
            _set_nested(data, f.yaml_path, values.get(f.path, ""))
    write_secrets_data(data)


def _sops_encrypt_argv(recipients: str, out: str, src: str) -> list[str]:
    """Build the sops --encrypt command envY uses for every secret write.

    `--config /dev/null` makes the explicit `--age` recipient list authoritative
    and decouples encryption from `.sops.yaml` creation_rules. sops 3.13+ rejects
    an encrypt whose target path matches no creation rule even when `--age` is
    given, which broke re-encrypting the plaintext temp file. Keeping
    this in one place ensures the doctor write-capability probe uses identical
    flags.
    """
    return [
        "sops", "--encrypt", "--config", "/dev/null",
        "--age", recipients, "--output", out, src,
    ]


def write_secrets_data(data: dict) -> None:
    """Encrypt a secure temporary plaintext and atomically publish ciphertext."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    from envy.keys.storage import read_sops_yaml_keys
    keys = read_sops_yaml_keys()
    age_recipients = ",".join(keys.values())
    if not age_recipients:
        raise RuntimeError("No age recipients in .sops.yaml")

    plaintext = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    try:
        with secure_temporary_path(
            Path(os.environ.get("TMPDIR", "/tmp")),
            prefix=".envy-secrets-plain-", suffix=".yaml",
        ) as plain_path, secure_temporary_path(
            SECRETS_DIR, prefix=".secrets-encrypted-", suffix=".yaml"
        ) as encrypted_path:
            atomic_write_text(plain_path, plaintext, mode=0o600)
            run_cmd(_sops_encrypt_argv(age_recipients, str(encrypted_path), str(plain_path)))
            if not is_sops_encrypted(encrypted_path):
                raise RuntimeError("sops output is not encrypted")
            replace_prepared_file(encrypted_path, SECRETS_FILE, mode=0o644)
    except Exception:
        log.error("secrets", "sops encryption failed; previous encrypted file was preserved")
        raise


def assert_no_plaintext_secret_artifacts() -> None:
    """Fail closed when a crashed encryption left plaintext in the checkout."""
    leftovers = sorted(SECRETS_DIR.glob(".secrets-plain-*.yaml")) if SECRETS_DIR.exists() else []
    if leftovers:
        raise RuntimeError(
            "plaintext secret temporary files remain; remove them securely before continuing: "
            + ", ".join(str(path) for path in leftovers)
        )

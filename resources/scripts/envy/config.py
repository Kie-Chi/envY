"""Config engine for versioned machine policy and sops secrets."""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path

import typer
import yaml
from rich.table import Table

from envy import log
from envy.evaluation import invalidate_machine_manifest, machine_manifest, manifest_settings
from envy.jsonio import emit, emit_error
from envy.legacy import SOFTWARE_OPTION_ALIASES
from envy.schemas.config import (
    FieldDef,
    LEGACY_CONFIG_PATHS,
    LEGACY_MACHINE_KEYS,
    MACHINE_FIELDS,
    OBSOLETE_MACHINE_KEYS,
    OBSOLETE_SECRET_PATHS,
    SECRET_FIELDS,
)
from envy.software import (
    MANAGED_START as SOFTWARE_MANAGED_START,
    SoftwarePolicyError,
    groups_for_manifest,
    read_managed_policy,
)
from envy.secure_io import (
    atomic_write_text,
    replace_prepared_file,
    secure_temporary_path,
)
from envy.process import run_process
from envy.mutation import offer_mutation_commit
from envy.transaction import FileTransaction, transaction_lock
from envy.utils import (
    AGE_KEY_FILE,
    AGE_KEY_DIR,
    DEVICE_LABEL_FILE,
    ENVY_ROOT,
    SOPS_YAML,
    LEGACY_MACHINE_SELECTOR,
    LEGACY_SYSTEM_CONFIG,
    LEGACY_USER_CONFIG,
    SECRETS_DIR,
    SECRETS_FILE,
    current_machine_id,
    device_metadata_is_toml,
    is_sops_encrypted,
    machine_config_file as versioned_machine_file,
    platform_name,
    read_device_metadata,
    run_cmd,
    set_device_machine_id,
)


MANAGED_START = "  # BEGIN ENVY MANAGED CONFIG"
MANAGED_END = "  # END ENVY MANAGED CONFIG"
MIRROR_MANAGED_START = "  # BEGIN ENVY MANAGED MIRROR OVERRIDES"
MIRROR_MANAGED_END = "  # END ENVY MANAGED MIRROR OVERRIDES"


@dataclass
class RefineReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    changed: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "RefineReport") -> None:
        self.added.extend(other.added)
        self.removed.extend(other.removed)
        self.warnings.extend(other.warnings)
        self.errors.extend(other.errors)
        self.changed = self.changed or other.changed


def _escape_nix_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _unescape_nix_string(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


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


def read_legacy_config_nix() -> dict:
    """Read the old ignored config.nix only as an upgrade source."""
    for path in (LEGACY_USER_CONFIG, ENVY_ROOT / "config.nix", LEGACY_SYSTEM_CONFIG):
        if path.exists():
            try:
                return _read_nix_assignments(path)
            except OSError:
                continue
    return {}


def _legacy_config_value(path: str, values: dict, legacy_values: dict) -> str:
    sources = LEGACY_CONFIG_PATHS.get(path, ())
    if isinstance(sources, str):
        sources = (sources,)
    for source in sources:
        value = values.get(source, legacy_values.get(source, ""))
        if str(value).strip():
            return value
    return ""


def _legacy_llm_values(values: dict) -> dict:
    """Resolve both versioned and pre-machine LLM migration sources."""
    legacy = read_legacy_config_nix()
    return {
        key: values.get(key) or legacy.get(key) or _legacy_config_value(key, values, legacy)
        for key in LEGACY_MACHINE_KEYS
    }


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

    # A scalar edit/setup save can also rewrite an old managed block. Preserve
    # its LLM selections in the agent block before omitting the legacy fields.
    source_values = _read_nix_assignments(path) | {
        key: values[key] for key in LEGACY_MACHINE_KEYS if key in values
    }
    if any(key in source_values for key in LEGACY_MACHINE_KEYS):
        from envy.agent import refine_managed

        migration = refine_managed(
            machine_values=_legacy_llm_values(source_values), write=True, path=path,
        )
        if migration["errors"]:
            raise ValueError("cannot remove legacy LLM fields before agent migration succeeds")

    text = path.read_text()
    block = _render_machine_block(values)
    managed_pattern = re.compile(
        rf"(?ms)^{re.escape(MANAGED_START)}$.*?^{re.escape(MANAGED_END)}$"
    )
    if managed_pattern.search(text):
        updated = managed_pattern.sub(block, text, count=1)
        # Legacy LLM assignments may still sit outside the managed block.
        # Remove only those exact scalar assignments; preserve other policy.
        legacy_paths = set(LEGACY_MACHINE_KEYS)
        assignment = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=.*;\s*$")
        kept_lines = []
        for line in updated.splitlines(keepends=True):
            match = assignment.match(line)
            if not match or match.group(1) not in legacy_paths:
                kept_lines.append(line)
        updated = "".join(kept_lines)
    else:
        # Move legacy flat assignments for managed fields into the controlled
        # block while leaving imports and all other machine policy untouched.
        managed_paths = (
            {field.path for field in MACHINE_FIELDS}
            | set(LEGACY_MACHINE_KEYS)
            | set(OBSOLETE_MACHINE_KEYS)
        )
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
        anchor = f"  # END ENVY MANAGED CONFIG"
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
    given, which broke re-encrypting the `.secrets-plain-*` temp file. Keeping
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
    from envy.key import read_sops_yaml_keys
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


def _validate_field(field_def: FieldDef, value: str, report: RefineReport, scope: str) -> None:
    if field_def.required and not str(value).strip():
        target = field_def.yaml_path if field_def.dest == "secret" else field_def.path
        report.errors.append(target)
        log.error(scope, "required field is empty", field=target)
        if field_def.dest == "secret":
            log.hint(f"Run: envy config secret-set {target}")
        else:
            log.hint(f"Run: envy config set {target} <value>")
        return

    if field_def.choices and str(value) not in field_def.choices:
        target = field_def.yaml_path if field_def.dest == "secret" else field_def.path
        report.errors.append(target)
        log.error(scope, "value is not one of the allowed choices", field=target)
        log.hint(f"Allowed values: {', '.join(field_def.choices)}")
        return

    for validator in field_def.validators:
        err = validator(str(value))
        if err:
            target = field_def.yaml_path if field_def.dest == "secret" else field_def.path
            report.errors.append(target)
            log.error(scope, err, field=target)


def refine_device_metadata(*, write: bool = True, strict: bool = False) -> RefineReport:
    """Validate and migrate the device-local TOML identity document."""
    report = RefineReport()
    try:
        metadata = read_device_metadata()
    except (OSError, ValueError) as exc:
        report.errors.append(str(DEVICE_LABEL_FILE))
        log.error("device", str(exc))
        return report

    machine_id = metadata.get("machine_id", "")
    sops_label = metadata.get("sops_label", "")
    legacy_format = DEVICE_LABEL_FILE.exists() and not device_metadata_is_toml()
    legacy_selector = LEGACY_MACHINE_SELECTOR.exists()

    if not machine_id:
        machine_id = current_machine_id()
        if write:
            report.added.append("device.machine_id")
            log.fix("device", "added machine ID", machine=machine_id)
        else:
            report.errors.append("device.machine_id")
            log.error("device", "machine ID is missing", path=str(DEVICE_LABEL_FILE))
    if machine_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", machine_id):
        report.errors.append("device.machine_id")
        log.error("device", "machine ID has invalid characters", machine=machine_id)

    machine_path = machine_config_file(machine_id) if machine_id else None
    if machine_path is not None and not machine_path.exists():
        report.errors.append(str(machine_path))
        log.error("device", "selected machine configuration is missing", path=str(machine_path))
        log.hint(f"Run: envy host init {machine_id}")

    if sops_label and not re.fullmatch(r"[a-z0-9_]+", sops_label):
        if write:
            from envy.key import sanitize_label
            normalized = sanitize_label(sops_label) or "unknown"
            log.fix("device", "normalized sops label", old=sops_label, new=normalized)
            sops_label = normalized
            report.changed = True
        else:
            report.errors.append("device.sops_label")
            log.error("device", "sops label must contain only lowercase letters, digits, and underscores")

    if not sops_label and not write:
        report.errors.append("device.sops_label")
        log.error("device", "sops label is missing", path=str(DEVICE_LABEL_FILE))

    if legacy_format and not write:
        report.errors.append("device.version")
        log.error("device", "legacy one-line .device-label must be migrated to TOML")
        log.hint("Run: envy config refine")

    if legacy_selector and not write:
        report.errors.append(str(LEGACY_MACHINE_SELECTOR))
        log.error("device", "legacy machine selector still exists", path=str(LEGACY_MACHINE_SELECTOR))
        log.hint("Run: envy config refine")

    if write and report.ok:
        needs_write = legacy_format or legacy_selector or not device_metadata_is_toml()
        if needs_write or metadata.get("machine_id") != machine_id:
            set_device_machine_id(machine_id)
            report.changed = True

        from envy.key import ensure_sops_label, set_sops_label
        persisted = read_device_metadata()
        if sops_label:
            if persisted.get("sops_label") != sops_label:
                set_sops_label(sops_label)
                report.changed = True
        else:
            sops_label = ensure_sops_label()
            report.added.append("device.sops_label")
            report.changed = True

        updated = read_device_metadata()
        if updated.get("machine_id") != machine_id or updated.get("sops_label") != sops_label:
            report.errors.append(str(DEVICE_LABEL_FILE))
            log.error("device", "device metadata did not persist correctly")
        elif report.changed:
            log.ok("device", "device metadata refined", path=str(DEVICE_LABEL_FILE))
        else:
            log.ok("device", "device metadata is complete")
    elif report.ok:
        log.ok("device", "device metadata is valid")

    if strict and report.errors:
        log.error("device", "device metadata refinement blocked by invalid fields")
    return report


def refine_sensitive_permissions(*, write: bool = True) -> RefineReport:
    """Validate or harden the local age directory, key, and sensitive backups."""
    report = RefineReport()
    checks: list[tuple[Path, int]] = []
    if AGE_KEY_DIR.exists():
        checks.append((AGE_KEY_DIR, 0o700))
    if AGE_KEY_FILE.exists():
        checks.append((AGE_KEY_FILE, 0o600))
        checks.extend((path, 0o600) for path in AGE_KEY_DIR.glob(AGE_KEY_FILE.name + ".bak*"))
    for path, expected in checks:
        mode = path.stat().st_mode & 0o777
        if mode == expected:
            continue
        if write:
            path.chmod(expected)
            report.changed = True
            report.added.append(f"permissions:{path}")
            log.fix("secrets", "hardened sensitive permissions", path=str(path), mode=f"{expected:04o}")
        else:
            report.errors.append(f"permissions:{path}")
            log.error(
                "secrets",
                "unsafe sensitive permissions",
                path=str(path),
                expected=f"{expected:04o}",
                actual=f"{mode:04o}",
            )
            log.hint("Run: envy config refine")
    return report


def refine_config(*, write: bool = True, strict: bool = False) -> RefineReport:
    report = RefineReport()
    path = machine_config_file()
    if not path.exists():
        report.errors.append(str(path))
        log.error("machine", "selected machine configuration is missing", path=str(path))
        log.hint(f"Run: envy host init {current_machine_id()}")
        return report

    # Complete/validate agent migration before rewriting any scalar fields.
    # This also protects callers that invoke refine_config directly.
    agent_report = refine_agents(write=write, strict=strict)
    report.extend(agent_report)
    if not agent_report.ok:
        return report

    values = read_machine_nix()
    original = dict(values)
    legacy_values = read_legacy_config_nix()

    for key in OBSOLETE_MACHINE_KEYS:
        if key in values:
            values.pop(key)
            report.removed.append(key)
            log.fix("machine", "removed obsolete managed field", field=key)

    for key in LEGACY_MACHINE_KEYS:
        if key in values:
            values.pop(key)
            report.removed.append(key)
            if write:
                log.fix("machine", "removed migrated legacy LLM field", field=key)
            else:
                report.errors.append(key)
                log.error("machine", "legacy LLM field needs migration cleanup", field=key)
                log.hint("Run: envy config refine")

    for f in MACHINE_FIELDS:
        if f.path not in values:
            legacy_value = _legacy_config_value(f.path, original, legacy_values)
            values[f.path] = legacy_value if str(legacy_value).strip() else f.default_fn()
            report.added.append(f.path)
            action = "migrated legacy field" if str(legacy_value).strip() else "added missing field"
            log.fix("machine", action, field=f.path)

    for f in MACHINE_FIELDS:
        _validate_field(f, values.get(f.path, ""), report, "machine")

    needs_managed_block = MANAGED_START not in path.read_text()
    if write and (values != original or needs_managed_block):
        write_machine_nix(values)
        report.changed = True
        log.ok(
            "machine",
            "machine configuration refined",
            path=str(path),
            added=len(report.added),
            removed=len(report.removed),
        )
    elif not report.added and not report.removed:
        log.ok("machine", "managed machine fields are complete")

    if strict and report.errors:
        log.error("machine", "machine refinement blocked by invalid fields")
    return report


def refine_agents(*, write: bool = True, strict: bool = False) -> RefineReport:
    """Migrate legacy scalar LLM settings into the dynamic agent block."""
    report = RefineReport()
    log.debug("agents", "starting managed agent validation", write=write, strict=strict)
    path = machine_config_file()
    if not path.exists():
        report.errors.append(str(path))
        log.error("agents", "selected machine configuration is missing", path=str(path))
        return report
    # Keep the parser and writer in agent.py so the CLI and refine workflow
    # cannot gradually acquire different managed-block formats.
    from envy.agent import read_managed_values, refine_managed

    values = read_machine_nix()
    _managed, present, _errors = read_managed_values(path)
    migration_values = (
        _legacy_llm_values(values)
        if not present or any(key in values for key in LEGACY_MACHINE_KEYS)
        else {}
    )
    result = refine_managed(machine_values=migration_values, write=write, path=path)
    report.added.extend(result.get("added", []))
    report.removed.extend(result.get("removed", []))
    report.warnings.extend(result.get("warnings", []))
    report.errors.extend(result.get("errors", []))
    report.changed = bool(result.get("changed", False))
    if report.errors:
        log.error(
            "agents",
            "agent refinement blocked by invalid fields",
            count=len(report.errors),
            fields=", ".join(report.errors),
        )
    else:
        log.debug("agents", "managed agent validation passed", changed=report.changed)
    return report


def refine_software_policy(*, write: bool = True, strict: bool = False) -> RefineReport:
    """Validate the direct managed software include/exclude assignments."""
    report = RefineReport()
    path = machine_config_file()
    text = path.read_text()
    legacy_paths = [old for old in SOFTWARE_OPTION_ALIASES if old in text]
    if legacy_paths and write:
        for old in legacy_paths:
            text = text.replace(old, SOFTWARE_OPTION_ALIASES[old])
            report.removed.append(old)
            report.added.append(SOFTWARE_OPTION_ALIASES[old])
            log.fix(
                "software", "migrated option path",
                old=old, new=SOFTWARE_OPTION_ALIASES[old],
            )
        atomic_write_text(path, text)
        invalidate_machine_manifest()
        report.changed = True
    elif legacy_paths:
        report.errors.extend(legacy_paths)
        for old in legacy_paths:
            log.error("software", "legacy option path requires migration", option=old)
        log.hint("Run: envy config refine")

    groups = groups_for_manifest(machine_manifest(), include_empty=True)
    try:
        includes, exclusions = read_managed_policy(path, groups)
    except (OSError, SoftwarePolicyError) as exc:
        report.errors.append(str(path))
        log.error("software", str(exc))
        log.hint("Fix the managed software blocks or remove them and reopen envy setup.")
        return report

    if SOFTWARE_MANAGED_START in path.read_text():
        count = sum(len(names) for names in exclusions.values())
        log.ok(
            "software", "managed machine selections are valid",
            included=len(includes), excluded=count,
        )
    if strict and report.errors:
        log.error("software", "software policy validation failed")
    return report


def refine_secrets(*, write: bool = True, strict: bool = False, prune: bool = False) -> RefineReport:
    report = RefineReport()
    try:
        assert_no_plaintext_secret_artifacts()
    except RuntimeError as exc:
        report.errors.append("plaintext-secret-artifact")
        log.error("secrets", str(exc))
        return report
    data, decrypt_ok = read_secrets_data()
    if data is None:
        report.errors.append("secrets.yaml")
        log.error("secrets", "cannot read secrets.yaml")
        return report
    if SECRETS_FILE.exists() and not decrypt_ok and is_sops_encrypted(SECRETS_FILE):
        report.errors.append("secrets.yaml")
        log.error("secrets", "cannot decrypt secrets.yaml")
        log.hint(f"Expected age key file: {AGE_KEY_FILE}")
        log.hint("Run: envy key import")
        return report

    config_values = read_machine_nix()
    values = dict(config_values)
    original = yaml.dump(data, sort_keys=True)

    for f in SECRET_FIELDS:
        if f.condition and not f.condition(values):
            continue
        current, blocked_path, blocked_type = _lookup_nested(data, f.yaml_path)
        if current is _MISSING and blocked_path is not None:
            report.errors.append(f.yaml_path)
            log.error(
                "secrets", "secret path component must be a mapping",
                key=blocked_path, found=_secret_type_name(blocked_type),
            )
            values[f.path] = ""
            continue
        if current is _MISSING:
            default_value = f.default_fn()
            _set_nested(data, f.yaml_path, default_value)
            report.added.append(f.yaml_path)
            log.fix("secrets", "created missing secret path", key=f.yaml_path)
            values[f.path] = str(default_value)
            continue

        normalized = _secret_scalar_text(current)
        if normalized is None:
            report.errors.append(f.yaml_path)
            log.error(
                "secrets", "secret value must be a scalar string",
                key=f.yaml_path, found=_secret_type_name(current),
            )
            values[f.path] = ""
            continue
        if not isinstance(current, str):
            if write:
                _set_nested(data, f.yaml_path, normalized)
                log.fix(
                    "secrets", "normalized secret scalar to string",
                    key=f.yaml_path, found=_secret_type_name(current),
                )
            else:
                report.errors.append(f.yaml_path)
                log.error(
                    "secrets", "secret value must be a string",
                    key=f.yaml_path, found=_secret_type_name(current),
                )
        values[f.path] = normalized

    # Credentials are dynamic: each agent adapter may introduce its own
    # logical name and encrypted YAML path.  Validate those references here,
    # while keeping values completely out of logs and JSON output.
    from envy.agent import managed_credential_paths

    for credential_name, secret_path in managed_credential_paths().items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", secret_path):
            report.errors.append(f"credential:{credential_name}")
            log.error("secrets", "credential secret path is invalid", credential=credential_name)
            continue
        current, blocked_path, blocked_type = _lookup_nested(data, secret_path)
        if current is _MISSING and blocked_path is not None:
            report.errors.append(secret_path)
            log.error(
                "secrets", "secret path component must be a mapping",
                key=blocked_path, found=_secret_type_name(blocked_type),
            )
            current = ""
        elif current is _MISSING:
            report.added.append(secret_path)
            if write:
                _set_nested(data, secret_path, "")
                log.fix("secrets", "created missing credential secret path", credential=credential_name)
            else:
                report.errors.append(secret_path)
                log.error("secrets", "credential secret path is missing", credential=credential_name)
            current = ""
        else:
            normalized = _secret_scalar_text(current)
            if normalized is None:
                report.errors.append(secret_path)
                log.error(
                    "secrets", "secret value must be a scalar string",
                    key=secret_path, found=_secret_type_name(current),
                )
                current = ""
            else:
                if not isinstance(current, str):
                    if write:
                        _set_nested(data, secret_path, normalized)
                        log.fix(
                            "secrets", "normalized secret scalar to string",
                            key=secret_path, found=_secret_type_name(current),
                        )
                    else:
                        report.errors.append(secret_path)
                        log.error(
                            "secrets", "secret value must be a string",
                            key=secret_path, found=_secret_type_name(current),
                        )
                current = normalized
        if not str(current).strip():
            report.warnings.append(secret_path)
            log.warn("secrets", "credential secret is empty", credential=credential_name)

    if prune:
        for path in OBSOLETE_SECRET_PATHS:
            if _unset_nested(data, path):
                report.removed.append(path)
                log.fix("secrets", "removed obsolete secret path", key=path)
    else:
        for path in OBSOLETE_SECRET_PATHS:
            if _get_nested(data, path) is not _MISSING:
                report.warnings.append(path)
                log.warn("secrets", "obsolete secret path present", key=path)
                log.hint("Run: envy config refine --prune to remove obsolete secret paths")

    for f in SECRET_FIELDS:
        if f.condition and not f.condition(values):
            continue
        _validate_field(f, values.get(f.path, ""), report, "secrets")

    changed = yaml.dump(data, sort_keys=True) != original
    if strict and report.errors:
        log.error("secrets", "secret refinement blocked by validation errors")
        return report
    if write and changed:
        write_secrets_data(data)
        report.changed = True
        log.ok("secrets", "secrets.yaml refined", added=len(report.added), removed=len(report.removed))
    elif not report.added and not report.removed:
        log.ok("secrets", "secret paths are complete")

    if strict and report.errors:
        log.error("secrets", "secret refinement blocked by invalid fields")
    return report


def refine_all(*, write: bool = True, strict: bool = False, include_secrets: bool = True,
               prune: bool = False) -> RefineReport:
    log.step("config", "checking device metadata, selected machine, agents, software, and secrets")
    log.debug("config", "validation started", write=write, strict=strict, include_secrets=include_secrets)
    report = RefineReport()
    device_report = refine_device_metadata(write=write, strict=strict)
    report.extend(device_report)
    if not device_report.ok:
        log.error("config", "validation failed", count=len(report.errors))
        return report
    report.extend(refine_sensitive_permissions(write=write))
    # refine_config migrates agents before deleting old LLM scalar fields.
    report.extend(refine_config(write=write, strict=strict))
    report.extend(refine_software_policy(write=write, strict=strict))
    if include_secrets:
        report.extend(refine_secrets(write=write, strict=strict, prune=prune))
    if report.ok:
        log.ok("config", "refine completed" if write else "check completed")
    else:
        log.error(
            "config",
            "refine failed" if write else "check failed",
            count=len(report.errors),
            fields=", ".join(report.errors),
        )
    return report


def set_config_value(path: str, value: str, *, quiet: bool = False) -> None:
    if path.startswith(("envy.agents.", "envy.llm.endpoints.", "envy.llm.credentials.")):
        from envy.agent import _set
        try:
            _set(path, value)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not quiet:
            log.ok("machine", "agent value updated", field=path, machine=current_machine_id())
        return
    values = read_machine_nix()
    valid_paths = {f.path for f in MACHINE_FIELDS}
    if path not in valid_paths:
        if not quiet:
            log.error("machine", "unknown managed machine field", field=path)
            log.hint("Edit the machine file directly for custom policy.")
        raise typer.BadParameter(f"unknown managed machine field: {path}")
    legacy_values = read_legacy_config_nix()
    for field in MACHINE_FIELDS:
        if field.path not in values:
            legacy_value = _legacy_config_value(field.path, values, legacy_values)
            values[field.path] = legacy_value if str(legacy_value).strip() else field.default_fn()
    values[path] = value
    field_def = next(field for field in MACHINE_FIELDS if field.path == path)
    validation = RefineReport()
    _validate_field(field_def, value, validation, "machine")
    if validation.errors:
        raise typer.BadParameter(f"invalid value for {path}")
    write_machine_nix(values)
    if not quiet:
        log.ok("machine", "machine value updated", field=path, machine=current_machine_id())


def set_secret_value(yaml_path: str, value: str, *, replace: bool = False) -> None:
    if not replace:
        assert_no_plaintext_secret_artifacts()
        data, decrypt_ok = read_secrets_data()
        if data is None or not decrypt_ok:
            raise RuntimeError(
                "cannot update a secret without decrypting secrets.yaml; "
                "use --replace on a new device to initialize a new secrets file"
            )
        _set_nested(data, yaml_path, value)
        write_secrets_data(data)
        log.ok("secrets", "secret value updated", key=yaml_path)
        return

    from envy.keys.identity import ensure_sops_label
    from envy.keys.storage import (
        current_device_public_key,
        generate_device_age_key,
        read_sops_yaml_keys,
        write_sops_yaml_keys,
    )

    assert_no_plaintext_secret_artifacts()
    with transaction_lock(ENVY_ROOT / ".envy-key.lock"):
        with FileTransaction([SOPS_YAML, SECRETS_FILE, AGE_KEY_FILE, DEVICE_LABEL_FILE]) as transaction:
            if not AGE_KEY_FILE.exists():
                generate_device_age_key()
            public_key = current_device_public_key()
            if not public_key:
                raise RuntimeError("cannot initialize secrets: current age key is unavailable")
            keys = read_sops_yaml_keys()
            label = ensure_sops_label(public_key=public_key)
            if label == "recovery":
                raise RuntimeError("device sops label cannot be recovery")
            keys[label] = public_key
            data: dict = {}
            _set_nested(data, yaml_path, value)
            write_sops_yaml_keys(keys)
            write_secrets_data(data)
            transaction.commit()
    log.ok("secrets", "secret value replaced", key=yaml_path)


app = typer.Typer(
    name="config",
    help="Validate device metadata and edit the selected machine configuration and sops secrets",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


def complete_machine_paths(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete managed non-secret machine option paths."""
    from envy.agent_completion import complete_agent_paths

    return [
        (field.path, field.prompt or field.group)
        for field in MACHINE_FIELDS
        if field.path.startswith(incomplete)
    ] + complete_agent_paths(ctx, incomplete)


def complete_secret_paths(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete secret YAML paths without reading or exposing secret values."""
    from envy.agent_completion import complete_credential_paths

    candidates = dict(complete_credential_paths(ctx, incomplete))
    candidates.update({
        field.yaml_path: field.prompt or field.group
        for field in SECRET_FIELDS
        if field.yaml_path and field.yaml_path.startswith(incomplete)
    })
    return sorted(candidates.items())


def complete_machine_values(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete enumerated values for the machine path already selected."""
    params = getattr(ctx, "params", {}) if ctx is not None else {}
    path = params.get("path") if isinstance(params, dict) else None
    field = next((item for item in MACHINE_FIELDS if item.path == path), None)
    if field is None:
        from envy.agent_completion import complete_agent_values

        return complete_agent_values(ctx, incomplete)
    if not field.choices:
        return []
    return [
        (choice, field.path)
        for choice in field.choices
        if choice.startswith(incomplete)
    ]


@app.command(name="check")
def cmd_check():
    """Strictly check all managed configuration without writing."""
    report = refine_all(write=False, strict=True, include_secrets=True)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command(name="refine")
def cmd_refine(
    prune: bool = typer.Option(False, "--prune", help="Remove known obsolete config/secret paths"),
):
    """Refine device metadata, the selected machine file, and secrets.yaml."""
    report = refine_all(write=True, strict=True, include_secrets=True, prune=prune)
    if not report.ok:
        raise typer.Exit(code=1)
    if report.changed:
        offer_mutation_commit(
            [machine_config_file(), SECRETS_FILE],
            f"chore(config): refine {current_machine_id()}",
        )


@app.command(name="doctor")
def cmd_doctor():
    """Run a stricter local config and secrets diagnostic."""
    report = refine_all(write=False, strict=True, include_secrets=True)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command(name="set")
def cmd_set(
    path: str = typer.Argument(
        ..., help="Machine or agent path, for example envy.vscode.mode or envy.agents.pi.api",
        autocompletion=complete_machine_paths,
    ),
    value: str = typer.Argument(..., help="Value to write", autocompletion=complete_machine_values),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Assume yes; skip interactive commit prompts"
    ),
):
    """Set a managed non-secret value in the selected machine file."""
    try:
        set_config_value(path, value, quiet=json_output)
    except typer.BadParameter as exc:
        if json_output:
            emit_error("config.set", str(exc), code="invalid-value")
            raise typer.Exit(code=1) from exc
        raise
    offer_mutation_commit(
        [machine_config_file()],
        f"chore(config): update {current_machine_id()} machine values",
        quiet=json_output or yes,
    )
    if json_output:
        emit("config.set", data={
            "path": path,
            "value": value,
            "machine": current_machine_id(),
        })


@app.command(name="secret-set")
def cmd_secret_set(
    yaml_path: str = typer.Argument(
        ..., help="Secret path, for example llm/steps/apikey", autocompletion=complete_secret_paths,
    ),
    stdin: bool = typer.Option(False, "--stdin", help="Read the secret from standard input"),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Initialize a new secrets file without reading the existing ciphertext",
    ),
):
    """Set a secret without exposing it in argv or shell history."""
    if stdin:
        value = sys.stdin.read().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            log.error("secrets", "non-interactive secret input requires --stdin")
            raise typer.Exit(code=1)
        value = getpass(f"{yaml_path}: ")
    try:
        set_secret_value(yaml_path, value, replace=replace)
    except (OSError, RuntimeError, ValueError) as exc:
        log.error("secrets", str(exc))
        if replace:
            log.hint("The new secrets file was not written; retry with a valid age installation.")
        else:
            log.hint("Use --replace only when this is a new device and the old secrets are intentionally discarded.")
        raise typer.Exit(code=1) from exc
    offer_mutation_commit(
        [SOPS_YAML, SECRETS_FILE],
        f"chore(secrets): {'replace' if replace else 'update'} {yaml_path}",
    )


@app.command(name="show")
def cmd_show(
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show evaluated machine values and secret status."""
    manifest = machine_manifest(refresh=refresh)
    evaluated_values = manifest_settings(manifest)
    config_values = evaluated_values or read_machine_nix()
    device_values = read_device_metadata()
    secret_values, _ = read_secrets_yaml()
    secret_data, decrypt_ok = read_secrets_data()
    from envy.agent import config_view

    agent_view = config_view(manifest, secret_data=secret_data, decrypt_ok=decrypt_ok)
    dynamic_values = agent_view["values"]
    dynamic_fields = agent_view["fields"]
    manifest_llm = agent_view["llm"]
    manifest_agents = agent_view["agents"]
    secret_values_for_fields: dict[str, str] = {}
    for field in SECRET_FIELDS:
        secret_values_for_fields[field.path] = str(secret_values.get(field.path, ""))
    payload = {
        "schemaVersion": 1,
        "source": "evaluated" if evaluated_values else "source-fallback",
        # Keep the target platform in the machine-readable config envelope.
        # It is runtime metadata, not a user-editable machine option.
        "platform": platform_name(),
        "device": {
            "machineId": device_values.get("machine_id", ""),
            "sopsLabel": device_values.get("sops_label", ""),
        },
        "values": {
            field.path: config_values.get(field.path, "")
            for field in MACHINE_FIELDS
        } | dynamic_values,
        "fields": [
            {
                "path": field.path,
                "value": config_values.get(field.path, ""),
                "choices": list(field.choices),
            }
            for field in MACHINE_FIELDS
        ] + dynamic_fields,
        "llm": manifest_llm,
        "agents": manifest_agents,
        "secrets": {
            field.yaml_path: bool(secret_values_for_fields.get(field.path, ""))
            for field in SECRET_FIELDS
        },
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    table = Table(title="envy config (evaluated)" if evaluated_values else "envy config (source fallback)")
    table.add_column("Kind")
    table.add_column("Path")
    table.add_column("Value")
    table.add_row("device", "device.machine_id", device_values.get("machine_id", ""))
    table.add_row("device", "device.sops_label", device_values.get("sops_label", ""))
    for f in MACHINE_FIELDS:
        table.add_row("value", f.path, config_values.get(f.path, ""))
    for field in dynamic_fields:
        path = field["path"]
        if path.startswith("envy.llm.endpoints."):
            kind = "endpoint"
        elif path.startswith("envy.llm.credentials."):
            kind = "credential"
        else:
            kind = "agent"
        table.add_row(kind, path, field["value"])
    credentials = manifest_llm.get("credentials", {}) if isinstance(manifest_llm, dict) else {}
    if isinstance(credentials, dict):
        for name, credential in sorted(credentials.items()):
            if isinstance(credential, dict):
                table.add_row(
                    "secret",
                    f"credential/{name}",
                    "<set>" if credential.get("credentialSet") else "<empty>",
                )
    if not manifest:
        log.warn(
            "config",
            "Nix evaluation failed; showing direct machine assignments without imported defaults",
        )

    for f in SECRET_FIELDS:
        value = secret_values_for_fields[f.path]
        status = "<set>" if value else "<empty>"
        table.add_row("secret", f.yaml_path, status)

    log.console.print(table)
@app.command(name="edit")
def cmd_edit():
    """Open the selected versioned machine file in $EDITOR."""
    path = machine_config_file()
    if not path.exists():
        log.error("machine", "selected machine configuration is missing", path=str(path))
        raise typer.Exit(code=1)
    editor = os.environ.get("EDITOR", "vim")
    run_process([editor, str(path)], check=True)

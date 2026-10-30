"""Validation and completion of device metadata, machine policy, and secrets."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from envy import log
from envy.evaluation import machine_manifest
from envy.schemas.config import (
    FieldDef,
    MACHINE_FIELDS,
    OBSOLETE_MACHINE_KEYS,
    OBSOLETE_SECRET_PATHS,
    SECRET_FIELDS,
)
from envy.software.model import SoftwarePolicyError, groups_for_manifest
from envy.software.policy import read_managed_policy
from envy.software.policy_text import MANAGED_START as SOFTWARE_MANAGED_START
from envy.utils import (
    AGE_KEY_FILE,
    AGE_KEY_DIR,
    DEVICE_LABEL_FILE,
    DEVICE_METADATA_HINT,
    SECRETS_FILE,
    current_machine_id,
    device_metadata_is_toml,
    is_sops_encrypted,
    read_device_metadata,
    set_device_machine_id,
)
from envy.config import machine_nix, secret_store


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
    """Validate and complete the device-local TOML identity document."""
    report = RefineReport()
    try:
        metadata = read_device_metadata()
    except (OSError, ValueError) as exc:
        report.errors.append(str(DEVICE_LABEL_FILE))
        log.error("device", str(exc))
        log.hint(DEVICE_METADATA_HINT)
        return report

    machine_id = metadata.get("machine_id", "")
    sops_label = metadata.get("sops_label", "")

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

    machine_path = machine_nix.machine_config_file(machine_id) if machine_id else None
    if machine_path is not None and not machine_path.exists():
        report.errors.append(str(machine_path))
        log.error("device", "selected machine configuration is missing", path=str(machine_path))
        log.hint(f"Run: envy host init {machine_id}")

    if sops_label and not re.fullmatch(r"[a-z0-9_]+", sops_label):
        if write:
            from envy.keys.identity import sanitize_label
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

    if write and report.ok:
        needs_write = not device_metadata_is_toml()
        if needs_write or metadata.get("machine_id") != machine_id:
            set_device_machine_id(machine_id)
            report.changed = True

        from envy.keys.identity import ensure_sops_label, set_sops_label
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
    path = machine_nix.machine_config_file()
    if not path.exists():
        report.errors.append(str(path))
        log.error("machine", "selected machine configuration is missing", path=str(path))
        log.hint(f"Run: envy host init {current_machine_id()}")
        return report

    # Validate the agent block before rewriting any scalar fields. This also
    # protects callers that invoke refine_config directly.
    agent_report = refine_agents(write=write, strict=strict)
    report.extend(agent_report)
    if not agent_report.ok:
        return report

    values = machine_nix.read_machine_nix()
    original = dict(values)

    for key in OBSOLETE_MACHINE_KEYS:
        if key in values:
            values.pop(key)
            report.removed.append(key)
            log.fix("machine", "removed obsolete managed field", field=key)

    for f in MACHINE_FIELDS:
        if f.path not in values:
            values[f.path] = f.default_fn()
            report.added.append(f.path)
            log.fix("machine", "added missing field", field=f.path)

    for f in MACHINE_FIELDS:
        _validate_field(f, values.get(f.path, ""), report, "machine")

    needs_managed_block = machine_nix.MANAGED_START not in path.read_text()
    if write and (values != original or needs_managed_block):
        machine_nix.write_machine_nix(values)
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
    """Validate the managed agent block, creating it when it is missing."""
    report = RefineReport()
    log.debug("agents", "starting managed agent validation", write=write, strict=strict)
    path = machine_nix.machine_config_file()
    if not path.exists():
        report.errors.append(str(path))
        log.error("agents", "selected machine configuration is missing", path=str(path))
        return report
    # Keep the parser and writer in agent.py so the CLI and refine workflow
    # cannot gradually acquire different managed-block formats.
    from envy.agent import refine_managed

    result = refine_managed(write=write, path=path)
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
    path = machine_nix.machine_config_file()
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
        secret_store.assert_no_plaintext_secret_artifacts()
    except RuntimeError as exc:
        report.errors.append("plaintext-secret-artifact")
        log.error("secrets", str(exc))
        return report
    data, decrypt_ok = secret_store.read_secrets_data()
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

    config_values = machine_nix.read_machine_nix()
    values = dict(config_values)
    original = yaml.dump(data, sort_keys=True)

    for f in SECRET_FIELDS:
        if f.condition and not f.condition(values):
            continue
        current, blocked_path, blocked_type = secret_store._lookup_nested(data, f.yaml_path)
        if current is secret_store._MISSING and blocked_path is not None:
            report.errors.append(f.yaml_path)
            log.error(
                "secrets", "secret path component must be a mapping",
                key=blocked_path, found=secret_store._secret_type_name(blocked_type),
            )
            values[f.path] = ""
            continue
        if current is secret_store._MISSING:
            default_value = f.default_fn()
            secret_store._set_nested(data, f.yaml_path, default_value)
            report.added.append(f.yaml_path)
            log.fix("secrets", "created missing secret path", key=f.yaml_path)
            values[f.path] = str(default_value)
            continue

        normalized = secret_store._secret_scalar_text(current)
        if normalized is None:
            report.errors.append(f.yaml_path)
            log.error(
                "secrets", "secret value must be a scalar string",
                key=f.yaml_path, found=secret_store._secret_type_name(current),
            )
            values[f.path] = ""
            continue
        if not isinstance(current, str):
            if write:
                secret_store._set_nested(data, f.yaml_path, normalized)
                log.fix(
                    "secrets", "normalized secret scalar to string",
                    key=f.yaml_path, found=secret_store._secret_type_name(current),
                )
            else:
                report.errors.append(f.yaml_path)
                log.error(
                    "secrets", "secret value must be a string",
                    key=f.yaml_path, found=secret_store._secret_type_name(current),
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
        current, blocked_path, blocked_type = secret_store._lookup_nested(data, secret_path)
        if current is secret_store._MISSING and blocked_path is not None:
            report.errors.append(secret_path)
            log.error(
                "secrets", "secret path component must be a mapping",
                key=blocked_path, found=secret_store._secret_type_name(blocked_type),
            )
            current = ""
        elif current is secret_store._MISSING:
            report.added.append(secret_path)
            if write:
                secret_store._set_nested(data, secret_path, "")
                log.fix("secrets", "created missing credential secret path", credential=credential_name)
            else:
                report.errors.append(secret_path)
                log.error("secrets", "credential secret path is missing", credential=credential_name)
            current = ""
        else:
            normalized = secret_store._secret_scalar_text(current)
            if normalized is None:
                report.errors.append(secret_path)
                log.error(
                    "secrets", "secret value must be a scalar string",
                    key=secret_path, found=secret_store._secret_type_name(current),
                )
                current = ""
            else:
                if not isinstance(current, str):
                    if write:
                        secret_store._set_nested(data, secret_path, normalized)
                        log.fix(
                            "secrets", "normalized secret scalar to string",
                            key=secret_path, found=secret_store._secret_type_name(current),
                        )
                    else:
                        report.errors.append(secret_path)
                        log.error(
                            "secrets", "secret value must be a string",
                            key=secret_path, found=secret_store._secret_type_name(current),
                        )
                current = normalized
        if not str(current).strip():
            report.warnings.append(secret_path)
            log.warn("secrets", "credential secret is empty", credential=credential_name)

    if prune:
        for path in OBSOLETE_SECRET_PATHS:
            if secret_store._unset_nested(data, path):
                report.removed.append(path)
                log.fix("secrets", "removed obsolete secret path", key=path)
    else:
        for path in OBSOLETE_SECRET_PATHS:
            if secret_store._get_nested(data, path) is not secret_store._MISSING:
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
        secret_store.write_secrets_data(data)
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

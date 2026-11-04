"""Read and write managed software policy in the selected machine file."""

import hashlib
from pathlib import Path
from typing import Any

from envy.evaluation import (
    invalidate_machine_manifest,
    last_manifest_error,
    machine_manifest,
    manifest_software_groups,
)
from envy.secure_io import atomic_write_text
from envy.software.model import (
    ConcurrentMachineEdit,
    GROUPS,
    ManagedInclude,
    SelectionGroup,
    SoftwarePolicyError,
    _evaluated_item_state,
)
from envy.software.policy_text import (
    _parse_managed_policy_source,
    _update_managed_policy_source,
    update_machine_source,
)
from envy.utils import machine_config_file


def machine_file(machine_id: str | None = None) -> Path:
    return machine_config_file(machine_id)


def source_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_managed_exclusions(
    path: Path | None = None,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> dict[str, list[str]]:
    return read_managed_policy(path, groups)[1]


def read_managed_policy(
    path: Path | None = None,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> tuple[list[ManagedInclude], dict[str, list[str]]]:
    active_groups = groups if groups is not None else GROUPS
    target = path or machine_file()
    if not target.exists():
        raise SoftwarePolicyError(f"machine configuration is missing: {target}")
    return _parse_managed_policy_source(target.read_text(), active_groups)


def write_managed_exclusions(
    values: dict[str, list[str]],
    path: Path | None = None,
    *,
    expected_digest: str | None = None,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> None:
    target = path or machine_file()
    original = target.read_text()
    if expected_digest is not None and source_digest(original) != expected_digest:
        raise ConcurrentMachineEdit(f"machine configuration changed while setup was open: {target}")
    updated = update_machine_source(original, values, groups)
    if updated != original:
        atomic_write_text(target, updated)


def write_and_validate_exclusions(
    values: dict[str, list[str]],
    path: Path | None = None,
    *,
    expected_digest: str | None = None,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> dict[str, Any]:
    target = path or machine_file()
    original = target.read_text()
    if expected_digest is not None and source_digest(original) != expected_digest:
        raise ConcurrentMachineEdit(f"machine configuration changed while setup was open: {target}")

    updated = update_machine_source(original, values, groups)
    if updated != original:
        atomic_write_text(target, updated)
    manifest = machine_manifest(refresh=True)
    if manifest is None or not manifest_software_groups(manifest):
        if updated != original:
            atomic_write_text(target, original)
        invalidate_machine_manifest()
        detail = last_manifest_error()
        message = "software manifest evaluation failed"
        if detail:
            message = f"{message}: {detail}"
        raise SoftwarePolicyError(
            f"{message}; restored the original machine configuration"
        )
    return manifest


def write_and_validate_software_policy(
    includes: list[ManagedInclude] | tuple[ManagedInclude, ...],
    exclusions: dict[str, list[str]],
    *,
    group_key: str,
    item_id: str,
    expected_effective: bool,
    path: Path | None = None,
    expected_digest: str | None = None,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> dict[str, Any]:
    """Atomically write direct managed include/exclude assignments and verify intent."""
    target = path or machine_file()
    original = target.read_text()
    if expected_digest is not None and source_digest(original) != expected_digest:
        raise ConcurrentMachineEdit(f"machine configuration changed while planning: {target}")
    active_groups = groups if groups is not None else GROUPS
    updated = _update_managed_policy_source(
        original, includes, exclusions, active_groups
    )
    if updated != original:
        atomic_write_text(target, updated)
    try:
        manifest = machine_manifest(refresh=True)
        if manifest is None or not manifest_software_groups(manifest):
            detail = last_manifest_error()
            message = "software manifest evaluation failed"
            if detail:
                message = f"{message}: {detail}"
            raise SoftwarePolicyError(message)
        selected_repositories = {
            item.get("sourceId") or "native:" + item["id"]
            for item in manifest_software_groups(manifest).get("native.system.repository", {}).get("selection", {}).get("effective", [])
        }
        for item in manifest_software_groups(manifest).get("native.system.package", {}).get("selection", {}).get("effective", []):
            if item.get("sourceId") not in (None, "native:system") and item["sourceId"] not in selected_repositories:
                raise SoftwarePolicyError("native software source prerequisite is not effective")
        _, effective, _ = _evaluated_item_state(manifest, group_key, item_id)
        if effective != expected_effective:
            expectation = "effective" if expected_effective else "absent from effective"
            raise SoftwarePolicyError(
                f"software intent verification failed: {group_key}/{item_id} is not {expectation}"
            )
    except Exception as exc:
        if updated != original:
            atomic_write_text(target, original)
        invalidate_machine_manifest()
        if isinstance(exc, SoftwarePolicyError):
            raise
        raise SoftwarePolicyError(str(exc)) from exc
    return manifest

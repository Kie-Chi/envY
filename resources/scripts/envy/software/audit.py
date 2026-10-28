"""Audit managed software policy and explain evaluated item ownership."""

from typing import Any

from envy.evaluation import manifest_software_groups
from envy.software.model import (
    AuditFinding,
    ManagedInclude,
    SelectionGroup,
    _evaluated_item_state,
    _managed_source_counts,
    _software_entries,
    require_group,
)


def audit_policy(
    manifest: dict[str, Any],
    includes: list[ManagedInclude],
    exclusions: dict[str, list[str]],
    groups: tuple[SelectionGroup, ...],
) -> list[AuditFinding]:
    """Find redundant or ambiguous machine-owned software policy."""
    findings: list[AuditFinding] = []
    manifest_groups = manifest_software_groups(manifest)
    for group in groups:
        selection = manifest_groups.get(group.key, {}).get("selection", {})
        evaluated_include = {
            item["id"] for item in _software_entries(selection.get("include"))
        }
        evaluated_effective = {
            item["id"] for item in _software_entries(selection.get("effective"))
        }
        managed_include = {
            item.id for item in includes if item.group == group.key
        }
        managed_exclude = set(exclusions.get(group.key, []))
        _, external_include, external_exclude = _managed_source_counts(
            manifest, group, includes, exclusions,
        )
        for item_id in sorted(managed_include & managed_exclude):
            findings.append(AuditFinding(
                "warn", "managed-include-exclude", group.key, item_id,
                "machine policy both includes and excludes this stable ID",
            ))
        for item_id in sorted(managed_exclude - evaluated_include):
            findings.append(AuditFinding(
                "warn", "stale-exclusion", group.key, item_id,
                "machine exclusion no longer masks an evaluated contribution",
            ))
        for item_id in sorted(managed_include):
            if external_include[item_id] > 0:
                findings.append(AuditFinding(
                    "info", "redundant-managed-include", group.key, item_id,
                    "the same stable ID is also contributed outside the managed block",
                ))
            if external_exclude[item_id] > 0:
                findings.append(AuditFinding(
                    "warn", "external-exclusion", group.key, item_id,
                    "an external exclusion masks this machine-managed include",
                ))
            if item_id in evaluated_include and item_id not in evaluated_effective and (
                item_id not in managed_exclude and external_exclude[item_id] == 0
            ):
                findings.append(AuditFinding(
                    "warn", "unexpected-ineffective", group.key, item_id,
                    "item is included but not effective without a visible exclusion",
                ))

    effective_names: dict[str, list[tuple[str, str]]] = {}
    for group in groups:
        selection = manifest_groups.get(group.key, {}).get("selection", {})
        for item in _software_entries(selection.get("effective")):
            effective_names.setdefault(item["name"].casefold(), []).append(
                (group.key, item["id"])
            )
    for matches in effective_names.values():
        ecosystems = {
            require_group(group_key, groups).ecosystem
            for group_key, _ in matches
        }
        if len(matches) > 1 and len(ecosystems) > 1:
            for group_key, item_id in matches:
                findings.append(AuditFinding(
                    "warn", "cross-ecosystem-name", group_key, item_id,
                    "the same effective software name appears in another ecosystem",
                ))
    return sorted(
        findings,
        key=lambda item: (
            0 if item.severity == "warn" else 1,
            item.group, item.item_id, item.code,
        ),
    )


def explain_policy_item(
    manifest: dict[str, Any],
    includes: list[ManagedInclude],
    exclusions: dict[str, list[str]],
    groups: tuple[SelectionGroup, ...],
    value: str,
    group_filter: str | None = None,
) -> list[dict[str, object]]:
    """Explain evaluated and machine-owned state for every exact item match."""
    rows: list[dict[str, object]] = []
    manifest_groups = manifest_software_groups(manifest)
    selected_groups = [
        group for group in groups
        if group_filter is None or group.key == group_filter
    ]
    if group_filter is not None and not selected_groups:
        require_group(group_filter, groups)
    for group in selected_groups:
        selection = manifest_groups.get(group.key, {}).get("selection", {})
        entries = {
            item["id"]: item
            for key in ("include", "effective")
            for item in _software_entries(selection.get(key))
        }
        candidates = {
            item_id for item_id, item in entries.items()
            if value in {item_id, item["name"], item.get("ref")}
        }
        candidates.update(
            item.id for item in includes
            if item.group == group.key and value in {item.id, item.name, item.ref}
        )
        if value in exclusions.get(group.key, []):
            candidates.add(value)
        for item_id in sorted(candidates):
            included, effective, excluded = _evaluated_item_state(
                manifest, group.key, item_id,
            )
            managed_include = next((
                item for item in includes
                if item.group == group.key and item.id == item_id
            ), None)
            machine_excluded = item_id in exclusions.get(group.key, [])
            _, external_include, external_exclude = _managed_source_counts(
                manifest, group, includes, exclusions,
            )
            entry = entries.get(item_id, {})
            rows.append({
                "group": group.key,
                "label": group.label,
                "item": item_id,
                "name": entry.get("name") or (managed_include.name if managed_include else item_id),
                "ref": entry.get("ref") or (managed_include.ref if managed_include else None),
                "included": included,
                "excluded": excluded,
                "effective": effective,
                "machineInclude": managed_include is not None,
                "machineExclude": machine_excluded,
                "externalInclude": external_include[item_id] > 0,
                "externalExclude": external_exclude[item_id] > 0,
            })
    return rows

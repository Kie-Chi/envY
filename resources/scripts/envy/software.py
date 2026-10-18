"""Direct managed software include/exclude policy and CLI."""

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from envy import log
from envy.evaluation import (
    invalidate_machine_manifest,
    last_manifest_error,
    machine_manifest,
    manifest_software_groups,
)
from envy.jsonio import emit, emit_error
from envy.mutation import offer_mutation_commit
from envy.process import run_process
from envy.search.index import RegistryIndex
from envy.search.model import SearchResult
from envy.search.providers import resolve_exact
from envy.sources.catalog import legacy_source_id, source_for
from envy.sources.completion import complete_add_source_ids, complete_search_source_ids
from envy.secure_io import atomic_write_text
from envy.utils import current_machine_id, machine_config_file, platform_name


MANAGED_START = "  # BEGIN ENVY MANAGED SOFTWARE"
MANAGED_END = "  # END ENVY MANAGED SOFTWARE"
LEGACY_MANAGED_START = "  # BEGIN ENVY MANAGED EXCLUSIONS"
LEGACY_MANAGED_END = "  # END ENVY MANAGED EXCLUSIONS"
MANAGED_INCLUDE_METADATA = "# envy: "


@dataclass(frozen=True)
class SelectionGroup:
    key: str
    option: str
    label: str
    ecosystem: str = ""
    scope: str = ""
    kind: str = ""
    editable_include: bool = True


COMMON_GROUPS = (
    SelectionGroup(
        "nix.user.package", "envy.software.nix.packages", "Nix packages",
        "nix", "user", "package",
    ),
    SelectionGroup(
        "npm.user.tool", "envy.software.npm.tools", "NPM tools",
        "npm", "user", "tool",
    ),
    SelectionGroup(
        "pypi.user.tool", "envy.software.pypi.tools", "Python tools",
        "pypi", "user", "tool",
    ),
)
DARWIN_GROUPS = (
    SelectionGroup(
        "nix.system.package", "envy.darwin.software.nix.systemPackages",
        "Darwin system packages", "nix", "system", "package",
    ),
    SelectionGroup(
        "nix.system.font", "envy.darwin.software.nix.fonts",
        "Darwin fonts", "nix", "system", "font",
    ),
    SelectionGroup(
        "homebrew.system.formula", "envy.darwin.software.homebrew.formulae",
        "Homebrew formulae", "homebrew", "system", "formula",
    ),
    SelectionGroup(
        "homebrew.system.cask", "envy.darwin.software.homebrew.casks",
        "Homebrew casks", "homebrew", "system", "cask",
    ),
    SelectionGroup(
        "homebrew.system.repository", "envy.darwin.software.homebrew.repositories",
        "Homebrew repositories", "homebrew", "system", "repository",
    ),
)
LINUX_GROUPS = (
    SelectionGroup(
        "native.system.package", "envy.linux.software.native.packages",
        "Native system packages", "native", "system", "package",
    ),
    SelectionGroup(
        "native.system.repository", "envy.linux.software.native.repositories",
        "Native repositories", "native", "system", "repository",
    ),
    SelectionGroup(
        "url.system.artifact", "envy.linux.software.url.artifacts",
        "System artifacts", "url", "system", "artifact", True,
    ),
)


def groups_for_platform(platform: str) -> tuple[SelectionGroup, ...]:
    if platform == "darwin":
        return COMMON_GROUPS + DARWIN_GROUPS
    return COMMON_GROUPS + LINUX_GROUPS


GROUPS = groups_for_platform(platform_name())


def groups_for_manifest(
    manifest: dict[str, Any] | None,
    *,
    include_empty: bool = False,
) -> tuple[SelectionGroup, ...]:
    groups: list[SelectionGroup] = []
    for group_id, value in manifest_software_groups(manifest).items():
        editable = value.get("editable")
        selection = value.get("selection")
        if not isinstance(editable, dict) or not editable.get("exclude"):
            continue
        if not isinstance(selection, dict):
            continue
        if not include_empty and not any(
            isinstance(selection.get(key), list) and selection[key]
            for key in ("include", "exclude", "effective")
        ):
            continue
        option = value.get("optionPath")
        if not isinstance(option, str) or not option.startswith("envy."):
            continue
        groups.append(SelectionGroup(
            key=group_id,
            option=option,
            label=str(value.get("label") or group_id),
            ecosystem=str(value.get("ecosystem") or ""),
            scope=str(value.get("scope") or ""),
            kind=str(value.get("kind") or ""),
            editable_include=bool(editable.get("include")),
        ))
    if groups:
        priority = {
            name: index
            for index, name in enumerate(
                ("nix", "homebrew", "native", "url", "npm", "pypi")
            )
        }
        groups.sort(key=lambda group: (
            priority.get(group.ecosystem, len(priority)), group.scope, group.kind
        ))
        return tuple(groups)
    platform = manifest.get("platform") if isinstance(manifest, dict) else platform_name()
    return groups_for_platform(str(platform))


@dataclass(frozen=True)
class SoftwareItem:
    id: str
    name: str
    version: str | None
    ref: str | None
    included: bool
    checked: bool
    managed: bool
    locked: bool
    stale: bool
    changed: bool
    source_id: str | None = None
    resolved_version: str | None = None


@dataclass(frozen=True)
class ManagedInclude:
    group: str
    id: str
    name: str
    version: str | None = None
    ref: str | None = None
    parameters: dict[str, Any] | None = None

    source_id: str | None = None

    def to_selection(self) -> dict[str, Any]:
        selection: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.version is not None:
            selection["version"] = self.version
        if self.ref is not None:
            selection["ref"] = self.ref
        if self.source_id is not None:
            selection["sourceId"] = self.source_id
        if self.parameters:
            selection["parameters"] = self.parameters
        return selection


@dataclass(frozen=True)
class SoftwarePlan:
    action: str
    group: SelectionGroup
    item_id: str
    includes_before: tuple[ManagedInclude, ...]
    includes_after: tuple[ManagedInclude, ...]
    exclusions_before: tuple[str, ...]
    exclusions_after: tuple[str, ...]
    expected_included: bool
    expected_excluded: bool
    expected_effective: bool
    clean: bool
    blocked: str | None = None
    prerequisites: tuple[dict[str, Any], ...] = ()
    source_id: str | None = None

    @property
    def include_added(self) -> tuple[str, ...]:
        before = {(item.group, item.id) for item in self.includes_before}
        return tuple(
            item.id for item in self.includes_after
            if (item.group, item.id) not in before
        )

    @property
    def include_removed(self) -> tuple[str, ...]:
        after = {(item.group, item.id) for item in self.includes_after}
        return tuple(
            item.id for item in self.includes_before
            if (item.group, item.id) not in after
        )

    @property
    def exclude_added(self) -> tuple[str, ...]:
        return tuple(item for item in self.exclusions_after if item not in self.exclusions_before)

    @property
    def exclude_removed(self) -> tuple[str, ...]:
        return tuple(item for item in self.exclusions_before if item not in self.exclusions_after)

    @property
    def changed(self) -> bool:
        return bool(
            self.include_added or self.include_removed
            or self.exclude_added or self.exclude_removed
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the complete plan contract consumed by TUI frontends."""
        return {
            "action": self.action,
            "group": {
                "id": self.group.key,
                "label": self.group.label,
                "ecosystem": self.group.ecosystem,
                "scope": self.group.scope,
                "kind": self.group.kind,
            },
            "item": self.item_id,
            "includeAdded": list(self.include_added),
            "includeRemoved": list(self.include_removed),
            "excludeAdded": list(self.exclude_added),
            "excludeRemoved": list(self.exclude_removed),
            "expected": {
                "included": self.expected_included,
                "excluded": self.expected_excluded,
                "effective": self.expected_effective,
            },
            "clean": self.clean,
            "changed": self.changed,
            "blocked": self.blocked,
            "prerequisites": list(self.prerequisites),
            "sourceId": self.source_id,
        }


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    code: str
    group: str
    item_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "group": self.group,
            "item": self.item_id,
            "message": self.message,
        }


class SoftwarePolicyError(ValueError):
    """Raised when managed software policy cannot be safely handled."""

    def __init__(self, message: str, code: str = "invalid-policy"):
        super().__init__(message)
        self.code = code


class ConcurrentMachineEdit(SoftwarePolicyError):
    """Raised when machine.nix changed while an editor session was open."""


def machine_file(machine_id: str | None = None) -> Path:
    return machine_config_file(machine_id)


def empty_exclusions(
    groups: tuple[SelectionGroup, ...] | None = None,
) -> dict[str, list[str]]:
    active_groups = groups if groups is not None else GROUPS
    return {group.key: [] for group in active_groups}


def normalize_exclusions(
    values: dict[str, list[str]] | None,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> dict[str, list[str]]:
    active_groups = groups if groups is not None else GROUPS
    normalized = empty_exclusions(active_groups)
    for group in active_groups:
        seen: set[str] = set()
        for raw in (values or {}).get(group.key, []):
            item_id = str(raw)
            _validate_name(item_id)
            if item_id not in seen:
                seen.add(item_id)
                normalized[group.key].append(item_id)
    return normalized


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


def normalize_managed_includes(
    values: list[ManagedInclude | dict[str, Any]] | tuple[ManagedInclude, ...] | None,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> list[ManagedInclude]:
    """Validate, de-duplicate, and deterministically order CLI-owned includes."""
    active_groups = groups if groups is not None else GROUPS
    group_by_key = {group.key: group for group in active_groups}
    normalized: list[ManagedInclude] = []
    by_key: dict[tuple[str, str], ManagedInclude] = {}
    for raw in values or []:
        if isinstance(raw, ManagedInclude):
            item = raw
        elif isinstance(raw, dict):
            required = {key: raw.get(key) for key in ("group", "id", "name")}
            if not all(isinstance(value, str) for value in required.values()):
                raise SoftwarePolicyError(
                    "managed software group, id, and name must be strings"
                )
            version = raw.get("version")
            ref = raw.get("ref")
            if version is not None and not isinstance(version, str):
                raise SoftwarePolicyError("managed software version must be a string or null")
            if ref is not None and not isinstance(ref, str):
                raise SoftwarePolicyError("managed software reference must be a string or null")
            parameters = raw.get("parameters", {})
            item = ManagedInclude(
                group=required["group"],
                id=required["id"],
                name=required["name"],
                version=version,
                ref=ref,
                parameters=parameters if isinstance(parameters, dict) else None,
                source_id=raw.get("sourceId") or raw.get("source_id"),
            )
        else:
            raise SoftwarePolicyError("managed software entries must be objects")
        group = group_by_key.get(item.group)
        if group is None:
            raise SoftwarePolicyError(f"unknown managed software group: {item.group}")
        if not group.editable_include:
            raise SoftwarePolicyError(f"software group does not support managed includes: {item.group}")
        _validate_name(item.id)
        _validate_name(item.name)
        if item.version is not None:
            _validate_text_value(item.version, "version")
        if item.ref is not None:
            _validate_text_value(item.ref, "reference")
        if item.parameters is None or not _json_value_supported(item.parameters):
            raise SoftwarePolicyError(f"invalid managed software parameters: {item.group}/{item.id}")
        key = (item.group, item.id)
        previous = by_key.get(key)
        if previous is not None:
            if previous != item:
                raise SoftwarePolicyError(
                    f"conflicting managed software entries: {item.group}/{item.id}"
                )
            continue
        by_key[key] = item
        normalized.append(item)
    priority = {group.key: index for index, group in enumerate(active_groups)}
    normalized.sort(key=lambda item: (priority[item.group], item.id))
    return normalized


def render_managed_policy(
    includes: list[ManagedInclude | dict[str, Any]] | tuple[ManagedInclude, ...],
    exclusions: dict[str, list[str]],
    groups: tuple[SelectionGroup, ...] | None = None,
) -> str:
    active_groups = groups if groups is not None else GROUPS
    normalized_includes = normalize_managed_includes(includes, active_groups)
    normalized_exclusions = normalize_exclusions(exclusions, active_groups)
    by_group = {
        group.key: [item for item in normalized_includes if item.group == group.key]
        for group in active_groups
    }
    lines = [
        MANAGED_START + "\n",
        "  # `envy setup` and `envy software` own only these machine-local selections.\n",
    ]
    for group in active_groups:
        includes = by_group[group.key]
        if includes:
            lines.append(f"\n  {group.option}.include = ")
            if group.ecosystem == "nix":
                lines.append("[\n")
                for item in includes:
                    if item.ref is None:
                        raise SoftwarePolicyError(
                            f"managed Nix package has no canonical reference: {item.id}"
                        )
                    metadata = {"id": item.id, "ref": item.ref, "sourceId": item.source_id}
                    payload = json.dumps(
                        metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                    )
                    lines.append(f"    {MANAGED_INCLUDE_METADATA}{payload}\n")
                    lines.append(f"    {_render_nix_package(item)}\n")
                lines.append("  ];\n")
                references = {item.id: item.ref for item in includes}
                json_text = json.dumps(
                    references, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                lines.append(
                    f"  {group.option}.references = builtins.fromJSON "
                    f"{json.dumps(json_text, ensure_ascii=False)};\n"
                )
            elif group.ecosystem == "homebrew":
                lines.append("[\n")
                for item in includes:
                    lines.append(f'    "{_escape_nix_string(item.name)}"\n')
                lines.append("  ];\n")
            else:
                payload = [item.to_selection() for item in includes]
                json_text = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                lines.append(f"builtins.fromJSON {json.dumps(json_text, ensure_ascii=False)};\n")

        names = normalized_exclusions[group.key]
        if names:
            lines.append(f"\n  {group.option}.exclude = [\n")
            for name in names:
                lines.append(f'    "{_escape_nix_string(name)}"\n')
            lines.append("  ];\n")
    lines.append(MANAGED_END)
    return "".join(lines)


def _update_managed_policy_source(
    text: str,
    includes: list[ManagedInclude | dict[str, Any]] | tuple[ManagedInclude, ...],
    exclusions: dict[str, list[str]],
    groups: tuple[SelectionGroup, ...],
) -> str:
    normalized_includes = normalize_managed_includes(includes, groups)
    normalized_exclusions = normalize_exclusions(exclusions, groups)
    has_values = bool(normalized_includes) or any(normalized_exclusions.values())
    match = _managed_match(text)
    if match is not None:
        if has_values:
            updated = (
                text[:match.start()]
                + render_managed_policy(normalized_includes, normalized_exclusions, groups)
                + text[match.end():]
            )
        else:
            updated = text[:match.start()].rstrip() + "\n\n" + text[match.end():].lstrip("\n")
    elif has_values:
        updated = _insert_managed_source_block(
            text, render_managed_policy(normalized_includes, normalized_exclusions, groups)
        )
    else:
        updated = text
    if any(item.group.startswith("nix.") for item in normalized_includes):
        updated = _ensure_module_argument(updated, "pkgs")
    if any((item.parameters or {}).get("input") for item in normalized_includes):
        updated = _ensure_module_argument(updated, "softwareSourcePackages")
    return updated if updated.endswith("\n") else updated + "\n"


def update_machine_source(
    text: str,
    values: dict[str, list[str]],
    groups: tuple[SelectionGroup, ...] | None = None,
) -> str:
    active_groups = groups if groups is not None else GROUPS
    includes = _parse_managed_policy_source(text, active_groups)[0]
    return _update_managed_policy_source(text, includes, values, active_groups)


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


def build_software_items(
    manifest: dict[str, Any] | None,
    managed: dict[str, list[str]],
    original_managed: dict[str, list[str]] | None = None,
    query: str = "",
    *,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> dict[str, list[SoftwareItem]]:
    active_groups = groups or groups_for_manifest(manifest)
    current = normalize_exclusions(managed, active_groups)
    original = normalize_exclusions(
        original_managed if original_managed is not None else managed,
        active_groups,
    )
    manifest_groups = manifest_software_groups(manifest)
    result: dict[str, list[SoftwareItem]] = {}
    needle = query.casefold().strip()

    for group in active_groups:
        selection = manifest_groups.get(group.key, {}).get("selection", {})
        include_entries = _software_entries(selection.get("include"))
        include_by_id = {entry["id"]: entry for entry in include_entries}
        final_exclude = _string_list(selection.get("exclude"))
        original_set = set(original[group.key])
        current_set = set(current[group.key])
        final_counts = Counter(final_exclude)
        managed_counts = Counter(original[group.key])
        external_set = {
            item_id
            for item_id, count in final_counts.items()
            if count > managed_counts[item_id]
        }
        predicted_excluded = external_set | current_set
        item_ids = _ordered_unique([
            *include_by_id,
            *final_exclude,
            *original[group.key],
            *current[group.key],
        ])
        items: list[SoftwareItem] = []
        for item_id in item_ids:
            entry = include_by_id.get(item_id, {"id": item_id, "name": item_id})
            name = str(entry.get("name") or item_id)
            if needle and needle not in name.casefold() and needle not in item_id.casefold():
                continue
            included = item_id in include_by_id
            locked = item_id in external_set
            managed_here = item_id in current_set
            items.append(SoftwareItem(
                id=item_id,
                name=name,
                version=_optional_string(entry.get("version")),
                resolved_version=_optional_string(entry.get("resolvedVersion")),
                ref=_optional_string(entry.get("ref")),
                included=included,
                checked=included and item_id not in predicted_excluded,
                managed=managed_here,
                locked=locked,
                stale=not included and item_id in predicted_excluded,
                changed=(item_id in current_set) != (item_id in original_set),
                source_id=entry.get("sourceId") or legacy_source_id(group.ecosystem, entry.get("ref"), group.kind),
            ))
        result[group.key] = items
    return result


def set_excluded(
    values: dict[str, list[str]],
    group_key: str,
    item_id: str,
    excluded: bool,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> None:
    active_groups = groups if groups is not None else GROUPS
    group = require_group(group_key, active_groups)
    _validate_name(item_id)
    current = normalize_exclusions(values, active_groups)
    names = current[group.key]
    if excluded and item_id not in names:
        names.append(item_id)
    elif not excluded and item_id in names:
        names.remove(item_id)
    values.clear()
    values.update(current)


def require_group(
    value: str,
    groups: tuple[SelectionGroup, ...] | None = None,
) -> SelectionGroup:
    active_groups = groups if groups is not None else GROUPS
    option = value.removesuffix(".exclude")
    matches = [
        group for group in active_groups
        if value == group.key or option == group.option
    ]
    if len(matches) == 1:
        return matches[0]
    allowed = ", ".join(item.key for item in active_groups)
    raise typer.BadParameter(f"unknown software group: {value}; choose one of: {allowed}")


def _group_matches_item(
    manifest: dict[str, Any], groups: tuple[SelectionGroup, ...], item_value: str,
) -> list[SelectionGroup]:
    source = machine_file().read_text()
    _, exclusions = _parse_managed_policy_source(source, groups)
    items_by_group = build_software_items(manifest, exclusions, groups=groups)
    needle = item_value.casefold()
    matches: list[SelectionGroup] = []
    for group in groups:
        if any(
            needle in {
                item.id.casefold(),
                item.name.casefold(),
                (item.ref or "").casefold(),
            }
            for item in items_by_group[group.key]
        ):
            matches.append(group)
            continue
        if group.ecosystem in {"homebrew", "npm", "pypi", "native"}:
            indexed = RegistryIndex().lookup(group.ecosystem, group.kind, item_value)
            if indexed is not None:
                matches.append(group)
    return matches


def _reference_groups(
    groups: tuple[SelectionGroup, ...], item_value: str,
) -> list[SelectionGroup]:
    if ":" not in item_value:
        return []
    ecosystem, reference = item_value.split(":", 1)
    if ecosystem == "flake":
        ecosystem = "nix"
    matches = [group for group in groups if group.ecosystem == ecosystem]
    if ecosystem == "homebrew" and "/" in reference:
        kind = reference.split("/", 1)[0]
        aliases = {"cask": "cask", "formula": "formula", "tap": "repository"}
        if kind in aliases:
            matches = [group for group in matches if group.kind == aliases[kind]]
    return matches


def _choose_cli_group(
    item_value: str,
    *,
    action: str,
    json_output: bool,
) -> str:
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    matches = _group_matches_item(manifest, groups, item_value)
    if not matches:
        matches = _reference_groups(groups, item_value)
    if not matches and action == "add":
        matches = [group for group in groups if group.editable_include]
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        if not json_output:
            log.info(
                "software", "selected compatible group",
                group=matches[0].key, label=matches[0].label,
            )
        return matches[0].key
    if json_output or not sys.stdin.isatty():
        if not matches:
            raise typer.BadParameter(
                f"cannot infer a software group for {item_value}; pass --group <group>"
            )
        choices = ", ".join(group.key for group in matches)
        raise typer.BadParameter(
            f"software group is ambiguous for {item_value}; pass --group with one of: {choices}"
        )
    if not matches:
        raise typer.BadParameter(f"no compatible software group found for: {item_value}")

    table = Table(title=f"Choose how envY should manage {item_value}")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Type")
    table.add_column("Canonical group", style="dim")
    for index, group in enumerate(matches, start=1):
        table.add_row(str(index), group.label, group.key)
    log.console.print(table)
    while True:
        selected = typer.prompt("Software type", default=1, type=int)
        if 1 <= selected <= len(matches):
            return matches[selected - 1].key
        log.warn("software", f"choose a number between 1 and {len(matches)}")


def software_changes(
    original: dict[str, list[str]],
    current: dict[str, list[str]],
    groups: tuple[SelectionGroup, ...] | None = None,
) -> list[tuple[str, list[str], list[str]]]:
    active_groups = groups if groups is not None else GROUPS
    before = normalize_exclusions(original, active_groups)
    after = normalize_exclusions(current, active_groups)
    changes = []
    for group in active_groups:
        disabled = [name for name in after[group.key] if name not in before[group.key]]
        enabled = [name for name in before[group.key] if name not in after[group.key]]
        if disabled or enabled:
            changes.append((group.label, disabled, enabled))
    return changes


def _managed_match(text: str) -> re.Match[str] | None:
    current = _named_managed_match(text, MANAGED_START, MANAGED_END, "software")
    legacy = _named_managed_match(
        text, LEGACY_MANAGED_START, LEGACY_MANAGED_END, "legacy software"
    )
    if current is not None and legacy is not None:
        raise SoftwarePolicyError("managed software block is duplicated")
    return current or legacy


def _named_managed_match(
    text: str,
    start: str,
    end: str,
    label: str,
) -> re.Match[str] | None:
    start_count = text.count(start)
    end_count = text.count(end)
    if start_count != end_count or start_count > 1:
        raise SoftwarePolicyError(f"managed {label} block markers are missing or duplicated")
    if start_count == 0:
        return None
    pattern = re.compile(rf"(?ms)^{re.escape(start)}$.*?^{re.escape(end)}$")
    match = pattern.search(text)
    if match is None:
        raise SoftwarePolicyError(f"cannot parse managed {label} block")
    return match


def _insert_managed_source_block(text: str, block: str) -> str:
    anchor = "  # END ENVY MANAGED CONFIG"
    anchor_index = text.find(anchor)
    if anchor_index >= 0:
        insert_at = text.find("\n", anchor_index)
        insert_at = len(text) if insert_at < 0 else insert_at + 1
    else:
        insert_at = text.rfind("\n}")
        if insert_at < 0:
            raise SoftwarePolicyError(
                "cannot locate the top-level closing brace in machine configuration"
            )
        insert_at += 1
    prefix = text[:insert_at].rstrip()
    suffix = text[insert_at:].lstrip("\n")
    updated = prefix + "\n\n" + block + "\n\n" + suffix
    return updated if updated.endswith("\n") else updated + "\n"


def _parse_managed_policy_source(
    text: str,
    groups: tuple[SelectionGroup, ...],
) -> tuple[list[ManagedInclude], dict[str, list[str]]]:
    match = _managed_match(text)
    if match is None:
        return [], empty_exclusions(groups)
    block = match.group(0)
    exclusions = empty_exclusions(groups)
    includes: list[ManagedInclude] = []
    group_by_option = {group.option: group for group in groups}
    seen: set[tuple[str, str]] = set()
    body = block.split("\n", 1)[1].rsplit("\n", 1)[0]
    options = "|".join(re.escape(option) for option in group_by_option)
    assignment = re.compile(
        rf"(?ms)^\s*({options})\.(include|exclude|references)\s*=\s*(.*?)\s*;[ \t]*(?:\n|$)"
    )
    cursor = 0
    nix_include_values: list[tuple[SelectionGroup, str]] = []
    nix_references: dict[str, dict[str, str]] = {}
    for assignment_match in assignment.finditer(body):
        gap = body[cursor:assignment_match.start()]
        if _strip_managed_comments(gap).strip():
            raise SoftwarePolicyError("unsupported content in managed software block")
        group = group_by_option[assignment_match.group(1)]
        field = assignment_match.group(2)
        key = (group.key, field)
        if key in seen:
            raise SoftwarePolicyError(f"duplicate managed assignment: {group.option}.{field}")
        seen.add(key)
        value = assignment_match.group(3)
        if field == "exclude":
            exclusions[group.key] = _parse_string_selection(value, "exclusion")
        elif field == "references":
            if group.ecosystem != "nix":
                raise SoftwarePolicyError(
                    f"managed source references are only supported for Nix groups: {group.key}"
                )
            nix_references[group.key] = _parse_nix_references(value)
        elif group.ecosystem == "nix":
            nix_include_values.append((group, value))
        elif group.ecosystem == "homebrew":
            includes.extend(
                ManagedInclude(
                    group=group.key,
                    id=name,
                    name=name,
                    ref=f"homebrew:{'tap' if group.kind == 'repository' else group.kind}/{name}",
                    parameters={},
                )
                for name in _parse_string_selection(value, "include")
            )
        else:
            includes.extend(_parse_item_includes(value, group))
        cursor = assignment_match.end()
    if _strip_managed_comments(body[cursor:]).strip():
        raise SoftwarePolicyError("unsupported content in managed software block")
    for group, value in nix_include_values:
        parsed = _parse_nix_includes(value, group)
        references = nix_references.get(group.key)
        if references is not None:
            expected = {item.id: item.ref for item in parsed}
            if references != expected:
                raise SoftwarePolicyError(
                    f"managed Nix source references do not match includes: {group.key}"
                )
        includes.extend(parsed)
    return (
        normalize_managed_includes(includes, groups),
        normalize_exclusions(exclusions, groups),
    )


def _strip_managed_comments(value: str) -> str:
    return re.sub(r"(?m)^\s*#.*$", "", value)


def _parse_string_selection(value: str, label: str) -> list[str]:
    match = re.fullmatch(r"\s*\[(.*)\]\s*", value, flags=re.DOTALL)
    if match is None:
        raise SoftwarePolicyError(f"managed {label} must be a quoted string list")
    return _parse_string_list(match.group(1), label=label)


def _parse_item_includes(value: str, group: SelectionGroup) -> list[ManagedInclude]:
    match = re.fullmatch(
        r'\s*builtins\.fromJSON\s+("(?:\\.|[^"\\])*")\s*',
        value,
        flags=re.DOTALL,
    )
    if match is None:
        raise SoftwarePolicyError("managed structured include must use builtins.fromJSON")
    try:
        payload = json.loads(json.loads(match.group(1)))
    except (TypeError, json.JSONDecodeError) as exc:
        raise SoftwarePolicyError("cannot parse managed structured include") from exc
    if not isinstance(payload, list):
        raise SoftwarePolicyError("managed structured include must be a list")
    return normalize_managed_includes(
        [{**item, "group": group.key} if isinstance(item, dict) else item for item in payload],
        (group,),
    )


def _parse_nix_includes(value: str, group: SelectionGroup) -> list[ManagedInclude]:
    match = re.fullmatch(r"\s*\[(.*)\]\s*", value, flags=re.DOTALL)
    if match is None:
        raise SoftwarePolicyError("managed Nix include must be a package list")
    lines = match.group(1).splitlines()
    includes: list[ManagedInclude] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        metadata_line = lines[index].strip()
        if not metadata_line.startswith(MANAGED_INCLUDE_METADATA):
            raise SoftwarePolicyError("managed Nix package is missing envY metadata")
        try:
            metadata = json.loads(metadata_line.removeprefix(MANAGED_INCLUDE_METADATA))
        except json.JSONDecodeError as exc:
            raise SoftwarePolicyError("cannot parse managed Nix package metadata") from exc
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines):
            raise SoftwarePolicyError("managed Nix package expression is missing")
        expression = lines[index].strip()
        attr_path = _parse_nix_package(expression)
        source_input = None
        if expression.startswith("softwareSourcePackages."):
            source_input, *attr_path = attr_path
        if not isinstance(metadata, dict):
            raise SoftwarePolicyError("managed Nix package metadata must be an object")
        item_id = metadata.get("id")
        ref = metadata.get("ref")
        if not isinstance(item_id, str) or not isinstance(ref, str):
            raise SoftwarePolicyError("managed Nix package metadata requires id and ref")
        if source_input:
            if ref != f"flake:{source_input}#{'.'.join(attr_path)}" or metadata.get("sourceId") not in (None, "nix:" + source_input):
                raise SoftwarePolicyError("Nix source metadata conflicts with package expression")
        elif ref.startswith("nix:"):
            declared = ref[4:].split(".")
            if declared[:1] in (["legacyPackages"], ["packages"]):
                declared = declared[2:]
            if declared != attr_path:
                raise SoftwarePolicyError("Nix reference conflicts with package expression")
        else:
            raise SoftwarePolicyError("managed Nix expression has no matching source reference")
        includes.append(ManagedInclude(
            group=group.key,
            id=item_id,
            name=item_id,
            ref=ref,
            parameters={"attrPath": attr_path, **({"input": source_input} if source_input else {})},
            source_id=metadata.get("sourceId"),
        ))
        index += 1
    return normalize_managed_includes(includes, (group,))


def _parse_nix_references(value: str) -> dict[str, str]:
    match = re.fullmatch(
        r'\s*builtins\.fromJSON\s+("(?:\\.|[^"\\])*")\s*', value, flags=re.DOTALL,
    )
    if match is None:
        raise SoftwarePolicyError("managed Nix source references must use builtins.fromJSON")
    try:
        payload = json.loads(json.loads(match.group(1)))
    except (TypeError, json.JSONDecodeError) as exc:
        raise SoftwarePolicyError("cannot parse managed Nix source references") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(item_id, str) and isinstance(ref, str)
        for item_id, ref in payload.items()
    ):
        raise SoftwarePolicyError("managed Nix source references must be a string mapping")
    for item_id, ref in payload.items():
        _validate_name(item_id)
        _validate_text_value(ref, "reference")
    return payload


def _parse_string_list(body: str, *, label: str = "exclusion") -> list[str]:
    strings = re.compile(r'"((?:\\.|[^"\\])*)"')
    values = []
    cursor = 0
    for match in strings.finditer(body):
        if body[cursor:match.start()].strip():
            raise SoftwarePolicyError(f"{label} lists may contain only quoted strings")
        values.append(_unescape_nix_string(match.group(1)))
        cursor = match.end()
    if body[cursor:].strip():
        raise SoftwarePolicyError(f"{label} lists may contain only quoted strings")
    return values


def _software_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for raw in value:
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            result.append(raw)
        elif isinstance(raw, str):
            result.append({"id": raw, "name": raw})
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _evaluated_item_state(
    manifest: dict[str, Any],
    group_key: str,
    item_id: str,
) -> tuple[bool, bool, bool]:
    """Return whether an item is included, effective, and excluded after evaluation."""
    group = manifest_software_groups(manifest).get(group_key, {})
    selection = group.get("selection") if isinstance(group, dict) else None
    if not isinstance(selection, dict):
        return False, False, False
    included = item_id in {
        item["id"] for item in _software_entries(selection.get("include"))
    }
    effective = item_id in {
        item["id"] for item in _software_entries(selection.get("effective"))
    }
    excluded = item_id in _string_list(selection.get("exclude"))
    return included, effective, excluded


def _validate_name(name: str) -> None:
    if (
        not name
        or name != name.strip()
        or "${" in name
        or any(ord(char) < 32 for char in name)
        or any(char in name for char in ('"', "\\"))
    ):
        raise SoftwarePolicyError(f"invalid software name: {name!r}")


def _validate_text_value(value: str, label: str) -> None:
    if "${" in value or any(ord(char) < 32 for char in value):
        raise SoftwarePolicyError(f"invalid software {label}: {value!r}")


def _json_value_supported(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return "${" not in value and not any(ord(char) < 32 for char in value)
    if isinstance(value, list):
        return all(_json_value_supported(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and "${" not in key
            and not any(ord(char) < 32 for char in key)
            and _json_value_supported(item)
            for key, item in value.items()
        )
    return False


def _render_nix_package(item: ManagedInclude) -> str:
    parameters = item.parameters or {}
    attr_path = parameters.get("attrPath")
    if not isinstance(attr_path, list) or not attr_path or not all(
        isinstance(part, str) and part for part in attr_path
    ):
        raise SoftwarePolicyError(f"managed Nix package has no attr path: {item.id}")
    prefix = "pkgs"
    if parameters.get("input"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", parameters["input"]):
            raise SoftwarePolicyError("invalid Nix source input")
        prefix = "softwareSourcePackages." + json.dumps(parameters["input"])
    return prefix + "".join(f".{json.dumps(part, ensure_ascii=False)}" for part in attr_path)


def _parse_nix_package(value: str) -> list[str]:
    match = re.fullmatch(r'(?:pkgs|softwareSourcePackages)((?:\."(?:\\.|[^"\\])*")+)', value)
    if match is None:
        raise SoftwarePolicyError("unsupported managed Nix package expression")
    try:
        return [json.loads(raw) for raw in re.findall(r'("(?:\\.|[^"\\])*")', match.group(1))]
    except json.JSONDecodeError as exc:
        raise SoftwarePolicyError("cannot parse managed Nix package attr path") from exc


def _ensure_module_argument(text: str, argument: str) -> str:
    header = re.match(r"(?s)^(\s*)\{([^{}]*)\}\s*:", text)
    if header is None:
        raise SoftwarePolicyError(
            f"cannot expose {argument!r} in the machine module header"
        )
    raw_arguments = header.group(2)
    tokens = [token.strip() for token in raw_arguments.split(",") if token.strip()]
    names = {token.split("?", 1)[0].strip() for token in tokens if token != "..."}
    if argument in names:
        return text
    if "..." not in tokens:
        raise SoftwarePolicyError(
            f"machine module header must accept ... before envY can add {argument!r}"
        )
    replacement = "{ " + ", ".join([argument, *tokens]) + " }:"
    return text[:header.start()] + header.group(1) + replacement + text[header.end():]


def _escape_nix_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")


def _unescape_nix_string(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _completion_policy(manifest=None) -> tuple[
    tuple[SelectionGroup, ...],
    dict[str, list[SoftwareItem]],
]:
    """Load completion candidates without allowing evaluation errors to escape."""
    try:
        if manifest is None:
            manifest = machine_manifest(completion=True)
        if not manifest_software_groups(manifest):
            return (), {}
        groups = groups_for_manifest(manifest, include_empty=True)
        managed = read_managed_exclusions(groups=groups)
        return groups, build_software_items(manifest, managed, groups=groups)
    except (OSError, RuntimeError, TypeError, ValueError):
        return (), {}


def _completion_candidates(action: str) -> tuple[
    tuple[SelectionGroup, ...],
    dict[str, list[SoftwareItem]],
]:
    groups, items_by_group = _completion_policy()
    candidates = {
        group.key: [
            item
            for item in items_by_group.get(group.key, [])
            if (
                item.included and item.checked and not item.locked
                if action == "dis"
                else item.managed
                if action == "en"
                else not item.checked and not item.locked
                if action == "add"
                else item.included or item.managed
            )
        ]
        for group in groups
    }
    return groups, candidates


def _complete_groups(incomplete: str, *, action: str) -> list[tuple[str, str]]:
    groups, items_by_group = _completion_candidates(action)
    return [
        (group.key, group.label)
        for group in groups
        if group.key.startswith(incomplete)
        and (
            action == "rm"
            or action == "add" and (group.editable_include or items_by_group[group.key])
            or action in {"en", "dis"} and items_by_group[group.key]
        )
    ]


def _complete_items(ctx, incomplete: str, *, action: str) -> list[tuple[str, str]]:
    group_key = None
    if ctx is not None:
        group_key = ctx.params.get("group") or ctx.params.get("group_or_item")
    if not isinstance(group_key, str):
        return []
    groups, items_by_group = _completion_candidates(action)
    try:
        group = require_group(group_key, groups)
    except typer.BadParameter:
        return []
    needle = incomplete.casefold()
    return [
        (
            item.id,
            _completion_help(item, group, action=action),
        )
        for item in items_by_group[group.key]
        if (
            item.id.casefold().startswith(needle)
            or item.name.casefold().startswith(needle)
            or (item.ref or "").casefold().startswith(needle)
        )
    ]


def _completion_help(
    item: SoftwareItem,
    group: SelectionGroup,
    *,
    action: str,
) -> str:
    if action == "add":
        intent = (
            "add include and remove stale exclusion"
            if item.stale
            else "remove machine exclusion"
        )
    elif action == "rm" and item.checked:
        intent = "add machine exclusion"
    elif action == "rm" and item.stale:
        intent = "already stale; --clean can prune"
    elif action == "rm":
        intent = "already excluded; --clean can normalize"
    elif action == "en" and item.stale:
        intent = "stale exclusion; remove machine mask"
    else:
        intent = ""
    details = " ".join(filter(None, (
        item.name if item.name != item.id else "",
        f"v{item.version}" if item.version else "",
    )))
    if intent and details:
        return f"{intent}; {details}"
    return intent or details or group.label


def complete_disable_groups(ctx, incomplete: str) -> list[tuple[str, str]]:
    del ctx
    return _complete_groups(incomplete, action="dis")


def complete_enable_groups(ctx, incomplete: str) -> list[tuple[str, str]]:
    del ctx
    return _complete_groups(incomplete, action="en")


def complete_disable_items(ctx, incomplete: str) -> list[tuple[str, str]]:
    return _complete_items(ctx, incomplete, action="dis")


def complete_enable_items(ctx, incomplete: str) -> list[tuple[str, str]]:
    return _complete_items(ctx, incomplete, action="en")


def complete_add_groups(ctx, incomplete: str) -> list[tuple[str, str]]:
    del ctx
    return _complete_groups(incomplete, action="add")


def complete_remove_groups(ctx, incomplete: str) -> list[tuple[str, str]]:
    del ctx
    return _complete_groups(incomplete, action="rm")


def _complete_smart_targets(incomplete: str, *, action: str) -> list[tuple[str, str]]:
    groups, items_by_group = _completion_candidates(action)
    candidates = [
        (group.key, group.label)
        for group in groups
        if group.key.startswith(incomplete)
        and (
            action == "rm"
            or action == "add" and (group.editable_include or items_by_group[group.key])
            or action in {"en", "dis"} and items_by_group[group.key]
        )
    ]
    needle = incomplete.casefold()
    seen = {value for value, _ in candidates}
    for group in groups:
        for item in items_by_group.get(group.key, []):
            if item.id in seen or not (
                item.id.casefold().startswith(needle)
                or item.name.casefold().startswith(needle)
                or (item.ref or "").casefold().startswith(needle)
            ):
                continue
            candidates.append((item.id, f"{group.label}; {_completion_help(item, group, action=action)}"))
            seen.add(item.id)
    return candidates


def complete_add_targets(ctx, incomplete: str) -> list[tuple[str, str]]:
    return _complete_add(ctx, incomplete, smart=True)


def complete_remove_targets(ctx, incomplete: str) -> list[tuple[str, str]]:
    del ctx
    return _complete_smart_targets(incomplete, action="rm")


def complete_add_items(ctx, incomplete: str) -> list[tuple[str, str]]:
    return _complete_add(ctx, incomplete, smart=False)


def _complete_add(ctx, incomplete: str, *, smart: bool) -> list[tuple[str, str]]:
    params = getattr(ctx, "params", {}) or {}
    group_key = params.get("group") or (None if smart else params.get("group_or_item"))
    try:
        manifest = machine_manifest(completion=True)
        if not manifest:
            return []
        groups, all_items_by_group = _completion_policy(manifest)
        if isinstance(group_key, str):
            groups = (require_group(group_key, groups),)
        elif not smart:
            return []
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError, typer.BadParameter):
        return []

    source_id = params.get("source_id")
    source_id = source_id if isinstance(source_id, str) else None
    if source_id:
        groups = tuple(group for group in groups if group.ecosystem == source_id.split(":", 1)[0])
    index = RegistryIndex()
    sources = {}
    if index.path.exists() and manifest.get("schemaVersion") == 3:
        try:
            from envy.sources.catalog import catalog
            # These observations read local package-manager state and lock data;
            # they never query a registry or refresh package metadata.
            sources = catalog(manifest)
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
            pass
    candidates = []
    if smart and group_key is None:
        candidates.extend((group.key, group.label) for group in groups
                          if group.key.startswith(incomplete) and group.editable_include)
    for group in groups:
        candidates.extend(_complete_add_in_group(
            group, all_items_by_group.get(group.key, []), incomplete,
            index=index, manifest=manifest, sources=sources, source_id=source_id,
            canonical=smart and group_key is None,
        ))
    return list(dict(candidates).items())


def _complete_add_in_group(group, all_items, incomplete, *, index, manifest, sources,
                           source_id, canonical):
    items = [
        item for item in all_items
        if not item.checked and not item.locked
        and (not source_id or (item.source_id or legacy_source_id(group.ecosystem, item.ref, group.kind)) == source_id)
    ]
    needle = incomplete.casefold()
    candidates = [
        (item.id, _completion_help(item, group, action="add"))
        for item in items
        if (
            item.id.casefold().startswith(needle)
            or item.name.casefold().startswith(needle)
            or (item.ref or "").casefold().startswith(needle)
        )
    ]
    if not group.editable_include:
        return candidates

    known = {
        value.casefold()
        for item in all_items
        for value in (item.id, item.name, item.ref)
        if value
    }
    if manifest.get("schemaVersion") == 3:
        indexed_results = [indexed for source in sources.values()
                           if source.ecosystem == group.ecosystem
                           and (not source_id or source.id == source_id)
                           and source.capabilities.get("managedInclude")
                           for indexed in index.suggest(group.ecosystem, group.kind, incomplete,
                               source_id=source.id, fingerprint=source.fingerprint)]
    else:
        indexed_results = index.suggest(group.ecosystem, group.kind, incomplete, source_id=source_id)
    for indexed in indexed_results:
        result = indexed.result
        identity = {value.casefold() for value in (result.name, result.ref) if value}
        if known & identity:
            continue
        details = ["registry index"]
        if result.version:
            details.append(f"v{result.version}")
        if result.ref:
            details.append(result.ref)
        if result.source_id:
            details.append(result.source_id)
        value = result.ref if (canonical or group.ecosystem == "nix") and result.ref else result.name
        if group.ecosystem == "url":
            value = result.source_id or result.name
        candidates.append((value, "; ".join(details)))
        known.update(identity)
    return candidates


def complete_remove_items(ctx, incomplete: str) -> list[tuple[str, str]]:
    return _complete_items(ctx, incomplete, action="rm")


def complete_why_groups(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete every evaluated policy group, including currently empty groups."""
    del ctx
    try:
        manifest = machine_manifest(completion=True)
        if not manifest_software_groups(manifest):
            return []
        groups = groups_for_manifest(manifest, include_empty=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    return [
        (group.key, group.label)
        for group in groups
        if group.key.startswith(incomplete)
    ]


def complete_why_items(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete exact explainable IDs across evaluated and machine-owned policy."""
    try:
        manifest = machine_manifest(completion=True)
        if not manifest_software_groups(manifest):
            return []
        groups = groups_for_manifest(manifest, include_empty=True)
        includes, exclusions = read_managed_policy(groups=groups)
    except (OSError, RuntimeError, SoftwarePolicyError, TypeError, ValueError):
        return []

    group_filter = ctx.params.get("group") if ctx is not None else None
    if group_filter is not None and not isinstance(group_filter, str):
        return []
    if isinstance(group_filter, str):
        try:
            selected_groups = (require_group(group_filter, groups),)
        except typer.BadParameter:
            return []
    else:
        selected_groups = groups

    manifest_groups = manifest_software_groups(manifest)
    needle = incomplete.casefold()
    matches: dict[str, dict[str, object]] = {}
    for group in selected_groups:
        selection = manifest_groups.get(group.key, {}).get("selection", {})
        entries = {
            item["id"]: item
            for key in ("include", "effective")
            for item in _software_entries(selection.get(key))
        }
        for managed in includes:
            if managed.group == group.key:
                entries.setdefault(managed.id, managed.to_selection())
        for item_id in _ordered_unique([
            *entries,
            *_string_list(selection.get("exclude")),
            *exclusions.get(group.key, []),
        ]):
            entry = entries.get(item_id, {"id": item_id, "name": item_id})
            name = str(entry.get("name") or item_id)
            ref = _optional_string(entry.get("ref"))
            if not any(
                value.casefold().startswith(needle)
                for value in (item_id, name, ref)
                if value
            ):
                continue
            match = matches.setdefault(item_id, {"groups": [], "details": []})
            match["groups"].append(group.key)
            for detail in (name if name != item_id else None, ref):
                if detail and detail not in match["details"]:
                    match["details"].append(detail)

    candidates = []
    for item_id, match in matches.items():
        group_help = ", ".join(match["groups"])
        details = "; ".join(match["details"])
        candidates.append((item_id, f"{group_help}; {details}" if details else group_help))
    return candidates


def complete_search_sources(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete canonical providers, including comma-separated source lists."""
    from envy.search.providers import available_providers

    prefix, separator, needle = incomplete.rpartition(",")
    selected = set(prefix.split(",")) if separator else set()
    previous = ctx.params.get("source", []) if ctx is not None else []
    if isinstance(previous, list):
        selected.update(
            source
            for value in previous
            if isinstance(value, str)
            for source in value.split(",")
        )
    descriptions = {
        "nix": "Nixpkgs",
        "homebrew": "Homebrew formulae and casks",
        "native": "Detected Linux package manager",
        "npm": "npm registry",
        "pypi": "PyPI exact lookup",
        "cargo": "crates.io",
        "go": "pkg.go.dev",
    }
    try:
        sources = sorted(available_providers())
    except OSError:
        return []
    return [
        (
            f"{prefix},{source}" if separator else source,
            descriptions.get(source, source),
        )
        for source in sources
        if source not in selected and source.startswith(needle)
    ]


def _resolve_existing_item(
    items: list[SoftwareItem],
    value: str,
) -> SoftwareItem | None:
    """Prefer the stable ID before falling back to reference or display name."""
    exact_ids = [item for item in items if item.id == value]
    if len(exact_ids) == 1:
        return exact_ids[0]
    if len(exact_ids) > 1:
        raise SoftwarePolicyError(f"stable software ID is duplicated: {value}")
    exact_refs = [item for item in items if item.ref == value]
    if len(exact_refs) == 1:
        return exact_refs[0]
    exact_names = [item for item in items if item.name == value]
    if len(exact_refs) + len(exact_names) == 1:
        return (exact_refs + exact_names)[0]
    if exact_refs or exact_names:
        choices = ", ".join(sorted({item.id for item in exact_refs + exact_names}))
        raise SoftwarePolicyError(
            f"software name or reference is ambiguous: {value}; use stable ID: {choices}"
        )
    return None


def _resolve_managed_include(
    includes: list[ManagedInclude],
    group_key: str,
    value: str,
) -> ManagedInclude | None:
    candidates = [item for item in includes if item.group == group_key]
    for attribute in ("id", "ref", "name"):
        matches = [item for item in candidates if getattr(item, attribute) == value]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            choices = ", ".join(sorted({item.id for item in matches}))
            raise SoftwarePolicyError(
                f"managed software {attribute} is ambiguous: {value}; use stable ID: {choices}"
            )
    return None


def _managed_source_counts(
    manifest: dict[str, Any],
    group: SelectionGroup,
    includes: list[ManagedInclude],
    exclusions: dict[str, list[str]],
) -> tuple[Counter[str], Counter[str], Counter[str]]:
    selection = manifest_software_groups(manifest).get(group.key, {}).get("selection", {})
    include_counts = Counter(
        item["id"] for item in _software_entries(selection.get("include"))
    )
    exclude_counts = Counter(_string_list(selection.get("exclude")))
    managed_include = Counter(
        item.id for item in includes if item.group == group.key
    )
    managed_exclude = Counter(exclusions[group.key])
    external_include = include_counts.copy()
    external_include.subtract(managed_include)
    external_exclude = exclude_counts.copy()
    external_exclude.subtract(managed_exclude)
    return include_counts, +external_include, +external_exclude


def _nix_request(ref: str, manifest: dict[str, Any]) -> ManagedInclude:
    from envy.sources.adapters import resolve_nix
    from envy.sources.catalog import source_for
    from envy.sources.model import SourceError
    if ref.startswith("flake:") and "#" in ref:
        input_name, attr = ref[6:].split("#", 1)
        source_id = "nix:" + input_name
    elif ref.startswith("nix:"):
        source_id, input_name, attr = "nix:nixpkgs", "nixpkgs", ref[4:]
    else:
        raise SoftwarePolicyError("Nix include requires nix:<attr> or flake:<input>#<attr>")
    try:
        source = source_for(source_id, manifest)
        outcome = resolve_nix(source, manifest, ref)
    except SourceError as exc:
        raise SoftwarePolicyError(str(exc)) from exc
    if outcome.result is None:
        raise SoftwarePolicyError(outcome.error or "locked Nix package cannot be resolved")
    result = outcome.result
    attr = result.ref.split("#", 1)[1] if "#" in result.ref else result.ref[4:]
    return ManagedInclude("", result.name, result.name, ref=result.ref,
        parameters={"attrPath": attr.split("."), **({"input": input_name} if input_name != "nixpkgs" else {})},
        source_id=source.id)


def _include_for_spec(
    group: SelectionGroup,
    value: str,
    manifest: dict[str, Any],
    *,
    explicit_ref: str | None = None,
    resolved: SearchResult | None = None,
) -> ManagedInclude:
    ref = explicit_ref or (value if ":" in value else None)
    if group.ecosystem == "nix":
        ref = ref or f"nix:{value}"
        if not ref.startswith(("nix:", "flake:")):
            raise SoftwarePolicyError(f"{group.key} requires a nix: or flake: canonical reference")
        if resolved is not None and resolved.ecosystem == "nix" and resolved.kind == "package" and resolved.ref:
            canonical = resolved.ref
            source_input = canonical[6:].split("#", 1)[0] if canonical.startswith("flake:") else "nixpkgs"
            attr = canonical.split("#", 1)[1] if canonical.startswith("flake:") else canonical[4:]
            request = ManagedInclude("", resolved.name, resolved.name, ref=canonical,
                                     parameters={"attrPath": attr.split("."), **({"input": source_input} if source_input != "nixpkgs" else {})},
                                     source_id=resolved.source_id)
        else:
            request = _nix_request(ref, manifest)
        if explicit_ref is not None and value != request.id:
            raise SoftwarePolicyError(
                f"requested stable ID {value!r} does not match resolved Nix package {request.id!r}"
            )
        return ManagedInclude(
            group=group.key,
            id=request.id,
            name=request.name,
            version=request.version,
            ref=request.ref,
            parameters=request.parameters,
            source_id=request.source_id,
        )

    if group.ecosystem == "url":
        if resolved is None or resolved.source_id is None:
            raise SoftwarePolicyError("artifact must resolve from a verified artifact source")
        source = source_for(resolved.source_id, manifest)
        return ManagedInclude(group.key, source.parameters["packageName"], source.parameters["packageName"],
                              version=source.parameters.get("version"), ref=source.parameters["url"], parameters=source.parameters, source_id=source.id)

    expected_prefix: str
    if group.ecosystem == "homebrew":
        ref_kind = "tap" if group.kind == "repository" else group.kind
        expected_prefix = f"homebrew:{ref_kind}/"
        ref = ref or f"{expected_prefix}{value}"
        if not ref.startswith(expected_prefix):
            raise SoftwarePolicyError(
                f"{group.key} requires a {expected_prefix}<name> reference"
            )
        name = ref.removeprefix(expected_prefix)
        if explicit_ref is not None and value != name:
            raise SoftwarePolicyError(
                f"{group.key} uses the Homebrew name as its stable ID: {name}"
            )
    elif group.ecosystem in {"npm", "pypi", "native"}:
        expected_prefix = f"{group.ecosystem}:"
        ref = ref or f"{expected_prefix}{value}"
        if not ref.startswith(expected_prefix):
            raise SoftwarePolicyError(
                f"{group.key} requires a {expected_prefix}<name> reference"
            )
        name = ref.removeprefix(expected_prefix)
    else:
        raise SoftwarePolicyError(
            f"software group does not support registry-backed includes: {group.key}"
        )
    if resolved is not None:
        if resolved.ecosystem != group.ecosystem or resolved.kind != group.kind:
            raise SoftwarePolicyError(
                f"resolved registry object does not match {group.key}: "
                f"{resolved.ecosystem}.{resolved.kind}"
            )
        names_match = (
            re.sub(r"[-_.]+", "-", resolved.name).casefold()
            == re.sub(r"[-_.]+", "-", name).casefold()
            if group.ecosystem == "pypi"
            else resolved.name == name
        )
        refs_match = (
            names_match and resolved.ref is not None and resolved.ref.startswith("pypi:")
            if group.ecosystem == "pypi"
            else resolved.ref == ref
        )
        if not refs_match:
            raise SoftwarePolicyError(
                f"resolved registry reference does not match request: {resolved.ref or '<none>'}"
            )
        if not names_match:
            raise SoftwarePolicyError(
                f"resolved registry name does not match request: {resolved.name}"
            )
        name = resolved.name
        ref = resolved.ref
    _validate_name(name)
    item_id = value if explicit_ref is not None else name
    _validate_name(item_id)
    return ManagedInclude(
        group=group.key,
        id=item_id,
        name=name,
        version=None,
        ref=resolved.ref if resolved is not None else ref,
        parameters=(
            {"registry": source_for(resolved.source_id, manifest).parameters["registry"]}
            if resolved and resolved.source_id and group.ecosystem == "npm" else
            {"index": source_for(resolved.source_id, manifest).parameters["index"]}
            if resolved and resolved.source_id and group.ecosystem == "pypi" else {}
        ),
        source_id=resolved.source_id if resolved else None,
    )


def build_desired_plan(
    *,
    action: str,
    group: SelectionGroup,
    item_value: str,
    manifest: dict[str, Any],
    includes: list[ManagedInclude],
    exclusions: dict[str, list[str]],
    clean: bool,
    explicit_ref: str | None = None,
    resolved: SearchResult | None = None,
) -> SoftwarePlan:
    if action not in {"add", "rm"}:
        raise ValueError(f"unknown desired-state action: {action}")
    active_groups = groups_for_manifest(manifest, include_empty=True)
    normalized_includes = normalize_managed_includes(includes, active_groups)
    items = build_software_items(
        manifest, exclusions, groups=(group,)
    )[group.key]
    managed_match = _resolve_managed_include(
        normalized_includes, group.key, item_value
    )
    existing = _resolve_existing_item(
        items, managed_match.id if managed_match is not None else item_value
    )
    request: ManagedInclude | None = None
    if managed_match is not None:
        item_id = managed_match.id
        request = managed_match
    elif existing is not None:
        item_id = existing.id
    elif action == "add":
        if not group.editable_include:
            raise SoftwarePolicyError(
                f"software group does not support managed includes: {group.key}"
            )
        request = _include_for_spec(
            group, item_value, manifest, explicit_ref=explicit_ref, resolved=resolved,
        )
        item_id = request.id
    else:
        item_id = item_value
        _validate_name(item_id)

    include_counts, external_include, external_exclude = _managed_source_counts(
        manifest, group, normalized_includes, exclusions
    )
    includes_before = tuple(normalized_includes)
    includes_after = list(normalized_includes)
    exclusions_before = tuple(exclusions[group.key])
    exclusions_after = list(exclusions_before)
    managed_for_item = [
        item for item in includes_after
        if item.group == group.key and item.id == item_id
    ]

    blocked = None
    if action == "add" and request is not None:
        for candidate in _software_entries(manifest_software_groups(manifest).get(group.key, {}).get("selection", {}).get("include")):
            if candidate["id"] == item_id and candidate.get("ref") and candidate["ref"] != request.ref:
                raise SoftwarePolicyError("stable software ID is already bound to a different reference")
    if action == "add":
        if external_exclude[item_id] > 0:
            blocked = "an external or hand-written exclusion still masks this stable ID"
        else:
            if include_counts[item_id] == 0:
                if managed_for_item:
                    blocked = "the existing managed contribution is missing from evaluated include"
                else:
                    request = request or _include_for_spec(
                        group,
                        existing.ref if existing and existing.ref else item_value,
                        manifest,
                        explicit_ref=explicit_ref,
                        resolved=resolved,
                    )
                    includes_after.append(request)
                    managed_for_item = [request]
            if clean and external_include[item_id] > 0 and managed_for_item:
                includes_after = [
                    item for item in includes_after
                    if not (item.group == group.key and item.id == item_id)
                ]
            exclusions_after = [item for item in exclusions_after if item != item_id]
    else:
        if clean and managed_for_item:
            includes_after = [
                item for item in includes_after
                if not (item.group == group.key and item.id == item_id)
            ]
        if not clean:
            if item_id not in exclusions_after:
                exclusions_after.append(item_id)
        else:
            needs_managed_exclude = (
                external_include[item_id] > 0 and external_exclude[item_id] == 0
            )
            if needs_managed_exclude and item_id not in exclusions_after:
                exclusions_after.append(item_id)
            elif not needs_managed_exclude:
                exclusions_after = [item for item in exclusions_after if item != item_id]

    prerequisites = []
    source_id = request.source_id if request else (resolved.source_id if resolved else None)
    if action == "add" and source_id and group.kind != "repository":
        from envy.sources.policy import prerequisite
        dependency = prerequisite(source_id, manifest, includes_after, exclusions)
        if dependency:
            includes_after.append(dependency)
            prerequisites.append({"sourceId": source_id, "group": dependency.group, "item": dependency.id, "action": "select"})
    includes_after = normalize_managed_includes(
        includes_after, active_groups
    )
    expected_included = external_include[item_id] > 0 or any(
        item.group == group.key and item.id == item_id for item in includes_after
    )
    expected_excluded = external_exclude[item_id] > 0 or item_id in exclusions_after
    expected_effective = expected_included and not expected_excluded
    if action == "add" and not blocked and not expected_effective:
        blocked = "the proposed managed changes cannot make this item effective"
    if action == "rm" and expected_effective:
        blocked = "the proposed managed changes cannot remove this item from effective"
    return SoftwarePlan(
        action=action,
        group=group,
        item_id=item_id,
        includes_before=includes_before,
        includes_after=tuple(includes_after),
        exclusions_before=exclusions_before,
        exclusions_after=tuple(exclusions_after),
        expected_included=expected_included,
        expected_excluded=expected_excluded,
        expected_effective=expected_effective,
        clean=clean,
        blocked=blocked,
        prerequisites=tuple(prerequisites),
        source_id=source_id,
    )


def _render_plan(plan: SoftwarePlan) -> None:
    table = Table(title=f"Software plan - {plan.action} {plan.group.key}/{plan.item_id}")
    table.add_column("Selection")
    table.add_column("Change")
    table.add_column("Stable ID")
    table.add_column("Ownership")
    rows = [
        *(('include', '+', item, 'envy managed') for item in plan.include_added),
        *(('include', '-', item, 'envy managed') for item in plan.include_removed),
        *(('exclude', '+', item, 'envy managed') for item in plan.exclude_added),
        *(('exclude', '-', item, 'envy managed') for item in plan.exclude_removed),
    ]
    if rows:
        for row in rows:
            table.add_row(*row)
    else:
        table.add_row("-", "=", plan.item_id, "no managed change")
    log.console.print(table)
    log.info(
        "software",
        "expected policy",
        included=str(plan.expected_included).lower(),
        excluded=str(plan.expected_excluded).lower(),
        effective=str(plan.expected_effective).lower(),
        clean=str(plan.clean).lower(),
    )
    if plan.blocked:
        log.error("software", "plan is blocked", reason=plan.blocked)


def _emit_mutation_result(
    plan: SoftwarePlan,
    *,
    result: str,
    machine: str | None = None,
    message: str | None = None,
    command: str | None = None,
) -> None:
    data: dict[str, Any] = {
        "result": result,
        "plan": plan.to_dict(),
    }
    if machine is not None:
        data["machine"] = machine
    if message is not None:
        data["message"] = message
    emit(command or f"software.{plan.action}", data=data)


def _resolve_new_include(
    *,
    action: str,
    group: SelectionGroup,
    item_value: str,
    explicit_ref: str | None,
    manifest: dict[str, Any],
    includes: list[ManagedInclude],
    exclusions: dict[str, list[str]],
    offline: bool,
    refresh: bool,
    quiet: bool = False,
    source_id: str | None = None,
) -> SearchResult | None:
    """Verify a genuinely new registry include before the planner can write it."""
    if action != "add":
        return None
    active_groups = groups_for_manifest(manifest, include_empty=True)
    normalized_includes = normalize_managed_includes(includes, active_groups)
    managed = _resolve_managed_include(normalized_includes, group.key, item_value)
    items = build_software_items(
        manifest, exclusions, groups=(group,)
    )[group.key]
    existing = _resolve_existing_item(
        items, managed.id if managed is not None else item_value
    )
    if managed is not None or existing is not None:
        declared_source = managed.source_id if managed else existing.source_id
        if source_id and declared_source and source_id != declared_source:
            raise SoftwarePolicyError("existing software selection is bound to another source")
        return None
    if not group.editable_include:
        return None
    if group.ecosystem not in {"nix", "homebrew", "npm", "pypi", "native", "url"}:
        raise SoftwarePolicyError(
            f"software group has no exact registry resolver: {group.key}"
        )

    requested = explicit_ref or item_value
    from envy.sources.catalog import catalog, legacy_source_id
    from envy.sources.model import SourceError
    sources = catalog(manifest)
    source_id = source_id or (requested if group.kind == "repository" and group.ecosystem == "native" and requested.startswith("native:") else None)
    if source_id is None and group.ecosystem in ("npm", "pypi"):
        candidates = [s for s in sources.values() if s.ecosystem == group.ecosystem and s.ready]
        if len(candidates) > 1:
            raise SoftwarePolicyError("multiple registry sources are available; select --source-id", "ambiguous-source")
    if group.ecosystem == "url":
        source_id = source_id or requested
    else:
        source_id = source_id or legacy_source_id(group.ecosystem, requested, group.kind)
    bound_source = sources.get(source_id)
    if source_id and bound_source is None:
        raise SoftwarePolicyError(f"unknown source: {source_id}")
    if bound_source and not bound_source.capabilities.get("configure" if group.kind == "repository" and group.ecosystem == "native" else "exactResolve"):
        from envy.sources.model import SourceError
        raise SourceError("source does not support exact package resolution", "unsupported")
    if bound_source and bound_source.ecosystem != group.ecosystem:
        raise SoftwarePolicyError("source ecosystem does not match software group")
    from envy.sources.model import fingerprint
    scope = {"source_id": source_id, "fingerprint": bound_source.fingerprint if bound_source else (
        fingerprint({s.id: s.fingerprint for s in sources.values() if s.ecosystem == group.ecosystem})
        if manifest.get("schemaVersion") == 3 else ""
    )}
    index = RegistryIndex()
    cached = None if refresh else index.lookup(
        group.ecosystem, group.kind, requested, allow_stale=offline, **scope,
    )
    if bound_source is None and group.ecosystem == "homebrew" and not refresh and manifest.get("schemaVersion") == 3:
        candidates = [index.lookup(group.ecosystem, group.kind, requested, allow_stale=offline,
                                  source_id=s.id, fingerprint=s.fingerprint)
                      for s in sources.values() if s.type == "tap" and (s.ready or s.selected)]
        candidates = [c for c in candidates if c is not None]
        if len(candidates) > 1:
            raise SoftwarePolicyError("multiple cached taps contain this name; select --source-id", "ambiguous-source")
        cached = candidates[0] if candidates else None
    if cached is not None:
        state = "stale index" if cached.stale else "registry index"
        if not quiet:
            log.info(
                "software", "resolved registry identity",
                source=state, ref=cached.result.ref or "",
                age=f"{cached.age_seconds}s",
            )
        return cached.result
    if offline:
        raise SoftwarePolicyError(
            f"offline registry index miss for {group.key}: {requested}", "offline-index-miss"
        )
    if not refresh and index.recently_missing(group.ecosystem, group.kind, requested, **scope):
        raise SoftwarePolicyError(
            f"software was not found during a recent exact lookup: {requested}", "package-not-found"
        )

    if not quiet:
        log.info(
            "software", "resolving exact registry identity",
            provider=group.ecosystem, kind=group.kind, item=requested,
        )
    if bound_source:
        from envy.sources.adapters import resolve
        outcome = resolve(bound_source, manifest, group.kind, requested)
    else:
        outcome = resolve_exact(group.ecosystem, group.kind, requested)
    if outcome.status == "not_found":
        try:
            index.put_miss(group.ecosystem, group.kind, requested, **scope)
        except (OSError, sqlite3.Error):
            pass
        detail = f": {outcome.error}" if outcome.error else ""
        raise SoftwarePolicyError(
            f"software does not exist in {group.ecosystem}.{group.kind}: {requested}{detail}", "package-not-found"
        )
    if outcome.status == "unavailable" or outcome.result is None:
        raise SoftwarePolicyError(
            f"cannot verify software identity with {group.ecosystem}: "
            f"{outcome.error or 'registry unavailable'}", "source-unavailable"
        )
    if outcome.result.source_id in sources:
        from envy.sources.adapters import bind
        bind(outcome.result, sources[outcome.result.source_id])
    elif manifest.get("schemaVersion") == 3:
        raise SoftwarePolicyError("provider did not identify a registered software source", "unverified-source")
    try:
        index.put_results([outcome.result])
    except (OSError, sqlite3.Error):
        pass
    if not quiet:
        log.info(
            "software", "resolved registry identity",
            source="registry", ref=outcome.result.ref or "",
            version=outcome.result.version or "unknown",
        )
    return outcome.result


def _apply_desired_state(
    group_value: str,
    item_value: str,
    *,
    action: str,
    clean: bool,
    yes: bool,
    dry_run: bool,
    explicit_ref: str | None,
    offline: bool = False,
    refresh: bool = False,
    json_output: bool = False,
    source_id: str | None = None,
    command_prefix: str = "software",
) -> None:
    command = f"{command_prefix}.{action}"
    try:
        manifest = _manifest_or_exit()
        groups = groups_for_manifest(manifest, include_empty=True)
        group = require_group(group_value, groups)
        target = machine_file()
        original_source = target.read_text()
        digest = source_digest(original_source)
        includes, exclusions = _parse_managed_policy_source(original_source, groups)
    except (OSError, SoftwarePolicyError, typer.BadParameter) as exc:
        if json_output:
            emit_error(command, str(exc), code="invalid-policy")
        else:
            log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    try:
        resolved = _resolve_new_include(
            action=action,
            group=group,
            item_value=item_value,
            explicit_ref=explicit_ref,
            manifest=manifest,
            includes=includes,
            exclusions=exclusions,
            offline=offline,
            refresh=refresh,
            quiet=json_output,
            source_id=source_id,
        )
        if source_id and group.ecosystem == "nix":
            wanted = "nix:" + ((explicit_ref or item_value)[6:].split("#", 1)[0]) if (explicit_ref or item_value).startswith("flake:") else "nix:nixpkgs"
            if wanted != source_id:
                raise SoftwarePolicyError("Nix reference conflicts with --source-id")
        plan = build_desired_plan(
            action=action,
            group=group,
            item_value=item_value,
            manifest=manifest,
            includes=includes,
            exclusions=exclusions,
            clean=clean,
            explicit_ref=explicit_ref,
            resolved=resolved,
        )
        if source_id:
            plan = replace(plan, source_id=source_id)
    except (SoftwarePolicyError, ValueError) as exc:
        if json_output:
            emit_error(command, str(exc), code=getattr(exc, "code", "invalid-policy"))
        else:
            log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    if not json_output:
        _render_plan(plan)
    if plan.blocked:
        if json_output:
            emit(command, ok=False, data={"result": "blocked", "plan": plan.to_dict()},
                 error={"code": "blocked", "message": plan.blocked})
        raise typer.Exit(code=1)
    if dry_run:
        if json_output:
            _emit_mutation_result(plan, result="dry-run", command=command)
        else:
            log.info("software", "dry run; no files changed")
        return
    if not plan.changed:
        if json_output:
            _emit_mutation_result(plan, result="already-satisfied", command=command)
        else:
            log.ok("software", f"already satisfies {action} intent", group=group.key, item=plan.item_id)
        return
    if json_output and not yes:
        emit(command, ok=False, data={"result": "confirmation-required", "plan": plan.to_dict()},
             error={"code": "confirmation-required", "message": "pass --yes to apply JSON mutation"})
        raise typer.Exit(code=2)
    if not yes and not typer.confirm("Apply this software plan?", default=None):
        if json_output:
            _emit_mutation_result(plan, result="cancelled")
        else:
            log.info("software", "plan cancelled; no files changed")
        return
    updated_exclusions = {key: list(values) for key, values in exclusions.items()}
    updated_exclusions[group.key] = list(plan.exclusions_after)
    try:
        evaluated = write_and_validate_software_policy(
            list(plan.includes_after),
            updated_exclusions,
            group_key=group.key,
            item_id=plan.item_id,
            expected_effective=plan.expected_effective,
            expected_digest=digest,
            groups=groups,
        )
    except SoftwarePolicyError as exc:
        if json_output:
            emit_error(command, str(exc), code="apply-failed")
        else:
            log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    machine = evaluated.get("id", current_machine_id())
    if json_output:
        _emit_mutation_result(plan, result="applied", machine=machine, command=command)
    else:
        log.ok(
            "software",
            "intent applied",
            action=action,
            group=group.key,
            item=plan.item_id,
            effective=str(plan.expected_effective).lower(),
            machine=machine,
        )
    offer_mutation_commit(
        [target],
        f"chore(host): {action} {plan.item_id} on {evaluated.get('id') or current_machine_id()}",
        quiet=json_output,
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


app = typer.Typer(
    name="software",
    help="Inspect, search, and manage software for the selected machine",
    invoke_without_command=True,
    no_args_is_help=False,
)

cache_app = typer.Typer(
    name="cache",
    help="Inspect or clear the exact registry identity index",
    no_args_is_help=True,
)


@app.callback()
def cmd_software(ctx: typer.Context):
    """List evaluated software policy when no subcommand is supplied."""
    if ctx.invoked_subcommand is None:
        _print_policy(details=False, json_output=False)


@app.command(name="list")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_list(
    details: bool = typer.Option(False, "--details", "-d", help="Include empty groups and package metadata"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List checkbox state for all software groups."""
    _print_policy(details=details, json_output=json_output)


@app.command(name="status")
@app.command(name="st", rich_help_panel="Aliases")
def cmd_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Summarize the selected machine's evaluated software policy."""
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    managed = read_managed_exclusions(groups=groups)
    items_by_group = build_software_items(manifest, managed, groups=groups)
    included = sum(item.included for items in items_by_group.values() for item in items)
    effective = sum(item.checked for items in items_by_group.values() for item in items)
    excluded = sum(not item.checked for items in items_by_group.values() for item in items)
    payload = {
        "schemaVersion": 1,
        "machine": manifest.get("id", "current"),
        "groups": len(groups),
        "included": included,
        "effective": effective,
        "excluded": excluded,
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        log.info(
            "software", "policy status", machine=payload["machine"],
            groups=len(groups), included=included, effective=effective, excluded=excluded,
        )


@app.command(name="audit")
def cmd_audit(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when warnings are found"),
):
    """Find stale, redundant, or ambiguous machine software policy."""
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    source = machine_file().read_text()
    includes, exclusions = _parse_managed_policy_source(source, groups)
    findings = audit_policy(manifest, includes, exclusions, groups)
    payload = {
        "schemaVersion": 1,
        "machine": manifest.get("id", current_machine_id()),
        "findings": [finding.to_dict() for finding in findings],
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
    elif findings:
        table = Table(title=f"Software audit - {payload['machine']}")
        table.add_column("Severity")
        table.add_column("Code")
        table.add_column("Group")
        table.add_column("Item")
        table.add_column("Finding")
        for finding in findings:
            style = "yellow" if finding.severity == "warn" else "cyan"
            table.add_row(
                f"[{style}]{finding.severity.upper()}[/{style}]",
                finding.code, finding.group, finding.item_id, finding.message,
            )
        log.console.print(table)
    else:
        log.ok("software", "policy audit found no issues")
    warnings = sum(finding.severity == "warn" for finding in findings)
    if strict and warnings:
        raise typer.Exit(code=1)


@app.command(name="why")
def cmd_why(
    item: str = typer.Argument(
        ..., help="Stable ID, exact name, or canonical reference",
        autocompletion=complete_why_items,
    ),
    group: str | None = typer.Option(
        None, "--group", "-g", help="Restrict to one canonical group",
        autocompletion=complete_why_groups,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Explain why an item is included, excluded, or effective."""
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    source = machine_file().read_text()
    includes, exclusions = _parse_managed_policy_source(source, groups)
    rows = explain_policy_item(
        manifest, includes, exclusions, groups, item, group_filter=group,
    )
    payload = {
        "schemaVersion": 1,
        "machine": manifest.get("id", current_machine_id()),
        "query": item,
        "matches": rows,
    }
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if not rows:
        log.error("software", "no exact policy item match", item=item)
        raise typer.Exit(code=1)
    table = Table(title=f"Why software - {item}")
    for column in (
        "Group", "Item", "Reference", "Included", "Excluded", "Effective", "Ownership",
    ):
        table.add_column(column)
    for row in rows:
        ownership = ", ".join(name for name, enabled in (
            ("machine include", row["machineInclude"]),
            ("shared include", row["externalInclude"]),
            ("machine exclude", row["machineExclude"]),
            ("external exclude", row["externalExclude"]),
        ) if enabled) or "evaluated only"
        table.add_row(
            str(row["group"]), str(row["item"]), str(row["ref"] or ""),
            "yes" if row["included"] else "no",
            "yes" if row["excluded"] else "no",
            "yes" if row["effective"] else "no",
            ownership,
        )
    log.console.print(table)


@app.command(name="disable")
@app.command(name="dis", rich_help_panel="Aliases")
def cmd_disable(
    group: str = typer.Argument(
        ..., help="Canonical software group ID",
        autocompletion=complete_disable_groups,
    ),
    item_id: str = typer.Argument(
        ..., help="Stable item ID to exclude",
        autocompletion=complete_disable_items,
    ),
):
    """Exclude one contributed item on the selected machine."""
    _change_one(group, item_id, excluded=True)


@app.command(name="enable")
@app.command(name="en", rich_help_panel="Aliases")
def cmd_enable(
    group: str = typer.Argument(
        ..., help="Canonical software group ID",
        autocompletion=complete_enable_groups,
    ),
    item_id: str = typer.Argument(
        ..., help="Stable managed exclusion ID to restore",
        autocompletion=complete_enable_items,
    ),
):
    """Remove one machine-local exclusion."""
    _change_one(group, item_id, excluded=False)


@app.command(name="add")
def cmd_add(
    group_or_item: str = typer.Argument(
        ..., help="Software item, or canonical group ID when followed by ITEM",
        autocompletion=complete_add_targets,
    ),
    item: str | None = typer.Argument(
        None, help="Stable ID, package name, or canonical reference in legacy GROUP ITEM form",
        autocompletion=complete_add_items,
    ),
    group: str | None = typer.Option(
        None, "--group", "-g", help="Select a canonical group without an interactive chooser",
        autocompletion=complete_add_groups,
    ),
    source_id: str | None = typer.Option(None, "--source-id", help="Require a concrete software source",
                                       autocompletion=complete_add_source_ids),
    clean: bool = typer.Option(False, "--clean", help="Remove redundant envY-managed state for this item"),
    ref: str | None = typer.Option(None, "--ref", help="Canonical registry reference when item is a custom stable ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the displayed plan without confirmation"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Display the plan without writing"),
    offline: bool = typer.Option(
        False, "--offline", help="Require a cached identity; stale positive entries are allowed",
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore registry identity cache and resolve again",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a stable plan/result document for frontends",
    ),
):
    """Ensure one software item is effective; infer or ask for its software type."""
    if item is not None:
        if group is not None:
            raise typer.BadParameter("use either legacy GROUP ITEM arguments or --group, not both")
        selected_group = group_or_item
        selected_item = item
    else:
        selected_item = group_or_item
        selected_group = group or _choose_cli_group(
            selected_item, action="add", json_output=json_output,
        )
    _apply_desired_state(
        selected_group, selected_item, action="add", clean=clean, yes=yes,
        dry_run=dry_run, explicit_ref=ref, offline=offline, refresh=refresh,
        json_output=json_output, source_id=source_id,
    )


@app.command(name="remove")
@app.command(name="rm", rich_help_panel="Aliases")
def cmd_remove(
    group_or_item: str = typer.Argument(
        ..., help="Software item, or canonical group ID when followed by ITEM",
        autocompletion=complete_remove_targets,
    ),
    item: str | None = typer.Argument(
        None, help="Stable item ID, name, or reference in legacy GROUP ITEM form",
        autocompletion=complete_remove_items,
    ),
    group: str | None = typer.Option(
        None, "--group", "-g", help="Select a canonical group without an interactive chooser",
        autocompletion=complete_remove_groups,
    ),
    clean: bool = typer.Option(False, "--clean", help="Remove redundant envY-managed state for this item"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the displayed plan without confirmation"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Display the plan without writing"),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a stable plan/result document for frontends",
    ),
):
    """Ensure one software item is not effective; infer or ask for its software type."""
    if item is not None:
        if group is not None:
            raise typer.BadParameter("use either legacy GROUP ITEM arguments or --group, not both")
        selected_group = group_or_item
        selected_item = item
    else:
        selected_item = group_or_item
        selected_group = group or _choose_cli_group(
            selected_item, action="rm", json_output=json_output,
        )
    _apply_desired_state(
        selected_group, selected_item, action="rm", clean=clean, yes=yes,
        dry_run=dry_run, explicit_ref=None,
        json_output=json_output,
    )


@app.command(name="search")
@app.command(name="se", rich_help_panel="Aliases")
def cmd_search(
    query: str = typer.Argument(..., help="Package or tool name to search for"),
    source: list[str] | None = typer.Option(
        None, "--source", "-s", help="Restrict search providers",
        autocompletion=complete_search_sources,
    ),
    source_id: list[str] | None = typer.Option(None, "--source-id", help="Filter concrete source identities",
                                             autocompletion=complete_search_source_ids),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=100),
    exact: bool = typer.Option(False, "--exact", help="Show exact name matches only"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved registry cache"),
):
    """Search all available software registries."""
    from envy.search import search_and_render

    search_and_render(
        query, sources=source, limit=limit, exact=exact,
        json_output=json_output, refresh=refresh, source_ids=source_id,
    )


@cache_app.command(name="status")
def cmd_cache_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show exact registry index freshness and provider counts."""
    stats = RegistryIndex().stats()
    payload = {"schemaVersion": 1, **stats}
    if json_output:
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="Software registry index")
    table.add_column("Field")
    table.add_column("Value")
    for key in ("path", "entries", "fresh", "stale", "misses"):
        table.add_row(key, str(stats[key]))
    providers = stats.get("providers")
    if isinstance(providers, dict):
        for provider, count in sorted(providers.items()):
            table.add_row(f"provider.{provider}", str(count))
    log.console.print(table)


@cache_app.command(name="clean")
def cmd_cache_clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm exact registry index deletion"),
):
    """Delete the exact registry identity index; future search/add recreates it."""
    index = RegistryIndex()
    if not index.path.exists():
        log.info("software", "registry index is already empty", path=str(index.path))
        return
    if not yes and not typer.confirm(f"Delete registry index {index.path}?", default=None):
        log.info("software", "registry index cleanup cancelled")
        return
    removed = index.clear()
    if removed:
        log.ok("software", "registry index removed", path=str(index.path))
        log.hint("It will be rebuilt by software search and exact add lookups.")


app.add_typer(cache_app, name="cache")


def _manifest_or_exit() -> dict[str, Any]:
    manifest = machine_manifest()
    if manifest is None or not manifest_software_groups(manifest):
        log.error("software", "cannot evaluate the selected machine software manifest")
        raise typer.Exit(code=1)
    return manifest


def _change_one(group_value: str, item_value: str, *, excluded: bool) -> None:
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=True)
    group = require_group(group_value, groups)
    target = machine_file()
    original_source = target.read_text() if target.exists() else None
    values = read_managed_exclusions(groups=groups)
    if (
        original_source is not None
        and source_digest(target.read_text()) != source_digest(original_source)
    ):
        log.error("software", "machine configuration changed while reading policy")
        raise typer.Exit(code=1)
    items = build_software_items(manifest, values, groups=groups)[group.key]
    try:
        item = _resolve_existing_item(items, item_value)
    except SoftwarePolicyError as exc:
        log.error("software", str(exc), group=group.key, item=item_value)
        raise typer.Exit(code=1)
    if item is None:
        log.error("software", "item is not uniquely identified", group=group.key, item=item_value)
        raise typer.Exit(code=1)
    if excluded:
        if not item.included:
            log.error("software", "item is not contributed by the selected machine", group=group.key, item=item.id)
            raise typer.Exit(code=1)
        if item.locked:
            log.error("software", "item is already excluded outside the managed machine block", group=group.key, item=item.id)
            raise typer.Exit(code=1)
        if item.managed:
            log.ok("software", "already disabled", group=group.key, item=item.id)
            return
    elif item.id not in values[group.key]:
        if item.included and item.checked and not item.locked:
            log.ok("software", "already enabled", group=group.key, item=item.id)
            return
        log.error("software", "item is not excluded by the managed machine block", group=group.key, item=item.id)
        log.hint("External or hand-written exclusions must be edited at their source.")
        raise typer.Exit(code=1)

    set_excluded(values, group.key, item.id, excluded, groups)
    try:
        evaluated = write_and_validate_exclusions(
            values,
            expected_digest=(
                source_digest(original_source) if original_source is not None else None
            ),
            groups=groups,
        )
    except SoftwarePolicyError as exc:
        log.error("software", str(exc))
        raise typer.Exit(code=1) from exc
    included_after, effective_after, excluded_after = _evaluated_item_state(
        evaluated, group.key, item.id
    )
    machine = evaluated.get("id", "current")
    if excluded:
        if included_after and excluded_after and not effective_after:
            log.ok("software", "disabled", group=group.key, item=item.id, machine=machine)
            commit_action = f"disable {item.id}"
        else:
            log.warn(
                "software", "managed exclusion saved, but evaluated state is unexpected",
                group=group.key, item=item.id, machine=machine,
            )
            commit_action = f"exclude {item.id}"
    elif not included_after and excluded_after:
        log.warn(
            "software",
            "managed stale exclusion removed; item remains excluded by another policy",
            group=group.key, item=item.id, machine=machine,
        )
        log.hint("The item is not contributed, and an external stale exclusion still exists.")
        commit_action = f"remove managed stale exclusion for {item.id}"
    elif not included_after:
        log.ok(
            "software", "stale exclusion removed",
            group=group.key, item=item.id, machine=machine,
        )
        log.hint("The item is not contributed by any active module, so no software was enabled.")
        commit_action = f"remove stale exclusion for {item.id}"
    elif effective_after:
        log.ok("software", "enabled", group=group.key, item=item.id, machine=machine)
        commit_action = f"enable {item.id}"
    elif excluded_after:
        log.warn(
            "software", "managed exclusion removed; item remains excluded by another policy",
            group=group.key, item=item.id, machine=machine,
        )
        log.hint("Edit the hand-written or imported exclusion at its source to enable the item.")
        commit_action = f"remove managed exclusion for {item.id}"
    else:
        log.warn(
            "software", "managed exclusion removed, but item is still not effective",
            group=group.key, item=item.id, machine=machine,
        )
        commit_action = f"remove managed exclusion for {item.id}"
    target_machine = evaluated.get("id") or current_machine_id()
    offer_mutation_commit(
        [machine_file()],
        f"chore(host): {commit_action} on {target_machine}",
    )


def _print_policy(*, details: bool, json_output: bool = False) -> None:
    manifest = _manifest_or_exit()
    groups = groups_for_manifest(manifest, include_empty=details)
    managed = read_managed_exclusions(groups=groups)
    items_by_group = build_software_items(manifest, managed, groups=groups)
    if json_output:
        payload = {
            "schemaVersion": 1,
            "machine": manifest.get("id", current_machine_id()),
            "platform": manifest.get("platform"),
            "groups": [{
                "id": group.key,
                "label": group.label,
                "ecosystem": group.ecosystem,
                "scope": group.scope,
                "kind": group.kind,
                "items": [{
                    "id": item.id,
                    "name": item.name,
                    "version": item.version,
                    "ref": item.ref,
                    "sourceId": item.source_id,
                    "resolvedVersion": item.resolved_version,
                    "versionConstraintApplied": not (item.version and group.ecosystem == "native"),
                    "included": item.included,
                    "effective": item.checked,
                    "machineExclude": item.managed,
                    "externalExclude": item.locked,
                    "stale": item.stale,
                } for item in items_by_group[group.key]],
            } for group in groups],
        }
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title=f"Machine software - {manifest.get('id', current_machine_id())}")
    table.add_column("Group")
    table.add_column("State")
    table.add_column("Name")
    if details:
        table.add_column("Version")
        table.add_column("Reference")
    table.add_column("Source")
    for group in groups:
        items = items_by_group[group.key]
        if not items and details:
            row = [group.label, "-", "<empty>"]
            if details:
                row.extend(["", ""])
            table.add_row(*row, group.key)
            continue
        for item in items:
            state = Text("[x]" if item.checked else ("[-]" if item.locked else "[ ]"))
            source = "machine exclusion" if item.managed else ("external exclusion" if item.locked else "included")
            if item.stale:
                source = "stale exclusion"
            row = [group.label, state, item.name]
            if details:
                row.extend([item.version or item.resolved_version or "", item.ref or ""])
            table.add_row(*row, source)
    log.console.print(table)


from envy.sources.cli import app as source_app
app.add_typer(source_app, name="source")

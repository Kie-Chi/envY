"""Software selection groups, items, managed includes, and their normalization."""

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

import typer

from envy.evaluation import manifest_software_groups
from envy.sources.catalog import legacy_source_id
from envy.utils import platform_name


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


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


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

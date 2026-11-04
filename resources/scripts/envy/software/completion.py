"""Shell completion for software commands."""

import subprocess

import typer

from envy.evaluation import machine_manifest, manifest_software_groups
from envy.search.index import RegistryIndex
from envy.software.model import (
    SelectionGroup,
    SoftwareItem,
    SoftwarePolicyError,
    _optional_string,
    _ordered_unique,
    _software_entries,
    _string_list,
    build_software_items,
    groups_for_manifest,
    require_group,
)
from envy.software.policy import read_managed_exclusions, read_managed_policy
from envy.sources.catalog import legacy_source_id


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

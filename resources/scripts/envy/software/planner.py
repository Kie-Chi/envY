"""Resolve requested software into a desired managed policy plan."""

import re
import sqlite3
from typing import Any

from envy import log
from envy.evaluation import manifest_software_groups
from envy.search.index import RegistryIndex
from envy.search.model import SearchResult
from envy.search.providers import resolve_exact
from envy.software.model import (
    ManagedInclude,
    SelectionGroup,
    SoftwareItem,
    SoftwarePlan,
    SoftwarePolicyError,
    _managed_source_counts,
    _software_entries,
    _validate_name,
    build_software_items,
    groups_for_manifest,
    normalize_managed_includes,
)
from envy.sources.catalog import source_for


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

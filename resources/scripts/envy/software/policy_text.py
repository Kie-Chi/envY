"""Parse and render the ENVY MANAGED SOFTWARE block of a machine file."""

import json
import re
from typing import Any

from envy.software.model import (
    GROUPS,
    ManagedInclude,
    SelectionGroup,
    SoftwarePolicyError,
    _validate_name,
    _validate_text_value,
    empty_exclusions,
    normalize_exclusions,
    normalize_managed_includes,
)


MANAGED_START = "  # BEGIN ENVY MANAGED SOFTWARE"

MANAGED_END = "  # END ENVY MANAGED SOFTWARE"

LEGACY_MANAGED_START = "  # BEGIN ENVY MANAGED EXCLUSIONS"

LEGACY_MANAGED_END = "  # END ENVY MANAGED EXCLUSIONS"

MANAGED_INCLUDE_METADATA = "# envy: "


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

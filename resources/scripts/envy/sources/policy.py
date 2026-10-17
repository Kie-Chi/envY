"""Source prerequisites and strictly bounded machine source definitions."""
from __future__ import annotations

import json
import re

from envy.sources.catalog import source_for
from envy.sources.model import Source, SourceError

START = "  # BEGIN ENVY MANAGED SOURCES"
END = "  # END ENVY MANAGED SOURCES"
HOMEBREW_BUILTIN_TAPS = {"homebrew/core", "homebrew/cask"}


def prerequisite(source_id, manifest, includes, exclusions):
    from envy.software import ManagedInclude
    source = source_for(source_id, manifest)
    if manifest.get("platform") not in source.platforms:
        raise SourceError("source is incompatible with machine platform", "unsupported-platform")
    if source.type == "tap" and source.parameters.get("tap") in HOMEBREW_BUILTIN_TAPS:
        return None
    if source.type not in ("native", "tap") or source.parameters.get("system"):
        return None
    group = "native.system.repository" if source.type == "native" else "homebrew.system.repository"
    item_id = source_id.split(":", 1)[1]
    selection = manifest.get("software", {}).get("groups", {}).get(group, {}).get("selection", {})
    if item_id in selection.get("exclude", []) or item_id in exclusions.get(group, []):
        raise SourceError(f"required source is explicitly excluded: {source_id}", "source-excluded")
    if any(item.get("id") == item_id for item in selection.get("effective", [])):
        return None
    if any(item.group == group and item.id == item_id for item in includes):
        return None
    if source.type == "native":
        manager = source.context.get("manager")
        if not source.capabilities.get("configure"):
            raise SourceError(f"source cannot be configured by {manager}", "unsupported")
        distros = source.parameters.get("distros", [])
        if distros and source.context.get("distro") not in distros:
            raise SourceError("source does not support this distribution", "unsupported-platform")
    return ManagedInclude(group, item_id, item_id, ref=f"homebrew:tap/{item_id}" if source.type == "tap" else source_id,
                          parameters={}, source_id=source_id)


def parse_definitions(text):
    matches = list(re.finditer(re.escape(START) + r"\n(.*?)\n" + re.escape(END), text, re.S))
    if not matches:
        return {}
    if len(matches) != 1:
        raise SourceError("duplicate managed source block")
    match = re.fullmatch(r'\s*envy\.software\.sources\s*=\s*builtins\.fromJSON\s+("(?:\\.|[^"\\])*")\s*;\s*', matches[0][1], re.S)
    if not match:
        raise SourceError("unsupported managed source expression")
    definitions = json.loads(json.loads(match[1]))
    if not isinstance(definitions, dict):
        raise SourceError("source definitions must be an object")
    for sid, raw in definitions.items():
        Source.from_dict(sid, raw)
    return definitions


def render_definitions(text, definitions):
    from envy.software import _insert_managed_source_block
    for sid, raw in definitions.items():
        Source.from_dict(sid, raw)
    payload = json.dumps(json.dumps(definitions, sort_keys=True, ensure_ascii=False), ensure_ascii=False)
    # Nix quoted strings are JSON-compatible except interpolation.
    if "${" in payload:
        raise SourceError("source data cannot contain Nix interpolation")
    block = f"{START}\n  envy.software.sources = builtins.fromJSON {payload};\n{END}\n"
    pattern = re.escape(START) + r"\n.*?\n" + re.escape(END) + r"\n?"
    if re.search(pattern, text, re.S):
        return re.sub(pattern, lambda _: block if definitions else "", text, count=1, flags=re.S)
    return _insert_managed_source_block(text, block) if definitions else text


def source_dependents(source_id, manifest):
    from envy.sources.catalog import legacy_source_id
    found = []
    for gid, group in manifest.get("software", {}).get("groups", {}).items():
        if group.get("kind") == "repository":
            continue
        for item in group.get("selection", {}).get("effective", []):
            if (item.get("sourceId") or legacy_source_id(group.get("ecosystem", ""), item.get("ref"), group.get("kind", ""))) == source_id or source_id in item.get("parameters", {}).get("sourceDependencies", []):
                found.append({"machine": manifest.get("id"), "group": gid, "item": item["id"]})
    return found

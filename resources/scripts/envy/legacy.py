"""Central registry and helpers for configuration migrations.

Legacy names are accepted only long enough for ``envy config refine`` to
rewrite them into the current schema. Runtime modules should use canonical
names and must not add compatibility aliases locally.
"""

from __future__ import annotations

from typing import Any


# Agent field renames are expressed without the ``envy.agents.<name>.``
# prefix so the same rule applies to every managed agent instance.
AGENT_FIELD_ALIASES = {"more_thinking": "moreThinking"}
OBSOLETE_AGENT_NAMES = frozenset({"omp"})
OBSOLETE_AGENT_ADAPTERS = frozenset({"oh-my-pi"})

# Older software option paths are migrated by config.refine before the current
# managed policy is validated.
SOFTWARE_OPTION_ALIASES = {
    "envy.packages.home": "envy.software.nix.packages",
    "envy.darwin.packages.system": "envy.darwin.software.nix.systemPackages",
    "envy.darwin.packages.fonts": "envy.darwin.software.nix.fonts",
    "envy.darwin.homebrew.brews": "envy.darwin.software.homebrew.formulae",
    "envy.darwin.homebrew.casks": "envy.darwin.software.homebrew.casks",
    "envy.darwin.homebrew.taps": "envy.darwin.software.homebrew.repositories",
}


def is_obsolete_agent(name: str, adapter: Any = None) -> bool:
    """Recognize retired instances, including renamed Oh My Pi agents."""
    return name in OBSOLETE_AGENT_NAMES or (
        isinstance(adapter, str) and adapter in OBSOLETE_AGENT_ADAPTERS
    )


def obsolete_agent_paths(values: dict[str, Any], name_pattern: str = r"[A-Za-z0-9][A-Za-z0-9_-]*") -> list[str]:
    """Return all managed fields belonging to retired agent instances."""
    import re

    names = set(OBSOLETE_AGENT_NAMES)
    names.update(
        path.split(".")[2]
        for path, value in values.items()
        if re.fullmatch(r"envy\.agents\." + name_pattern + r"\.adapter", path)
        and is_obsolete_agent(path.split(".")[2], value)
    )
    return sorted(path for path in values if path.startswith("envy.agents.")
                  and path.split(".")[2] in names)


def normalize_agent_fields(values: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Rename valid legacy agent fields and report conflicts.

    Invalid legacy values stay under their old key so validation can report
    the exact source field instead of silently rewriting malformed input.
    """
    normalized = dict(values)
    added: list[str] = []
    removed: list[str] = []
    errors: list[str] = []
    for old_field, new_field in AGENT_FIELD_ALIASES.items():
        suffix = "." + old_field
        for old_path, value in list(values.items()):
            if not old_path.startswith("envy.agents.") or not old_path.endswith(suffix):
                continue
            new_path = old_path[: -len(old_field)] + new_field
            if not isinstance(value, bool):
                continue
            if new_path in values and values[new_path] != value:
                errors.extend((old_path, new_path))
                continue
            normalized.pop(old_path, None)
            normalized[new_path] = value
            added.append(new_path)
            removed.append(old_path)
    return normalized, sorted(set(added)), sorted(set(removed)), sorted(set(errors))


def canonicalize_agent_values(values: dict[str, Any]) -> dict[str, Any]:
    """Expose current field names when reading an older managed block."""
    normalized = dict(values)
    for old_field, new_field in AGENT_FIELD_ALIASES.items():
        for old_path, value in list(values.items()):
            if not old_path.startswith("envy.agents.") or not old_path.endswith("." + old_field):
                continue
            new_path = old_path[:-len(old_field)] + new_field
            normalized.setdefault(new_path, value)
            normalized.pop(old_path, None)
    return normalized


def canonicalize_agent_object(item: dict[str, Any]) -> dict[str, Any]:
    """Expose evaluated legacy manifests with current field spelling."""
    value = dict(item)
    for old_field, new_field in AGENT_FIELD_ALIASES.items():
        if new_field not in value and old_field in value:
            value[new_field] = value[old_field]
        value.pop(old_field, None)
    return value

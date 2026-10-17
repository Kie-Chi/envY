"""Source completion from public declarations, without probing registries."""

from __future__ import annotations

import subprocess

from envy.evaluation import machine_manifest
from envy.sources.catalog import default_definitions
from envy.sources.model import Source
from envy.utils import platform_name


ECOSYSTEMS = {
    "nix": "Nix packages", "native": "Native packages and repositories",
    "homebrew": "Homebrew formulae, casks and taps", "npm": "npm tools",
    "pypi": "Python tools", "cargo": "Rust crates", "go": "Go packages",
    "url": "Verified artifacts",
}


def declared_sources(manifest=None):
    """Read validated definitions only; installed state is unnecessary for IDs."""
    try:
        if manifest is None:
            manifest = machine_manifest(completion=True)
        definitions = default_definitions()
        definitions.update((manifest or {}).get("software", {}).get("sources", {}))
        platform = (manifest or {}).get("platform", platform_name())
        result = {}
        for source_id, definition in definitions.items():
            source = Source.from_dict(source_id, definition)
            if platform in source.platforms:
                result[source_id] = source
        return result
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
        return {}


def _params(ctx):
    return getattr(ctx, "params", {}) or {}


def _values(value):
    if isinstance(value, str):
        return {part for part in value.split(",") if part}
    if isinstance(value, (list, tuple)):
        return {part for item in value if isinstance(item, str) for part in item.split(",") if part}
    return set()


def complete_source_ids(ctx, incomplete: str):
    del ctx
    return [(source.id, source.label) for source in declared_sources().values()
            if source.id.casefold().startswith(incomplete.casefold())]


def complete_ecosystems(ctx, incomplete: str):
    del ctx
    return [(name, label) for name, label in ECOSYSTEMS.items()
            if name.startswith(incomplete.casefold())]


def complete_add_source_ids(ctx, incomplete: str):
    params = _params(ctx)
    group = params.get("group") or params.get("group_or_item")
    ecosystem = None
    if isinstance(group, str):
        from envy.software import require_group
        import typer
        try:
            ecosystem = require_group(group).ecosystem
        except typer.BadParameter:
            # A canonical item reference can constrain the first argument too.
            ecosystem = group.split(":", 1)[0] if ":" in group else None
        if ecosystem == "flake":
            ecosystem = "nix"
        if ecosystem not in ECOSYSTEMS:
            ecosystem = None
    return [(source.id, source.label) for source in declared_sources().values()
            if source.id.casefold().startswith(incomplete.casefold())
            and (ecosystem is None or source.ecosystem == ecosystem)
            and source.type != "local"
            and not source.parameters.get("discoveryOnly")]


def complete_search_source_ids(ctx, incomplete: str):
    params = _params(ctx)
    providers = _values(params.get("source"))
    selected = _values(params.get("source_id"))
    return [(source.id, source.label) for source in declared_sources().values()
            if source.id.casefold().startswith(incomplete.casefold())
            and source.id not in selected and source.type != "local"
            and (not providers or source.ecosystem in providers)]

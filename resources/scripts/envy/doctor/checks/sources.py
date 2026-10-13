"""Declarative source readiness and unsupported constraint diagnostics."""
from envy.doctor.model import SECTION_SYSTEM, error, info, warn, ok
from envy.sources.catalog import catalog
from envy.sources.model import SourceError


def run_checks(manifest):
    results = []
    try:
        sources = catalog(manifest)
    except (SourceError, ValueError) as exc:
        return [error(SECTION_SYSTEM, "software sources", str(exc), hint="Run: envy sw source ls --json")]
    for source in sources.values():
        if not source.selected:
            continue
        if source.ready:
            results.append(ok(SECTION_SYSTEM, f"source {source.id}", "registered and available"))
        elif source.type == "native" and not source.parameters.get("system"):
            results.append(warn(SECTION_SYSTEM, f"source {source.id}", "selected repository is not ready",
                                hint="Apply the repository declaration, then inspect envy sw source show " + source.id))
    for group in (manifest or {}).get("software", {}).get("groups", {}).values():
        for item in group.get("selection", {}).get("effective", []):
            if group.get("ecosystem") == "native" and item.get("version"):
                results.append(warn(SECTION_SYSTEM, f"version constraint {item['id']}",
                                    "legacy native version constraint is declared but not enforced"))
            if group.get("ecosystem") == "url" and not item.get("parameters", {}).get("hash"):
                results.append(info(SECTION_SYSTEM, f"artifact {item['id']}",
                                    "legacy artifact checks package metadata but has no pinned content hash"))
    return results

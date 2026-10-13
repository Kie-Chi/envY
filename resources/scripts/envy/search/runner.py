"""Concurrent search orchestration, ranking, manifest matching, and rendering."""

import hashlib
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer
from rich.table import Table

from envy import log
from envy.evaluation import machine_manifest, manifest_software_groups
from envy.search.index import RegistryIndex
from envy.search.model import ProviderReport, SearchResult
from envy.search.providers import available_providers
from envy.utils import platform_name


CACHE_VERSION = 2
CACHE_TTL_SECONDS = 15 * 60
SOURCE_ALIASES = {
    "apt": "native",
    "dnf": "native",
    "pacman": "native",
    "zypper": "native",
    "uv": "pypi",
}


def search_and_render(
    query: str,
    *,
    sources: list[str] | None,
    limit: int,
    exact: bool,
    json_output: bool,
    refresh: bool = False,
    timeout: float = 10,
    source_ids: list[str] | None = None,
) -> None:
    selected = _normalize_sources(sources)
    providers = available_providers()
    unknown = sorted(set(selected or ()) - set(providers))
    if unknown:
        available = ", ".join(sorted(providers))
        raise typer.BadParameter(
            f"unavailable search source(s): {', '.join(unknown)}; available: {available}"
        )
    active = {name: providers[name] for name in selected} if selected else providers
    manifest = machine_manifest()
    from envy.sources.catalog import catalog
    from envy.sources.adapters import search
    declared = catalog(manifest)
    if source_ids:
        missing = set(source_ids) - set(declared)
        if missing:
            raise typer.BadParameter("unknown source IDs: " + ", ".join(sorted(missing)))
    # Keep dependency injection for provider tests; production providers are
    # expanded into concrete origins before running concurrently.
    if source_ids or all(getattr(provider, "__module__", "") == "envy.search.providers" for provider in active.values()):
        candidates = [source for sid, source in declared.items()
                      if (not source_ids or sid in source_ids)
                      and (source.ecosystem in active or (source_ids and not selected))
                      and (source_ids or source.capabilities.get("search"))
                      and (source_ids or source.ready or source.selected)
                      and platform_name() in source.platforms]
        active = {source.id: (lambda q, n, t, source=source: search(source, manifest or {}, q, n, t))
                  for source in candidates}
    contexts = {sid: source.fingerprint for sid, source in declared.items() if sid in active}
    if not active:
        log.error("software", "no search providers are available")
        raise typer.Exit(code=1)

    reports = None if refresh else _read_cache(query, sorted(active), limit, contexts)
    if reports is None:
        reports = _run_providers(active, query, limit, timeout)
        if all(report.error is None for report in reports):
            _write_cache(query, sorted(active), limit, reports, contexts)

    # Query cache and exact identity index have different lifecycles. Preserve
    # every successful provider result even when another provider failed.
    try:
        RegistryIndex().put_reports(reports)
    except (OSError, sqlite3.Error):
        pass

    results = [result for report in reports for result in report.results]
    if exact:
        results = [result for result in results if result.name.casefold() == query.casefold()]
    _mark_managed(results, manifest)
    results.sort(key=lambda result: _rank(result, query))

    if json_output:
        payload = {
            "query": query,
            "platform": platform_name(),
            "results": [result.to_dict() for result in results],
            "providers": [report.to_dict() for report in reports],
            "sources": [source.to_dict() for sid, source in declared.items() if sid in active],
        }
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    for report in reports:
        if report.error:
            log.warn("search", "provider failed", source=report.source, error=report.error)
    table = Table(title=f"Software search - {query}")
    table.add_column("Source")
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Reference")
    table.add_column("Managed")
    table.add_column("Description")
    for result in results:
        table.add_row(
            result.source_id or result.source,
            result.kind,
            result.name,
            result.version or "",
            result.ref or "",
            result.managed_group or "",
            result.description,
        )
    log.console.print(table)
    if not results:
        log.info("search", "no matching packages found", query=query)


def _run_providers(providers, query: str, limit: int, timeout: float) -> list[ProviderReport]:
    reports = []
    with ThreadPoolExecutor(max_workers=min(8, len(providers))) as executor:
        futures = {
            executor.submit(provider, query, limit, timeout): name
            for name, provider in providers.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                reports.append(future.result())
            except Exception as exc:
                reports.append(ProviderReport(name, [], str(exc)[:500]))
    reports.sort(key=lambda report: report.source)
    return reports


def _mark_managed(results: list[SearchResult], manifest) -> None:
    managed: dict[str, list[tuple[str, str | None, str]]] = {}
    for group_id, group in manifest_software_groups(manifest).items():
        ecosystem = group.get("ecosystem")
        selection = group.get("selection")
        effective = selection.get("effective") if isinstance(selection, dict) else None
        if not isinstance(ecosystem, str) or not isinstance(effective, list):
            continue
        for item in effective:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                from envy.sources.catalog import legacy_source_id
                item_source = item.get("sourceId") or legacy_source_id(ecosystem, item.get("ref"), group.get("kind", ""))
                names = [item["name"]]
                parameters = item.get("parameters")
                provider_names = parameters.get("names") if isinstance(parameters, dict) else None
                if isinstance(provider_names, dict):
                    names.extend(
                        name for name in provider_names.values()
                        if isinstance(name, str)
                    )
                ref = item.get("ref") if isinstance(item.get("ref"), str) else None
                managed.setdefault(ecosystem, []).extend(
                    (name.casefold(), ref, group_id, item_source)
                    for name in names
                )
    for result in results:
        for name, ref, group_id, item_source in managed.get(result.ecosystem, []):
            if result.source_id and item_source and result.source_id != item_source:
                continue
            if name == result.name.casefold() or (ref and ref == result.ref):
                result.managed_group = group_id
                break


def _rank(result: SearchResult, query: str) -> tuple[int, int, str, str]:
    name = result.name.casefold()
    needle = query.casefold()
    return (
        0 if name == needle else 1 if name.startswith(needle) else 2,
        0 if result.managed_group else 1,
        result.source,
        name,
    )


def _normalize_sources(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    result = []
    for value in values:
        for raw in value.split(","):
            raw = raw.strip().casefold()
            if raw in ("apt", "dnf", "pacman", "zypper"):
                from envy.sources.catalog import native_manager
                if raw != native_manager():
                    raise typer.BadParameter(f"native manager alias {raw} does not match this host")
            source = SOURCE_ALIASES.get(raw, raw)
            if source and source not in result:
                result.append(source)
    return result


def _cache_path(query: str, sources: list[str], limit: int, contexts=None) -> Path:
    payload = json.dumps({
        "version": CACHE_VERSION,
        "platform": platform_name(),
        "query": query.casefold(),
        "sources": sources,
        "limit": limit,
        "contexts": contexts or {},
    }, sort_keys=True, separators=(",", ":")).encode()
    key = hashlib.sha256(payload).hexdigest()
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "envy" / "search" / f"{key}.json"


def _read_cache(query: str, sources: list[str], limit: int, contexts=None) -> list[ProviderReport] | None:
    path = _cache_path(query, sources, limit, contexts)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    created_at = value.get("createdAt")
    if not isinstance(created_at, (int, float)) or time.time() - created_at > CACHE_TTL_SECONDS:
        return None
    raw_reports = value.get("reports")
    if not isinstance(raw_reports, list):
        return None
    reports = []
    for raw_report in raw_reports:
        if not isinstance(raw_report, dict):
            return None
        source = raw_report.get("source")
        raw_results = raw_report.get("results")
        error = raw_report.get("error")
        if (
            not isinstance(source, str)
            or not isinstance(raw_results, list)
            or (error is not None and not isinstance(error, str))
        ):
            return None
        try:
            results = [
                SearchResult.from_dict(item)
                for item in raw_results
                if isinstance(item, dict)
            ]
        except TypeError:
            return None
        if len(results) != len(raw_results):
            return None
        reports.append(ProviderReport(source, results, error))
    return reports


def _write_cache(query: str, sources: list[str], limit: int, reports: list[ProviderReport], contexts=None) -> None:
    path = _cache_path(query, sources, limit, contexts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({
            "createdAt": time.time(),
            "reports": [report.to_dict() for report in reports],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
    except OSError:
        pass



def invalidate_source_cache(source_id: str) -> None:
    """Discard this source's query snapshots after explicit metadata synchronization."""
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "envy" / "search"
    for path in root.glob("*.json"):
        try:
            reports = json.loads(path.read_text()).get("reports", [])
            if any(report.get("source") == source_id or
                   any(item.get("sourceId") == source_id for item in report.get("results", []))
                   for report in reports):
                path.unlink()
        except (OSError, ValueError, AttributeError):
            continue

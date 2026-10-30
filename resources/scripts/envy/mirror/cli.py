"""The envy mirror command group."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.table import Table

from envy import log
from envy.config.machine_nix import machine_config_file, read_mirror_overrides, write_mirror_overrides
from envy.evaluation import machine_manifest
from envy.jsonio import emit, emit_error
# Reach probes through their module so tests can replace network access there.
from envy.mirror import probe
from envy.mirror.cache import MirrorCache, mirror_cache_path
from envy.mirror.measure import (
    MEASUREMENT_PROVIDERS,
    _format_throughput,
    _fresh_cached_measurements,
    _measurement_cache_target,
    complete_measurement_providers,
    measured_sources,
)
from envy.mirror.sources import (
    TARGET_SPECS,
    _source_overrides,
    _target_overrides,
    annotate_current_selection,
    complete_mirror_sources,
    complete_mirror_targets,
    current_mirror_mode,
    current_selection,
    source_candidates,
)
from envy.mutation import offer_mutation_commit
from envy.nix_trust import current_nix_trust_context, nix_trust_is_applicable, run_nix_trust


app = typer.Typer(
    name="mirror",
    help="Inspect and probe the evaluated network mirror policy.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


def _manifest_or_exit(refresh: bool) -> dict[str, Any]:
    manifest = machine_manifest(refresh=refresh)
    mirrors = manifest.get("mirrors") if isinstance(manifest, dict) else None
    if not isinstance(mirrors, dict):
        log.error("mirror", "evaluated mirror policy is unavailable")
        log.hint("Run: envy config check")
        raise typer.Exit(code=1)
    return {"manifest": manifest, "mirrors": mirrors}


@app.command(name="status")
def cmd_status(
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Show the effective mirror endpoints for the selected machine."""
    evaluated = _manifest_or_exit(refresh)
    manifest = evaluated["manifest"]
    entries = list(probe.mirror_entries(evaluated["mirrors"]))
    if json_output:
        payload = {
            "schemaVersion": 1,
            "machine": manifest.get("id", "current"),
            "platform": manifest.get("platform"),
            "settings": dict(entries),
        }
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title=f"Mirror policy - {manifest.get('id', 'current')}")
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Value")
    for path, value in entries:
        table.add_row(path, value)
    log.console.print(table)


@app.command(name="targets")
def cmd_targets(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """List ecosystem targets that support generated overrides."""
    mode = current_mirror_mode()
    overrides = read_mirror_overrides()
    values = []
    for target, spec in TARGET_SPECS.items():
        selection = current_selection(target, mode=mode, overrides=overrides)
        values.append({
            "id": target,
            "label": spec["label"],
            "chsrcTarget": spec["chsrc"],
            "selectedSource": selection["source"],
            "selectionOrigin": selection["origin"],
            "selectedIdentity": selection["identity"],
        })
    if json_output:
        emit("mirror.targets", data={"mode": mode, "targets": values})
        return
    table = Table(title=f"Mirror targets - {mode} profile")
    table.add_column("Target", style="cyan")
    table.add_column("Label")
    table.add_column("chsrc target")
    table.add_column("Selected")
    for value in values:
        table.add_row(
            value["id"], value["label"], value["chsrcTarget"],
            f"{value['selectedSource']} ({value['selectionOrigin']})",
        )
    log.console.print(table)


@app.command(name="sources")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_sources(
    target: str = typer.Argument(
        ..., help="Mirror target, for example npm or rust", autocompletion=complete_mirror_targets,
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved source cache."),
    provider: str = typer.Option(
        "chsrc", "--provider", "-p", help="Measurement cache to display: chsrc or curl",
        autocompletion=complete_measurement_providers,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """List candidate mirrors for one ecosystem."""
    target = target.casefold()
    provider = provider.casefold()
    if provider not in MEASUREMENT_PROVIDERS:
        raise typer.BadParameter(f"unsupported mirror measurement provider: {provider}")
    values = source_candidates(target, refresh=refresh)
    measurements = {
        str(value.get("code")): value
        for value in MirrorCache().get(
            "measurements", _measurement_cache_target(target, provider), allow_stale=True,
        )
    }
    values = [
        {
            **source,
            **{
                key: measurement[key]
                for key in (
                    "ok", "httpStatus", "throughputBps", "detail",
                    "measurementProvider", "measurementUrl",
                )
                if key in measurement
            },
            "measurementStale": bool(measurement.get("stale")),
        }
        if (measurement := measurements.get(str(source.get("code")))) is not None
        else source
        for source in values
    ]
    mode = current_mirror_mode()
    values, selection = annotate_current_selection(target, values, mode=mode)
    payload = {
        "target": target,
        "label": TARGET_SPECS[target]["label"],
        "provider": provider,
        "mode": mode,
        "selection": selection,
        "sources": values,
        "cache": "refreshed" if refresh else "cached-or-discovered",
    }
    if json_output:
        emit("mirror.sources", data=payload)
        return
    table = Table(title=f"Mirror sources - {target} ({provider})")
    table.add_column("Code", style="cyan")
    table.add_column("Label")
    table.add_column("URL")
    table.add_column("Provider")
    table.add_column("Current")
    for source in values:
        table.add_row(
            str(source.get("code", "")), str(source.get("label", "")),
            str(source.get("url", "")), str(source.get("provider", "catalog")),
            str(source.get("selectionOrigin") or ""),
        )
    log.console.print(table)


@app.command(name="measure")
def cmd_measure(
    target: str = typer.Argument(
        ..., help="Mirror target, for example npm or rust", autocompletion=complete_mirror_targets,
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Run chsrc again even when a result is cached."),
    provider: str = typer.Option(
        "chsrc", "--provider", "-p", help="Measurement backend: chsrc or curl",
        autocompletion=complete_measurement_providers,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Measure one ecosystem with a selected provider and envY's persistent cache."""
    target = target.casefold()
    provider = provider.casefold()
    cache_hit = bool(not refresh and _fresh_cached_measurements(target, provider))
    values = measured_sources(target, refresh=refresh, provider=provider)
    mode = current_mirror_mode()
    values, selection = annotate_current_selection(target, values, mode=mode)
    successful = [value for value in values if value.get("ok") is True]
    recommended = max(successful, key=lambda value: value.get("throughputBps", 0), default=None)
    payload = {
        "target": target,
        "provider": provider,
        "mode": mode,
        "selection": selection,
        "results": values,
        "recommended": recommended,
        "failed": len(values) - len(successful),
        "cache": "hit" if cache_hit else ("refresh" if refresh else "measured"),
    }
    if json_output:
        emit("mirror.measure", data=payload)
        if values and not successful:
            raise typer.Exit(code=1)
        return
    cache_label = "cached" if cache_hit else ("refreshed" if refresh else "measured")
    table = Table(title=f"Mirror measure - {target} ({provider}, {cache_label})")
    table.add_column("State")
    table.add_column("Code", style="cyan")
    table.add_column("Throughput", justify="right")
    table.add_column("HTTP", justify="right")
    table.add_column("Probe URL")
    for value in sorted(values, key=lambda item: (not item.get("ok", False), -int(item.get("throughputBps", 0)))):
        throughput = int(value.get("throughputBps", 0))
        http_status = value.get("httpStatus")
        table.add_row(
            "OK" if value.get("ok") else "FAIL",
            str(value.get("code", "")),
            _format_throughput(throughput),
            str(http_status) if http_status is not None else "-",
            str(value.get("measurementUrl") or value.get("url", "")),
        )
    log.console.print(table)
    if cache_hit:
        log.hint(f"Cached result; use --refresh to run {provider} again")
    if values and not successful:
        raise typer.Exit(code=1)


def _render_set_payload(
    target: str, source: dict[str, Any] | None,
    before: dict[str, str], after: dict[str, str],
) -> dict[str, Any]:
    return {
        "target": target,
        "source": source,
        "before": _target_overrides(before, target),
        "after": _target_overrides(after, target),
        "changed": _target_overrides(before, target) != _target_overrides(after, target),
        "machine": str(machine_config_file()),
    }


@app.command(name="set")
def cmd_set(
    target: str = typer.Argument(
        ..., help="Mirror target, for example npm or rust", autocompletion=complete_mirror_targets,
    ),
    source: str = typer.Argument(
        ..., help="Source code from `envy mirror sources TARGET`", autocompletion=complete_mirror_sources,
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the generated machine override."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Write the generated override."),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh the candidate list first."),
    json_output: bool = typer.Option(False, "--json", help="Emit a stable mutation envelope."),
):
    """Generate one machine-local override; never calls `chsrc set`."""
    target = target.casefold()
    if target not in TARGET_SPECS:
        raise typer.BadParameter(f"unsupported mirror target: {target}")
    selected = next(
        (item for item in source_candidates(target, refresh=refresh) if item.get("code") == source),
        None,
    )
    if selected is None:
        message = f"source {source!r} is not an available {target} candidate"
        if json_output:
            emit_error("mirror.set", message, code="source-not-found")
            raise typer.Exit(code=1)
        raise typer.BadParameter(message)

    before = read_mirror_overrides()
    after = {
        key: value for key, value in before.items()
        if not key.startswith(f"envy.mirrors.overrides.{target}.")
    }
    after.update(_source_overrides(target, selected))
    plan = _render_set_payload(target, selected, before, after)
    if dry_run:
        if json_output:
            emit("mirror.set", data={"result": "dry-run", "plan": plan})
        else:
            log.info("mirror", "mirror override preview", target=target, source=source)
            for key, value in sorted(plan["after"].items()):
                log.info("mirror", "generated assignment", path=key, value=value)
        return
    if not yes:
        if json_output:
            emit_error("mirror.set", "mirror mutation requires --yes", code="confirmation-required")
            raise typer.Exit(code=1)
        if not typer.confirm(f"Write the {target} mirror override for {source}?", default=None):
            return
    write_mirror_overrides(after)
    offer_mutation_commit(
        [machine_config_file()], f"chore(mirror): select {target} source {source}",
        quiet=json_output or yes,
    )
    if json_output:
        emit("mirror.set", data={"result": "applied", "plan": plan})
    else:
        log.ok("mirror", "mirror override generated", target=target, source=source)
        log.hint("Run: envy plan && envy apply")


@app.command(name="reset")
def cmd_reset(
    target: str = typer.Argument(
        ..., help="Mirror target to reset to its profile default", autocompletion=complete_mirror_targets,
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview removal of the generated override."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Write the reset."),
    json_output: bool = typer.Option(False, "--json", help="Emit a stable mutation envelope."),
):
    """Remove one generated override and restore the profile default."""
    target = target.casefold()
    if target not in TARGET_SPECS:
        raise typer.BadParameter(f"unsupported mirror target: {target}")
    before = read_mirror_overrides()
    after = {
        key: value for key, value in before.items()
        if not key.startswith(f"envy.mirrors.overrides.{target}.")
    }
    plan = _render_set_payload(target, None, before, after)
    if dry_run:
        if json_output:
            emit("mirror.reset", data={"result": "dry-run", "plan": plan})
        else:
            log.info("mirror", "mirror override reset preview", target=target)
        return
    if not yes:
        if json_output:
            emit_error("mirror.reset", "mirror mutation requires --yes", code="confirmation-required")
            raise typer.Exit(code=1)
        if not typer.confirm(f"Reset the {target} mirror override?", default=None):
            return
    write_mirror_overrides(after)
    offer_mutation_commit(
        [machine_config_file()], f"chore(mirror): reset {target} source", quiet=json_output or yes,
    )
    if json_output:
        emit("mirror.reset", data={"result": "applied", "plan": plan})
    else:
        log.ok("mirror", "mirror override reset", target=target)


cache_app = typer.Typer(name="cache", help="Inspect or clear mirror source and measurement cache", no_args_is_help=True)
trust_app = typer.Typer(name="trust", help="Inspect or repair Nix daemon cache trust", no_args_is_help=True)


def _run_trust_command(action: str, *, json_output: bool) -> None:
    context = current_nix_trust_context()
    if not nix_trust_is_applicable():
        data = {
            "applicable": False,
            "platform": sys.platform,
            "state": "unsupported",
            "mode": context.mode,
        }
        if json_output:
            emit(f"mirror.trust.{action}", data=data)
        else:
            log.info("mirror", "Nix daemon trust is not supported on this platform")
        return

    capture = action == "status"
    quiet = json_output and action == "repair"
    trust_args = {
        "context": context,
        "capture": capture,
        "quiet": quiet,
    }
    if action == "repair":
        trust_args["reload"] = True
    result = run_nix_trust(action, **trust_args)
    ready = result.returncode == 0
    state = "ready" if ready else ("repair-required" if action == "status" and result.returncode == 1 else "error")
    detail = (result.stderr or result.stdout or "").strip()
    if json_output:
        emit(
            f"mirror.trust.{action}",
            ok=ready,
            data={
                "applicable": True,
                "platform": sys.platform,
                "state": state,
                "mode": context.mode,
                "user": context.user,
                "exitCode": result.returncode,
            },
            error=None if ready else {"code": state, "message": detail or state},
        )
    elif capture:
        if result.stdout:
            log.console.print(result.stdout.rstrip(), markup=False)
        if result.stderr:
            log.diagnostic_console.print(result.stderr.rstrip(), markup=False)
    if not ready:
        raise typer.Exit(code=result.returncode)


@trust_app.command(name="status")
def cmd_trust_status(
    json_output: bool = typer.Option(False, "--json", help="Emit a stable status envelope."),
):
    """Check whether the selected cache policy is trusted by the Nix daemon."""
    _run_trust_command("status", json_output=json_output)


@trust_app.command(name="repair")
def cmd_trust_repair(
    json_output: bool = typer.Option(False, "--json", help="Emit a stable repair envelope."),
):
    """Reconcile daemon trust and restart an active Nix daemon only when changed."""
    _run_trust_command("repair", json_output=json_output)


@cache_app.command(name="status")
def cmd_cache_status(json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON.")):
    stats = MirrorCache().stats()
    if json_output:
        emit("mirror.cache.status", data=stats)
        return
    table = Table(title="Mirror cache")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in stats.items():
        table.add_row(key, str(value))
    log.console.print(table)


@cache_app.command(name="clean")
def cmd_cache_clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deleting mirror cache."),
    json_output: bool = typer.Option(False, "--json", help="Emit a stable result envelope."),
):
    path = mirror_cache_path()
    if not yes:
        if json_output:
            emit_error("mirror.cache.clean", "cache deletion requires --yes", code="confirmation-required")
            raise typer.Exit(code=1)
        if not typer.confirm(f"Delete mirror cache {path}?", default=None):
            return
    removed = MirrorCache().clear()
    if json_output:
        emit("mirror.cache.clean", data={"removed": removed, "path": str(path)})
    else:
        log.ok("mirror", "mirror cache removed" if removed else "mirror cache already empty", path=str(path))


app.add_typer(cache_app, name="cache")
app.add_typer(trust_app, name="trust")


@app.command(name="probe")
def cmd_probe(
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the saved manifest cache."),
    timeout: int = typer.Option(15, "--timeout", min=1, max=120, help="Per-endpoint timeout in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Test effective mirror endpoints without modifying configuration."""
    evaluated = _manifest_or_exit(refresh)
    specs = probe.probe_specs(evaluated["mirrors"])
    if not specs:
        if json_output:
            log.console.print_json(json.dumps({
                "schemaVersion": 1,
                "machine": evaluated["manifest"].get("id", "current"),
                "failed": 0,
                "results": [],
            }, ensure_ascii=False))
        else:
            log.warn("mirror", "no probe endpoints are declared")
        return

    table = Table(title=f"Mirror probe - {evaluated['manifest'].get('id', 'current')}")
    table.add_column("State", no_wrap=True)
    table.add_column("Endpoint", style="cyan", no_wrap=True)
    table.add_column("HTTP", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("URL")
    failed = 0
    results = []
    for name, url in specs:
        result = probe.probe_endpoint(name, url, timeout=timeout)
        results.append(result)
        if not result.ok:
            failed += 1
        if not json_output:
            table.add_row(
                "[green]OK[/green]" if result.ok else "[red]FAIL[/red]",
                result.name,
                result.status,
                f"{result.elapsed_ms} ms" if result.elapsed_ms is not None else "-",
                result.url,
            )
            if result.detail:
                log.debug("mirror", "probe detail", endpoint=result.name, detail=result.detail)
    if json_output:
        payload = {
            "schemaVersion": 1,
            "machine": evaluated["manifest"].get("id", "current"),
            "failed": failed,
            "results": [result.to_dict() for result in results],
        }
        log.console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        log.console.print(table)
    if failed:
        if not json_output:
            log.warn("mirror", "one or more endpoints failed", failed=failed, total=len(specs))
        raise typer.Exit(code=1)

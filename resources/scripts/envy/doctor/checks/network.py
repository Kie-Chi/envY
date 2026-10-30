"""Explicit read-only network checks using the evaluated mirror catalog."""

from __future__ import annotations

from envy.doctor.model import SECTION_SYSTEM, CheckResult, error, ok, warn
from envy.evaluation import machine_manifest
from envy.mirror.probe import probe_endpoint, probe_specs


def run_checks() -> list[CheckResult]:
    manifest = machine_manifest()
    mirrors = manifest.get("mirrors") if isinstance(manifest, dict) else None
    if not isinstance(mirrors, dict):
        return [error(
            SECTION_SYSTEM,
            "mirror policy",
            "evaluated mirror policy is unavailable",
            hint="Run: envy config check",
        )]
    specs = probe_specs(mirrors)
    if not specs:
        return [warn(SECTION_SYSTEM, "mirror endpoints", "no endpoints are declared")]
    results: list[CheckResult] = []
    for name, url in specs:
        probe = probe_endpoint(name, url, timeout=8)
        if probe.ok:
            latency = f", {probe.elapsed_ms} ms" if probe.elapsed_ms is not None else ""
            results.append(ok(SECTION_SYSTEM, f"network {name}", f"HTTP {probe.status}{latency}"))
        else:
            results.append(error(
                SECTION_SYSTEM,
                f"network {name}",
                f"HTTP {probe.status}",
                hint=f"Check the evaluated endpoint: {url}",
            ))
    return results

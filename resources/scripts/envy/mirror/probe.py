"""Read-only connectivity probes for the evaluated mirror endpoints."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class ProbeResult:
    name: str
    url: str
    ok: bool
    status: str
    elapsed_ms: int | None
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": self.url,
            "ok": self.ok,
            "status": self.status,
            "elapsedMs": self.elapsed_ms,
            "detail": self.detail,
        }


def mirror_entries(mirrors: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Flatten configured mirror values while hiding probe-only metadata."""

    def walk(prefix: str, value: Any) -> Iterator[tuple[str, str]]:
        if isinstance(value, dict):
            for key in sorted(value):
                if not prefix and key == "probes":
                    continue
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                yield from walk(next_prefix, value[key])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(f"{prefix}[{index}]", item)
        elif value is None:
            yield prefix, "disabled"
        elif isinstance(value, bool):
            yield prefix, "true" if value else "false"
        else:
            yield prefix, str(value)

    if "mode" in mirrors:
        yield "mode", str(mirrors["mode"])
    for key in sorted(key for key in mirrors if key not in {"mode", "probes"}):
        yield from walk(key, mirrors[key])


def probe_specs(mirrors: dict[str, Any]) -> list[tuple[str, str]]:
    values = mirrors.get("probes", [])
    if not isinstance(values, list):
        return []
    specs: list[tuple[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        url = value.get("url")
        if isinstance(name, str) and isinstance(url, str):
            specs.append((name, url))
    return specs


def probe_endpoint(name: str, url: str, timeout: int = 15) -> ProbeResult:
    """Probe one catalog endpoint without changing any local settings."""
    try:
        result = subprocess.run(
            [
                "curl",
                "--head",
                "--location",
                "--silent",
                "--show-error",
                "--output", "/dev/null",
                "--connect-timeout", "5",
                "--max-time", str(timeout),
                "--write-out", "%{http_code}\t%{time_total}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult(name, url, False, "error", None, str(exc))

    status, separator, elapsed = result.stdout.strip().partition("\t")
    try:
        status_code = int(status)
    except ValueError:
        status_code = 0
    try:
        elapsed_ms = round(float(elapsed) * 1000) if separator else None
    except ValueError:
        elapsed_ms = None
    ok = result.returncode == 0 and 200 <= status_code < 400
    detail = result.stderr.strip()
    return ProbeResult(name, url, ok, status or "error", elapsed_ms, detail)

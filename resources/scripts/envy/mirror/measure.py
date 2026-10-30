"""Mirror speed measurement with chsrc or curl, cached per provider."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import typer

# Reach source discovery through its module so tests can replace chsrc there.
from envy.mirror import sources as mirror_sources
from envy.mirror.cache import MirrorCache


MEASUREMENT_TTL = 6 * 60 * 60
NEGATIVE_MEASUREMENT_TTL = 5 * 60
CHSRC_MEASURE_TIMEOUT = 180
CURL_TIMEOUT = 30
CURL_CONNECT_TIMEOUT = 10
# Keep the request bounded while still downloading a representative artifact.
# The range is advisory: mirrors that do not support Range may send the whole
# object, and the max-time guard still prevents a stuck provider from blocking
# the other candidates.
CURL_SAMPLE_BYTES = 2 * 1024 * 1024
# Bump when chsrc's human-readable measurement format handling changes. This
# lets an upgraded envY ignore a short-lived negative cache written by an older
# parser without requiring the user to remember `--refresh`.
MEASUREMENT_PARSER_VERSION = 3

MEASUREMENT_PROVIDERS: dict[str, str] = {
    "chsrc": "Use chsrc's ecosystem-aware source measurement",
    "curl": "Measure each source URL with curl",
}

# These are the same representative resources used by chsrc where chsrc
# exposes a stable speed-test artifact.  A registry root is often a tiny
# metadata response (or even a deliberate 404), which measures response
# overhead rather than mirror throughput.
CURL_PROBE_PATHS: dict[str, str] = {
    "npm": "/@tensorflow/tfjs/-/tfjs-4.22.0.tgz",
    "python": "/pip/",
    "go": "/github.com/golang/go/@v/v1.24.0.mod",
}

RUST_CURL_ARTIFACT = "/api/v1/crates/windows/windows-0.62.2/download"


def _parse_speed(value: str) -> float:
    # chsrc has used both the compact ``MB/s`` spelling and the more explicit
    # ``MByte/s`` spelling over time.  Keep the parser permissive because this
    # is human-oriented output rather than a stable machine-readable API.
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?(?:Byte|iB|B))/s",
        value,
        re.I,
    )
    if not match:
        return 0.0
    scale = {
        "b": 1,
        "byte": 1,
        "kb": 1024,
        "kbyte": 1024,
        "kib": 1024,
        "mb": 1024 ** 2,
        "mbyte": 1024 ** 2,
        "mib": 1024 ** 2,
        "gb": 1024 ** 3,
        "gbyte": 1024 ** 3,
        "gib": 1024 ** 3,
        "tb": 1024 ** 4,
        "tbyte": 1024 ** 4,
        "tib": 1024 ** 4,
    }
    return float(match.group(1)) * scale.get(match.group(2).casefold(), 1)


def _format_throughput(throughput_bps: int) -> str:
    if throughput_bps <= 0:
        return "-"
    return f"{throughput_bps / (1024 ** 2):.2f} MB/s"


def complete_measurement_providers(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete the supported mirror measurement backends without I/O."""
    del ctx
    return [
        (provider, description)
        for provider, description in MEASUREMENT_PROVIDERS.items()
        if provider.startswith(incomplete.casefold())
    ]


def _measure_with_chsrc(target: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = mirror_sources._run_chsrc(
        ["measure", "-no-color", mirror_sources.TARGET_SPECS[target]["chsrc"]],
        timeout=CHSRC_MEASURE_TIMEOUT,
    )
    by_url = {str(source.get("url")): source for source in sources}
    by_label = {
        str(source.get("label")): source
        for source in sources
        if source.get("label")
    }
    measured: dict[str, dict[str, Any]] = {}
    if result is not None:
        stdout, stderr, code = result
        for line in stdout.splitlines():
            url_match = re.search(r"\((https?://[^)]+)\)", line)
            source = by_url.get(url_match.group(1)) if url_match else None
            if source is None:
                source = next(
                    (candidate for label, candidate in by_label.items() if label in line),
                    None,
                )
            if source is None:
                continue
            status_match = re.search(
                r"HTTP(?:\s*(?:code|status)|码)?\s*:?\s*([0-9]{3})",
                line,
                re.I,
            )
            # Recent chsrc builds omit the HTTP suffix for successful probes,
            # while older builds print ``HTTP码 200``/``HTTP code 200``.  A
            # positive throughput is still a valid success when no status was
            # emitted; retain ``None`` so the UI does not invent an HTTP code.
            status = int(status_match.group(1)) if status_match else None
            speed = _parse_speed(line)
            # chsrc prints a recommendation after the measurement rows. That
            # line repeats the winning source label but has neither a speed
            # nor an HTTP status (for example, ``最快镜像站: npmmirror ...``).
            # Do not let it overwrite the real measurement with a synthetic
            # zero-throughput failure.
            if source["code"] in measured and speed <= 0 and status is None:
                continue
            ok = speed > 0 and (status is None or 200 <= status < 400)
            if ok:
                detail = ""
            elif stderr:
                detail = stderr.strip()[:300]
            elif status is not None and status != 0:
                detail = f"chsrc reported HTTP {status}"
            else:
                detail = "chsrc reported HTTP 000 / no response"
            measured[source["code"]] = {
                **source,
                "httpStatus": status,
                "throughputBps": round(speed),
                "ok": ok,
                "detail": detail,
                "measurementProvider": "chsrc",
                "measurementParserVersion": MEASUREMENT_PARSER_VERSION,
            }
        if measured:
            missing_detail = (
                stderr.strip()[:300]
                if code != 0 and stderr
                else "chsrc did not report this source"
            )
            return [
                measured.get(
                    str(source.get("code")),
                    {
                        **source,
                        "httpStatus": 0,
                        "throughputBps": 0,
                        "ok": False,
                        "detail": missing_detail,
                        "measurementProvider": "chsrc",
                        "measurementParserVersion": MEASUREMENT_PARSER_VERSION,
                    },
                )
                for source in sources
            ]
        if code != 0:
            return [{**source, "ok": False, "httpStatus": 0, "throughputBps": 0,
                     "detail": stderr.strip()[:300] if stderr else "chsrc failed",
                     "measurementParserVersion": MEASUREMENT_PARSER_VERSION} for source in sources]
    return [{**source, "ok": False, "httpStatus": 0, "throughputBps": 0,
             "detail": "chsrc unavailable or returned no valid measurements",
             "measurementParserVersion": MEASUREMENT_PARSER_VERSION} for source in sources]


def _join_probe_url(base: str, path: str) -> str:
    """Join a catalog base URL and a probe path without duplicating slashes."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _curl_probe_urls(source: dict[str, Any], target: str | None = None) -> list[str]:
    """Return target-aware URLs suitable for a curl throughput sample.

    The optional target keeps the old private helper API useful for callers
    that only want to exercise curl against an arbitrary URL.  The command
    path always supplies it, so normal measurements use representative
    ecosystem resources rather than the registry root.
    """
    base = str(source.get("url", "")).strip()
    if not base:
        return []
    if not target:
        return [base]
    target = target.casefold()
    code = str(source.get("code", "")).casefold()
    if target == "rust":
        # crates.io keeps its download service on static.crates.io.  rsproxy
        # and Aliyun expose the sparse/git index and the crates API on a
        # sibling path; the latter is the same artifact family used by chsrc.
        if code in {"upstream", "crates.io", "cratesio"} or "crates.io/" in base.casefold():
            return ["https://static.crates.io/crates/windows/windows-0.62.2.crate"]
        if "rsproxy.cn" in base.casefold():
            return [_join_probe_url(base.split("/index", 1)[0], RUST_CURL_ARTIFACT)]
        marker = "/crates.io-index"
        if marker in base:
            root = base.split(marker, 1)[0]
            return [_join_probe_url(root, "/crates" + RUST_CURL_ARTIFACT)]
        # Sparse indexes have a config document, which is a better health
        # check than the root but is intentionally kept small.
        return [_join_probe_url(base, "config.json")]
    path = CURL_PROBE_PATHS.get(target)
    return [_join_probe_url(base, path)] if path else [base]


def _curl_failure(
    source: dict[str, Any], detail: str, *, probe_url: str | None = None,
) -> dict[str, Any]:
    return {
        **source,
        "ok": False,
        "httpStatus": 0,
        "throughputBps": 0,
        "detail": detail,
        "measurementProvider": "curl",
        "measurementParserVersion": MEASUREMENT_PARSER_VERSION,
        **({"measurementUrl": probe_url} if probe_url else {}),
    }


def _measure_one_with_curl(
    source: dict[str, Any], target: str | None = None,
) -> dict[str, Any]:
    """Measure one representative resource with curl counters."""
    probe_urls = _curl_probe_urls(source, target)
    if not probe_urls:
        return _curl_failure(source, "source has no URL")
    url = probe_urls[0]
    binary = shutil.which("curl")
    if binary is None:
        return _curl_failure(source, "curl is not available on PATH", probe_url=url)
    try:
        result = subprocess.run(
            [
                binary,
                "--location",
                "--silent",
                "--show-error",
                "--fail",
                "--connect-timeout",
                str(CURL_CONNECT_TIMEOUT),
                "--max-time",
                str(CURL_TIMEOUT),
                "--range",
                f"0-{CURL_SAMPLE_BYTES - 1}",
                "--output",
                os.devnull,
                "--write-out",
                "%{http_code}\t%{speed_download}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=CURL_TIMEOUT + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _curl_failure(source, f"curl timed out after {CURL_TIMEOUT}s", probe_url=url)
    except OSError as exc:
        return _curl_failure(source, f"could not run curl: {exc}", probe_url=url)

    status = 0
    speed = 0.0
    output = (result.stdout or "").strip()
    if output:
        match = re.search(r"(?m)([0-9]{3})\s+([0-9]+(?:\.[0-9]+)?)\s*$", output)
        if match:
            status = int(match.group(1))
            speed = float(match.group(2))
    ok = 200 <= status < 400 and speed > 0
    if ok:
        detail = ""
    elif (result.stderr or "").strip():
        detail = (result.stderr or "").strip()[:300]
    elif status:
        detail = f"curl reported HTTP {status}"
    else:
        detail = "curl returned HTTP 000 / no response"
    return {
        **source,
        "ok": ok,
        "httpStatus": status,
        "throughputBps": round(speed),
        "detail": detail,
        "measurementProvider": "curl",
        "measurementParserVersion": MEASUREMENT_PARSER_VERSION,
        "measurementUrl": url,
    }


def _measure_with_curl(
    sources: list[dict[str, Any]], target: str | None = None,
) -> list[dict[str, Any]]:
    """Measure all candidate URLs concurrently while preserving source order."""
    if not sources:
        return []
    results: dict[str, dict[str, Any]] = {}
    workers = min(8, len(sources))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="envy-curl") as pool:
        futures = {
            pool.submit(_measure_one_with_curl, source, target): str(source.get("code", ""))
            for source in sources
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:  # defensive isolation for one provider worker
                source = next((item for item in sources if str(item.get("code", "")) == code), {})
                results[code] = _curl_failure(source, f"curl measurement failed: {exc}")
    return [
        results.get(str(source.get("code", "")), _curl_failure(source, "curl did not report this source"))
        for source in sources
    ]


def _measurement_cache_target(target: str, provider: str) -> str:
    # Keep the historical unprefixed chsrc rows readable while isolating curl
    # results from them in the same SQLite table.
    return target if provider == "chsrc" else f"{provider}:{target}"


def _fresh_cached_measurements(target: str, provider: str) -> list[dict[str, Any]]:
    """Read a provider-specific cache only when every row uses this parser."""
    cached = MirrorCache().get(
        "measurements", _measurement_cache_target(target, provider), allow_stale=True,
    )
    if (
        cached
        and any(not value.get("stale") for value in cached)
        and all(
            value.get("measurementParserVersion") == MEASUREMENT_PARSER_VERSION
            for value in cached
        )
    ):
        return cached
    return []


def measured_sources(
    target: str,
    *,
    refresh: bool = False,
    provider: str = "chsrc",
) -> list[dict[str, Any]]:
    provider = provider.casefold()
    if provider not in MEASUREMENT_PROVIDERS:
        raise typer.BadParameter(
            f"unsupported mirror measurement provider: {provider}; "
            f"choose from {', '.join(MEASUREMENT_PROVIDERS)}"
        )
    cache = MirrorCache()
    cache_target = _measurement_cache_target(target, provider)
    if not refresh:
        cached = _fresh_cached_measurements(target, provider)
        if cached:
            return cached
    sources = mirror_sources.source_candidates(target, refresh=refresh)
    measured = (
        _measure_with_chsrc(target, sources)
        if provider == "chsrc"
        else _measure_with_curl(sources, target=target)
    )
    successful = any(value.get("ok") is True for value in measured)
    ttl = MEASUREMENT_TTL if successful else NEGATIVE_MEASUREMENT_TTL
    cache.put("measurements", cache_target, measured, ttl)
    return measured

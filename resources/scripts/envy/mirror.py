"""Read-only mirror policy inspection and connectivity probes."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import typer
from rich.table import Table

from envy import log
from envy.config import (
    machine_config_file,
    read_machine_nix,
    read_mirror_overrides,
    write_mirror_overrides,
)
from envy.evaluation import machine_manifest
from envy.jsonio import emit, emit_error
from envy.mutation import offer_mutation_commit
from envy.nix_trust import (
    current_nix_trust_context,
    nix_trust_is_applicable,
    run_nix_trust,
)


app = typer.Typer(
    name="mirror",
    help="Inspect and probe the evaluated network mirror policy.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


SOURCE_CACHE_TTL = 30 * 24 * 60 * 60
MEASUREMENT_TTL = 6 * 60 * 60
NEGATIVE_MEASUREMENT_TTL = 5 * 60
STALE_MAX_AGE = 7 * 24 * 60 * 60
CHSRC_TIMEOUT = 75
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


TARGET_SPECS: dict[str, dict[str, Any]] = {
    "npm": {"label": "NPM", "chsrc": "npm", "fields": ("registry",)},
    "rust": {
        "label": "Rust",
        "chsrc": "rust",
        "fields": ("cargoIndex", "distServer", "updateRoot"),
    },
    "python": {"label": "Python / PyPI", "chsrc": "python", "fields": ("index",)},
    "go": {"label": "Go modules", "chsrc": "go", "fields": ("proxy",)},
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


def complete_mirror_targets(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete mirror ecosystems from the static target registry."""
    del ctx
    prefix = incomplete.casefold()
    return [
        (target, str(spec["label"]))
        for target, spec in TARGET_SPECS.items()
        if target.startswith(prefix)
    ]


def _completion_target(ctx) -> str:
    params = getattr(ctx, "params", {}) if ctx is not None else {}
    target = params.get("target") if isinstance(params, dict) else None
    return str(target or "").casefold()


def complete_mirror_sources(ctx, incomplete: str) -> list[tuple[str, str]]:
    """Complete source IDs from cache/catalog without starting network work."""
    target = _completion_target(ctx)
    if target not in TARGET_SPECS:
        return []
    values = MirrorCache().get("sources", target, allow_stale=True)
    if not values:
        values = [dict(value, provider="catalog") for value in BUILTIN_SOURCES.get(target, [])]
    prefix = incomplete.casefold()
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for value in values:
        code = str(value.get("code", ""))
        if not code or code in seen or not code.casefold().startswith(prefix):
            continue
        seen.add(code)
        label = str(value.get("label", "")) or str(value.get("url", ""))
        candidates.append((code, label))
    return candidates

# The profile source IDs mirror the audited endpoint choices in
# modules/mirrors/catalog.nix. They make the UI's effective selection explicit
# even when a target has no machine-local override.
PROFILE_DEFAULT_SOURCES: dict[str, dict[str, str]] = {
    "upstream": {
        "npm": "upstream",
        "rust": "upstream",
        "python": "upstream",
        "go": "upstream",
    },
    "china": {
        "npm": "npmmirror",
        "rust": "rsproxycn",
        "python": "tuna",
        "go": "goproxy.cn",
    },
}


# Bootstrap-safe candidate catalog. Keep these stable endpoints aligned with
# the profile endpoints in modules/mirrors/catalog.nix; chsrc results are
# cached separately and never overwrite the versioned policy catalog.
BUILTIN_SOURCES: dict[str, list[dict[str, str]]] = {
    "npm": [
        {"code": "upstream", "label": "Upstream", "url": "https://registry.npmjs.org/"},
        {"code": "npmmirror", "label": "npmmirror", "url": "https://registry.npmmirror.com"},
        {"code": "huawei", "label": "Huawei Cloud", "url": "https://mirrors.huaweicloud.com/repository/npm/"},
        {"code": "tencent", "label": "Tencent Public", "url": "https://mirrors.cloud.tencent.com/npm/"},
    ],
    "rust": [
        {"code": "upstream", "label": "Upstream", "url": "https://crates.io/"},
        {"code": "rsproxycn", "label": "RsProxy.cn", "url": "https://rsproxy.cn/index/"},
        {"code": "ustc", "label": "USTC", "url": "https://mirrors.ustc.edu.cn/crates.io-index/"},
        {"code": "tuna", "label": "TUNA", "url": "https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/"},
        {"code": "ali", "label": "Ali OPSX Public", "url": "https://mirrors.aliyun.com/crates.io-index/"},
    ],
    "python": [
        {"code": "upstream", "label": "Upstream", "url": "https://pypi.org/simple"},
        {"code": "tuna", "label": "TUNA", "url": "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"},
    ],
    "go": [
        {"code": "upstream", "label": "Upstream", "url": "https://proxy.golang.org"},
        {"code": "goproxy.cn", "label": "Goproxy.cn", "url": "https://goproxy.cn"},
    ],
}


def mirror_cache_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".cache"
    return base / "envy" / "mirrors" / "index-v1.sqlite3"


class MirrorCache:
    """Private cache for chsrc candidates and slow target measurements."""

    def __init__(self, path: Path | None = None):
        self.path = path or mirror_cache_path()

    def _connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                target TEXT NOT NULL,
                code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY(target, code)
            );
            CREATE TABLE IF NOT EXISTS measurements (
                target TEXT NOT NULL,
                code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY(target, code)
            );
        """)
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema', '1')"
        )
        connection.commit()
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any] | None:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["fetchedAt"] = int(row["fetched_at"])
        payload["expiresAt"] = int(row["expires_at"])
        payload["stale"] = int(row["expires_at"]) <= int(time.time())
        return payload

    def get(self, table: str, target: str, *, allow_stale: bool = False) -> list[dict[str, Any]]:
        if table not in {"sources", "measurements"} or not self.path.exists():
            return []
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=1)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE target=? ORDER BY code", (target,)
            ).fetchall()
        except (OSError, sqlite3.Error):
            return []
        finally:
            if connection is not None:
                connection.close()
        values = [decoded for row in rows if (decoded := self._decode(row)) is not None]
        if not allow_stale:
            values = [value for value in values if not value["stale"]]
        return values

    def put(self, table: str, target: str, values: list[dict[str, Any]], ttl: int) -> None:
        if table not in {"sources", "measurements"}:
            raise ValueError(f"invalid mirror cache table: {table}")
        now = int(time.time())
        try:
            connection = self._connection()
        except (OSError, sqlite3.Error):
            # Cache is an optimization; an unwritable home/cache must not make
            # source discovery or TUI selection fail.
            return
        try:
            connection.execute(f"DELETE FROM {table} WHERE target=?", (target,))
            connection.executemany(
                f"INSERT INTO {table}(target, code, payload_json, fetched_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (target, str(value.get("code", "")), json.dumps(value, ensure_ascii=False, sort_keys=True), now, now + ttl)
                    for value in values if value.get("code")
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def stats(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"path": str(self.path), "sources": 0, "measurements": 0, "stale": 0}
        try:
            with sqlite3.connect(self.path) as connection:
                sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
                measurements = connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
                stale = connection.execute(
                    "SELECT COUNT(*) FROM measurements WHERE expires_at <= ?", (int(time.time()),)
                ).fetchone()[0]
        except (OSError, sqlite3.Error):
            return {"path": str(self.path), "sources": 0, "measurements": 0, "stale": 0}
        return {"path": str(self.path), "sources": sources, "measurements": measurements, "stale": stale}

    def clear(self) -> bool:
        if not self.path.exists():
            return False
        try:
            self.path.unlink()
            return True
        except OSError:
            return False


def _subprocess_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _run_chsrc(args: list[str], timeout: int = CHSRC_TIMEOUT) -> tuple[str, str, int] | None:
    binary = shutil.which("chsrc")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _subprocess_text(exc.stdout)
        error = _subprocess_text(exc.stderr).strip()
        detail = f"chsrc timed out after {timeout}s"
        if error:
            detail = f"{detail}: {error}"
        return stdout, detail, 124
    except OSError:
        return None
    return result.stdout, result.stderr, result.returncode


def _parse_chsrc_sources(target: str) -> list[dict[str, Any]]:
    result = _run_chsrc(["list", "-en", "-no-color", target])
    if result is None:
        return []
    stdout, _, _ = result
    values: list[dict[str, Any]] = []
    in_sources = False
    for line in stdout.splitlines():
        if "Available Sources:" in line or "可用源" in line:
            in_sources = True
            continue
        if in_sources and ("Available Features:" in line or "可用功能" in line):
            break
        if not in_sources:
            continue
        source_url = re.search(r"https?://[^\s]+", line)
        if not source_url:
            continue
        before_url = line[:source_url.start()].strip()
        if not before_url:
            continue
        code = before_url.split()[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", code):
            continue
        # Some long chsrc URLs consume their padding column, leaving only one
        # space before the mirror name. Splitting on multi-space columns then
        # incorrectly makes "URL + name" the label (and makes the TUI wrap the
        # source row). The text after the URL is the authoritative name.
        label = line[source_url.end():].strip()
        if not label:
            columns = re.split(r"\s{2,}", before_url)
            label = columns[-1].strip() if len(columns) > 1 else code
        values.append({
            "code": code,
            "label": label,
            "url": source_url.group(0).rstrip(".,"),
            "provider": "chsrc",
        })
    return values


def _normalize_source_labels(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair pre-v2 cache rows created by the long-URL chsrc table bug."""
    normalized: list[dict[str, Any]] = []
    for value in values:
        source = dict(value)
        url = str(source.get("url", ""))
        label = str(source.get("label", "")).strip()
        if url and url in label:
            label = label.split(url, 1)[1].strip()
        source["label"] = label or str(source.get("code", "source"))
        normalized.append(source)
    return normalized


def source_candidates(target: str, *, refresh: bool = False) -> list[dict[str, Any]]:
    target = target.casefold()
    if target not in TARGET_SPECS:
        raise typer.BadParameter(f"unsupported mirror target: {target}")
    cache = MirrorCache()
    cached = [] if refresh else cache.get("sources", target)
    if cached:
        return _normalize_source_labels(cached)
    stale = [] if refresh else cache.get("sources", target, allow_stale=True)
    values = _parse_chsrc_sources(TARGET_SPECS[target]["chsrc"])
    if not values:
        recent_stale = [
            value for value in stale
            if int(time.time()) - int(value.get("fetchedAt", 0)) <= STALE_MAX_AGE
        ]
        values = recent_stale or [
            dict(value, provider="catalog") for value in BUILTIN_SOURCES.get(target, [])
        ]
    values = _normalize_source_labels(values)
    cache.put("sources", target, values, SOURCE_CACHE_TTL)
    return values


def current_mirror_mode() -> str:
    """Read the selected profile without needing a fresh Nix evaluation."""
    try:
        mode = str(read_machine_nix().get("envy.mirrors.mode", "china")).casefold()
    except OSError:
        mode = "china"
    return mode if mode in PROFILE_DEFAULT_SOURCES else "china"


def _source_code_from_identity(target: str, identity: object) -> str | None:
    prefix = f"chsrc:{TARGET_SPECS[target]['chsrc']}/"
    value = str(identity or "")
    return value[len(prefix):] if value.startswith(prefix) else None


def current_selection(
    target: str,
    *,
    mode: str | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """Return the effective source identity shown by targets and source lists."""
    mode = mode or current_mirror_mode()
    if mode not in PROFILE_DEFAULT_SOURCES:
        mode = "china"
    overrides = overrides if overrides is not None else read_mirror_overrides()
    identity = overrides.get(f"envy.mirrors.overrides.{target}.source")
    source = _source_code_from_identity(target, identity)
    if source:
        return {"source": source, "origin": "override", "profile": mode, "identity": str(identity)}
    return {
        "source": PROFILE_DEFAULT_SOURCES[mode][target],
        "origin": "profile",
        "profile": mode,
        "identity": None,
    }


def annotate_current_selection(
    target: str,
    values: list[dict[str, Any]],
    *,
    mode: str | None = None,
    overrides: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """Attach current-effective selection metadata to source rows."""
    selection = current_selection(target, mode=mode, overrides=overrides)
    selected_code = selection["source"]
    annotated = [
        {
            **value,
            "current": value.get("code") == selected_code,
            "selectionOrigin": selection["origin"] if value.get("code") == selected_code else None,
            "profile": selection["profile"],
        }
        for value in values
    ]
    return annotated, selection


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
    result = _run_chsrc(
        ["measure", "-no-color", TARGET_SPECS[target]["chsrc"]],
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
    sources = source_candidates(target, refresh=refresh)
    measured = (
        _measure_with_chsrc(target, sources)
        if provider == "chsrc"
        else _measure_with_curl(sources, target=target)
    )
    successful = any(value.get("ok") is True for value in measured)
    ttl = MEASUREMENT_TTL if successful else NEGATIVE_MEASUREMENT_TTL
    cache.put("measurements", cache_target, measured, ttl)
    return measured


def _source_overrides(target: str, source: dict[str, Any]) -> dict[str, str]:
    code = str(source.get("code", ""))
    url = str(source.get("url", ""))
    prefix = f"envy.mirrors.overrides.{target}"
    if target == "npm":
        return {f"{prefix}.source": f"chsrc:{TARGET_SPECS[target]['chsrc']}/{code}", f"{prefix}.registry": url}
    if target == "python":
        return {f"{prefix}.source": f"chsrc:{TARGET_SPECS[target]['chsrc']}/{code}", f"{prefix}.index": url.rstrip("/")}
    if target == "go":
        proxy = url if "," in url else f"{url},direct"
        return {f"{prefix}.source": f"chsrc:{TARGET_SPECS[target]['chsrc']}/{code}", f"{prefix}.proxy": proxy}
    if target == "rust":
        known = {
            "upstream": {
                "cargoIndex": "sparse+https://index.crates.io/",
                "distServer": "https://static.rust-lang.org",
                "updateRoot": "https://static.rust-lang.org/rustup",
            },
            "rsproxycn": {
                "cargoIndex": "sparse+https://rsproxy.cn/index/",
                "distServer": "https://rsproxy.cn",
                "updateRoot": "https://rsproxy.cn/rustup",
            },
        }
        normalized_url = url.casefold()
        if code.casefold() in {"upstream", "crates.io", "cratesio"} or "index.crates.io" in normalized_url:
            values = known["upstream"]
        elif code.casefold() in {"rsproxy", "rsproxy.cn", "rsproxycn"} or "rsproxy.cn" in normalized_url:
            values = known["rsproxycn"]
        else:
            # Most chsrc Rust mirrors expose the traditional git index. Keep
            # the endpoint as-is; only rsproxy/upstream are known sparse URLs.
            values = {"cargoIndex": f"{url.rstrip('/')}/"}
        return {
            f"{prefix}.source": f"chsrc:{TARGET_SPECS[target]['chsrc']}/{code}",
            **{f"{prefix}.{name}": value for name, value in values.items()},
        }
    raise typer.BadParameter(f"unsupported mirror target: {target}")


def _target_overrides(values: dict[str, str], target: str) -> dict[str, str]:
    prefix = f"envy.mirrors.overrides.{target}."
    return {key: value for key, value in values.items() if key.startswith(prefix)}


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
    entries = list(mirror_entries(evaluated["mirrors"]))
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
    specs = probe_specs(evaluated["mirrors"])
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
        result = probe_endpoint(name, url, timeout=timeout)
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

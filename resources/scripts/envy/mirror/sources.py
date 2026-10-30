"""Mirror targets, chsrc source discovery, and the configured selection."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Any

import typer

from envy.config import read_machine_nix, read_mirror_overrides
from envy.mirror.cache import MirrorCache


SOURCE_CACHE_TTL = 30 * 24 * 60 * 60
STALE_MAX_AGE = 7 * 24 * 60 * 60
CHSRC_TIMEOUT = 75

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

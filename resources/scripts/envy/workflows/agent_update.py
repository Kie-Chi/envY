"""Independent update workflow for fast-moving coding agent runtimes."""

from __future__ import annotations

import copy
import json
import os
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any

from envy import log
from envy.journal import operation_stage, record_operation
from envy.process import prime_flake_sources, run_process
from envy.transaction import FileTransaction
from envy.utils import ENVY_ROOT
from envy.workflows.check import check_or_exit
from envy.workflows.update import update_inputs

AGENT_LOCK = ENVY_ROOT / "modules" / "agents" / "providers" / "agent-lock.json"
AGENT_NAMES = ("claude", "codex")
PLATFORMS = ("linux-arm64", "linux-x64")


def _read_lock() -> dict[str, Any]:
    try:
        value = json.loads(AGENT_LOCK.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read agent lock: {AGENT_LOCK}: {exc}") from None
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise RuntimeError("unsupported agent lock schema")
    if not isinstance(value.get("registry"), str) or not isinstance(value.get("agents"), dict):
        raise RuntimeError("invalid agent lock structure")
    for name in AGENT_NAMES:
        platforms = value["agents"].get(name, {}).get("platforms")
        if not isinstance(platforms, dict) or any(platform not in platforms for platform in PLATFORMS):
            raise RuntimeError(f"agent lock is missing {name} platform metadata")
    return value


def _get_json(registry: str, path: str) -> dict[str, Any]:
    url = f"{registry.rstrip('/')}/{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"cannot read {url}: {exc}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"registry returned a non-object for {path}")
    return value


def _package_metadata(registry: str, name: str, platform: str) -> dict[str, str]:
    if name == "claude":
        metadata = _get_json(registry, f"@anthropic-ai/claude-code-{platform}/latest")
        version = metadata.get("version")
        dist = metadata.get("dist")
    else:
        base = _get_json(registry, "@openai/codex/latest")
        version = base.get("version")
        if not isinstance(version, str):
            raise RuntimeError("Codex registry metadata has no version")
        metadata = _get_json(registry, f"@openai/codex/{version}-{platform}")
        dist = metadata.get("dist")
    if not isinstance(version, str) or not isinstance(dist, dict):
        raise RuntimeError(f"{name} registry metadata is incomplete for {platform}")
    url = dist.get("tarball")
    integrity = dist.get("integrity")
    if not isinstance(url, str) or not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise RuntimeError(f"{name} registry integrity is missing for {platform}")
    return {"version": version, "url": url, "integrity": integrity}


def resolve_agent(lock: dict[str, Any], name: str) -> dict[str, Any]:
    if name not in AGENT_NAMES:
        raise ValueError(f"unsupported fast agent: {name}")
    registry = lock["registry"]
    result = copy.deepcopy(lock["agents"])
    result[name] = {
        "platforms": {
            platform: _package_metadata(registry, name, platform)
            for platform in PLATFORMS
        }
    }
    return result


def _write_lock(lock: dict[str, Any]) -> None:
    temporary = AGENT_LOCK.with_name(f".{AGENT_LOCK.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    temporary.replace(AGENT_LOCK)


def _lock_fingerprint(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _update_lock(names: tuple[str, ...], *, include_pi: bool = False) -> dict[str, Any]:
    lock_file = AGENT_LOCK
    flake_lock = ENVY_ROOT / "flake.lock"
    paths = [lock_file, flake_lock] if include_pi else [lock_file]
    before = _lock_fingerprint(lock_file)
    log.step("agent", "resolving independent agent releases", agents=", ".join(names))
    operation_stage("update-agent-lock", "started", agents=list(names), lockBefore=before)
    with FileTransaction(paths) as transaction:
        lock = _read_lock()
        for name in names:
            lock["agents"] = resolve_agent(lock, name)
        _write_lock(lock)
        operation_stage("update-agent-lock", "ok", lockAfter=_lock_fingerprint(lock_file))
        if include_pi:
            operation_stage("update-pi-input", "started")
            run_process(["nix", "flake", "update", "pi"], cwd=ENVY_ROOT, check=True,
                        activity="Pi flake input update")
            prime_flake_sources(ENVY_ROOT)
            operation_stage("update-pi-input", "ok")
        operation_stage("validate", "started")
        check_or_exit(all_machines=True)
        operation_stage("validate", "ok")
        transaction.commit()
    log.ok("agent", "agent releases updated", agents=", ".join(names))
    return lock


@record_operation(
    "update-agents",
    detail=lambda target=None, **_: {"target": target or "all"},
)
def update_agent(target: str) -> dict[str, Any]:
    """Update one fast agent, Pi, amux, or all declarative agent sources."""
    if target in AGENT_NAMES:
        return {"target": target, "lock": _update_lock((target,))}
    if target == "pi":
        update_inputs("pi", validate=True)
        return {"target": target, "input": "pi"}
    if target == "all":
        return {"target": target, "lock": _update_lock(AGENT_NAMES, include_pi=True)}
    if target == "amux":
        run_process(["amux", "update", "all"], cwd=ENVY_ROOT, check=True,
                    activity="amux agent image update")
        return {"target": target, "runtime": "amux"}
    raise ValueError(f"unsupported agent update target: {target}")


def agent_status(*, refresh: bool = False) -> dict[str, Any]:
    lock = _read_lock()
    latest: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if refresh:
        for name in AGENT_NAMES:
            try:
                latest[name] = {
                    "platforms": {
                        platform: _package_metadata(lock["registry"], name, platform)
                        for platform in PLATFORMS
                    }
                }
            except RuntimeError as exc:
                errors[name] = str(exc)
    return {
        "lock": lock,
        "latest": latest,
        "errors": errors,
        "pi": {"input": "pi"},
        "amux": {"runtime": "amux"},
    }

"""Offline diagnostics for deployed envY-managed agent resources."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from envy.doctor.model import SECTION_AGENTS, CheckResult, error, info, ok, warn
from envy.doctor.policy import app_policy

NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
REGISTRATION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}\Z")


def _store_path(value: object, *, executable: bool = False, directory: bool = False) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("invalid store path")
    path = Path(value).resolve(strict=True)
    if not path.is_relative_to("/nix/store") or len(path.parts) < 4:
        raise ValueError("untrusted runtime path")
    if directory:
        if not path.is_dir():
            raise ValueError("missing runtime directory")
    elif not path.is_file() or (executable and not os.access(path, os.X_OK)):
        raise ValueError("missing runtime file")
    return path


def _json(path: Path) -> dict:
    if path.stat().st_size > 2_000_000:
        raise ValueError("oversized metadata")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid object")
    return data


def _metadata(path: Path) -> dict:
    source = _store_path(str(path))
    if source.stat().st_mode & 0o222:
        raise ValueError("mutable metadata")
    data = _json(source)
    if data.get("schemaVersion") != 1:
        raise ValueError("unsupported metadata schema")
    pi = data["pi"]
    if not isinstance(pi.get("enabled"), bool):
        raise ValueError("invalid policy flag")
    if not isinstance(pi.get("version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", pi["version"]):
        raise ValueError("invalid version")
    for key in ("node", "probe", "nodeModules"):
        pi[key] = str(_store_path(pi[key], executable=key == "node", directory=key == "nodeModules"))
    if Path(pi["node"]).name != "node" or Path(pi["probe"]).name != "doctor.mjs":
        raise ValueError("unexpected runtime executable")
    for kind in ("extensions", "roles", "skills"):
        entries = data[kind]
        if not isinstance(entries, list) or len(entries) > 256:
            raise ValueError("invalid resources")
        names = set()
        for entry in entries:
            name = entry["name"]
            if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
                raise ValueError("invalid resource name")
            if kind == "extensions" and not name.startswith("envy-"):
                raise ValueError("unmanaged extension")
            names.add(name)
            if not isinstance(entry.get("recursive", False), bool):
                raise ValueError("invalid recursive flag")
            target = entry["target"]
            prefix = {"extensions": ".pi/agent/extensions", "roles": ".pi/agent/agents", "skills": ".pi/agent/skills"}[kind]
            if not isinstance(target, str) or Path(target).is_absolute() or Path(target).parent != Path(prefix) or Path(target).name != name:
                raise ValueError("invalid resource target")
            entry["source"] = str(_store_path(entry["source"], directory=False)) if Path(entry["source"]).is_file() else str(_store_path(entry["source"], directory=True))
    servers = data["mcpServers"]
    if not isinstance(servers, dict) or len(servers) > 256:
        raise ValueError("invalid server metadata")
    for name, server in servers.items():
        if not NAME.fullmatch(name) or not isinstance(server, dict) or not isinstance(server.get("enabled"), bool) or not isinstance(server.get("command"), str) or not isinstance(server.get("args"), list):
            raise ValueError("invalid server metadata")
    return data


def _skill_metadata(path: Path) -> dict:
    source = _store_path(str(path))
    if source.stat().st_mode & 0o222:
        raise ValueError("mutable metadata")
    data = _json(source)
    if data.get("schemaVersion") != 1:
        raise ValueError("unsupported metadata schema")
    entries = data["skills"]
    if not isinstance(entries, list) or len(entries) > 512:
        raise ValueError("invalid skills")
    targets = set()
    for entry in entries:
        name = entry["name"]
        adapter = entry["adapter"]
        prefix = {"claude": ".claude/skills", "codex": ".codex/skills"}.get(adapter)
        if not isinstance(name, str) or not NAME.fullmatch(name) or prefix is None:
            raise ValueError("invalid skill identity")
        target = entry["target"]
        if target != f"{prefix}/{name}" or target in targets:
            raise ValueError("invalid skill target")
        targets.add(target)
        if not isinstance(entry.get("recursive", False), bool):
            raise ValueError("invalid recursive flag")
        entry["source"] = str(_store_path(entry["source"], directory=True))
    return data


def _skill_checks(home: Path) -> list[CheckResult]:
    metadata = home / ".config/envy/agent-skills.json"
    if not metadata.exists() and not metadata.is_symlink():
        return [warn(SECTION_AGENTS, "Agent skill metadata", "Managed skill metadata is not deployed",
                     hint="Run envy apply, then envy doctor agents")]
    try:
        data = _skill_metadata(metadata)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return [error(SECTION_AGENTS, "Agent skill metadata", "Managed skill metadata is invalid or not a read-only Nix store source")]
    results = [ok(SECTION_AGENTS, "Agent skill metadata", "Managed skill contract validated")]
    for entry in data["skills"]:
        link = home / entry["target"]
        source = Path(entry["source"])
        try:
            marker = source / "SKILL.md"
            matches = marker.is_file() and (
                (link / "SKILL.md").is_symlink()
                and (link / "SKILL.md").resolve(strict=True) == marker.resolve(strict=True)
                if entry.get("recursive") else
                link.is_symlink() and link.resolve(strict=True) == source
                and (link / "SKILL.md").is_file()
            )
        except (OSError, RuntimeError):
            matches = False
        label = {"claude": "Claude", "codex": "Codex"}[entry["adapter"]]
        results.append((ok if matches else error)(SECTION_AGENTS, f"{label} skills: {entry['name']}",
            "Managed link matches expected source" if matches else "Managed link is missing or has a different source"))
    return results


def _probe(data: dict) -> dict:
    """Launch only the validated store Node/probe, with no inherited credentials."""
    with tempfile.TemporaryDirectory(prefix="envy-agent-doctor-") as temporary:
        env = {"HOME": temporary, "TMPDIR": temporary, "PATH": str(Path(data["pi"]["node"]).parent),
               "XDG_CONFIG_HOME": temporary, "XDG_CACHE_HOME": temporary}
        completed = subprocess.run(
            [data["pi"]["node"], data["pi"]["probe"]],
            input=json.dumps(data), text=True, capture_output=True, timeout=45,
            cwd=temporary, env=env, check=False,
        )
    if completed.returncode not in (0, 1) or len(completed.stdout) > 2_000_000:
        raise ValueError("probe failed")
    result = json.loads(completed.stdout)
    if result.get("schemaVersion") != 1 or result.get("status") not in {"ok", "error"}:
        raise ValueError("invalid probe response")
    return result



def _executable(command: str, args: list) -> bool:
    """Inspect an executable and the supported env wrapper without running it."""
    resolved = shutil.which(os.path.expanduser(command))
    if not resolved:
        return False
    if Path(resolved).name != "env":
        return True
    index = 0
    while index < len(args):
        argument = args[index]
        if not isinstance(argument, str):
            return False
        if argument in {"-u", "--unset"}:
            if index + 1 >= len(args) or not isinstance(args[index + 1], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args[index + 1]):
                return False
            index += 2
        elif argument.startswith("--unset="):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", argument.removeprefix("--unset=")):
                return False
            index += 1
        elif argument == "--":
            index += 1
            break
        elif argument.startswith("-") or "=" in argument:
            return False
        else:
            break
    return index < len(args) and isinstance(args[index], str) and shutil.which(os.path.expanduser(args[index])) is not None

def run_checks() -> list[CheckResult]:
    results = list(_skill_checks(Path.home()))
    enabled, _ = app_policy(SimpleNamespace(casks=[], brews=[], packages=["pi"]))
    if not enabled:
        return results + [info(SECTION_AGENTS, "Pi runtime", "Pi is excluded by machine policy; agent checks skipped")]
    home = Path.home()
    agent = home / ".pi/agent"
    if (home / ".config/envy/agent-runtime/pi.env").exists():
        results.append(info(SECTION_AGENTS, "Pi runtime override", "Immediate runtime override exists; envy apply clears it"))
    metadata = agent / "envy-runtime.json"
    if not metadata.exists() and not metadata.is_symlink():
        return results + [warn(SECTION_AGENTS, "Pi metadata", "This runtime metadata version is not deployed", hint="Run envy apply, then envy doctor agents")]
    try:
        data = _metadata(metadata)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return results + [error(SECTION_AGENTS, "Pi metadata", "Runtime metadata is invalid or not a read-only Nix store source", hint="Run envy apply to restore managed metadata")]
    if not data["pi"]["enabled"]:
        return results + [info(SECTION_AGENTS, "Pi runtime", "Pi is excluded by deployed machine policy; agent checks skipped")]
    results.append(ok(SECTION_AGENTS, "Pi metadata", "Deployed runtime contract validated"))
    for kind in ("extensions", "roles", "skills"):
        for entry in data[kind]:
            link = home / entry["target"]
            try:
                if entry.get("recursive") and kind == "skills":
                    matches = (link / "SKILL.md").is_file() and (link / "SKILL.md").resolve(strict=True) == (Path(entry["source"]) / "SKILL.md").resolve(strict=True)
                else:
                    matches = link.is_symlink() and link.resolve(strict=True) == Path(entry["source"])
            except (OSError, RuntimeError):
                matches = False
            results.append((ok if matches else error)(SECTION_AGENTS, f"Pi {kind}: {entry['name']}",
                "Managed link matches expected source" if matches else "Managed link is missing or has a different source"))
    try:
        actual = _json(agent / "mcp.json").get("mcpServers", {}) if data["mcpServers"] else {}
        if not isinstance(actual, dict):
            raise ValueError("invalid servers")
        for name, expected in data["mcpServers"].items():
            server = actual.get(name)
            matches = isinstance(server, dict) and isinstance(server.get("enabled", True), bool) and server.get("enabled", True) == expected["enabled"] and server.get("command") == expected["command"] and server.get("args", []) == expected["args"]
            executable = not expected["enabled"] or (matches and _executable(server["command"], server.get("args", [])))
            results.append((ok if matches and executable else error)(SECTION_AGENTS, f"Pi MCP: {name}",
                "Managed server state and executable validated" if matches and executable else "Managed server differs or its command is unavailable"))
    except (OSError, ValueError, TypeError):
        results.append(error(SECTION_AGENTS, "Pi MCP", "Managed MCP configuration is missing or invalid"))
    try:
        probe = _probe(data)
        # Never render raw subprocess output, exception messages, or extension logs.
        if probe["status"] == "error":
            results.append(error(SECTION_AGENTS, "Pi extension discovery", "Offline extension probe failed"))
        else:
            results.append(ok(SECTION_AGENTS, "Pi extension discovery", "Offline Pi discovery completed"))
        if probe.get("hostVersion") != data["pi"]["version"]:
            results.append(error(SECTION_AGENTS, "Pi host version", "Host version differs from deployed metadata"))
        else:
            results.append(ok(SECTION_AGENTS, "Pi host version", f"Pi {data['pi']['version']} matches deployed metadata"))
        entries = probe.get("extensions", [])
        if not isinstance(entries, list) or len(entries) != len(data["extensions"]):
            raise ValueError("incomplete response")
        for expected, entry in zip(data["extensions"], entries):
            name = expected["name"]
            status = entry.get("status")
            factory = {"ok": ok, "skipped": info}.get(status, error)
            tools = entry.get("tools", [])
            commands = entry.get("commands", [])
            safe_names = lambda values: isinstance(values, list) and len(values) < 4096 and all(isinstance(v, str) and REGISTRATION.fullmatch(v) for v in values)
            if not safe_names(tools) or not safe_names(commands):
                raise ValueError("invalid registration names")
            results.append(factory(SECTION_AGENTS, f"Pi load: {name}",
                "Factory skipped to avoid cron daemon side effects" if status == "skipped" else "Factory skipped: host is below the static minimum" if status == "unsupported" else "Extension factory loaded" if status == "ok" else "Extension factory failed or exposed no entry points",
                tools=tools, commands=commands))
            guard = entry.get("guard", {})
            state = guard.get("status")
            minimum = guard.get("minimum")
            if state in {"ok", "error"} and isinstance(minimum, str) and re.fullmatch(r"\d+\.\d+\.\d+", minimum):
                results.append((ok if state == "ok" else error)(SECTION_AGENTS, f"Pi version guard: {name}", f"Static MIN_PI_VERSION {minimum}: {state}"))
            else:
                results.append(info(SECTION_AGENTS, f"Pi version guard: {name}", "No proven static minimum; compatibility is unknown"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        results.append(error(SECTION_AGENTS, "Pi extension probe", "Offline probe could not produce a valid result", hint="Run envy apply and retry envy doctor agents"))
    return results

"""Explicit native maintenance actions; storage scheduling never runs them."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from envy.process import run_process
from envy.utils import PLATFORM
from envy.storage.model import Policy, StorageError

ACTIONS = ("nix", "docker", "go", "uv", "npm", "pip", "brew")

COMMANDS = {
    "nix": [["nix-collect-garbage"]],
    "docker": [["docker", "builder", "prune", "-f", "--filter", "until=336h"],
               ["docker", "image", "prune", "-f", "--filter", "until=336h"]],
    "go": [["go", "clean", "-cache"]],
    "uv": [["uv", "cache", "prune"]],
    "npm": [["npm", "cache", "verify"]],
    "pip": [["pip", "cache", "purge"]],
    "brew": [["brew", "cleanup", "--prune=30"]],
}

NOTES = {
    "nix": "Collect unreferenced store paths; retain all generations and GC roots.",
    "docker": "Prune unused builder cache and dangling images older than 336h; retain volumes.",
    "go": "Remove the entire Go build cache, without an age cutoff; retain module source downloads.",
    "uv": "Prune unreachable cache objects using native in-use checks; no --force or --ci.",
    "npm": "Verify cache integrity and garbage-collect unneeded data; verify writes to the cache.",
    "pip": "Purge the entire pip cache, without an age cutoff; explicit maintenance only.",
    "brew": "Cleanup includes old installed kegs, stale locks and downloads, not just download cache. "
            "--prune=30 applies to cache files, not a uniform age cutoff for every object. "
            "Autoremove and auto-update are disabled.",
}

BREW_ENV = {"HOMEBREW_NO_AUTOREMOVE": "1", "HOMEBREW_NO_AUTO_UPDATE": "1"}


def _probe(argv: list[str], timeout: int = 20, *, env: dict[str, str] | None = None):
    return run_process(argv, capture=True, check=False, timeout=timeout, env=env)


def _pip_environment() -> dict[str, str]:
    """Keep the standalone pip interpreter independent of envY's Python."""
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    if PLATFORM == "darwin":
        # Apple's pip uses xcrun; the Nix developer directory selects Nix Python.
        environment.pop("DEVELOPER_DIR", None)
    return environment


def _failure(returncode: int) -> str:
    if returncode == 124:
        return "command timed out"
    if returncode == 127:
        return "command is not available"
    return f"command failed with exit code {returncode}"


def _docker_ready() -> tuple[bool, str]:
    try:
        result = _probe(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    except subprocess.TimeoutExpired:
        return False, "Docker engine probe timed out; nothing was pruned"
    except OSError as exc:
        return False, f"Docker engine probe could not start: {exc}"
    if result.returncode != 0:
        return False, f"Docker engine is unavailable ({_failure(result.returncode)}); nothing was pruned"
    return True, ""


def _pip_command() -> tuple[str | None, str]:
    failures = []
    for executable in ("pip", "pip3"):
        if shutil.which(executable) is None:
            failures.append(f"{executable} command is not available")
            continue
        try:
            result = _probe([executable, "cache", "--help"], env=_pip_environment())
        except subprocess.TimeoutExpired:
            failures.append(f"{executable} cache help timed out")
            continue
        except OSError as exc:
            failures.append(f"{executable} cache help could not start: {exc}")
            continue
        if result.returncode != 0:
            failures.append(f"{executable} cache help: {_failure(result.returncode)}")
        elif "purge" not in (result.stdout or ""):
            failures.append(f"{executable} does not advertise cache purge support")
        else:
            return executable, ""
    return None, "; ".join(failures)


def preview(policy: Policy, actions: list[str] | None = None) -> dict:
    selected = list(dict.fromkeys(actions or policy.maintenance))
    if not selected:
        raise StorageError("No maintenance action is enabled by machine policy")
    # Validate the entire selection before even performing read-only probes.
    for action in selected:
        if action not in ACTIONS:
            raise StorageError("Unknown maintenance action: " + action)
        if action not in policy.maintenance:
            raise StorageError("Maintenance action is not enabled by machine policy: " + action)
    items = []
    for action in selected:
        commands = [list(command) for command in COMMANDS[action]]
        reason = ""
        if action == "pip":
            executable, reason = _pip_command()
            if executable:
                commands[0][0] = executable
        elif shutil.which(commands[0][0]) is None:
            reason = commands[0][0] + " command is not available"
        elif action == "docker":
            _, reason = _docker_ready()
        if action == "nix" and PLATFORM == "darwin":
            commands = [["sudo", "-n", "-H", *command] for command in commands]
            if not reason and shutil.which("sudo") is None:
                reason = "sudo command is not available"
        note = NOTES[action] + " Preview only lists commands; no cleanup command was run."
        if action == "nix" and PLATFORM == "darwin":
            note += " Execution requires an existing sudo ticket; no password is read or prompted for."
        item = {"action": action, "available": not reason, "commands": commands,
                "note": note + (" " + reason if reason else "")}
        if action == "brew":
            item["environment"] = dict(BREW_ENV)
        items.append(item)
    return {"execute": False, "items": items}


def maintain(policy: Policy, actions: list[str] | None = None) -> dict:
    """Execute opted-in actions; the CLI calls this only with explicit --yes."""
    data = preview(policy, actions)
    for item in data["items"]:
        if not item["available"]:
            continue
        item["note"] = NOTES[item["action"]]
        item["output"] = []
        for argv in item["commands"]:
            kwargs = {"capture": True, "check": False, "timeout": 1800}
            if item["action"] == "brew":
                kwargs["env"] = dict(os.environ, **BREW_ENV)
            elif item["action"] == "pip":
                kwargs["env"] = _pip_environment()
            try:
                result = run_process(argv, **kwargs)
                code = result.returncode
                reason = _failure(code) if code else ""
            except subprocess.TimeoutExpired:
                code, reason = 124, "command timed out"
            except OSError as exc:
                code, reason = 127, f"command could not start: {exc}"
            item["output"].append({"command": shlex.join(argv), "returncode": code})
            if code != 0:
                item["available"] = False
                item["note"] += f" {shlex.join(argv)}: {reason}; remaining steps were skipped."
                break
    data["execute"] = True
    data["errors"] = [item["action"] for item in data["items"] if not item["available"]]
    return data

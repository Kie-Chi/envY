"""Explicit native maintenance actions; storage scheduling never runs them."""

from __future__ import annotations

import shutil

from envy.process import run_process
from envy.utils import PLATFORM, esudo
from envy.storage.model import Policy, StorageError

ACTIONS = ("nix", "docker", "go")

COMMANDS = {
    "nix": [["nix-collect-garbage"]],
    "docker": [["docker", "builder", "prune", "-f", "--filter", "until=336h"],
               ["docker", "image", "prune", "-f"]],
    "go": [["go", "clean", "-cache"]],
}


def _probe(argv: list[str], timeout: int = 20):
    return run_process(argv, capture=True, check=False, timeout=timeout)


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker command is not available"
    try:
        result = _probe(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    except OSError as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, "Docker engine is not answering; nothing was pruned"
    return True, ""
    return True, ""


def _nix_dead_paths() -> int | None:
    if shutil.which("nix-store") is None:
        return None
    result = _probe(["nix-store", "--gc", "--print-dead"], timeout=180)
    if result.returncode != 0:
        return None
    return len([line for line in (result.stdout or "").splitlines() if line.strip()])


def preview(policy: Policy, actions: list[str] | None = None) -> dict:
    selected = actions or list(policy.maintenance)
    if not selected:
        raise StorageError("No maintenance action is enabled by machine policy")
    items = []
    for action in selected:
        if action not in policy.maintenance:
            raise StorageError("Maintenance action is not enabled by machine policy: " + action)
        item = {"action": action, "available": True, "commands": COMMANDS[action], "note": ""}
        if action == "docker":
            ready, reason = _docker_ready()
            item["available"] = ready
            item["note"] = reason
        elif action == "go" and shutil.which("go") is None:
            item["available"] = False
            item["note"] = "go command is not available"
        elif action == "nix":
            dead = _nix_dead_paths()
            item["note"] = ("unreferenced store paths: " + str(dead)) if dead is not None                 else "could not count unreferenced store paths"
        items.append(item)
    return {"execute": False, "items": items}


def maintain(policy: Policy, actions: list[str] | None = None) -> dict:
    data = preview(policy, actions)
    for item in data["items"]:
        if not item["available"]:
            continue
        item["output"] = []
        for command in item["commands"]:
            argv = command
            if item["action"] == "nix" and PLATFORM == "darwin":
                result = esudo("-H", *argv, capture=True)
            else:
                result = run_process(argv, capture=True, check=False, timeout=1800)
            item["output"].append({"command": " ".join(argv), "returncode": result.returncode})
            if result.returncode != 0:
                item["available"] = False
                item["note"] = "command failed; remaining steps were skipped"
                break
    data["execute"] = True
    data["errors"] = [item["action"] for item in data["items"] if not item["available"]]
    return data

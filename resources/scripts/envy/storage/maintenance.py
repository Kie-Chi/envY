"""Explicit native maintenance actions; storage scheduling never runs them."""

from __future__ import annotations

import copy
import io
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Callable

from envy.process import CommandError, run_process
from envy.utils import PLATFORM, esudo
from envy.storage.model import Policy, StorageError
from envy.storage.output import NativeOutput, sanitize_output
from envy.storage import docker, native_context

COMMANDS = {
    "nix": [["nix-collect-garbage"]],
    "docker": [["docker", "builder", "prune", "-f", "--filter", "until=336h"],
               ["docker", "image", "prune", "-f", "--filter", "until=336h"]],
    "go": [["go", "clean", "-cache"]],
    "go-modcache": [["go", "clean", "-modcache"]],
    "uv": [["uv", "cache", "prune"]],
    "npm": [["npm", "cache", "verify"]],
    "pip": [["pip", "cache", "purge"]],
    "brew": [["brew", "cleanup", "--prune=30"]],
}

ACTIONS = tuple(COMMANDS)

NOTES = {
    "nix": "Collect unreferenced store paths; retain all generations and GC roots.",
    "docker": "Prune unused builder cache and dangling images older than 336h; retain volumes.",
    "go": "Remove the entire Go build cache, without an age cutoff; retain module source downloads.",
    "go-modcache": "Remove the entire Go module download cache without an age cutoff; "
                   "future builds may require network access to download dependencies again.",
    "uv": "Prune unreachable cache objects using native in-use checks; no --force or --ci.",
    "npm": "Verify cache integrity and garbage-collect unneeded data; verify writes to the cache.",
    "pip": "Purge the entire pip cache, without an age cutoff; explicit maintenance only.",
    "brew": "Cleanup includes old installed kegs, stale locks and downloads, not just download cache. "
            "--prune=30 applies to cache files, not a uniform age cutoff for every object. "
            "Autoremove and auto-update are disabled.",
}

BREW_ENV = {"HOMEBREW_NO_AUTOREMOVE": "1", "HOMEBREW_NO_AUTO_UPDATE": "1"}


def action_metadata(enabled: tuple[str, ...]) -> list[dict]:
    """Describe policy opt-ins without probing tools, targets or cache contents."""
    return [{"id": name, "flag": "--" + name,
             "policyKey": "envy.storage.maintenance." + name,
             "policyDefault": False, "policyEnabled": name in enabled,
             "requiresPolicyOptIn": True, "scheduled": False,
             "preview": {"supported": True, "writes": False,
                         "probes": name in {"docker", "go-modcache", "pip"}},
             "sideEffects": NOTES[name]}
            for name in ACTIONS]


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


def _selection(policy: Policy, actions: list[str] | None) -> list[str]:
    selected = list(dict.fromkeys(actions or policy.maintenance))
    if not selected:
        raise StorageError("No maintenance action is enabled by machine policy")
    # Validate the entire selection before even performing read-only probes.
    for action in selected:
        if action not in ACTIONS:
            raise StorageError("Unknown maintenance action: " + action)
        if action not in policy.maintenance:
            raise StorageError("Maintenance action is not enabled by machine policy: " + action)
    return selected


def preview(policy: Policy, actions: list[str] | None = None, *, bind_targets: bool = False) -> dict:
    selected = _selection(policy, actions)
    if bind_targets:
        return _bound_preview(policy, selected)
    items = []
    for action in selected:
        if action == "go-modcache" or (action == "nix" and PLATFORM == "darwin"):
            items.extend(_bound_preview(policy, [action])["items"])
            continue
        commands = [list(command) for command in COMMANDS[action]]
        reason = ""
        details = {}
        if action == "pip":
            executable, reason = _pip_command()
            if executable:
                commands[0][0] = executable
        elif shutil.which(commands[0][0]) is None:
            reason = commands[0][0] + " command is not available"
        elif action == "docker":
            details["inspection"] = docker.inspect()
            try:
                details["target"] = docker.maintenance_target()
                commands = docker.bound_commands(details["target"])
            except StorageError as exc:
                reason = str(exc)
        note = NOTES[action] + " Read-only preview; no cleanup command was run."
        item = {"action": action, "available": not reason, "commands": commands,
                "note": note + (" " + reason if reason else ""), **details}
        if action == "brew":
            item["environment"] = dict(BREW_ENV)
        items.append(item)
    return {"execute": False, "items": items}


_REPORT_TAIL_BYTES = 64 * 1024
_SIZE = r"(?P<size>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>bytes?|[kKMGT]i?B|B)"
_REPORT_PATTERNS = {
    "nix": rf"[0-9]+ store paths deleted, {_SIZE} freed",
    "docker": rf"Total reclaimed space:\s*{_SIZE}",
    "uv": rf"(?:Removed|Pruned) [0-9]+ (?:files?|directories|entries|packages?) \({_SIZE}\)",
    "brew": rf"(?:==> )?This operation has freed approximately {_SIZE} of disk space\.",
    "pip": rf"Files removed: [0-9]+ \({_SIZE}\)",
    "npm": rf"Content garbage-collected: [0-9]+ \({_SIZE}\)",
}


def _native_failure(action: str, returncode: int, current: dict, *, error_output=None,
                    show_output: bool = False) -> str:
    """Describe a failure with sanitized details or bound diagnostic commands."""
    cause = _failure(returncode)
    message = raw_message = ""
    if error_output is not None and returncode not in {124, 127}:
        try:
            end = error_output.seek(0, os.SEEK_END)
            error_output.seek(max(0, end - _REPORT_TAIL_BYTES))
            raw_message = error_output.read(_REPORT_TAIL_BYTES).decode("utf-8", errors="replace")
            message = raw_message.casefold()
        except OSError:
            message = ""
        for markers, label in (
            (("permission denied", "eacces", "operation not permitted"), "permission denied"),
            (("no space left on device", "enospc"), "no space left on device"),
            (("read-only file system", "erofs"), "read-only filesystem"),
        ):
            if any(marker in message for marker in markers):
                cause = f"{label} (exit code {returncode})"
                break
        if show_output:
            lines = sanitize_output(raw_message).splitlines()
            lines = [line.strip() for line in lines if line.strip()]
            if lines:
                errors = [line for line in lines if re.search(r"(?i)\b(error|fatal|failed|denied)\b", line)]
                detail = (errors[0] if errors else lines[-1])[:500]
                return f"{detail} (exit code {returncode})"
    target = current.get("target", {})
    binary = current.get("executable", {}).get("path")
    if returncode == 127 and binary:
        return cause + "; check the executable: " + shlex.join(["ls", "-l", "--", binary])
    if action == "docker" and binary and target.get("endpoint"):
        command = [binary, "--host", target["endpoint"], "info"]
        return cause + "; check the bound daemon: " + shlex.join(command)
    keys = (("storeDir", "stateDir") if action == "nix"
            else ("cache", "cellar", "locks") if action == "brew" else ("resolvedPath", "path"))
    paths = list(dict.fromkeys(target[key] for key in keys if target.get(key)))
    if paths:
        if cause.startswith("no space left on device"):
            return cause + "; check free space: " + shlex.join(["df", "-h", paths[0]])
        return cause + "; inspect target ownership and access: " + shlex.join(["ls", "-ld", "--", *paths])
    return cause + "; run envy storage prune --dry-run to inspect the " + action + " target"


def _activity(action: str, step: int) -> str:
    if action == "docker":
        return "Docker build cache" if step == 0 else "Docker images"
    return {"nix": "Nix GC", "go": "Go build cache", "go-modcache": "Go module cache",
            "uv": "uv cache", "npm": "npm cache", "pip": "pip cache", "brew": "Homebrew cleanup"}[action]


def _reported_removed(action: str, handle) -> int | None:
    patterns = {
        "nix": rf"(?P<count>[0-9]+) store paths deleted, {_SIZE} freed",
        "uv": rf"(?:Removed|Pruned) (?P<count>[0-9]+) (?:files?|director(?:y|ies)|entries|packages?) \({_SIZE}\)",
        "pip": rf"Files removed: (?P<count>[0-9]+)(?: \({_SIZE}\))?",
        "npm": rf"Content garbage-collected: (?P<count>[0-9]+) \({_SIZE}\)",
    }
    pattern = patterns.get(action)
    if pattern is None:
        return None
    end = handle.seek(0, os.SEEK_END)
    start = max(0, end - _REPORT_TAIL_BYTES)
    handle.seek(start)
    lines = handle.read(_REPORT_TAIL_BYTES).decode("utf-8", errors="replace").splitlines()
    if start:
        lines = lines[1:]
    values = [int(match["count"]) for line in lines
              if (match := re.fullmatch(pattern, line.strip())) and len(match["count"]) <= 20]
    return values[0] if len(values) == 1 and values[0] <= 2**64 - 1 else None


def _reported_reclaimed(action: str, handle) -> int | None:
    """Read only a bounded tail and retain only an unambiguous native byte count."""
    pattern = _REPORT_PATTERNS.get(action)
    if pattern is None:
        return None
    try:
        end = handle.seek(0, os.SEEK_END)
        start = max(0, end - _REPORT_TAIL_BYTES)
        handle.seek(start)
        tail = handle.read(_REPORT_TAIL_BYTES)
    except OSError:
        return None
    # A truncated first line cannot prove a complete summary.
    lines = tail.decode("utf-8", errors="replace").splitlines()
    if start:
        lines = lines[1:]
    values = []
    for line in lines:
        match = re.fullmatch(pattern, line.strip())
        if match:
            unit = match["unit"]
            exponent = "BKMGT".index(unit[0].upper()) if unit[0].upper() in "KMGT" else 0
            multiplier = (1024 if "i" in unit else 1000) ** exponent
            amount = Decimal(match["size"]) * multiplier
            if amount > 2**64 - 1:
                return None
            values.append(int(amount))
    return values[0] if len(values) == 1 else None


def maintain(policy: Policy, actions: list[str] | None = None, *, approved_preview: dict | None = None,
             report_reclaimed: bool = False,
             on_progress: Callable[[dict], None] | None = None,
             output_dir: Path | None = None,
             on_output: Callable[[str, str, str], None] | None = None) -> dict:
    """Execute opted-in actions; approval receipts and locking belong to the caller."""
    if approved_preview is not None:
        return _maintain_approved(policy, _selection(policy, actions), approved_preview,
                                  report_reclaimed=report_reclaimed, on_progress=on_progress,
                                  output_dir=output_dir, on_output=on_output)
    data = preview(policy, actions)
    for index, item in enumerate(data["items"]):
        if not item["available"]:
            continue
        if item["action"] == "go-modcache" or (item["action"] == "nix" and PLATFORM == "darwin"):
            approved = {"execute": False, "items": [item]}
            data["items"][index] = _maintain_approved(policy, [item["action"]], approved)["items"][0]
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
                if item["action"] == "docker":
                    docker.verify_target(item["target"])
                    kwargs["env"] = docker.bound_environment()
                result = run_process(argv, **kwargs)
                code = result.returncode
                reason = _failure(code) if code else ""
            except StorageError as exc:
                code, reason = 1, str(exc)
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


def _bound_preview(policy: Policy, selected: list[str]) -> dict:
    items = []
    for action in selected:
        item = {"action": action, "available": False, "commands": [], "note": NOTES[action]}
        try:
            binding = native_context.capture(action, policy, platform=PLATFORM)
            item.update(available=True, binding=binding, target=binding["target"],
                        commands=native_context.commands(action, binding, platform=PLATFORM))
        except StorageError as exc:
            item["reason"] = str(exc)
        except (OSError, ValueError, TypeError, KeyError):
            item["reason"] = "Native target is unavailable"
        if action == "brew":
            item["environment"] = dict(BREW_ENV)
        items.append(item)
    return {"execute": False, "items": items}


def _maintain_approved(policy: Policy, selected: list[str], approved: dict, *,
                       report_reclaimed: bool = False,
                       on_progress: Callable[[dict], None] | None = None,
                       output_dir: Path | None = None,
                       on_output: Callable[[str, str, str], None] | None = None) -> dict:
    """Revalidate every step and execute independent actions separately."""
    if (not isinstance(approved, dict) or approved.get("execute") is not False
            or not isinstance(approved.get("items"), list)):
        raise StorageError("A bound maintenance preview is required")
    items = approved["items"]
    if (any(not isinstance(item, dict) for item in items)
            or [item.get("action") for item in items] != selected):
        raise StorageError("Approved maintenance selection changed; generate a fresh preview")
    for item in items:
        if type(item.get("available")) is not bool:
            raise StorageError("Approved maintenance availability is invalid")
        binding = item.get("binding")
        if item["available"] and (not isinstance(binding, dict)
                                  or binding.get("action") != item["action"]
                                  or binding.get("version") != native_context.VERSION
                                  or any(not isinstance(binding.get(key), dict)
                                         for key in ("target", "cwd", "executable", "scope"))
                                  or not isinstance(binding.get("roots"), list)):
            raise StorageError("Approved maintenance target is missing; generate a fresh preview")
    data = copy.deepcopy(approved)
    for item in data["items"]:
        item["status"] = "pending" if item["available"] else "unavailable"
        if not item["available"]:
            item.setdefault("reason", item.get("note", "Native tool unavailable"))
    if on_progress is not None:
        data["execute"] = True
        on_progress(data)
    for item in data["items"]:
        if report_reclaimed:
            item["reportedReclaimedBytes"] = None
        if not item["available"]:
            continue
        action = item["action"]
        item["note"] = NOTES[action]
        item["output"] = []
        for step in range(2 if action == "docker" else 1):
            argv = None
            reclaimed = removed = None
            code, reason = 0, ""
            try:
                current = native_context.verify(action, item["binding"], policy, platform=PLATFORM)
                argv = native_context.commands(action, current, platform=PLATFORM)[step]
                environment = native_context.execution_environment(action, current, platform=PLATFORM)
            except StorageError as exc:
                code, reason = 1, str(exc)
            except subprocess.TimeoutExpired:
                code, reason = 124, "command timed out"
            except (OSError, ValueError, TypeError, KeyError, IndexError):
                code, reason = 1, "Native target or command is unavailable; run the command again"
            output = {"returncode": None}
            if argv is not None:
                output["command"] = shlex.join(argv)
            item["output"].append(output)
            if not code and action == "nix" and PLATFORM == "darwin":
                item["status"] = "running"
                output.update(state="running", phase="authentication")
                if on_progress is not None:
                    on_progress(data)
                try:
                    esudo("-v", capture=True, cwd=Path(current["cwd"]["path"]),
                          sudo_binary=current["target"]["sudoExecutable"]["path"], non_interactive=True)
                    # Authentication can wait; bind the provider again before cleanup.
                    current = native_context.verify(action, item["binding"], policy, platform=PLATFORM)
                    argv = native_context.commands(action, current, platform=PLATFORM)[step]
                    environment = native_context.execution_environment(action, current, platform=PLATFORM)
                    output["command"] = shlex.join(argv)
                except CommandError as exc:
                    code, reason = exc.returncode, "sudo authentication failed; check the stored password or run sudo -v"
                except StorageError as exc:
                    code, reason = 1, str(exc)
                except (OSError, ValueError, TypeError, KeyError):
                    code, reason = 1, "sudo authentication is unavailable"
            if not code:
                item["status"] = "running"
                output["state"] = "running"
                if action == "nix" and PLATFORM == "darwin":
                    output["phase"] = "command"
                if on_progress is not None:
                    on_progress(data)
                try:
                    if output_dir is not None or on_output is not None:
                        with NativeOutput(output_dir, action, step, on_output) as captured:
                            output["logs"] = captured.metadata
                            if on_progress is not None:
                                on_progress(data)
                            try:
                                if on_output is not None:
                                    on_output(action, "start", _activity(action, step))
                                result = run_process(argv, capture=False, check=False, timeout=1800,
                                                     stdout=captured.stdout, stderr=captured.stderr,
                                                     env=environment, cwd=current["cwd"]["path"],
                                                     activity=_activity(action, step))
                            finally:
                                captured.finish()
                                output["logs"] = captured.metadata
                                if on_progress is not None:
                                    on_progress(data)
                            if result.returncode == 0 and report_reclaimed:
                                primary, secondary = ((captured.stderr, captured.stdout) if action in {"nix", "uv"}
                                                      else (captured.stdout, captured.stderr))
                                reclaimed = _reported_reclaimed(action, primary)
                                if reclaimed is None:
                                    reclaimed = _reported_reclaimed(action, secondary)
                                removed = _reported_removed(action, primary)
                                if removed is None:
                                    removed = _reported_removed(action, secondary)
                            elif result.returncode:
                                reason = _native_failure(action, result.returncode, current,
                                                         error_output=io.BytesIO(captured.tail("stderr") or captured.tail("stdout")),
                                                         show_output=True)
                    elif report_reclaimed:
                        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                            result = run_process(argv, capture=False, check=False, timeout=1800,
                                                 stdout=stdout, stderr=stderr,
                                                 env=environment, cwd=current["cwd"]["path"])
                            if result.returncode == 0:
                                reclaimed = _reported_reclaimed(action, stderr if action in {"nix", "uv"} else stdout)
                            else:
                                reason = _native_failure(action, result.returncode, current, error_output=stderr)
                    else:
                        result = run_process(argv, capture=False, check=False, timeout=1800,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             env=environment, cwd=current["cwd"]["path"])
                    code = result.returncode
                    if code and not reason:
                        reason = _native_failure(action, code, current)
                except StorageError as exc:
                    code, reason = 1, str(exc)
                except subprocess.TimeoutExpired:
                    code, reason = 124, "command timed out"
                except (OSError, ValueError, TypeError, KeyError, IndexError):
                    code, reason = 1, "Native target or command is unavailable; run the command again"
            output.update(returncode=code, state="failed" if code else "completed")
            if report_reclaimed:
                output["reportedReclaimedBytes"] = reclaimed if code == 0 else None
                if removed is not None and code == 0:
                    output["reportedRemovedItems"] = removed
            if code:
                item.update(available=False, status="failed", reason=reason)
            else:
                item["status"] = "running" if action == "docker" and step == 0 else "completed"
            if on_progress is not None:
                on_progress(data)
            if code:
                break
        if report_reclaimed and item["available"]:
            reports = [output["reportedReclaimedBytes"] for output in item["output"]]
            if reports and all(value is not None for value in reports):
                item["reportedReclaimedBytes"] = sum(reports)
            removed_reports = [output.get("reportedRemovedItems") for output in item["output"]]
            if removed_reports and all(value is not None for value in removed_reports):
                item["reportedRemovedItems"] = sum(removed_reports)
    data["execute"] = True
    data["errors"] = [item["action"] for item in data["items"] if not item["available"]]
    if on_progress is not None:
        on_progress(data)
    return data

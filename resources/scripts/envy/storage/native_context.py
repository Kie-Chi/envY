"""Read-only native target evidence and fixed command rendering for approvals."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from envy.storage import docker, go_modcache
from envy.storage.measurement import ScanBudget
from envy.storage.model import Policy, StorageError, absolute_path, inside, is_protected, no_symlinks
from envy.storage.reporting import probe

VERSION = 1
BREW_ENV = {"HOMEBREW_NO_AUTOREMOVE": "1", "HOMEBREW_NO_AUTO_UPDATE": "1"}
TOOLS = {"nix": "nix-store", "docker": "docker", "go": "go",
         "go-modcache": "go", "uv": "uv", "npm": "npm", "pip": "pip", "brew": "brew"}
PROJECT_FILES = {
    "go": ("go.mod", "go.work"), "go-modcache": ("go.mod", "go.work"),
    "uv": ("uv.toml", "pyproject.toml", ".python-version"),
    "npm": (".npmrc", "package.json"), "pip": ("pip.conf", "pyproject.toml"),
}
SELECTORS = {
    "nix": ("NIX_",), "docker": ("DOCKER_", "BUILDX_", "BUILDKIT_"),
    "go": ("GO",), "go-modcache": ("GO",), "uv": ("UV_",),
    "npm": ("npm_config_", "NPM_CONFIG_"), "pip": ("PIP_", "VIRTUAL_ENV"),
    "brew": ("HOMEBREW_",),
}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _metadata(path: Path) -> dict:
    """Record identity only; never open private configuration contents."""
    try:
        info = path.stat()
        link = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    return {"path": str(path), "resolvedPath": str(path.resolve()), "exists": True,
            "identity": [info.st_dev, info.st_ino, info.st_mode, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns],
            "linkIdentity": [link.st_dev, link.st_ino, link.st_mode,
                             link.st_size, link.st_mtime_ns, link.st_ctime_ns]}


def _directory(path: Path) -> dict:
    no_symlinks(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise StorageError("Native working directory is unavailable")
    return {"path": str(path), "identity": [info.st_dev, info.st_ino, info.st_mode]}


def executable(name: str) -> dict:
    value = shutil.which(name)
    if not value:
        raise StorageError(name + " command is not available")
    path = absolute_path(value)
    data = _metadata(path)
    if not data["exists"] or not path.is_file() or not os.access(path, os.X_OK):
        raise StorageError("Native executable identity is unavailable")
    return data


def pip_environment(platform: str) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    if platform == "darwin":
        environment.pop("DEVELOPER_DIR", None)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def _environment(action: str, platform: str) -> dict[str, str]:
    if action == "pip":
        return pip_environment(platform)
    if action == "docker":
        return docker.bound_environment()
    if action == "brew":
        return dict(os.environ, **BREW_ENV)
    if action in {"go", "go-modcache"}:
        return dict(os.environ, GOTOOLCHAIN="local")
    if action == "npm":
        return dict(os.environ, npm_config_update_notifier="false", NPM_CONFIG_UPDATE_NOTIFIER="false")
    return dict(os.environ)


def _query(binary: str, args: list[str], *, environment: dict, cwd: Path,
           budget: ScanBudget) -> str:
    result = probe([binary, *args], env=environment, cwd=cwd, budget=budget,
                   timeout=5, max_bytes=131_072)
    if result.returncode:
        reason = {124: "deadline exhausted", 125: "output limit exceeded",
                  127: "command unavailable"}.get(result.returncode, "query failed")
        raise StorageError("Native target metadata " + reason)
    return result.stdout.strip()


def _json_query(binary, args, **kwargs) -> dict:
    try:
        value = json.loads(_query(binary, args, **kwargs))
    except (ValueError, RecursionError) as exc:
        raise StorageError("Native target metadata is invalid") from exc
    if not isinstance(value, dict):
        raise StorageError("Native target metadata must be an object")
    return value


def _path(value: str, policy: Policy, cwd: Path, *, store: bool = False) -> str:
    if (not isinstance(value, str) or len(value.splitlines()) != 1
            or value.startswith("//") or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise StorageError("Native target must be one absolute directory")
    path = absolute_path(value)
    no_symlinks(path)
    resolved = path.resolve()
    broad = {Path("/"), Path("/tmp"), Path("/var"), Path("/var/tmp"), Path("/usr"),
             Path("/usr/local"), Path("/opt"), Path("/nix"), Path("/Users"), Path("/home"),
             policy.home.resolve(), policy.home / "Library", policy.home / ".cache"}
    native_home = absolute_path(os.environ.get("HOME", str(policy.home))).resolve()
    broad.update({native_home, native_home / "Library", native_home / ".cache"})
    if (resolved in broad or inside(policy.home.resolve(), resolved)
            or len(resolved.parts) < 3 or inside(cwd, resolved) or is_protected(resolved, policy)):
        raise StorageError("Native target is broad, protected, home or project scope")
    if not store and any(inside(resolved, Path(root)) for root in (
            "/etc", "/System", "/bin", "/sbin", "/dev", "/proc", "/sys", "/nix/store")):
        raise StorageError("Native cache target is a protected system directory")
    if not store and resolved == Path("/nix/var/nix"):
        raise StorageError("Native cache target cannot be a Nix store")
    if path.exists() and not path.is_dir():
        raise StorageError("Native target is not a directory")
    return str(resolved)


def _root_identity(value: str) -> dict:
    path = absolute_path(value)
    no_symlinks(path)
    parent = path
    while not parent.exists():
        parent = parent.parent
    info = parent.stat()
    return {"path": value, "existingAncestor": str(parent),
            "identity": [info.st_dev, info.st_ino, info.st_mode]}


def _scope(action: str, policy: Policy, cwd: Path, environment: dict,
           extra: list[str]) -> dict:
    # Hash only manager selectors, never a complete environment snapshot.
    selectors = {key: value for key, value in environment.items()
                 if key.startswith(SELECTORS[action]) or key in {
                     "HOME", "PATH", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS",
                     "XDG_CACHE_HOME", "XDG_DATA_HOME"}}
    paths = []
    for parent in (cwd, *cwd.parents):
        paths.extend(parent / name for name in PROJECT_FILES.get(action, ()))
    native_home = absolute_path(environment.get("HOME", str(policy.home)))
    config_home = absolute_path(environment.get("XDG_CONFIG_HOME", str(native_home / ".config")))
    configs = {
        "go": [config_home / "go/env"], "go-modcache": [config_home / "go/env"],
        "uv": [config_home / "uv/uv.toml", Path("/etc/uv/uv.toml")],
        "npm": [native_home / ".npmrc", Path("/etc/npmrc")],
        "pip": [native_home / ".pip/pip.conf", config_home / "pip/pip.conf",
                Path("/etc/pip.conf"), Path("/etc/xdg/pip/pip.conf")],
        "nix": [Path("/etc/nix/nix.conf"), config_home / "nix/nix.conf"],
        "docker": [native_home / ".docker/config.json"],
    }
    paths.extend(configs.get(action, []))
    if action == "pip":
        for root in environment.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":"):
            if root:
                paths.append(absolute_path(root) / "pip/pip.conf")
    for value in extra:
        if value and value not in {"off", os.devnull}:
            paths.append(absolute_path(value))
    return {"selectorsDigest": _digest(selectors),
            "files": [_metadata(path) for path in sorted(set(paths))]}


def capture(action: str, policy: Policy, *, platform: str) -> dict:
    """Capture real manager metadata under the execution environment, without cleanup."""
    if action not in TOOLS:
        raise StorageError("Unknown native maintenance action")
    cwd = Path.cwd()
    directory = _directory(cwd)
    environment = _environment(action, platform)
    budget = ScanBudget()
    extra = []
    binary = None
    if action == "pip":
        for name in ("pip", "pip3"):
            try:
                candidate = executable(name)
                help_text = _query(candidate["path"], ["cache", "--help"],
                                   environment=environment, cwd=cwd, budget=budget)
                if "purge" in help_text:
                    binary = candidate
                    break
            except StorageError:
                continue
        if binary is None:
            raise StorageError("pip cache purge support is unavailable")
    else:
        binary = executable(TOOLS[action])
    query = {"environment": environment, "cwd": cwd, "budget": budget}
    command = binary["path"]
    if action == "docker":
        target = docker.maintenance_target(budget=budget)
        extra.append(environment.get("DOCKER_CONFIG", str(policy.home / ".docker")) + "/config.json")
    elif action == "go-modcache":
        target = go_modcache.target(home=policy.home, cwd=cwd, env=environment, binary=command)
        _path(target["resolvedPath"], policy, cwd)
        no_symlinks(absolute_path(target["path"]))
        values = _json_query(command, ["env", "-json", "GOENV", "GOMOD", "GOWORK"], **query)
        extra.extend(values.get(name, "") for name in ("GOENV", "GOMOD", "GOWORK"))
    elif action == "go":
        values = _json_query(command, ["env", "-json", "GOCACHE", "GOENV", "GOMOD", "GOWORK"], **query)
        target = {"path": _path(values.get("GOCACHE"), policy, cwd)}
        extra.extend(values.get(name, "") for name in ("GOENV", "GOMOD", "GOWORK"))
    elif action in {"uv", "npm", "pip"}:
        args = {"uv": ["cache", "dir"], "npm": ["config", "get", "cache"],
                "pip": ["cache", "dir"]}[action]
        target = {"path": _path(_query(command, args, **query), policy, cwd)}
        config_keys = {"uv": ("UV_CONFIG_FILE",),
                       "npm": ("NPM_CONFIG_USERCONFIG", "npm_config_userconfig",
                               "NPM_CONFIG_GLOBALCONFIG", "npm_config_globalconfig"),
                       "pip": ("PIP_CONFIG_FILE",)}[action]
        extra.extend(environment[key] for key in config_keys if environment.get(key))
        if action == "npm":
            for key in ("userconfig", "globalconfig"):
                extra.append(_query(command, ["config", "get", key], **query))
        if action == "pip" and environment.get("VIRTUAL_ENV"):
            extra.append(str(absolute_path(environment["VIRTUAL_ENV"]) / "pip.conf"))
    elif action == "brew":
        target = {name: _path(_query(command, ["--" + name], **query), policy, cwd)
                  for name in ("prefix", "cache", "cellar", "caskroom", "repository")}
        default_logs = (policy.home / "Library/Logs/Homebrew" if platform == "darwin"
                        else policy.home / ".cache/Homebrew/Logs")
        target["logs"] = _path(environment.get("HOMEBREW_LOGS", str(default_logs)), policy, cwd)
        target["locks"] = _path(environment.get("HOMEBREW_LOCKS",
                                str(Path(target["prefix"]) / "var/homebrew/locks")), policy, cwd)
    else:
        nix = executable("nix")
        values = _json_query(nix["path"], ["--extra-experimental-features", "nix-command",
                             "config", "show", "--json"], **query)
        def setting(name, default=None):
            value = values.get(name, default)
            return value.get("value") if isinstance(value, dict) else value
        if (setting("store", "auto") not in {"auto", "daemon", "local"}
                or environment.get("NIX_REMOTE", "") not in {"", "auto", "daemon", "local"}):
            raise StorageError("Remote or custom Nix stores cannot be bound for confirmed cleanup")
        expected = {"NIX_STORE_DIR": "/nix/store", "NIX_STATE_DIR": "/nix/var/nix",
                    "NIX_LOG_DIR": "/nix/var/log/nix", "NIX_REAL_STORE_DIR": "/nix/store"}
        if any(environment.get(key, value) != value for key, value in expected.items()):
            raise StorageError("Custom Nix store directories cannot be bound for confirmed cleanup")
        # Store/state directories are not global Settings in supported Nix releases.
        # Bind explicit local-store parameters instead of inferring them from config show.
        store = _path("/nix/store", policy, cwd, store=True)
        state = _path("/nix/var/nix", policy, cwd, store=True)
        endpoint = "local?" + urlencode({"root": "/", "store": store, "real": store,
                                         "state": state, "log": "/nix/var/log/nix"})
        readonly_endpoint = endpoint + "&read-only=true"
        metadata = _json_query(nix["path"], ["--extra-experimental-features",
                               "nix-command read-only-local-store", "store", "ping",
                               "--json", "--store", readonly_endpoint], **query)
        if (not isinstance(metadata.get("url"), str)
                or metadata["url"].partition("?")[0] not in {"local", "local://"}
                or parse_qs(metadata["url"].partition("?")[2], keep_blank_values=True)
                != parse_qs(readonly_endpoint.partition("?")[2], keep_blank_values=True)):
            raise StorageError("Nix did not verify the explicit local store endpoint")
        keep = {name: setting(name) for name in ("keep-outputs", "keep-derivations")}
        if any(type(value) is not bool for value in keep.values()):
            raise StorageError("Nix garbage collection retention settings are unavailable")
        target = {"endpoint": endpoint, "queryEndpoint": readonly_endpoint,
                  "storeDir": store, "stateDir": state, "retention": keep,
                  "configDigest": _digest(values), "queryExecutable": nix}
        if environment.get("NIX_CONF_DIR"):
            extra.append(str(absolute_path(environment["NIX_CONF_DIR"]) / "nix.conf"))
        extra.extend(value for value in environment.get("NIX_USER_CONF_FILES", "").split(":") if value)
        if platform == "darwin":
            target["sudoExecutable"] = executable("sudo")
    roots = ([target["resolvedPath"]] if action == "go-modcache" else
             [target["path"]] if action in {"go", "uv", "npm", "pip"} else
             list(target.values()) if action == "brew" else
             [target["storeDir"], target["stateDir"]] if action == "nix" else [])
    binding = {"version": VERSION, "action": action, "executable": binary,
               "cwd": directory, "scope": _scope(action, policy, cwd, environment, extra),
               "target": target, "roots": [_root_identity(value) for value in roots]}
    # Detect a provider replacement during the read-only probe itself.
    if executable(Path(binary["path"]).name) != binary or _directory(cwd) != directory:
        raise StorageError("Native executable or working directory changed; generate a fresh preview")
    return binding


def verify(action: str, binding: dict, policy: Policy, *, platform: str) -> dict:
    """Re-query the current provider; JSON evidence never grants an arbitrary path."""
    if not isinstance(binding, dict) or binding.get("action") != action or binding.get("version") != VERSION:
        raise StorageError("Native target binding is missing; generate a fresh preview")
    name = TOOLS.get(action)
    if action == "pip":
        name = Path(binding["executable"]["path"]).name
        if name not in {"pip", "pip3"}:
            raise StorageError("pip executable binding is invalid; generate a fresh preview")
    candidate = executable(name)
    if candidate != binding["executable"] or _directory(Path.cwd()) != binding["cwd"]:
        raise StorageError("Native executable or working directory changed; generate a fresh preview")
    if action == "docker":
        docker.verify_target(binding["target"])
    elif action == "go-modcache":
        go_modcache.verify(binding["target"], home=policy.home,
                           env=_environment(action, platform), binary=candidate["path"])
    current = capture(action, policy, platform=platform)
    if current != binding:
        raise StorageError("Native manager context or target changed; generate a fresh preview")
    return current


def commands(action: str, current: dict, *, platform: str) -> list[list[str]]:
    """Render only fixed native semantics from a freshly validated provider target."""
    # Preserve multicall entrypoints; current metadata was revalidated by verify().
    binary, target = current["executable"]["path"], current["target"]
    if action == "docker":
        return [[binary, *argv[1:]] for argv in docker.bound_commands(target)]
    args = {"go": ["clean", "-cache"], "go-modcache": ["clean", "-modcache"],
            "uv": ["cache", "prune", "--cache-dir", target.get("path", "")],
            "npm": ["--cache", target.get("path", ""), "cache", "verify"],
            "pip": ["--cache-dir", target.get("path", ""), "cache", "purge"],
            "brew": ["cleanup", "--prune=30"],
            "nix": ["--store", target.get("endpoint", ""), "--gc"]}[action]
    argv = [binary, *args]
    if action == "nix":
        for name in ("keep-outputs", "keep-derivations"):
            argv.extend(["--option", name, str(target["retention"][name]).lower()])
    if action == "nix" and platform == "darwin":
        argv = [target["sudoExecutable"]["path"], "-n", "-H", *argv]
    return [argv]


def execution_environment(action: str, current: dict, *, platform: str) -> dict:
    environment = _environment(action, platform)
    target = current["target"]
    if action == "go":
        environment["GOCACHE"] = target["path"]
    elif action == "go-modcache":
        environment.update(GOMODCACHE=target["resolvedPath"])
    elif action == "brew":
        environment.update(HOMEBREW_CACHE=target["cache"], HOMEBREW_LOGS=target["logs"],
                           HOMEBREW_LOCKS=target["locks"])
    return environment

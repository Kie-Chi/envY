"""Merge declared sources with observed state without changing machine policy."""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from envy.sources.model import Source, SourceError, fingerprint
from envy.utils import ENVY_ROOT, platform_name


DEFAULT_SOURCE_IDS = {"nix": "nix:nixpkgs", "native": "native:system", "npm": "npm:npmjs",
                      "pypi": "pypi:pypi", "cargo": "cargo:crates.io", "go": "go:pkg.go.dev",
                      "homebrew": "homebrew:homebrew/core", "url": "url:artifacts"}


def os_release(root: Path = Path("/")) -> dict[str, str]:
    try:
        return {key: value.strip().strip('"') for line in (root / "etc/os-release").read_text().splitlines()
                if "=" in line for key, value in [line.split("=", 1)]}
    except OSError:
        return {}


def native_manager() -> str | None:
    return next((name for name, command in (("apt", "apt-cache"), ("dnf", "dnf"),
                 ("pacman", "pacman"), ("zypper", "zypper")) if shutil.which(command)), None)


def default_definitions() -> dict[str, dict[str, Any]]:
    def entry(label, ecosystem, kind, parameters, scope="user", platforms=None):
        return {"label": label, "ecosystem": ecosystem, "type": kind, "parameters": parameters,
                "scope": scope, "platforms": platforms or ["darwin", "linux"]}
    return {
        "nix:nixpkgs": entry("Project nixpkgs", "nix", "flake", {"input": "nixpkgs"}, "project"),
        "nix:envy-local": entry("envY local derivations", "nix", "local", {"path": str(ENVY_ROOT)}, "project"),
        "native:system": entry("System repositories", "native", "native", {"system": True}, "system", ["linux"]),
        "homebrew:homebrew/core": entry("Homebrew core", "homebrew", "tap", {"tap": "homebrew/core"}, "system", ["darwin"]),
        "homebrew:homebrew/cask": entry("Homebrew cask", "homebrew", "tap", {"tap": "homebrew/cask"}, "system", ["darwin"]),
        "npm:npmjs": entry("npm", "npm", "registry", {"registry": "https://registry.npmjs.org"}),
        "pypi:pypi": entry("PyPI", "pypi", "registry", {"index": "https://pypi.org/simple", "metadata": "https://pypi.org/pypi"}),
        "cargo:crates.io": entry("crates.io", "cargo", "registry", {"metadata": "https://crates.io", "discoveryOnly": True}),
        "go:pkg.go.dev": entry("Go package discovery", "go", "registry", {"metadata": "https://pkg.go.dev", "discoveryOnly": True}),
    }


def catalog(manifest: dict[str, Any] | None = None, *, root: Path | None = None) -> dict[str, Source]:
    root = root or ENVY_ROOT
    definitions = default_definitions()
    definitions.update((manifest or {}).get("software", {}).get("sources", {}))
    try:
        lock = json.loads((root / "flake.lock").read_text())
        lock_inputs = lock["nodes"][lock["root"]]["inputs"]
    except (OSError, ValueError, KeyError):
        lock, lock_inputs = {}, {}
    groups = (manifest or {}).get("software", {}).get("groups", {})
    selected = set()
    for group in groups.values():
        for item in group.get("selection", {}).get("effective", []):
            if isinstance(item, dict):
                selected.update(item.get("parameters", {}).get("sourceDependencies", []))
                if item.get("sourceId"):
                    selected.add(item["sourceId"])
                if group.get("kind") == "repository":
                    selected.add(f"{group['ecosystem']}:{item['id']}")
    observed_taps = []
    if platform_name() == "darwin" and shutil.which("brew"):
        from envy.process import run_process
        response = run_process(["brew", "tap"], capture=True, check=False, timeout=10)
        observed_taps = (response.stdout or "").splitlines() if response.returncode == 0 else []
        for tap in observed_taps:
            sid = "homebrew:" + tap
            if sid not in definitions:
                definitions[sid] = {"label": tap, "ecosystem": "homebrew", "type": "tap",
                                    "platforms": ["darwin"], "scope": "system", "owner": "observed",
                                    "parameters": {"tap": tap}}
    result = {}
    for source_id, raw in definitions.items():
        source = Source.from_dict(source_id, raw)
        compatible = platform_name() in source.platforms
        source.selected = source_id in selected or (source_id in DEFAULT_SOURCE_IDS.values() and (manifest or {}).get("platform", platform_name()) in source.platforms)
        source.capabilities = {"search": source.type != "local", "exactResolve": source.type not in {"local"},
                               "configure": source.type in {"native", "tap", "flake"},
                               "indexRefresh": source.type in {"flake", "native", "tap", "registry"},
                               "enumerate": source.type == "flake",
                               "sourceConstraint": source.type in {"flake", "registry", "tap", "artifact"},
                               "versionPin": source.type in {"registry", "artifact"},
                               "managedInclude": not source.parameters.get("discoveryOnly", False)}
        source.context = {"platform": platform_name(), "system": (manifest or {}).get("system", platform.machine())}
        if source.type == "flake":
            node = lock_inputs.get(source.parameters["input"])
            if isinstance(node, list):
                node = None
            locked = lock.get("nodes", {}).get(node, {}).get("locked", {})
            if locked.get("url"):
                from envy.sources.model import public_url
                try:
                    public_url(locked["url"])
                except SourceError:
                    locked = {key: value for key, value in locked.items() if key != "url"}
            source.context["locked"] = locked
            source.context["lockGraph"] = fingerprint(lock)
            project_files = {}
            for relative in ("flake.nix", "modules/software/nix-sources.nix"):
                try:
                    project_files[relative] = fingerprint((root / relative).read_text())
                except OSError:
                    pass
            source.context["projectDefinition"] = fingerprint(project_files)
            source.registered = bool(locked)
            source.ready = compatible and source.registered and bool(shutil.which("nix"))
        elif source.type == "local":
            source.registered = source.ready = True
            source.capabilities.update(search=False, exactResolve=False, configure=False, managedInclude=False)
        elif source.type == "native":
            manager = native_manager()
            distro = os_release()
            source.context.update(manager=manager, distro=distro.get("ID"), release=distro.get("VERSION_ID"),
                                  architecture=platform.machine(), sources=native_configuration_fingerprint())
            source.registered = bool(source.parameters.get("system"))
            spec = source.parameters.get(manager or "", {})
            if manager == "apt" and spec:
                from envy.sources.native.apt import effective_files, configured_uris
                from envy.sources.native.apt import expand_value
                architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
                uri = expand_value(spec.get("uri", ""), distro, architecture)
                source.registered = any(uri.rstrip("/") in configured_uris(text, deb822=name.endswith(".sources"))
                                        for name, text in effective_files().items())
            if manager in {"dnf", "zypper"} and spec:
                from envy.sources.native.observe import public_observations
                source.registered = any(spec.get("uri", "").rstrip("/") in observation["origins"]
                                        for observation in public_observations())
            source.context["registration"] = "external-or-managed" if source.registered else "pending"
            source.ready = compatible and source.registered and manager is not None
            source.capabilities.update(configure=manager in {"apt", "dnf", "zypper"} and bool(source.parameters.get(manager or "")),
                                       sourceConstraint=manager == "apt" or bool(source.parameters.get("system")), versionPin=False,
                                       search=manager == "apt" or bool(source.parameters.get("system")),
                                       exactResolve=manager == "apt" or bool(source.parameters.get("system")),
                                       indexRefresh=manager == "apt",
                                       managedInclude=manager == "apt" or bool(source.parameters.get("system")))
        elif source.type == "tap":
            source.registered = source.parameters.get("tap") in {"homebrew/core", "homebrew/cask"}
            source.registered = source.registered or source.parameters.get("tap") in observed_taps
            source.ready = compatible and bool(shutil.which("brew")) and source.registered
            if source.parameters.get("tap") in observed_taps:
                from envy.process import run_process
                repo = run_process(["brew", "--repo", source.parameters["tap"]], capture=True, check=False, timeout=10)
                if repo.returncode == 0 and repo.stdout.strip():
                    revision = run_process(["git", "-C", repo.stdout.strip(), "rev-parse", "HEAD"], capture=True, check=False, timeout=5)
                    if revision.returncode == 0:
                        source.context["revision"] = revision.stdout.strip()
        elif source.type == "artifact":
            source.registered = True
            source.ready = compatible and (native_manager(), source.parameters["format"]) in {("apt", "deb"), ("dnf", "rpm"), ("zypper", "rpm")}
            source.capabilities.update(search=False, indexRefresh=False, configure=False)
        else:
            if source.ecosystem == "pypi":
                source.capabilities["search"] = bool(source.parameters.get("metadata"))
                source.capabilities["exactResolve"] = bool(source.parameters.get("metadata"))
                source.capabilities["managedInclude"] = bool(source.parameters.get("metadata"))
            source.registered = True
            if source.ecosystem in {"cargo", "go"}:
                source.capabilities["search"] = source.id == DEFAULT_SOURCE_IDS[source.ecosystem]
                source.capabilities["managedInclude"] = False
                source.parameters["discoveryOnly"] = True
            if source.parameters.get("discoveryOnly"):
                source.capabilities.update(exactResolve=False, configure=False, indexRefresh=False, versionPin=False)
            source.ready = compatible
        source.context["transport"] = (manifest or {}).get("mirrors", {}).get("python" if source.ecosystem == "pypi" else source.ecosystem, {})
        for endpoint_var in {"npm": ["NPM_CONFIG_REGISTRY", "npm_config_registry"], "pypi": ["UV_INDEX_URL", "PIP_INDEX_URL"]}.get(source.ecosystem, []):
            endpoint = os.environ.get(endpoint_var)
            if endpoint:
                from envy.sources.model import public_url
                try:
                    public_url(endpoint)
                    source.context["environmentEndpoint"] = endpoint
                except SourceError:
                    source.context["environmentEndpoint"] = "credentialed-endpoint"
        result[source_id] = source
    if platform_name() == "linux":
        from envy.sources.native.observe import public_observations
        observations = public_observations()
        for source in result.values():
            if source.type == "native":
                source.context["observations"] = observations
        for observed in observations:
            if observed["owner"] == "external":
                sid = "native:external." + fingerprint(observed["path"])[:16]
                source = Source(sid, Path(observed["path"]).name, "native", "native",
                                platforms=["linux"], scope="system", parameters={"system": True}, owner="observed",
                                known=False, registered=True, ready=bool(native_manager()))
                source.context = observed
                source.capabilities = {key: False for key in ("search", "exactResolve", "configure", "indexRefresh",
                                                              "sourceConstraint", "versionPin", "managedInclude")}
                result[sid] = source
    return result


def native_configuration_fingerprint(root: Path = Path("/")) -> str:
    paths = [root / "etc/apt/sources.list", root / "etc/pacman.conf"]
    for directory in ("etc/apt/sources.list.d", "etc/yum.repos.d", "etc/zypp/repos.d"):
        base = root / directory
        if base.exists():
            paths.extend(sorted(base.glob("*")))
    for directory in ("etc/apt/trusted.gpg.d", "etc/apt/keyrings", "etc/pki/rpm-gpg"):
        paths.extend(sorted((root / directory).glob("*")))
    paths.append(root / "etc/apt/trusted.gpg")
    import re
    for source_path in list(paths):
        try:
            text = source_path.read_text()
            for key_path in re.findall(r"(?:Signed-By:\s*|signed-by=)(/[^\s\]]+)", text, re.I):
                paths.append(root / key_path.lstrip("/"))
        except (OSError, UnicodeError):
            pass
    contents = {}
    for path in paths:
        try:
            if path.is_file():
                contents[str(path)] = fingerprint(path.read_bytes().hex())
        except OSError:
            continue
    return fingerprint(contents)


def source_for(source_id: str, manifest: dict[str, Any] | None = None) -> Source:
    found = catalog(manifest).get(source_id)
    if found is None:
        raise SourceError(f"unknown source: {source_id}", "source-not-found")
    return found


def legacy_source_id(ecosystem: str, ref: str | None = None, kind: str = "package") -> str | None:
    if ref and ref.startswith("flake:"):
        return "nix:" + ref[6:].split("#", 1)[0]
    if ref and ref.startswith("local:"):
        return "nix:envy-local"
    if ecosystem == "homebrew":
        name = (ref or "").split("/", 1)[-1]
        parts = name.split("/")
        return "homebrew:" + "/".join(parts[:2]) if len(parts) >= 3 else None
    return DEFAULT_SOURCE_IDS.get(ecosystem)

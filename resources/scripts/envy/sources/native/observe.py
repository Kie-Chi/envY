"""Public observations of native repository files; never export file contents."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit


def repository_files(root=Path("/")):
    paths = [root / "etc/apt/sources.list", root / "etc/pacman.conf"]
    for directory in ("etc/apt/sources.list.d", "etc/yum.repos.d", "etc/zypp/repos.d"):
        paths.extend(sorted((root / directory).glob("*")))
    return [path for path in paths if path.is_file()]


def snapshot(root=Path("/")):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in repository_files(root)}


def public_observations(root=Path("/")):
    observations = []
    for path in repository_files(root):
        try:
            content = path.read_text()
        except (OSError, UnicodeError):
            continue
        # Only expose public, credential-free URL origins; raw configuration stays private.
        origins = set()
        for value in re.findall(r"https?://[^\s\]\"']+", content):
            try:
                parsed = urlsplit(value)
            except ValueError:
                continue
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                continue
            origins.add(value.rstrip("/"))
        observations.append({"path": str(path), "fingerprint": hashlib.sha256(content.encode()).hexdigest(),
                             "origins": sorted(origins), "owner": "envy" if path.name.startswith("envy-") else "external"})
    return observations


def report_bootstrap(parameters, before, definitions):
    import json
    previous = json.loads(Path(before).read_text())
    current = snapshot()
    changed = [Path(path).name for path in current if current[path] != previous.get(path)]
    if changed:
        print("Artifact repository observations: " + ", ".join(changed))
    source_id = parameters.get("bootstrapRepository")
    if source_id:
        from envy.sources.native import os_release, native_manager
        from envy.sources.native.apt import expand_value
        import platform
        source = definitions.get(source_id)
        if source is None:
            raise ValueError("artifact bootstrap repository definition is missing")
        spec = source.get("parameters", {}).get(native_manager(), {})
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine()) if native_manager() == "apt" else platform.machine()
        uri = expand_value(spec.get("uri", ""), os_release(), architecture)
        if not uri or not any(uri.rstrip("/") in item["origins"] for item in public_observations()):
            raise ValueError("artifact bootstrap repository was not observed")
        observations = public_observations()
        matching = [item for item in observations if uri.rstrip("/") in item["origins"]]
        if any(item["owner"] == "envy" for item in matching) and any(item["owner"] == "external" for item in matching):
            print("Artifact bootstrap repository has external and envY declarations; inspect duplicates with envy sw source show " + source_id)
        print("Artifact bootstrap repository observed: " + source_id)

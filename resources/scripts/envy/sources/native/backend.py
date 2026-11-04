"""Signature-verified repository preparation and envY-owned activation."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from envy.sources.model import Source, SourceError, public_url
from envy.sources.native.apt import effective_options, repository_text, expand_value


def command(args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def verified_key(spec: dict, destination: Path, *, armor: bool = False) -> None:
    public_url(spec["keyUrl"])
    with tempfile.TemporaryDirectory(prefix="envy-key-") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        key = home / "download"
        with urllib.request.urlopen(spec["keyUrl"], timeout=30) as response:
            if not response.geturl().startswith("https://"):
                raise SourceError("signing key redirected to an insecure URL")
            key.write_bytes(response.read(2 * 1024 * 1024 + 1))
        if key.stat().st_size > 2 * 1024 * 1024:
            raise SourceError("repository key exceeds size limit")
        output = command(["gpg", "--batch", "--homedir", str(home), "--with-colons",
                          "--show-keys", str(key)]).stdout.decode()
        expected = spec["keyFingerprint"].upper()
        # Require exactly the pinned primary key. Additional unrelated keys are rejected.
        primaries = []
        primary = False
        for line in output.splitlines():
            if line.startswith("pub:"):
                primary = True
            elif line.startswith("fpr:") and primary:
                primaries.append(line.split(":")[9].upper())
                primary = False
        if primaries != [expected]:
            raise SourceError("repository signing key fingerprint mismatch", "trust-mismatch")
        command(["gpg", "--batch", "--homedir", str(home), "--import", str(key)])
        destination.write_bytes(command(["gpg", "--batch", "--homedir", str(home),
                                         *(["--armor"] if armor else []), "--export", expected]).stdout)
        destination.chmod(0o644)


def source_filename(source: Source) -> str:
    return "envy-source-" + hashlib.sha256(source.id.encode()).hexdigest()[:16]


def prepare(source: Source, directory: Path, manager: str) -> tuple[Path, Path | None]:
    from envy.sources.native import os_release
    if source.parameters.get("distros") and os_release().get("ID") not in source.parameters["distros"]:
        raise SourceError("repository does not support this distribution", "unsupported-platform")
    spec = source.parameters.get(manager)
    if not spec:
        raise SourceError(f"repository configuration unsupported for {manager}: {source.id}", "unsupported")
    stem = source_filename(source)
    key = directory / (stem + ".gpg")
    if manager == "pacman":
        # Pacman repositories rely on a separately trusted keyring; never auto-import keys.
        raise SourceError("Pacman source requires an explicit full system upgrade and trusted keyring", "unsupported")
    arch = command(["dpkg", "--print-architecture"]).stdout.decode().strip() if manager == "apt" else platform.machine()
    spec = dict(spec)
    for field in ("uri", "keyUrl", "server"):
        if field in spec:
            spec[field] = expand_value(spec[field], os_release(), arch)
    verified_key(spec, key, armor=manager in {"dnf", "zypper"})
    if manager == "apt":
        target = directory / (stem + ".sources")
        target.write_text(repository_text(spec, keyring=str(key), release=os_release(), architecture=arch))
    else:
        target = directory / (stem + ".repo")
        target.write_text(f"[{stem}]\nname={source.label}\nbaseurl={spec['uri']}\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=file://{key}\n")
    target.chmod(0o644)
    return target, key


def activate(definitions: dict, selected: list[str], manager: str):
    from envy.secure_io import atomic_write_bytes
    if manager not in {"apt", "dnf", "zypper"}:
        if selected:
            raise SourceError("repository configuration is unsupported by this package manager", "unsupported")
        return
    repo_dir = Path({"apt": "/etc/apt/sources.list.d", "dnf": "/etc/yum.repos.d",
                     "zypper": "/etc/zypp/repos.d"}.get(manager, "/nonexistent"))
    key_dir = Path("/etc/apt/keyrings" if manager == "apt" else "/etc/envy/keyrings")
    selected_sources = [Source.from_dict(sid, definitions[sid]) for sid in selected]
    repo_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    # Skip duplicate external declarations with the same URI; retain external files.
    if manager == "apt":
        from envy.sources.native.apt import configured_uris
        external = [configured_uris(path.read_text(), deb822=path.suffix == ".sources") for path in repo_dir.glob("*")
                    if path.suffix in (".sources", ".list") and not path.name.startswith("envy-source-")]
        main_sources = Path("/etc/apt/sources.list")
        if main_sources.is_file():
            external.append(configured_uris(main_sources.read_text(), deb822=False))
        from envy.sources.native import os_release
        release = os_release()
        architecture = command(["dpkg", "--print-architecture"]).stdout.decode().strip() if selected_sources else ""
        selected_sources = [source for source in selected_sources
            if not any(expand_value(source.parameters.get("apt", {}).get("uri", "MISSING"), release, architecture).rstrip("/") in uris
                       for uris in external)]
    with tempfile.TemporaryDirectory(prefix="envy-repos-") as temporary:
        prepared = [(source, *prepare(source, Path(temporary), manager)) for source in selected_sources]
        desired = {descriptor.name for _, descriptor, _ in prepared}
        owned = list(repo_dir.glob("envy-source-*.sources" if manager == "apt" else "envy-source-*.repo"))
        from envy.transaction import FileTransaction
        paths = owned + [repo_dir / descriptor.name for _, descriptor, _ in prepared] + [key_dir / key.name for _, _, key in prepared]
        with FileTransaction(list(dict.fromkeys(paths))) as transaction:
            for source, descriptor, key in prepared:
                real_key = key_dir / key.name
                real_descriptor = repo_dir / descriptor.name
                text = descriptor.read_text().replace(str(key), str(real_key))
                atomic_write_bytes(real_key, key.read_bytes(), mode=0o644)
                atomic_write_bytes(real_descriptor, text.encode(), mode=0o644)
            for path in owned:
                if path.name not in desired:
                    path.unlink()
            transaction.commit()



def install_source_packages(data, manager):
    """Install source-constrained content; dependency resolution uses the effective set."""
    from envy.sources.native import os_release
    from envy.sources.native.apt import signed_options, effective_options
    from envy.sources.artifacts import download, inspect
    from urllib.parse import urljoin
    bound_items = [item for item in data["packages"] if item.get("sourceId") not in (None, "native:system")]
    if not bound_items:
        return
    if manager != "apt":
        raise SourceError("source-constrained package installation requires APT", "unsupported")
    groups = {}
    for item in bound_items:
        sid = item.get("sourceId")
        if not sid or sid == "native:system":
            continue
        name = item.get("parameters", {}).get("names", {}).get(manager, item["name"])
        if subprocess.run(["dpkg-query", "-W", "-f=${Status}", name], capture_output=True).stdout.strip() == b"install ok installed":
            continue
        groups.setdefault(sid, []).append(name)
    if not groups:
        return
    with effective_options() as options:
        subprocess.run(["apt-get", *options, "-o", "APT::Update::Error-Mode=any", "update"], check=True)
    for sid, names in groups.items():
        source = Source.from_dict(sid, data["sources"][sid])
        with signed_options(source) as options, tempfile.TemporaryDirectory(prefix="envy-native-content-") as temporary:
            files = []
            architecture = command(["dpkg", "--print-architecture"]).stdout.decode().strip()
            uri = expand_value(source.parameters["apt"]["uri"], os_release(), architecture)
            for name in names:
                policy = command(["apt-cache", *options, "policy", name]).stdout.decode()
                match = re.search(r"(?m)^\s*Candidate:\s*(\S+)", policy)
                if not match or match[1] == "(none)":
                    raise SourceError("source package has no candidate", "package-not-found")
                version = match[1]
                metadata = command(["apt-cache", *options, "show", f"{name}={version}"]).stdout.decode()
                fields = dict(re.findall(r"(?m)^([A-Za-z0-9-]+):\s*(.*)$", metadata.split("\n\n")[0]))
                package_name, _, requested_arch = name.partition(":")
                if (fields.get("Package") != package_name or not fields.get("SHA256") or not fields.get("Filename")
                        or not fields.get("Architecture") or (requested_arch and fields["Architecture"] != requested_arch)):
                    raise SourceError("source package metadata is incomplete", "trust-mismatch")
                filename = fields["Filename"]
                if filename.startswith("/") or ".." in filename.split("/"):
                    raise SourceError("invalid repository artifact path")
                parameters = {"url": urljoin(uri.rstrip("/") + "/", filename), "hash": fields["SHA256"],
                              "format": "deb", "packageName": package_name, "version": version,
                              "architecture": fields["Architecture"]}
                target = Path(temporary) / (hashlib.sha256(name.encode()).hexdigest() + ".deb")
                download(parameters, target)
                inspect(parameters, target)
                files.append(str(target))
            with effective_options() as effective:
                subprocess.run(["apt-get", *effective, "install", "-y", *files], check=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["apt", "activate", "install"])
    parser.add_argument("--definitions")
    args, arguments = parser.parse_known_args()
    if args.operation == "apt":
        command_args = arguments
        if command_args[:1] == ["--"]:
            command_args = command_args[1:]
        with effective_options() as options:
            raise SystemExit(subprocess.call([command_args[0], *options, *command_args[1:]]))
    else:
        from envy.sources.native import native_manager
        data = json.loads(Path(args.definitions).read_text())
        if args.operation == "install":
            install_source_packages(data, native_manager())
        else:
            activate(data["sources"], data["selected"], native_manager())


if __name__ == "__main__":
    main()

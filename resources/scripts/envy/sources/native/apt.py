"""One effective APT source set for discovery, metadata, and installation."""
from __future__ import annotations

import contextlib
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ARCHIVE_HOSTS = {
    "ubuntu": {"archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com"},
    "debian": {"deb.debian.org", "security.debian.org", "ftp.debian.org"},
}


def configured_uris(text: str, *, deb822: bool) -> set[str]:
    """Package repositories actually enabled by a source descriptor."""
    uris = set()
    if deb822:
        for stanza in re.split(r"\n\s*\n", text):
            if re.search(r"(?im)^Enabled:\s*no\s*$", stanza):
                continue
            types = re.search(r"(?m)^Types:\s*(.*)", stanza)
            if types is None or "deb" not in types[1].split():
                continue
            match = re.search(r"(?m)^URIs:\s*(.*(?:\n[ \t]+.*)*)", stanza)
            if match:
                uris.update(uri.rstrip("/") for uri in match[1].split())
    else:
        for line in text.splitlines():
            match = re.match(r"\s*deb\s+(?:\[[^]]*\]\s+)?(\S+)", line)
            if match:
                uris.add(match[1].rstrip("/"))
    return uris


def distro_uri(uri: str, distro: str) -> bool:
    parsed = urlsplit(uri)
    host = parsed.hostname or ""
    if distro == "ubuntu" and host.endswith(".archive.ubuntu.com"):
        return parsed.path.rstrip("/") == "/ubuntu"
    return host in ARCHIVE_HOSTS.get(distro, set()) and parsed.path.rstrip("/") in {
        "/ubuntu", "/ubuntu-ports", "/debian", "/debian-security"
    }


def effective_files(root: Path = Path("/")) -> dict[str, str]:
    """Retain external/vendor configuration; replace only identifiable distro entries."""
    from envy.sources.native import os_release
    base = root / "etc/apt"
    mirror = base / "sources.list.d/envy-mirror.sources"
    replace = mirror.exists()
    distro = os_release(root).get("ID", "")
    files = [base / "sources.list", *sorted((base / "sources.list.d").glob("*.list")),
             *sorted((base / "sources.list.d").glob("*.sources"))]
    result = {}
    for index, path in enumerate(files):
        if not path.is_file():
            continue
        text = path.read_text()
        if replace and path != mirror:
            if path.suffix == ".sources":
                stanzas = []
                for stanza in re.split(r"\n\s*\n", text):
                    match = re.search(r"(?m)^URIs:\s*(.*(?:\n[ \t]+.*)*)", stanza)
                    uris = match.group(1).split() if match else []
                    retained = [uri for uri in uris if not distro_uri(uri, distro)]
                    if uris and not retained:
                        continue
                    if match and retained != uris:
                        stanza = stanza[:match.start()] + "URIs: " + " ".join(retained) + stanza[match.end():]
                    if stanza.strip():
                        stanzas.append(stanza)
                text = "\n\n".join(stanzas) + "\n"
            else:
                retained = []
                for line in text.splitlines():
                    match = re.match(r"\s*deb(?:-src)?\s+(?:\[[^]]*\]\s+)?(\S+)", line)
                    if match and distro_uri(match.group(1), distro):
                        continue
                    retained.append(line)
                text = "\n".join(retained) + "\n"
        if text.strip():
            result[f"{index}{'.sources' if path.suffix == '.sources' else '.list'}"] = text
    return result


@contextlib.contextmanager
def effective_options(root: Path = Path("/")):
    """APT reads its usual auth configuration; no credentials are copied or emitted."""
    with tempfile.TemporaryDirectory(prefix="envy-apt-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)  # APT's sandbox user must read source descriptors.
        parts = directory / "sources"
        parts.mkdir(mode=0o755)
        for name, text in effective_files(root).items():
            target = parts / name
            target.write_text(text)
            # Source files can contain existing credentials. Do not widen access.
            target.chmod(0o600)
        yield ["-o", "Dir::Etc::sourcelist=/dev/null", "-o", f"Dir::Etc::sourceparts={parts}"]


def expand_value(value: str, release: dict, architecture: str) -> str:
    return value.replace("{distro}", release.get("ID", "")).replace(
        "{codename}", release.get("VERSION_CODENAME", release.get("UBUNTU_CODENAME", ""))
    ).replace("{architecture}", architecture)


def repository_text(spec: dict, *, keyring: str, release: dict, architecture: str) -> str:
    """Expand only documented runtime placeholders, never shell expressions."""
    def expand(value):
        return expand_value(value, release, architecture)
    fields = {"Types": "deb", "URIs": expand(spec["uri"]),
              "Suites": " ".join(expand(value) for value in spec.get("suites", [])),
              "Components": " ".join(spec.get("components", [])), "Signed-By": keyring}
    if not fields["Suites"]:
        raise ValueError("repository suite could not be determined")
    if spec.get("architectures"):
        fields["Architectures"] = " ".join(expand(value) for value in spec["architectures"])
    if any("\n" in value or "\r" in value for value in fields.values()):
        raise ValueError("invalid repository field")
    return "".join(f"{key}: {value}\n" for key, value in fields.items() if value)


def candidate_from_uri(output: str, version: str, uri: str) -> bool:
    active = False
    for line in output.splitlines():
        header = re.fullmatch(r"\s*(?:\*\*\* )?(\S+)\s+\d+\s*", line)
        if header:
            active = header.group(1) == version
            continue
        if active:
            for origin in re.findall(r"https?://[^\s]+", line):
                if origin.rstrip("/") == uri.rstrip("/"):
                    return True
    return False


@contextlib.contextmanager
def signed_options(source):
    """Read signed vendor indexes in a private directory, including on registered hosts."""
    import subprocess
    from envy.sources.native.backend import prepare
    from envy.sources.model import SourceError
    with tempfile.TemporaryDirectory(prefix="envy-signed-apt-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        descriptor, _ = prepare(source, directory, "apt")
        lists = directory / "lists"
        lists.mkdir(mode=0o755)
        (lists / "partial").mkdir(mode=0o755)
        cache = directory / "cache"
        cache.mkdir(mode=0o755)
        hooks = directory / "private.conf"
        hooks.write_text("#clear APT::Update::Post-Invoke;\n#clear APT::Update::Post-Invoke-Success;\n")
        options = ["-c", str(hooks), "-o", f"Dir::Cache={cache}", "-o", "Dir::Cache::pkgcache=",
                   "-o", "Dir::Cache::srcpkgcache=", "-o", f"Dir::Etc::sourcelist={descriptor}", "-o", "Dir::Etc::sourceparts=-",
                   "-o", f"Dir::State::lists={lists}", "-o", "APT::Get::List-Cleanup=0",
                   "-o", "Debug::NoLocking=1", "-o", "APT::Update::Error-Mode=any",
                   "-o", "APT::Update::Post-Invoke::=", "-o", "APT::Update::Post-Invoke-Success::="]
        result = subprocess.run(["apt-get", *options, "update"], check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if result.returncode:
            raise SourceError("signed repository index refresh failed", "source-unavailable")
        yield options

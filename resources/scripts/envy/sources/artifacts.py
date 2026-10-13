"""Download and inspect fixed native artifacts before saving or installing."""
from __future__ import annotations

import argparse
import base64
import hashlib
import platform
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from envy.sources.model import Source, SourceError, public_url

MAX_BYTES = 1024 * 1024 * 1024


def expected_hash(value):
    if not isinstance(value, str):
        raise SourceError("artifact requires a SHA256 string", "unverified-artifact")
    if value.startswith("sha256-"):
        try:
            digest = base64.b64decode(value[7:], validate=True)
            if len(digest) != 32:
                raise ValueError("SHA256 hash must contain 32 bytes")
            return digest.hex()
        except ValueError as exc:
            raise SourceError("invalid SHA256 SRI hash") from exc
    value = value.removeprefix("sha256:")
    if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise SourceError("artifact requires a full SHA256 hash", "unverified-artifact")
    return value.lower()


def download(parameters, target: Path, timeout=60):
    public_url(parameters["url"])
    digest = hashlib.sha256()
    with urllib.request.urlopen(parameters["url"], timeout=timeout) as response, target.open("wb") as output:
        if response.geturl().startswith("https://") is False:
            raise SourceError("artifact redirected to an insecure URL")
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > MAX_BYTES:
                raise SourceError("artifact exceeds size limit")
            digest.update(block)
            output.write(block)
    if parameters.get("hash") and digest.hexdigest() != expected_hash(parameters["hash"]):
        raise SourceError("artifact content hash mismatch", "trust-mismatch")


def inspect(parameters, target: Path):
    kind = parameters["format"]
    if kind == "deb":
        args = ["dpkg-deb", "-f", str(target), "Package", "Version", "Architecture"]
    elif kind == "rpm":
        args = ["rpm", "-qp", "--qf", "%{NAME}\n%{VERSION}-%{RELEASE}\n%{ARCH}\n", str(target)]
    else:
        raise SourceError("unsupported artifact format", "unsupported")
    if not shutil.which(args[0]):
        raise SourceError("native artifact inspection tool is unavailable", "unsupported")
    result = subprocess.run(args, check=True, capture_output=True, timeout=30)
    values = [line.split(": ", 1)[-1] for line in result.stdout.decode().strip().splitlines()]
    if len(values) != 3:
        raise SourceError("invalid native artifact metadata")
    name, version, arch = values
    if name != parameters["packageName"]:
        raise SourceError("artifact package identity mismatch", "trust-mismatch")
    if parameters.get("version") and version != parameters["version"]:
        raise SourceError("artifact version mismatch", "trust-mismatch")
    declared = parameters.get("architecture")
    host_arch = platform.machine()
    compatible = {"x86_64": {"amd64", "x86_64", "all", "noarch"},
                  "aarch64": {"arm64", "aarch64", "all", "noarch"}}.get(host_arch, {host_arch, "all", "noarch"})
    if arch not in compatible or (declared and arch != declared):
        raise SourceError("artifact architecture mismatch", "unsupported-platform")
    return name, version, arch


def resolve_artifact(source: Source, timeout=60):
    # Activation uses a standard-library-only Python. Search belongs to the CLI.
    from envy.search.model import ResolveResult, SearchResult
    try:
        expected_hash(source.parameters.get("hash", ""))
        with tempfile.TemporaryDirectory(prefix="envy-artifact-") as temporary:
            target = Path(temporary) / ("package." + source.parameters["format"])
            download(source.parameters, target, timeout)
            name, version, arch = inspect(source.parameters, target)
        from envy.sources.adapters import bind
        return ResolveResult.found(bind(SearchResult("url", "url", name, "artifact", version=version,
            ref=source.parameters["url"], availability="installable"), source))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return ResolveResult.unavailable(str(exc))


def main():
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("parameters")
    parser.add_argument("destination")
    args = parser.parse_args()
    parameters = json.loads(Path(args.parameters).read_text())
    target = Path(args.destination)
    # Legacy unpinned artifacts retain their behavior, but now validate package metadata.
    download(parameters, target)
    inspect(parameters, target)


if __name__ == "__main__":
    main()

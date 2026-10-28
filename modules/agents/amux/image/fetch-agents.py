"""Install Claude Code and Codex Linux binaries from npm package tarballs.

amux resolves the newest versions on the host and passes each tarball URL
with the registry's sha512 integrity; nothing is installed unless it matches.
"""

import base64
import hashlib
import io
import os
import subprocess
import tarfile
import urllib.request

TRIPLES = {"arm64": "aarch64-unknown-linux-musl", "amd64": "x86_64-unknown-linux-musl"}


def fetch(url: str) -> bytes:
    print(f"fetch {url}", flush=True)
    with urllib.request.urlopen(url, timeout=900) as response:
        return response.read()


def verify(data: bytes, integrity: str, name: str) -> None:
    algorithm, _, digest = integrity.partition("-")
    if algorithm != "sha512":
        raise SystemExit(f"{name}: unsupported integrity {integrity}")
    if hashlib.sha512(data).digest() != base64.b64decode(digest):
        raise SystemExit(f"{name} package does not match its registry integrity")


def member(archive: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        handle = tar.extractfile(name)
        if handle is None:
            raise SystemExit(f"{name} is missing from the package")
        return handle.read()


def extract_tree(archive: bytes, prefix: str, dest: str) -> None:
    """Extract one package directory; Codex finds helpers next to its binary."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = []
        for item in tar.getmembers():
            if item.name.startswith(prefix):
                item.name = item.name[len(prefix):]
                members.append(item)
        if not members:
            raise SystemExit(f"{prefix} is missing from the package")
        tar.extractall(dest, members=members, filter="data")


def install(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o755)


def main() -> None:
    arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True, check=True).stdout.strip()
    if arch not in TRIPLES:
        raise SystemExit(f"unsupported architecture: {arch}")

    claude = fetch(os.environ["CLAUDE_URL"])
    verify(claude, os.environ["CLAUDE_INTEGRITY"], "Claude Code")
    install("/usr/local/bin/claude", member(claude, "package/claude"))

    codex = fetch(os.environ["CODEX_URL"])
    verify(codex, os.environ["CODEX_INTEGRITY"], "Codex")
    extract_tree(codex, f"package/vendor/{TRIPLES[arch]}/", "/opt/codex")
    os.chmod("/opt/codex/bin/codex", 0o755)
    os.symlink("/opt/codex/bin/codex", "/usr/local/bin/codex")


if __name__ == "__main__":
    main()

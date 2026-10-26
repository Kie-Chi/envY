"""Install the pinned Claude Code and Codex Linux binaries from an npm registry.

Claude Code is checked against the nixpkgs manifest checksum. nixpkgs builds
Codex from source, so its platform tarball is checked against the registry's
published integrity value, as npm itself does.
"""

import base64
import hashlib
import io
import json
import os
import subprocess
import tarfile
import urllib.request

PLATFORMS = {
    "arm64": ("linux-arm64", "aarch64-unknown-linux-musl", "CLAUDE_SHA256_ARM64"),
    "amd64": ("linux-x64", "x86_64-unknown-linux-musl", "CLAUDE_SHA256_AMD64"),
}


def fetch(url: str) -> bytes:
    print(f"fetch {url}", flush=True)
    with urllib.request.urlopen(url, timeout=900) as response:
        return response.read()


def member(archive: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        handle = tar.extractfile(name)
        if handle is None:
            raise SystemExit(f"{name} is missing from the package")
        return handle.read()


def install(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o755)


def main() -> None:
    registry = os.environ["NPM_REGISTRY"].rstrip("/")
    arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True, check=True).stdout.strip()
    if arch not in PLATFORMS:
        raise SystemExit(f"unsupported architecture: {arch}")
    platform, triple, checksum_name = PLATFORMS[arch]

    version = os.environ["CLAUDE_VERSION"]
    package = f"claude-code-{platform}"
    binary = member(fetch(f"{registry}/@anthropic-ai/{package}/-/{package}-{version}.tgz"), "package/claude")
    if hashlib.sha256(binary).hexdigest() != os.environ[checksum_name]:
        raise SystemExit("Claude Code checksum does not match the nixpkgs manifest")
    install("/usr/local/bin/claude", binary)

    version = f"{os.environ['CODEX_VERSION']}-{platform}"
    expected = json.loads(fetch(f"{registry}/@openai/codex/{version}"))["dist"]["integrity"]
    archive = fetch(f"{registry}/@openai/codex/-/codex-{version}.tgz")
    actual = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
    if actual != expected:
        raise SystemExit("Codex package integrity does not match the registry")
    install("/usr/local/bin/codex", member(archive, f"package/vendor/{triple}/bin/codex"))


if __name__ == "__main__":
    main()

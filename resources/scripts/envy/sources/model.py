"""Public source identities; credentials and transport are separate concerns."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit


class SourceError(ValueError):
    def __init__(self, message: str, code: str = "invalid-source"):
        super().__init__(message)
        self.code = code


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def public_url(value: str, *, schemes=("https",)) -> str:
    if not isinstance(value, str):
        raise SourceError("source URL must be a string")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise SourceError("invalid source URL") from exc
    if parsed.scheme not in schemes or not parsed.netloc or parsed.username or parsed.password:
        raise SourceError("source URL must use an allowed scheme without credentials")
    if any(re.search(r"token|secret|password|api.?key|signature|^sig$|authorization|credential", key, re.I) for key, _ in parse_qsl(parsed.query)):
        raise SourceError("source URL must not contain credential parameters")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or parsed.fragment:
        raise SourceError("source URL contains whitespace, control characters, or a fragment")
    return value


@dataclass
class Source:
    id: str
    label: str
    ecosystem: str
    type: str
    platforms: list[str] = field(default_factory=lambda: ["darwin", "linux"])
    scope: str = "user"
    parameters: dict[str, Any] = field(default_factory=dict)
    owner: str = "module"
    known: bool = True
    registered: bool = False
    selected: bool = False
    ready: bool = False
    capabilities: dict[str, bool] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, source_id: str, raw: dict[str, Any]) -> "Source":
        if not isinstance(raw, dict) or not isinstance(raw.get("parameters", {}), dict):
            raise SourceError("source definition and parameters must be objects")
        if not re.fullmatch(r"[a-z][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._/-]*", source_id):
            raise SourceError("invalid source ID")
        ecosystem = raw.get("ecosystem", source_id.split(":", 1)[0])
        if not isinstance(ecosystem, str) or not isinstance(raw.get("type", "registry"), str):
            raise SourceError("source ecosystem and type must be strings")
        if ecosystem != source_id.split(":", 1)[0]:
            raise SourceError(f"source ecosystem conflicts with ID: {source_id}")
        platforms = raw.get("platforms", ["darwin", "linux"])
        if not isinstance(platforms, list):
            raise SourceError("source platforms must be a list")
        source = cls(source_id, raw.get("label", source_id), ecosystem, raw.get("type", "registry"),
                     platforms, raw.get("scope", "user"),
                     dict(raw.get("parameters", {})), raw.get("owner", "module"))
        source.validate()
        return source

    def validate(self) -> None:
        compatible = {"nix": {"flake", "local"}, "native": {"native"}, "homebrew": {"tap"},
                      "npm": {"registry"}, "pypi": {"registry"}, "cargo": {"registry"},
                      "go": {"registry"}, "url": {"artifact"}}
        if self.type not in compatible.get(self.ecosystem, set()):
            raise SourceError(f"source type does not match ecosystem: {self.id}")
        if not self.platforms or any(value not in ("darwin", "linux") for value in self.platforms):
            raise SourceError("invalid source platforms")
        if not isinstance(self.label, str) or not isinstance(self.owner, str):
            raise SourceError("source label and owner must be strings")
        if self.scope not in ("user", "system", "project"):
            raise SourceError("invalid source scope")
        if any("\n" in str(value) or "\r" in str(value) for value in (self.label, self.owner)):
            raise SourceError("invalid source metadata")
        allowed = {
            "flake": {"input", "output", "follows"},
            "local": {"path"},
            "native": {"apt", "dnf", "pacman", "zypper", "distros", "system"},
            "tap": {"tap", "url"},
            "registry": {"registry", "index", "metadata", "discoveryOnly"},
            "artifact": {"url", "format", "packageName", "version", "architecture", "hash", "bootstrapRepository"},
        }
        if self.type not in allowed or set(self.parameters) - allowed[self.type]:
            raise SourceError(f"unsupported source parameters: {self.id}")
        for key in ("url", "registry", "index", "metadata"):
            if key in self.parameters:
                public_url(self.parameters[key])
        if self.type == "flake":
            for key in ("input", "output", "follows"):
                value = self.parameters.get(key, "packages" if key == "output" else "")
                if key == "follows" and key not in self.parameters:
                    continue
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                    raise SourceError(f"invalid flake {key} binding: {self.id}")
        if self.type == "flake" and self.id != "nix:" + self.parameters["input"]:
            raise SourceError("source ID conflicts with flake input binding")
        if self.type == "tap" and (not isinstance(self.parameters.get("tap"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.parameters["tap"])):
            raise SourceError("invalid Homebrew tap")
        if self.type == "tap" and self.id != "homebrew:" + self.parameters["tap"]:
            raise SourceError("source ID must match its Homebrew tap")
        if self.type == "native":
            if "system" in self.parameters and not isinstance(self.parameters["system"], bool):
                raise SourceError("system source marker must be boolean")
            if not isinstance(self.parameters.get("distros", []), list) or not all(
                    isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]+", value)
                    for value in self.parameters.get("distros", [])):
                raise SourceError("invalid distribution constraints")
            for manager in ("apt", "dnf", "pacman", "zypper"):
                spec = self.parameters.get(manager)
                if spec is None:
                    continue
                permitted = {"uri", "suites", "components", "architectures", "keyUrl", "keyFingerprint", "server", "sigLevel"}
                if not isinstance(spec, dict) or set(spec) - permitted:
                    raise SourceError(f"invalid {manager} repository parameters: {self.id}")
                for key in ("uri", "keyUrl", "server"):
                    if key in spec:
                        public_url(spec[key])
                if not spec.get("uri", spec.get("server")):
                    raise SourceError(f"missing {manager} repository URI: {self.id}")
                for list_key in ("suites", "components", "architectures"):
                    values = spec.get(list_key, [])
                    if not isinstance(values, list) or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_{}+./-]+", v) for v in values):
                        raise SourceError(f"invalid {list_key}: {self.id}")
                if manager != "pacman" and not spec.get("keyUrl"):
                    raise SourceError("repository signing key URL is required")
                key = spec.get("keyFingerprint", "")
                if manager != "pacman" and (not isinstance(key, str) or not re.fullmatch(r"(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})", key)):
                    raise SourceError(f"a full signing key fingerprint is required: {self.id}")
                if manager == "apt" and not spec.get("suites"):
                    raise SourceError("APT repository suites are required")
        if self.type == "local" and (not isinstance(self.parameters.get("path"), str) or not self.parameters["path"]):
            raise SourceError("local source path is required")
        logical = {
            "npm:npmjs": {"registry": "https://registry.npmjs.org"},
            "pypi:pypi": {"index": "https://pypi.org/simple", "metadata": "https://pypi.org/pypi"},
            "cargo:crates.io": {"metadata": "https://crates.io"},
            "go:pkg.go.dev": {"metadata": "https://pkg.go.dev"},
        }
        if self.id in logical and any(self.parameters.get(key, "").rstrip("/") != value.rstrip("/")
                                     for key, value in logical[self.id].items()):
            raise SourceError("default registry identity cannot be rebound; use mirrors or a new source ID")
        if self.type == "registry":
            required = "registry" if self.ecosystem == "npm" else "index" if self.ecosystem == "pypi" else "metadata"
            if not isinstance(self.parameters.get(required), str):
                raise SourceError(f"missing registry endpoint: {self.id}")
            if "discoveryOnly" in self.parameters and not isinstance(self.parameters["discoveryOnly"], bool):
                raise SourceError("discoveryOnly must be boolean")
        if self.type == "artifact":
            public_url(self.parameters.get("url"))
            if self.parameters.get("format") not in {"deb", "rpm"} or not self.parameters.get("packageName"):
                raise SourceError(f"artifact format and packageName are required: {self.id}")
            if not isinstance(self.parameters["packageName"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]*", self.parameters["packageName"]):
                raise SourceError("invalid native artifact package identity")
            for key in ("version", "architecture", "bootstrapRepository"):
                if key in self.parameters and (not isinstance(self.parameters[key], str) or not self.parameters[key]
                        or any(char.isspace() for char in self.parameters[key])):
                    raise SourceError(f"invalid artifact {key}")
            from envy.sources.artifacts import expected_hash
            expected_hash(self.parameters.get("hash", ""))

    @property
    def fingerprint(self) -> str:
        return fingerprint({"id": self.id, "type": self.type, "parameters": self.parameters, "context": self.context})

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["fingerprint"] = self.fingerprint
        return result

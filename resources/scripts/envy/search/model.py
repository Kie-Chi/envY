"""Normalized software registry search records."""

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass
class SearchResult:
    source: str
    ecosystem: str
    name: str
    kind: str
    version: str | None = None
    description: str = ""
    ref: str | None = None
    homepage: str | None = None
    publisher: str | None = None
    managed_group: str | None = None

    source_id: str | None = None
    source_fingerprint: str | None = None
    availability: str = "installable"
    resolved_version: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["sourceId"] = data.pop("source_id")
        data["sourceFingerprint"] = data.pop("source_fingerprint")
        data["resolvedVersion"] = data.pop("resolved_version") or self.version
        return data

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        for public, internal in (("sourceId", "source_id"), ("sourceFingerprint", "source_fingerprint"), ("resolvedVersion", "resolved_version")):
            if public in data:
                data[internal] = data.pop(public)
        return cls(**data)


@dataclass
class ProviderReport:
    source: str
    results: list[SearchResult]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "results": [result.to_dict() for result in self.results],
            "error": self.error,
        }


ResolveStatus = Literal["found", "not_found", "unavailable"]


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of an exact registry identity lookup."""

    status: ResolveStatus
    result: SearchResult | None = None
    error: str | None = None

    @classmethod
    def found(cls, result: SearchResult) -> "ResolveResult":
        return cls(status="found", result=result)

    @classmethod
    def not_found(cls, error: str | None = None) -> "ResolveResult":
        return cls(status="not_found", error=error)

    @classmethod
    def unavailable(cls, error: str) -> "ResolveResult":
        return cls(status="unavailable", error=error)

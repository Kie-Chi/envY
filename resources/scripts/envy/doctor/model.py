"""Shared result model for doctor checks."""

from dataclasses import dataclass, field
from typing import Any, Literal

DoctorSection = Literal[
    "self",
    "conf",
    "secr",
    "apps",
    "runs",
    "stat",
    "auth",
    "sync",
    "perm",
    "host",
    "agents",
]

SECTION_DOCTOR: DoctorSection = "self"
SECTION_CONFIG: DoctorSection = "conf"
SECTION_SECRETS: DoctorSection = "secr"
SECTION_INSTALL: DoctorSection = "apps"
SECTION_RUNTIME: DoctorSection = "runs"
SECTION_STATE: DoctorSection = "stat"
SECTION_AUTH: DoctorSection = "auth"
SECTION_SYNC: DoctorSection = "sync"
SECTION_PRIVACY: DoctorSection = "perm"
SECTION_SYSTEM: DoctorSection = "host"
SECTION_AGENTS: DoctorSection = "agents"

SECTION_ALIASES: dict[str, DoctorSection] = {
    "agents": SECTION_AGENTS,
    "doctor": SECTION_DOCTOR,
    "doc": SECTION_DOCTOR,
    "self": SECTION_DOCTOR,
    "config": SECTION_CONFIG,
    "cfg": SECTION_CONFIG,
    "conf": SECTION_CONFIG,
    "secrets": SECTION_SECRETS,
    "secret": SECTION_SECRETS,
    "sec": SECTION_SECRETS,
    "secr": SECTION_SECRETS,
    "install": SECTION_INSTALL,
    "inst": SECTION_INSTALL,
    "apps": SECTION_INSTALL,
    "runtime": SECTION_RUNTIME,
    "run": SECTION_RUNTIME,
    "runs": SECTION_RUNTIME,
    "state": SECTION_STATE,
    "stat": SECTION_STATE,
    "auth": SECTION_AUTH,
    "sync": SECTION_SYNC,
    "privacy": SECTION_PRIVACY,
    "priv": SECTION_PRIVACY,
    "permission": SECTION_PRIVACY,
    "permissions": SECTION_PRIVACY,
    "perm": SECTION_PRIVACY,
    "perms": SECTION_PRIVACY,
    "pers": SECTION_PRIVACY,
    "system": SECTION_SYSTEM,
    "sys": SECTION_SYSTEM,
    "host": SECTION_SYSTEM,
}

ALL_SECTIONS: tuple[DoctorSection, ...] = (
    SECTION_DOCTOR,
    SECTION_CONFIG,
    SECTION_SECRETS,
    SECTION_INSTALL,
    SECTION_RUNTIME,
    SECTION_STATE,
    SECTION_AUTH,
    SECTION_SYNC,
    SECTION_PRIVACY,
    SECTION_SYSTEM,
    SECTION_AGENTS,
)

Status = Literal["ok", "warn", "error", "info"]


@dataclass
class CheckResult:
    section: DoctorSection
    name: str
    status: Status
    message: str
    hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    action: dict[str, Any] | None = None

    @property
    def failed(self) -> bool:
        return self.status == "error"

    @property
    def warned(self) -> bool:
        return self.status == "warn"

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
            "action": self.action,
        }


def ok(
    section: DoctorSection, name: str, message: str,
    action: dict[str, Any] | None = None, **details: Any,
) -> CheckResult:
    return CheckResult(
        section=section, name=name, status="ok", message=message,
        details=details, action=action,
    )


def warn(
    section: DoctorSection, name: str, message: str, hint: str = "",
    action: dict[str, Any] | None = None, **details: Any,
) -> CheckResult:
    return CheckResult(
        section=section, name=name, status="warn", message=message,
        hint=hint, details=details, action=action,
    )


def error(
    section: DoctorSection, name: str, message: str, hint: str = "",
    action: dict[str, Any] | None = None, **details: Any,
) -> CheckResult:
    return CheckResult(
        section=section, name=name, status="error", message=message,
        hint=hint, details=details, action=action,
    )


def info(
    section: DoctorSection, name: str, message: str, hint: str = "",
    action: dict[str, Any] | None = None, **details: Any,
) -> CheckResult:
    return CheckResult(
        section=section, name=name, status="info", message=message,
        hint=hint, details=details, action=action,
    )

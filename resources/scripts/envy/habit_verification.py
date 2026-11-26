"""Read-only evidence for a closed set of native habit consumers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sys
from typing import Any

from envy.doctor.probes import command


_ADAPTERS = {
    "darwin.key-repeat": ("darwin", "darwin", "defaults:NSGlobalDomain.KeyRepeat"),
    "darwin.fn-state": ("darwin", "darwin", "defaults:NSGlobalDomain.com.apple.keyboard.fnState"),
    "linux.gnome-scratchpad": ("linux", "gnome", "gsettings:tilix-quake.binding"),
    "linux.niri-keyboard-layout": ("linux", "niri", "niri:keyboard-layouts"),
}
# No path or plugin override has a proven shared context with the GNOME consumer.
_GSETTINGS_CONTEXT_KEYS = {
    "GIO_MODULE_DIR", "GIO_EXTRA_MODULES", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS",
}
_GNOME_SCHEMA = (
    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-tilix-quake/"
)


def _layout(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    names, index = value.get("names"), value.get("current_idx")
    if (
        not isinstance(names, list) or not names
        or any(not isinstance(name, str) or not name
               or any(ord(char) < 32 for char in name) for name in names)
        or type(index) is not int or not 0 <= index <= 255 or index >= len(names)
    ):
        return None
    return {"names": names, "current_idx": index}


def readback_error(readback: Any, platform: Any, context: str) -> str | None:
    """Validate metadata without resolving any executable, domain, or file path."""
    if readback is None:
        return None
    if not isinstance(readback, dict) or set(readback) != {"adapter", "expected"}:
        return "invalid-readback"
    adapter = readback["adapter"]
    if not isinstance(adapter, str) or adapter not in _ADAPTERS:
        return "unsupported-adapter"
    expected = readback["expected"]
    target, required_context, _ = _ADAPTERS[adapter]
    if platform != target or context != required_context:
        return "adapter-context-mismatch"
    if adapter == "darwin.key-repeat" and type(expected) is int:
        return None
    if adapter == "darwin.fn-state" and type(expected) is bool:
        return None
    if adapter == "linux.gnome-scratchpad" and isinstance(expected, str) and 0 < len(expected) <= 512:
        return None
    if adapter == "linux.niri-keyboard-layout" and isinstance(expected, dict) and _layout(expected) is not None and _layout(expected) == expected:
        return None
    return "invalid-expected"


def _evidence(status: str, reason: str, source: str, scope: str) -> dict[str, str]:
    return {"status": status, "reason": reason, "source": source, "scope": scope}


def _gvariant_string(text: str) -> str | None:
    """Parse a GVariant string literal, rejecting expressions and unknown escapes."""
    text = text.strip()
    if len(text) < 2 or text[0] not in "\"'" or text[-1] != text[0]:
        return None
    quote = text[0]
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
               "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"'}
    result: list[str] = []
    index = 1
    while index < len(text) - 1:
        char = text[index]
        if char == quote or ord(char) < 32:
            return None
        if char == "\\":
            index += 1
            if index >= len(text) - 1:
                return None
            escape = text[index]
            if escape in escapes:
                char = escapes[escape]
            elif escape in {"u", "U"}:
                width = 4 if escape == "u" else 8
                digits = text[index + 1:index + 1 + width]
                if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    return None
                codepoint = int(digits, 16)
                if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                    return None
                char = chr(codepoint)
                index += width
            else:
                return None
        result.append(char)
        index += 1
    return "".join(result)


def _native(readback: dict[str, Any]) -> tuple[str, Any, str]:
    """Return the evidence axis, allowlisted typed projection, and safe reason."""
    adapter = readback["adapter"]
    if adapter.startswith("darwin."):
        key = "KeyRepeat" if adapter == "darwin.key-repeat" else "com.apple.keyboard.fnState"
        typed = command.run_bounded(["defaults", "read-type", "NSGlobalDomain", key])
        if typed.stdout is None:
            return "stored", None, typed.reason
        match = re.fullmatch(r"Type is (integer|boolean|string|float|array|dictionary|date|data)\s*", typed.stdout.strip())
        if match is None:
            return "stored", None, "unknown-native-type"
        expected_type = "integer" if adapter == "darwin.key-repeat" else "boolean"
        if match[1] != expected_type:
            return "stored", None, "type-mismatch"
        value = command.run_bounded(["defaults", "read", "NSGlobalDomain", key])
        if value.stdout is None:
            return "stored", None, value.reason
        text = value.stdout.strip()
        if expected_type == "integer" and re.fullmatch(r"-?[0-9]{1,20}", text):
            return "stored", int(text), "read"
        if expected_type == "boolean" and text in {"0", "1"}:
            return "stored", text == "1", "read"
        return "stored", None, "invalid-native-value"
    if adapter == "linux.gnome-scratchpad":
        if any(key.startswith(("GSETTINGS_", "DCONF_")) or key in _GSETTINGS_CONTEXT_KEYS for key in os.environ):
            return "stored", None, "settings-context-unconfirmed"
        desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").casefold().split(":")
        if ("gnome" not in desktops or not os.environ.get("DBUS_SESSION_BUS_ADDRESS")
                or os.environ.get("XDG_SESSION_TYPE") not in {"wayland", "x11"}
                or not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))):
            return "stored", None, "session-unavailable"
        result = command.run_bounded(["gsettings", "get", _GNOME_SCHEMA, "binding"])
        if result.stdout is None:
            return "stored", None, result.reason
        value = _gvariant_string(result.stdout)
        if value is None or len(value) > 512:
            return "stored", None, "invalid-native-value"
        return "stored", value, "read"
    if (not os.environ.get("NIRI_SOCKET") or not os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("XDG_SESSION_TYPE") != "wayland"):
        return "runtime", None, "session-unavailable"
    result = command.run_bounded(["niri", "msg", "--json", "keyboard-layouts"])
    if result.stdout is None:
        return "runtime", None, result.reason
    try:
        payload = json.loads(result.stdout)
    except (ValueError, RecursionError):
        return "runtime", None, "invalid-native-value"
    value = _layout(payload)
    return "runtime", value, "read" if value is not None else "invalid-native-value"


def verify(
    manifest: dict[str, Any], habit: dict[str, Any], implementation: dict[str, Any],
    *, declaration_valid: bool, declaration_only: bool = False,
    prerequisites: Any = None,
) -> dict[str, Any]:
    """Sample fresh native evidence; cached manifests contain declarations only."""
    identity = {
        "machine": manifest.get("id"), "platform": manifest.get("platform"),
        "habit": habit, "implementation": implementation, "prerequisites": prerequisites,
        "declarationValid": declaration_valid,
    }
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    result = {
        "declaration": _evidence(
            "statically-consistent" if declaration_valid else "invalid",
            "declaration-consistent" if declaration_valid else "invalid-declaration",
            "evaluated-manifest", "selected-machine-policy",
        ),
        "stored": _evidence("unknown", "no-readback-contract", "none", "stored-preference"),
        "runtime": _evidence("unknown", "no-consumer-readback", "none", "native-consumer"),
        "pending": _evidence("unknown", "no-item-specific-signal", "none", "restart-or-login"),
        "sampledAt": datetime.now(timezone.utc).isoformat(),
        "declarationFingerprint": fingerprint,
    }
    readback = implementation.get("readback")
    error = readback_error(readback, manifest.get("platform"), implementation["context"])
    if error:
        result["declaration"] = _evidence("invalid", error, "evaluated-manifest", "selected-machine-policy")
        return result
    if implementation["ownership"] == "application":
        for axis in ("stored", "runtime"):
            result[axis]["reason"] = "application-owned-manual"
        return result
    if not declaration_valid or declaration_only or readback is None:
        if declaration_only:
            result["stored"]["reason"] = "declaration-only"
            result["runtime"]["reason"] = "declaration-only"
        return result
    platform, _, source = _ADAPTERS[readback["adapter"]]
    if ("darwin" if sys.platform == "darwin" else "linux" if sys.platform == "linux" else "unknown") != platform:
        result["stored"]["reason"] = "host-platform-mismatch"
        result["runtime"]["reason"] = "host-platform-mismatch"
        return result
    axis, observed, reason = _native(readback)
    status = "unknown"
    if reason == "type-mismatch":
        status = "mismatch"
    elif reason == "read":
        status = "match" if observed == readback["expected"] else "mismatch"
        reason = "native-value-matches" if status == "match" else "native-value-differs"
    scope = "running-compositor-layout-identity" if axis == "runtime" else "stored-preference"
    result[axis] = _evidence(status, reason, source, scope)
    # Only a consumer's positive readback supplies an item-specific pending signal.
    if axis == "runtime" and status == "match":
        result["pending"] = _evidence("none", "consumer-reports-expected-layout", source, scope)
    return result

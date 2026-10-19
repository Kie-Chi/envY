"""Machine-owned agent, endpoint, and credential configuration.

The command deliberately edits a small envY-owned block instead of teaching
the generic scalar config editor about every possible agent.  Adapter modules
and the evaluated manifest remain the source of truth for runtime behavior;
this file only stores non-sensitive selections and secret references.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Optional

import typer

from envy import log
from envy.agent_completion import (
    complete_adapters,
    complete_agent_names,
    complete_agent_paths,
    complete_agent_values,
    complete_apis,
    complete_credential_paths,
    complete_credentials,
    complete_endpoints,
    complete_managed_agent_names,
)
from envy.config import _escape_nix_string, machine_config_file, read_secrets_data, set_secret_value
from envy.evaluation import invalidate_machine_manifest, machine_manifest
from envy.jsonio import emit, emit_error
from envy.legacy import (
    AGENT_FIELD_ALIASES,
    canonicalize_agent_object,
    canonicalize_agent_values,
    is_obsolete_agent,
    normalize_agent_fields,
    obsolete_agent_paths,
)
from envy.mutation import offer_mutation_commit
from envy.nix_literals import LiteralError, LiteralReader
from envy.secure_io import atomic_write_text, ensure_private_directory
from envy.transaction import FileTransaction
from envy.utils import SECRETS_FILE, current_machine_id


MANAGED_START = "  # BEGIN ENVY MANAGED AGENTS"
MANAGED_END = "  # END ENVY MANAGED AGENTS"
_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_PATH_RE = re.compile(r"^envy\.(agents|llm\.(?:endpoints|credentials))\." + _NAME + r"(?:\.[A-Za-z0-9_-]+)?$")
_SAFE_SECRET_PATH = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_KNOWN_ENDPOINTS = {"stepfun-openai", "stepfun-anthropic", "deepseek-openai"}
_KNOWN_CREDENTIALS = {"stepfun", "deepseek"}
_RUNTIME_DIR = Path.home() / ".config" / "envy" / "agent-runtime"
_LIVE_AGENT_FIELDS = {"model", "endpoint", "credential", "api", "contextWindow", "maxTokens"}
_LIVE_ENDPOINT_FIELDS = {"baseUrl", "protocol"}
API_CHOICES = ("openai-completions", "openai-responses", "anthropic-messages")


def _block_pattern() -> re.Pattern[str]:
    return re.compile(rf"(?ms)^{re.escape(MANAGED_START)}$.*?^{re.escape(MANAGED_END)}$")


def _read_values(path: Path | None = None) -> dict[str, Any]:
    values, _present, _errors = read_managed_values(path)
    return values


def read_managed_values(path: Path | None = None) -> tuple[dict[str, Any], bool, list[str]]:
    """Read the agent block and report malformed content without secret values."""
    path = path or machine_config_file()
    if not path.exists():
        return {}, False, [f"missing machine configuration: {path}"]
    text = path.read_text()
    starts = text.count(MANAGED_START)
    ends = text.count(MANAGED_END)
    if starts == 0 and ends == 0:
        return {}, False, []
    if starts != 1 or ends != 1:
        return {}, True, ["agent managed block has unbalanced or duplicate markers"]
    match = _block_pattern().search(text)
    if not match:
        return {}, True, ["agent managed block markers are malformed"]
    values: dict[str, Any] = {}
    errors: list[str] = []
    block = match.group(0)
    reader = LiteralReader(block)
    base_line = text.count("\n", 0, match.start()) + 1
    while True:
        # peek skips comments but restores position. Keep that position to
        # locate failures without including any assignment values in output.
        start = reader.position
        field = "agent managed block"
        try:
            if not reader.peek():
                break
            parts = [reader.take()]
            while reader.peek() == ".":
                reader.take()
                parts.append(reader.take())
            field = ".".join(parts)
            if not _PATH_RE.fullmatch(field):
                raise LiteralError("invalid managed field path")
            reader.expect("=")
            value = reader.value()
            reader.expect(";")
            if field in values:
                raise LiteralError("duplicate managed assignment")
            values[field] = value
        except LiteralError as exc:
            line = base_line + block.count("\n", 0, reader.position)
            errors.append(f"{field}: {exc} (line {line})")
            # Do not attempt recovery inside a nested attrset: doing so could
            # interpret its members as independent machine assignments.
            break
    log.debug("agents", "managed block parsed", path=str(path), fields=len(values), errors=len(errors))
    return values, True, errors


def _migration_values(machine_values: dict[str, Any]) -> dict[str, Any]:
    """Build the first managed block from the legacy scalar LLM settings."""
    steps_url = str(machine_values.get("envy.llm.steps.url", "") or os.getenv("STEPFUN_BASE_URL", "") or "https://stepfun.com")
    steps_model = str(machine_values.get("envy.llm.steps.model", "") or "step-3.7-flash")
    deepseek_url = str(machine_values.get("envy.llm.deepseek.url", "") or "https://api.deepseek.com")
    deepseek_model = str(machine_values.get("envy.llm.deepseek.model", "") or "deepseek-v4-pro")
    stepfun_openai = steps_url.rstrip("/")
    if not stepfun_openai.endswith("/v1"):
        stepfun_openai += "/v1"
    return {
        "envy.llm.credentials.stepfun.kind": "api-key",
        "envy.llm.credentials.stepfun.secretPath": "llm/steps/apikey",
        "envy.llm.credentials.deepseek.kind": "api-key",
        "envy.llm.credentials.deepseek.secretPath": "llm/deepseek/apikey",
        "envy.llm.endpoints.stepfun-openai.baseUrl": stepfun_openai,
        "envy.llm.endpoints.stepfun-openai.protocol": "openai-compatible",
        "envy.llm.endpoints.stepfun-anthropic.baseUrl": steps_url,
        "envy.llm.endpoints.stepfun-anthropic.protocol": "anthropic-compatible",
        "envy.llm.endpoints.deepseek-openai.baseUrl": deepseek_url,
        "envy.llm.endpoints.deepseek-openai.protocol": "openai-compatible",
        "envy.agents.codex.adapter": "codex",
        "envy.agents.codex.endpoint": "stepfun-openai",
        "envy.agents.codex.credential": "stepfun",
        "envy.agents.codex.model": steps_model,
        "envy.agents.cc.adapter": "claude-code",
        "envy.agents.cc.endpoint": "stepfun-anthropic",
        "envy.agents.cc.credential": "stepfun",
        "envy.agents.cc.model": steps_model,
        "envy.agents.cc.aliases": ["ccli"],
    }


def _validate_values(values: dict[str, Any]) -> list[str]:
    errors: list[str] = obsolete_agent_paths(values)
    agent_names: set[str] = set()
    endpoint_names: set[str] = set()
    credential_names: set[str] = set()
    for path, value in values.items():
        match = re.fullmatch(r"envy\.(agents|llm\.(?:endpoints|credentials))\.([A-Za-z0-9][A-Za-z0-9_-]*)(?:\.([A-Za-z0-9_-]+))?", path)
        if not match:
            errors.append(path)
            continue
        namespace, name, field = match.groups()
        if namespace == "agents":
            agent_names.add(name)
            if field not in {"adapter", "api", "endpoint", "credential", "model", "contextWindow", "maxTokens", "command", "args", "aliases", "skills", "additionalSettings", "moreThinking", *AGENT_FIELD_ALIASES}:
                errors.append(path)
            elif field in {"contextWindow", "maxTokens"} and value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                errors.append(path)
            elif field in {"args", "aliases", "skills"} and (
                not isinstance(value, list) or not all(isinstance(item, str) for item in value)
            ):
                errors.append(path)
            elif field == "additionalSettings" and not isinstance(value, dict):
                errors.append(path)
            elif field in {"moreThinking", *AGENT_FIELD_ALIASES} and not isinstance(value, bool):
                errors.append(path)
            elif field == "api" and value is not None and value not in API_CHOICES:
                errors.append(path)
            elif field in {"adapter", "endpoint", "credential", "model", "command"} and value is not None and not isinstance(value, str):
                errors.append(path)
        elif namespace == "llm.endpoints":
            endpoint_names.add(name)
            if field not in {"baseUrl", "protocol"} or not isinstance(value, str):
                errors.append(path)
            elif field == "protocol" and value not in {"openai-compatible", "anthropic-compatible", "native"}:
                errors.append(path)
            elif field == "baseUrl" and not value.startswith(("http://", "https://")):
                errors.append(path)
        elif namespace == "llm.credentials":
            credential_names.add(name)
            if field not in {"kind", "secretPath"} or not isinstance(value, str):
                errors.append(path)
            elif field == "kind" and value not in {"api-key", "token", "basic"}:
                errors.append(path)
            elif field == "secretPath" and not _SAFE_SECRET_PATH.fullmatch(value):
                errors.append(path)

    declared_endpoints = {path.split(".")[3] for path in values if path.startswith("envy.llm.endpoints.")}
    declared_credentials = {path.split(".")[3] for path in values if path.startswith("envy.llm.credentials.")}
    for name in sorted(agent_names):
        if f"envy.agents.{name}.adapter" not in values:
            errors.append(f"envy.agents.{name}.adapter")
    for name in sorted(endpoint_names):
        if f"envy.llm.endpoints.{name}.baseUrl" not in values:
            errors.append(f"envy.llm.endpoints.{name}.baseUrl")
    for name in sorted(credential_names):
        if f"envy.llm.credentials.{name}.secretPath" not in values:
            errors.append(f"envy.llm.credentials.{name}.secretPath")
    for path, value in values.items():
        if not path.startswith("envy.agents.") or not path.endswith((".endpoint", ".credential")) or not isinstance(value, str):
            continue
        if path.endswith(".endpoint") and value not in declared_endpoints | _KNOWN_ENDPOINTS:
            errors.append(path)
        if path.endswith(".credential") and value not in declared_credentials | _KNOWN_CREDENTIALS:
            errors.append(path)
    return sorted(set(errors))


def refine_managed(
    *, machine_values: dict[str, Any], write: bool = True, path: Path | None = None,
) -> dict[str, Any]:
    """Validate or create the agent block used by config refine/check."""
    values, present, errors = read_managed_values(path)
    result: dict[str, Any] = {"added": [], "removed": [], "warnings": [], "errors": list(errors), "changed": False}
    if errors:
        for error in errors:
            log.error("agents", "cannot parse managed agent configuration", path=str(path or machine_config_file()), reason=error)
        log.hint("Use literal Nix settings or builtins.fromJSON in the managed agent block; no values were changed.")
        return result
    if not present:
        migrated = _migration_values(machine_values)
        invalid = _validate_values(migrated)
        if invalid:
            result["errors"].extend(invalid)
            for field in invalid:
                log.error("agents", "invalid legacy LLM migration source", field=field)
            return result
        result["added"] = sorted(migrated)
        if write:
            _write_values(migrated, path=path)
            result["changed"] = True
            log.fix("agents", "created managed agent block from legacy LLM fields", count=len(migrated))
        else:
            result["errors"].append("envy agents managed block")
            log.error("agents", "agent managed block is missing")
            log.hint("Run: envy config refine")
        return result

    # A machine may already have new selections alongside old scalar fields.
    # Copy missing compatibility selections, keeping every explicit new value.
    # Once the scalar migration source is gone, do not recreate deleted fields.
    if any(machine_values.get(key) for key in (
        "envy.llm.steps.url", "envy.llm.steps.model",
        "envy.llm.deepseek.url", "envy.llm.deepseek.model",
    )):
        migrated = _migration_values(machine_values)
        result["added"] = sorted(set(migrated) - set(values))
        values = migrated | values

    values, renamed, legacy_removed, rename_errors = normalize_agent_fields(values)
    result["added"].extend(renamed)
    result["removed"].extend(legacy_removed)
    if rename_errors:
        for field in rename_errors:
            log.error("agents", "legacy agent field conflicts with moreThinking", field=field)

    obsolete_removed = obsolete_agent_paths(values)
    result["removed"].extend(obsolete_removed)
    for obsolete in obsolete_removed:
        values.pop(obsolete, None)

    invalid = sorted(set(_validate_values(values) + rename_errors))
    if invalid:
        result["errors"].extend(invalid)
        for path in invalid:
            log.error("agents", "invalid agent managed field", field=path)
        log.hint("Run: envy agent set ... or edit the managed agent block")
    else:
        if result["added"] or result["removed"]:
            if write:
                _write_values(values, path=path)
                result["changed"] = True
                if result["added"]:
                    log.fix("agents", "migrated missing legacy LLM selections", count=len(result["added"]))
                for obsolete in obsolete_removed:
                    log.fix("agents", "removed obsolete agent field", field=obsolete)
            else:
                result["errors"].extend(result["added"])
                result["errors"].extend(result["removed"])
                if result["added"]:
                    log.error("agents", "legacy LLM selections need migration")
                for obsolete in obsolete_removed:
                    log.error("agents", "obsolete agent field needs cleanup", field=obsolete)
                log.hint("Run: envy config refine")
        if not result["errors"]:
            log.ok("agents", "managed agent selections are valid", fields=len(values))
    return result


def managed_credential_paths() -> dict[str, str]:
    values, _present, _errors = read_managed_values()
    result: dict[str, str] = {}
    for path, value in values.items():
        prefix = "envy.llm.credentials."
        if path.startswith(prefix) and path.endswith(".secretPath") and isinstance(value, str):
            result[path[len(prefix):-len(".secretPath")]] = value
    return result


def _put_nested(root: dict[str, Any], parts: list[str], value: Any) -> None:
    current = root
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            return
        current = child
    current[parts[-1]] = value


def _managed_manifest(values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn the source managed block into the same shape as the Nix manifest."""
    llm: dict[str, Any] = {"credentials": {}, "endpoints": {}}
    agents: dict[str, Any] = {}
    for path, value in canonicalize_agent_values(values).items():
        if path.startswith("envy.llm."):
            _put_nested(llm, path.split(".")[2:], value)
        elif path.startswith("envy.agents."):
            _put_nested(agents, path.split(".")[2:], value)
    return llm, agents


def config_view(
    manifest: dict[str, Any] | None = None,
    *,
    secret_data: dict[str, Any] | None = None,
    decrypt_ok: bool | None = None,
) -> dict[str, Any]:
    """Normalize evaluated/source agent configuration for CLI and TUI views.

    Only endpoint metadata, logical credential references, and boolean secret
    status are returned. Secret values never enter this view.
    """
    values = _read_values()
    evaluated = isinstance(manifest, dict) and bool(manifest)
    if evaluated:
        llm = manifest.get("llm", {})
        agents = manifest.get("agents", {})
        llm = llm if isinstance(llm, dict) else {}
        agents = agents if isinstance(agents, dict) else {}
    else:
        llm, agents = _managed_manifest(values)

    if secret_data is None or decrypt_ok is None:
        secret_data, decrypt_ok = read_secrets_data()
    secret_data = secret_data if isinstance(secret_data, dict) else {}

    credentials = llm.get("credentials", {})
    credentials = credentials if isinstance(credentials, dict) else {}
    credentials = {name: dict(item) for name, item in credentials.items() if isinstance(item, dict)}
    for name, credential in credentials.items():
        path = credential.get("secretPath")
        current: Any = secret_data
        for part in str(path or "").split("/"):
            if not part:
                continue
            current = current.get(part, {}) if isinstance(current, dict) else {}
        credential["credentialSet"] = bool(
            path and decrypt_ok and isinstance(current, str) and current
        )
    # Older manifest caches may still carry the scalar compatibility tree.
    # Frontends expose only the current endpoint/credential configuration.
    llm = {"endpoints": llm.get("endpoints", {}), "credentials": credentials}

    normalized_agents: dict[str, Any] = {}
    for name, item in agents.items():
        if not isinstance(item, dict) or is_obsolete_agent(name, item.get("adapter")):
            continue
        agent = canonicalize_agent_object(item)
        if agent.get("adapter") == "pi":
            agent.setdefault("moreThinking", False)
        credential_name = agent.get("credential")
        credential = credentials.get(credential_name, {}) if isinstance(credential_name, str) else {}
        agent["credentialSet"] = bool(credential.get("credentialSet", False))
        normalized_agents[name] = agent

    dynamic_values: dict[str, str] = {}
    fields: list[dict[str, Any]] = []

    def add_field(path: str, value: Any, choices: list[str] | None = None) -> None:
        if value is None:
            value = ""
        if isinstance(value, (str, int, float, bool)):
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            dynamic_values[path] = rendered
            fields.append({"path": path, "value": rendered, "choices": choices or []})

    endpoints = llm.get("endpoints", {})
    if isinstance(endpoints, dict):
        for name, endpoint in sorted(endpoints.items()):
            if not isinstance(endpoint, dict):
                continue
            add_field(f"envy.llm.endpoints.{name}.baseUrl", endpoint.get("baseUrl"))
            add_field(
                f"envy.llm.endpoints.{name}.protocol",
                endpoint.get("protocol"),
                ["openai-compatible", "anthropic-compatible", "native"],
            )

    for name, credential in sorted(credentials.items()):
        add_field(
            f"envy.llm.credentials.{name}.kind",
            credential.get("kind"),
            ["api-key", "token", "basic"],
        )
        add_field(f"envy.llm.credentials.{name}.secretPath", credential.get("secretPath"))

    for name, agent in sorted(normalized_agents.items()):
        for field in ("adapter", "endpoint", "credential", "model", "contextWindow", "maxTokens", "command"):
            add_field(f"envy.agents.{name}.{field}", agent.get(field))
        if agent.get("adapter") == "pi":
            add_field(
                f"envy.agents.{name}.api",
                agent.get("api"),
                list(API_CHOICES),
            )
        if agent.get("adapter") == "pi":
            add_field(f"envy.agents.{name}.moreThinking", agent["moreThinking"], ["true", "false"])

    return {
        "evaluated": evaluated,
        "values": dynamic_values,
        "fields": fields,
        "llm": llm,
        "agents": normalized_agents,
        "managed": values,
    }


def _render_value(value: Any) -> str:
    if isinstance(value, dict):
        encoded = json.dumps(json.dumps(value, ensure_ascii=False, allow_nan=False), ensure_ascii=False)
        return "builtins.fromJSON " + encoded.replace("${", r"\${")
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[ " + " ".join(_render_value(item) for item in value) + " ]"
    return f'"{_escape_nix_string(str(value))}"'


def _render(values: dict[str, Any]) -> str:
    lines = [MANAGED_START + "\n", "  # envY owns agent selections, endpoint metadata, and credential references here.\n"]
    sections = [
        ("LLM CREDENTIALS", "envy.llm.credentials."),
        ("LLM ENDPOINTS", "envy.llm.endpoints."),
        ("AGENTS", "envy.agents."),
    ]
    for title, prefix in sections:
        keys = sorted(key for key in values if key.startswith(prefix))
        if not keys:
            continue
        lines.append(f"\n  # --- {title} ---\n")
        for key in keys:
            lines.append(f"  {key} = {_render_value(values[key])};\n")
    lines.append(MANAGED_END)
    return "".join(lines)


def _write_values(values: dict[str, Any], *, path: Path | None = None) -> Path:
    path = path or machine_config_file()
    if not path.exists():
        raise FileNotFoundError(f"machine configuration is missing: {path}")
    text = path.read_text()
    block = _render(values)
    if _block_pattern().search(text):
        updated = _block_pattern().sub(lambda _: block, text, count=1)
    else:
        closing = text.rfind("\n}")
        if closing < 0:
            raise ValueError(f"cannot locate the top-level closing brace in {path}")
        updated = text[:closing].rstrip() + "\n\n" + block + "\n" + text[closing:]
    atomic_write_text(path, updated if updated.endswith("\n") else updated + "\n")
    invalidate_machine_manifest()
    return path


def _nested_value(values: dict[str, Any], prefix: str, name: str, field: str, default: Any = None) -> Any:
    return values.get(f"{prefix}.{name}.{field}", default)


def _secret_value(data: dict[str, Any], path: str | None) -> str:
    current: Any = data
    for part in str(path or "").split("/"):
        if not part:
            continue
        current = current.get(part) if isinstance(current, dict) else None
    return current if isinstance(current, str) else ""


def _runtime_env(values: dict[str, Any], agent: dict[str, Any], secrets: dict[str, Any]) -> str:
    adapter = str(agent.get("adapter") or "")
    endpoint_name = agent.get("endpoint")
    endpoint = {}
    if isinstance(endpoint_name, str):
        endpoint = {
            "baseUrl": _nested_value(values, "envy.llm.endpoints", endpoint_name, "baseUrl", ""),
            "protocol": _nested_value(values, "envy.llm.endpoints", endpoint_name, "protocol", "openai-compatible"),
        }
    credential_name = agent.get("credential")
    secret_path = _nested_value(values, "envy.llm.credentials", str(credential_name), "secretPath", "") if credential_name else ""
    api = agent.get("api") or ("anthropic-messages" if endpoint.get("protocol") == "anthropic-compatible" else "openai-completions")
    prefix = "ANTHROPIC" if api == "anthropic-messages" or adapter == "claude-code" else ("STEPFUN" if adapter == "stepgo" else "OPENAI")
    unset_names = f"{prefix}_BASE_URL {prefix}_API_KEY {prefix}_MODEL"
    if prefix == "ANTHROPIC":
        unset_names += " ANTHROPIC_AUTH_TOKEN"
    lines = [f"unset {unset_names}"]
    if endpoint.get("baseUrl"):
        lines.append(f"export {prefix}_BASE_URL={shlex.quote(str(endpoint['baseUrl']))}")
    secret = _secret_value(secrets, secret_path)
    if secret:
        lines.append(f"export {prefix}_API_KEY={shlex.quote(secret)}")
        if prefix == "ANTHROPIC":
            lines.append(f"export ANTHROPIC_AUTH_TOKEN={shlex.quote(secret)}")
    if agent.get("model"):
        lines.append(f"export {prefix}_MODEL={shlex.quote(str(agent['model']))}")
        lines.append(f"export ENVY_AGENT_MODEL={shlex.quote(str(agent['model']))}")
    else:
        lines.append("unset ENVY_AGENT_MODEL")
    lines.append("export GPT_IMAGE_MODEL_NAME=gpt-image-2")
    return "\n".join(lines) + "\n"


def refresh_runtime(*, values: dict[str, Any] | None = None) -> list[str]:
    """Refresh private agent runtime overrides without evaluating or applying Nix."""
    values = values if values is not None else _read_values()
    secrets, decrypt_ok = read_secrets_data()
    if not isinstance(secrets, dict):
        secrets = {}
    if not decrypt_ok:
        log.warn("agents", "runtime credentials were not refreshed; sops decryption failed")
    agents: dict[str, dict[str, Any]] = {}
    for path, value in values.items():
        parts = path.split(".")
        if len(parts) == 4 and parts[:2] == ["envy", "agents"]:
            agents.setdefault(parts[2], {})[parts[3]] = value
    ensure_private_directory(_RUNTIME_DIR)
    written: list[str] = []
    for name, agent in agents.items():
        if not agent.get("adapter"):
            continue
        target = _RUNTIME_DIR / f"{name}.env"
        atomic_write_text(target, _runtime_env(values, agent, secrets), mode=0o600, private_parent=True)
        written.append(name)
    return written


def _runtime_change_kind(path: str) -> str:
    parts = path.split(".")
    if len(parts) == 4 and parts[:2] == ["envy", "agents"] and parts[3] in _LIVE_AGENT_FIELDS:
        return "live"
    if len(parts) == 5 and parts[:3] == ["envy", "llm", "endpoints"] and parts[4] in _LIVE_ENDPOINT_FIELDS:
        return "live"
    return "apply"


def _validate_path(path: str) -> None:
    if not _PATH_RE.fullmatch(path):
        raise typer.BadParameter(f"invalid envY agent path: {path}")


def _set(path: str, value: Any) -> Path:
    _validate_path(path)
    legacy_field = next((field for field in AGENT_FIELD_ALIASES if path.endswith("." + field)), None)
    if legacy_field is not None:
        raise typer.BadParameter(
            f"{path} was renamed to {path[:-len(legacy_field)]}{AGENT_FIELD_ALIASES[legacy_field]}; run envy config refine first"
        )
    if path.startswith("envy.agents."):
        field = path.rsplit(".", 1)[-1]
        if field in {"args", "aliases", "skills"}:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise typer.BadParameter(f"{path} requires a JSON array of strings") from exc
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise typer.BadParameter(f"{path} requires a JSON array of strings")
        elif field in {"endpoint", "credential", "model", "api", "contextWindow", "maxTokens"}:
            if value == "null":
                value = None
            elif field in {"contextWindow", "maxTokens"}:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise typer.BadParameter(f"{path} must be a positive integer or null") from None
                if value <= 0:
                    raise typer.BadParameter(f"{path} must be a positive integer or null")
    if path.startswith("envy.agents.") and path.endswith(".api") and value is not None and value not in API_CHOICES:
        raise typer.BadParameter(f"api must be one of: {', '.join(API_CHOICES)}")
    if path.startswith("envy.agents.") and path.endswith(".additionalSettings"):
        try:
            value = json.loads(value) if isinstance(value, str) else value
            if not isinstance(value, dict):
                raise ValueError
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError):
            raise typer.BadParameter("additionalSettings must be a JSON object") from None
    if path.startswith("envy.agents.") and path.endswith(".moreThinking"):
        if isinstance(value, str) and value in {"true", "false"}:
            value = value == "true"
        if not isinstance(value, bool):
            raise typer.BadParameter(f"{path} must be true or false")
    values, _present, errors = read_managed_values()
    if errors:
        raise ValueError("cannot edit malformed agent managed block")
    if path.startswith("envy.agents."):
        name = path.split(".")[2]
        adapter = value if path.endswith(".adapter") else values.get(f"envy.agents.{name}.adapter")
        if is_obsolete_agent(name, adapter):
            raise typer.BadParameter("OMP is retired; run envy config refine to remove its obsolete configuration")
    values[path] = value
    if path in _validate_values(values):
        raise typer.BadParameter(f"invalid agent configuration field: {path}")
    return _write_values(values)


def _manifest_agents() -> dict[str, Any]:
    manifest = machine_manifest()
    agents = manifest.get("agents", {}) if isinstance(manifest, dict) else {}
    if not isinstance(agents, dict):
        return {}
    manifest_llm = manifest.get("llm", {}) if isinstance(manifest, dict) else {}
    credentials = manifest_llm.get("credentials", {}) if isinstance(manifest_llm, dict) else {}
    secret_data, decrypt_ok = read_secrets_data()
    result = {}
    for name, agent in agents.items():
        if not isinstance(agent, dict) or is_obsolete_agent(name, agent.get("adapter")):
            continue
        value = canonicalize_agent_object(agent)
        credential_name = value.get("credential")
        secret_path = credentials.get(credential_name, {}).get("secretPath") if isinstance(credentials, dict) else None
        current = secret_data if decrypt_ok and isinstance(secret_data, dict) else {}
        for part in str(secret_path or "").split("/"):
            if not part:
                continue
            current = current.get(part, {}) if isinstance(current, dict) else {}
        value["credentialSet"] = bool(secret_path and decrypt_ok and isinstance(current, str) and current)
        result[name] = value
    return result


def _payload(values: dict[str, Any], agents: dict[str, Any]) -> dict[str, Any]:
    return {
        "machine": current_machine_id(),
        "agents": agents,
        "managed": values,
    }


app = typer.Typer(name="agent", help="Manage machine-owned LLM agent instances and connections")


@app.command(name="refresh")
def cmd_refresh(json_output: bool = typer.Option(False, "--json")):
    """Refresh private runtime overrides without running Nix apply."""
    refreshed = refresh_runtime()
    if json_output:
        emit("agent.refresh", data={"agents": refreshed})
    else:
        log.ok("agents", "runtime configuration refreshed", agents=", ".join(refreshed) or "none")
        log.hint("Changes apply to newly started agent processes.")


@app.command(name="list")
@app.command(name="ls", rich_help_panel="Aliases")
def cmd_list(json_output: bool = typer.Option(False, "--json")):
    """List evaluated agents without exposing secret values."""
    values = _read_values()
    agents = _manifest_agents()
    data = _payload(values, agents)
    if json_output:
        emit("agent.list", data=data)
        return
    for name, agent in sorted(agents.items()):
        status = "ready" if agent.get("endpointConfigured") and agent.get("credentialSet") else "incomplete"
        log.console.print(f"{name}: {agent.get('adapter', 'unknown')} ({status})")


@app.command(name="show")
def cmd_show(name: str = typer.Argument(..., autocompletion=complete_agent_names), json_output: bool = typer.Option(False, "--json")):
    """Show one evaluated agent and its non-sensitive selections."""
    agents = _manifest_agents()
    if name not in agents:
        if json_output:
            emit_error("agent.show", f"unknown agent: {name}", code="not-found")
            raise typer.Exit(code=1)
        raise typer.BadParameter(f"unknown agent: {name}")
    if json_output:
        emit("agent.show", data={"name": name, "agent": agents[name]})
    else:
        log.console.print_json(json.dumps({"name": name, "agent": agents[name]}, ensure_ascii=False))


@app.command(name="set")
def cmd_set(
    path: str = typer.Argument(..., help="For example envy.agents.codex.model", autocompletion=complete_agent_paths),
    value: str = typer.Argument(..., autocompletion=complete_agent_values),
    json_output: bool = typer.Option(False, "--json"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Set one agent, endpoint, or credential metadata field."""
    try:
        target = _set(path, value)
    except (OSError, ValueError, typer.BadParameter) as exc:
        if json_output:
            emit_error("agent.set", str(exc), code="invalid-value")
            raise typer.Exit(code=1) from exc
        raise
    changed_kind = _runtime_change_kind(path)
    refreshed = refresh_runtime() if changed_kind == "live" else []
    if changed_kind == "live" and not json_output:
        log.ok("agents", "runtime configuration refreshed", agents=", ".join(refreshed) or "none")
        log.hint("Changes apply to newly started agent processes.")
        log.hint("Review and commit the machine configuration when ready.")
    elif changed_kind == "apply" and not json_output:
        log.warn("agents", "this change requires `envy apply` before it takes effect")
        log.hint("Run: envy apply")
    if json_output:
        emit("agent.set", data={"path": path, "value": value, "machine": current_machine_id(), "changeKind": changed_kind, "runtimeRefreshed": bool(refreshed)})


@app.command(name="add")
def cmd_add(
    name: str = typer.Argument(...),
    adapter: str = typer.Option(..., "--adapter", autocompletion=complete_adapters),
    api: Optional[str] = typer.Option(None, "--api", help="Pi wire API override: openai-completions, openai-responses, or anthropic-messages", autocompletion=complete_apis),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", autocompletion=complete_endpoints),
    credential: Optional[str] = typer.Option(None, "--credential", autocompletion=complete_credentials),
    model: Optional[str] = typer.Option(None, "--model"),
    command: Optional[str] = typer.Option(None, "--command"),
    json_output: bool = typer.Option(False, "--json"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Create an agent instance from an adapter and optional LLM selection."""
    if not re.fullmatch(_NAME, name):
        raise typer.BadParameter(f"invalid agent name: {name}")
    if is_obsolete_agent(name, adapter):
        raise typer.BadParameter("OMP is retired and cannot be added")
    values = _read_values()
    prefix = f"envy.agents.{name}."
    if any(key.startswith(prefix) for key in values):
        raise typer.BadParameter(f"agent already exists: {name}")
    if api is not None and api not in API_CHOICES:
        raise typer.BadParameter(f"api must be one of: {', '.join(API_CHOICES)}")
    values[prefix + "adapter"] = adapter
    for field, value in (("api", api), ("endpoint", endpoint), ("credential", credential), ("model", model), ("command", command)):
        if value is not None:
            values[prefix + field] = value
    target = _write_values(values)
    offer_mutation_commit([target], f"chore(agent): add {name}", quiet=json_output or yes)
    if json_output:
        emit("agent.add", data={"name": name, "adapter": adapter, "machine": current_machine_id()})


@app.command(name="remove")
@app.command(name="rm", rich_help_panel="Aliases")
def cmd_remove(name: str = typer.Argument(..., autocompletion=complete_managed_agent_names), json_output: bool = typer.Option(False, "--json"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Remove an envY-managed agent instance."""
    values = _read_values()
    prefix = f"envy.agents.{name}."
    removed = [key for key in values if key.startswith(prefix)]
    if not removed:
        if json_output:
            emit_error("agent.remove", f"unknown managed agent: {name}", code="not-found")
            raise typer.Exit(code=1)
        raise typer.BadParameter(f"unknown managed agent: {name}")
    for key in removed:
        values.pop(key, None)
    target = _write_values(values)
    offer_mutation_commit([target], f"chore(agent): remove {name}", quiet=json_output or yes)
    if json_output:
        emit("agent.remove", data={"name": name, "removed": removed})


@app.command(name="credential-set")
def cmd_credential_set(
    name: str = typer.Argument(..., autocompletion=complete_credentials),
    secret_path: str = typer.Option(..., "--secret-path", help="Encrypted YAML path", autocompletion=complete_credential_paths),
    stdin: bool = typer.Option(False, "--stdin"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Create/update a logical credential and set its encrypted value."""
    if not stdin:
        raise typer.BadParameter("credential values require --stdin")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", secret_path):
        raise typer.BadParameter("secret path must contain slash-separated safe names")
    value = __import__("sys").stdin.read().rstrip("\r\n")
    machine_path = machine_config_file()
    with FileTransaction([machine_path, SECRETS_FILE]) as transaction:
        set_secret_value(secret_path, value)
        _set(f"envy.llm.credentials.{name}.secretPath", secret_path)
        transaction.commit()
    offer_mutation_commit([machine_path, SECRETS_FILE], f"chore(agent): update credential {name}", quiet=json_output)
    if json_output:
        emit("agent.credential-set", data={"name": name, "secretPath": secret_path, "set": bool(value)})

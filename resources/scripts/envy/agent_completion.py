"""Shell candidates from agent metadata; never inspect credential values."""

import re
import subprocess

from envy.evaluation import machine_manifest


ADAPTERS = ("claude-code", "codex", "openai-compatible-cli", "pi", "stepgo")
_AGENT_FIELDS = ("adapter", "endpoint", "credential", "model", "contextWindow", "maxTokens", "command", "additionalSettings")
_PROTOCOLS = ("openai-compatible", "anthropic-compatible", "native")
_KINDS = ("api-key", "token", "basic")


def _managed_values():
    from envy.agent import _read_values

    try:
        return _read_values()
    except (OSError, RuntimeError, ValueError):
        return {}


def _metadata():
    from envy.agent import _managed_manifest

    try:
        manifest = machine_manifest(completion=True) or {}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    local_llm, local_agents = _managed_manifest(_managed_values())
    llm = manifest.get("llm", {})
    llm = llm if isinstance(llm, dict) else {}

    def merge(evaluated, managed):
        result = {name: dict(item) for name, item in evaluated.items() if isinstance(item, dict)} if isinstance(evaluated, dict) else {}
        for name, item in managed.items():
            if isinstance(item, dict):
                result[name] = result.get(name, {}) | item
        return result

    return {
        "agents": merge(manifest.get("agents", {}), local_agents),
        "endpoints": merge(llm.get("endpoints", {}), local_llm["endpoints"]),
        "credentials": merge(llm.get("credentials", {}), local_llm["credentials"]),
    }


def _matches(values, incomplete, description):
    return [(value, description) for value in sorted({value for value in values if isinstance(value, str)}) if value.startswith(incomplete)]


def complete_agent_names(ctx, incomplete):
    return _matches(_metadata()["agents"], incomplete, "configured agent")


def complete_managed_agent_names(ctx, incomplete):
    from envy.agent import _managed_manifest

    _, agents = _managed_manifest(_managed_values())
    return _matches(agents, incomplete, "managed agent")


def complete_adapters(ctx, incomplete):
    return _matches(ADAPTERS, incomplete, "agent adapter")


def complete_apis(ctx, incomplete):
    from envy.agent import API_CHOICES

    return _matches(API_CHOICES, incomplete, "wire API")


def complete_endpoints(ctx, incomplete):
    return _matches(_metadata()["endpoints"], incomplete, "configured endpoint")


def complete_credentials(ctx, incomplete):
    return _matches(_metadata()["credentials"], incomplete, "credential reference")


def complete_profile_names(ctx, incomplete):
    from envy.agent import _profile_values

    return _matches(_profile_values(_managed_values()), incomplete, "agent profile")


def complete_managed_profile_names(ctx, incomplete):
    return complete_profile_names(ctx, incomplete)


def complete_credential_paths(ctx, incomplete):
    from envy.schemas.config import SECRET_FIELDS

    paths = {field.yaml_path for field in SECRET_FIELDS if field.yaml_path}
    paths.update(item["secretPath"] for item in _metadata()["credentials"].values()
                 if isinstance(item.get("secretPath"), str))
    return _matches(paths, incomplete, "encrypted YAML path")


def complete_agent_paths(ctx, incomplete):
    prefixes = ("envy.agents.", "envy.agentProfiles.", "envy.llm.endpoints.", "envy.llm.credentials.")
    if not any(prefix.startswith(incomplete) or incomplete.startswith(prefix) for prefix in prefixes):
        return []
    metadata = _metadata()
    from envy.agent import _profile_values

    paths = {}
    for name, agent in metadata["agents"].items():
        fields = list(_AGENT_FIELDS) + ["profile"]
        if agent.get("adapter") == "pi":
            fields.append("api")
        if agent.get("adapter") == "pi":
            fields.append("moreThinking")
        for field in fields:
            paths[f"envy.agents.{name}.{field}"] = "agent setting"
    profiles = _profile_values(_managed_values())
    for name in profiles:
        paths[f"envy.agentProfiles.{name}"] = "agent profile"
        for field in ("endpoint", "credential", "api", "model", "contextWindow", "maxTokens"):
            paths[f"envy.agentProfiles.{name}.{field}"] = "agent profile setting"
    for group, fields in (("endpoints", ("baseUrl", "protocol")), ("credentials", ("kind", "secretPath"))):
        for name in metadata[group]:
            for field in fields:
                paths[f"envy.llm.{group}.{name}.{field}"] = f"{group[:-1]} metadata"
    return [(path, description) for path, description in sorted(paths.items()) if path.startswith(incomplete)]


def complete_agent_values(ctx, incomplete):
    params = getattr(ctx, "params", {})
    path = params.get("path", "") if isinstance(params, dict) else ""
    match = re.fullmatch(r"envy\.(agents|agentProfiles|llm\.endpoints|llm\.credentials)\.([A-Za-z0-9][A-Za-z0-9_-]*)\.([A-Za-z0-9_-]+)", path or "")
    if not match:
        return []
    namespace, name, field = match.groups()
    if namespace == "agentProfiles":
        if field == "endpoint":
            return complete_endpoints(ctx, incomplete)
        if field == "credential":
            return complete_credentials(ctx, incomplete)
        if field == "api":
            return complete_apis(ctx, incomplete)
        return []
    if namespace == "agents":
        if field in {"model", "contextWindow", "maxTokens", "command", "additionalSettings"}:
            return []
        if field == "profile":
            return complete_profile_names(ctx, incomplete)
        if field not in {"adapter", "api", "endpoint", "credential", "moreThinking", "profile"}:
            return []
        metadata = _metadata()
        callbacks = {"adapter": complete_adapters, "api": complete_apis}
        if field in callbacks:
            return callbacks[field](ctx, incomplete)
        if field == "endpoint":
            return _matches(metadata["endpoints"], incomplete, "configured endpoint")
        if field == "credential":
            return _matches(metadata["credentials"], incomplete, "credential reference")
        if field == "moreThinking":
            return _matches(("true", "false"), incomplete, path)
    elif namespace == "llm.endpoints" and field == "protocol":
        return _matches(_PROTOCOLS, incomplete, path)
    elif namespace == "llm.credentials":
        if field == "kind":
            return _matches(_KINDS, incomplete, path)
        if field == "secretPath":
            return complete_credential_paths(ctx, incomplete)
    return []

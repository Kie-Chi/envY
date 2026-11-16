"""Presentation metadata for config show; conditions remain Python policy."""

from typing import Any

from envy.agent_completion import ADAPTERS
from envy.schemas.config import FieldDef


class _ConditionValues(dict):
    """Detect missing dependencies even when a condition supplies a default."""

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if value is None:
            raise KeyError(key)
        return value

    def get(self, key, default=None):
        return self[key]


def field_metadata(field: FieldDef, values: dict[str, Any]) -> dict[str, Any]:
    """Evaluate applicability without substituting schema defaults."""
    active = None
    if field.condition is None:
        active = True
    else:
        try:
            active = bool(field.condition(_ConditionValues(values)))
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    state = "unknown" if active is None else "active" if active else "inactive"
    return {
        "group": field.group,
        "setter": "config.set" if field.dest == "machine" else None,
        "prompt": field.prompt,
        "nixType": field.nix_type,
        "choices": list(field.choices),
        "required": field.required,
        "active": active,
        "conditionState": state,
        "activationHint": (
            "Applicability is unknown because condition inputs are unavailable."
            if active is None else
            "The current condition is not met; this value is retained."
            if not active else
            "Changes take effect after envy apply."
        ),
    }


_DYNAMIC_PROMPTS = {
    "adapter": "Agent adapter",
    "endpoint": "LLM endpoint reference",
    "credential": "LLM credential reference (secret value is never shown)",
    "model": "Model identifier",
    "contextWindow": "Context window in tokens (empty uses adapter defaults)",
    "maxTokens": "Maximum output tokens (empty uses adapter defaults)",
    "command": "Agent command",
    "api": "Wire API",
    "thinkingLevelMap": "Pi thinking level mapping as JSON (null uses adapter defaults)",
    "moreThinking": "Enable additional Pi thinking levels",
    "models": "Agent model candidates as a JSON list",
    "baseUrl": "LLM endpoint base URL",
    "protocol": "LLM endpoint protocol",
    "kind": "Credential kind",
    "secretPath": "Encrypted secret YAML path (not the secret value)",
}


def dynamic_field_metadata(entry: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Describe existing dynamic fields without changing their values or visibility."""
    path = entry["path"]
    name = path.rsplit(".", 1)[-1]
    group = "AGENT PROFILES" if path.startswith("envy.agentProfiles.") else "AGENTS" if path.startswith("envy.agents.") else "LLM"
    nix_type = {
        "contextWindow": "nullOr positiveInt",
        "maxTokens": "nullOr positiveInt",
        "thinkingLevelMap": "nullOr attrsOf (nullOr string)",
        "models": "listOf attrs",
        "moreThinking": "bool",
        "endpoint": "nullOr string",
        "credential": "nullOr string",
        "model": "nullOr string",
        "command": "nullOr string",
        "api": "nullOr string",
    }.get(name, "string")
    choices = list(entry.get("choices", []))
    if name == "adapter":
        choices = list(ADAPTERS)
    elif name in {"endpoint", "credential"}:
        prefix = "envy.llm.endpoints." if name == "endpoint" else "envy.llm.credentials."
        choices = sorted({key[len(prefix):].split(".", 1)[0] for key in values if key.startswith(prefix)})
    condition = None
    if path.startswith("envy.agents.") and name in {"api", "moreThinking", "thinkingLevelMap", "models"}:
        adapter_path = path.rsplit(".", 1)[0] + ".adapter"
        allowed = {"pi"} if name in {"moreThinking", "thinkingLevelMap"} else {"pi", "codex"}
        def supported_adapter(inputs):
            adapter = inputs[adapter_path]
            if not adapter:
                raise KeyError(adapter_path)
            return adapter in allowed
        condition = supported_adapter
    metadata = field_metadata(FieldDef(
        group=group, dest="machine", path=path,
        prompt=_DYNAMIC_PROMPTS.get(name, name), nix_type=nix_type,
        choices=choices, condition=condition,
    ), values)
    from envy.agent import _runtime_change_kind

    metadata["setter"] = "agent.set"
    metadata["changeKind"] = _runtime_change_kind(path)
    if metadata["active"] is True and metadata["changeKind"] == "live":
        metadata["activationHint"] = (
            "The setter refreshes runtime overrides when credentials are available; "
            "changes apply to newly started agent processes."
        )
    return entry | metadata

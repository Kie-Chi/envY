"""Resolve Pi model candidates without accessing credentials or application state."""

from __future__ import annotations

import hashlib
import json
from typing import Any


MODEL_LIMIT_FIELDS = ("contextWindow", "maxTokens")
MODEL_FIELDS = ("endpoint", "credential", "api", "model") + MODEL_LIMIT_FIELDS
API_CHOICES = ("openai-completions", "openai-responses", "anthropic-messages")


def profile_fields(profile: dict[str, Any]) -> dict[str, Any]:
    return {field: profile.get(field) for field in MODEL_FIELDS}


def model_bindings(values: dict[str, Any], agent: dict[str, Any]) -> list[dict[str, Any]]:
    """Return default-first, deduplicated bindings with stable connection identities."""
    models = agent.get("models", [])
    multi = bool(models)
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    defaults = {field: agent.get(field) for field in MODEL_LIMIT_FIELDS}

    def resolve(item: dict[str, Any], *, candidate: bool = False) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("models entries must be objects")
        if candidate and set(item) - set(MODEL_FIELDS) - {"profile"}:
            raise ValueError("models entry contains an unsupported field")
        profile = item.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or not profile:
                raise ValueError("models profile must be a non-empty string")
            prefix = f"envy.agentProfiles.{profile}."
            source = {field: values[prefix + field] for field in MODEL_FIELDS if prefix + field in values
                      and (field not in MODEL_LIMIT_FIELDS or values[prefix + field] is not None)}
            if not source:
                raise ValueError("models entry references an unknown profile")
            source = defaults | source
            if candidate:
                source |= {field: item[field] for field in MODEL_FIELDS if field in item}
        else:
            source = defaults | item
        selection = profile_fields(source)
        for field in ("endpoint", "credential", "model"):
            value = selection[field]
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"models {field} must be a non-empty string or null")
        if selection["api"] is not None and selection["api"] not in API_CHOICES:
            raise ValueError("models entry has an unsupported API")
        for field in MODEL_LIMIT_FIELDS:
            value = selection[field]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"models {field} must be a positive integer or null")
        return selection

    primary_source = dict(agent)
    if agent.get("adapter") == "pi":
        # Match the shared defaults, including clearing a list-only selection.
        primary_source.setdefault("model", None if multi else "gpt-6.1-sol")
        primary_source.setdefault("endpoint", "stepfun-openai")
        primary_source.setdefault("credential", "stepfun")
    primary = resolve(primary_source)
    candidates = [primary]
    if multi:
        candidates = ([primary] if primary["model"] is not None else []) + [resolve(item, candidate=True) for item in models]
    bindings: list[dict[str, Any]] = []
    for selection in candidates:
        endpoint_name = selection["endpoint"]
        prefix = f"envy.llm.endpoints.{endpoint_name}."
        endpoint = {
            "baseUrl": values.get(prefix + "baseUrl", ""),
            "protocol": values.get(prefix + "protocol", "openai-compatible"),
        }
        credential = selection["credential"]
        if multi and (not selection["model"] or not endpoint["baseUrl"] or (
            credential is not None and f"envy.llm.credentials.{credential}.secretPath" not in values
        )):
            raise ValueError("models require a model, a known endpoint, and a known credential reference or null")
        api = selection["api"] or ("anthropic-messages" if endpoint["protocol"] == "anthropic-compatible" else "openai-completions")
        selection = selection | {"api": api}
        identity = hashlib.sha256(json.dumps(
            [endpoint_name, api, credential], ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()[:16]
        provider = f"envy-pi-{identity}" if multi else "envy-pi"
        variable = f"ENVY_PI_KEY_{identity.upper()}" if multi else (
            "ANTHROPIC_API_KEY" if api == "anthropic-messages" else "OPENAI_API_KEY"
        )
        binding = {
            "selection": selection, "endpoint": endpoint, "api": api,
            "provider": provider, "keyVariable": variable,
            "apiKey": "envy-no-key" if multi and credential is None else f"${variable}",
        }
        duplicate = next((entry for entry in bindings if entry["provider"] == provider
                          and entry["selection"]["model"] == selection["model"]), None)
        if duplicate is not None:
            if duplicate["selection"] != selection:
                raise ValueError("models contain conflicting metadata for the same connection and model")
        else:
            bindings.append(binding)
    return bindings

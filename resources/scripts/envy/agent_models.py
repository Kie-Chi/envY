"""Resolve Pi model candidates without accessing credentials or application state."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from envy import error
from envy.errno import ErrorCode


MODEL_LIMIT_FIELDS = ("contextWindow", "maxTokens")
MODEL_FIELDS = ("endpoint", "credential", "api", "model") + MODEL_LIMIT_FIELDS
API_CHOICES = ("openai-completions", "openai-responses", "anthropic-messages")


def profile_fields(profile: dict[str, Any]) -> dict[str, Any]:
    return {field: profile.get(field) for field in MODEL_FIELDS}


def model_bindings(values: dict[str, Any], agent: dict[str, Any], *, name: str = "pi") -> list[dict[str, Any]]:
    """Return default-first bindings; retain diagnostic origins only during resolution."""
    root = f"envy.agents.{name}"
    models = agent.get("models", [])
    multi = bool(models)
    if not isinstance(models, list):
        raise error.ConfigValidationError(error.invalid_type(root + ".models", "a list"))
    defaults = {field: agent.get(field) for field in MODEL_LIMIT_FIELDS}

    def resolve(item: dict[str, Any], path: str, *, candidate: bool = False):
        if not isinstance(item, dict):
            raise error.ConfigValidationError(error.invalid_type(path, "an object"))
        if candidate and set(item) - set(MODEL_FIELDS) - {"profile"}:
            raise error.ConfigValidationError(error.unknown_field(path, ("profile",) + MODEL_FIELDS))
        origins = {field: path + "." + field for field in MODEL_FIELDS}
        profile = item.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or not profile:
                raise error.ConfigValidationError(error.invalid_type(path + ".profile", "a non-empty string"))
            prefix = f"envy.agentProfiles.{profile}."
            source = {field: values[prefix + field] for field in MODEL_FIELDS if prefix + field in values
                      and (field not in MODEL_LIMIT_FIELDS or values[prefix + field] is not None)}
            if not any(key.startswith(prefix) for key in values):
                raise error.ConfigValidationError(error.unknown_reference(
                    path + ".profile", "profile", name=profile, declared=prefix[:-1]))
            origins.update({field: prefix + field for field in source})
            source = defaults | source
            if candidate:
                overrides = {field: item[field] for field in MODEL_FIELDS if field in item}
                source |= overrides
                origins.update({field: path + "." + field for field in overrides})
        else:
            source = defaults | item
        selection = profile_fields(source)
        for field in ("endpoint", "credential", "model"):
            value = selection[field]
            if value is not None and (not isinstance(value, str) or not value):
                raise error.ConfigValidationError(error.invalid_type(origins[field], "a non-empty string or null"))
        if selection["api"] is not None and selection["api"] not in API_CHOICES:
            raise error.ConfigValidationError(error.invalid_choice(origins["api"], API_CHOICES))
        for field in MODEL_LIMIT_FIELDS:
            value = selection[field]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise error.ConfigValidationError(error.invalid_type(origins[field], "a positive integer or null"))
        return selection, origins, path

    primary_source = dict(agent)
    if agent.get("adapter") == "pi":
        primary_source.setdefault("model", None if multi else "gpt-6.1-sol")
        primary_source.setdefault("endpoint", "stepfun-openai")
        primary_source.setdefault("credential", "stepfun")
    primary = resolve(primary_source, root)
    candidates = [primary]
    if multi:
        candidates = ([primary] if primary[0]["model"] is not None else []) + [
            resolve(item, f"{root}.models[{index}]", candidate=True) for index, item in enumerate(models)]
    bindings: list[dict[str, Any]] = []
    binding_paths: list[str] = []
    for selection, origins, path in candidates:
        def fail(field: str, code: ErrorCode, message: str, hint: str):
            location = path + "." + field
            if origins[field] != location:
                message += f" (inherited from {origins[field]})"
            raise error.ConfigValidationError(error.ConfigError(code, location, message, hint))

        endpoint_name = selection["endpoint"]
        prefix = f"envy.llm.endpoints.{endpoint_name}."
        endpoint = {
            "baseUrl": values.get(prefix + "baseUrl", ""),
            "protocol": values.get(prefix + "protocol", "openai-compatible"),
        }
        credential = selection["credential"]
        if multi:
            if not selection["model"]:
                fail("model", ErrorCode.MISSING_REQUIRED, "model is not set", f"Set {origins['model']} to the intended model ID.")
            if endpoint_name is None:
                fail("endpoint", ErrorCode.MISSING_REQUIRED, "endpoint is not set", f"Set {origins['endpoint']} to a declared endpoint name.")
        if endpoint_name is not None:
            if not any(key.startswith(prefix) for key in values):
                fail("endpoint", ErrorCode.UNKNOWN_REFERENCE, f"endpoint {endpoint_name!r} is not declared",
                     f"Declare {prefix}baseUrl first, or correct the reference at {origins['endpoint']}.")
            if not endpoint["baseUrl"]:
                fail("endpoint", ErrorCode.MISSING_REQUIRED, f"endpoint {endpoint_name!r} baseUrl is not set", f"Set {prefix}baseUrl to the endpoint URL.")
        if credential is not None:
            credential_prefix = f"envy.llm.credentials.{credential}."
            if not any(key.startswith(credential_prefix) for key in values):
                fail("credential", ErrorCode.UNKNOWN_REFERENCE, f"credential {credential!r} is not declared",
                     f"Declare {credential_prefix}secretPath first, or correct the reference at {origins['credential']}.")
            if not values.get(credential_prefix + "secretPath"):
                fail("credential", ErrorCode.MISSING_REQUIRED, f"credential {credential!r} secretPath is not set",
                     f"Set {credential_prefix}secretPath to the encrypted YAML path; the key value belongs in sops, not Nix.")
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
        duplicate = next((index for index, entry in enumerate(bindings) if entry["provider"] == provider
                          and entry["selection"]["model"] == selection["model"]), None)
        if duplicate is not None:
            if bindings[duplicate]["selection"] != selection:
                fields = ", ".join(field for field in MODEL_FIELDS if bindings[duplicate]["selection"][field] != selection[field])
                raise error.ConfigValidationError(error.ConfigError(
                    ErrorCode.INVALID_VALUE, path,
                    f"models contain conflicting metadata for the same connection and model: {binding_paths[duplicate]} and {path} differ in {fields}",
                    "Keep one entry or align those fields."))
        else:
            bindings.append(binding)
            binding_paths.append(path)
    return bindings

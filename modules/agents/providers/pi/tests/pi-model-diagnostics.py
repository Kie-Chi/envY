"""Exercise actual Nix throws and Python diagnostics with isolated fake metadata."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from envy.agent_models import model_bindings
from envy.error import ConfigValidationError, parse_nix_error


def main():
    model_source, lib_source = map(Path, sys.argv[1:])
    values = {
        "envy.llm.endpoints.demo.baseUrl": "https://example.invalid/v1",
        "envy.llm.endpoints.demo.protocol": "openai-compatible",
        "envy.llm.credentials.demo.secretPath": "llm/demo/key",
        "envy.agentProfiles.fast.model": "test-model",
        "envy.agentProfiles.fast.endpoint": "demo",
        "envy.agentProfiles.fast.credential": "demo",
    }
    cases = [
        ("legacy-credential", [], {}, "unknown-reference", ".credential", "envy.llm.credentials.missing.secretPath"),
        ("credential", [{"profile": "fast", "credential": "missing"}], {}, "unknown-reference", ".models[0].credential", "envy.llm.credentials.missing.secretPath"),
        ("second-entry", [{"profile": "fast"}, {"model": "other", "endpoint": "demo", "credential": "missing"}], {}, "unknown-reference", ".models[1].credential", "envy.llm.credentials.missing.secretPath"),
        ("inherited", [{"profile": "fast"}], {"envy.agentProfiles.fast.credential": "missing"}, "unknown-reference", ".models[0].credential", "envy.agentProfiles.fast.credential"),
        ("endpoint", [{"profile": "fast", "endpoint": "missing"}], {}, "unknown-reference", ".models[0].endpoint", "envy.llm.endpoints.missing.baseUrl"),
        ("inherited-endpoint", [{"profile": "fast"}], {"envy.agentProfiles.fast.endpoint": "missing"}, "unknown-reference", ".models[0].endpoint", "envy.agentProfiles.fast.endpoint"),
        ("profile", [{"profile": "missing"}], {}, "unknown-reference", ".models[0].profile", "envy.agentProfiles.missing"),
        ("model", [{"endpoint": "demo"}], {}, "missing-required", ".models[0].model", ".models[0].model"),
        ("unset-endpoint", [{"profile": "fast", "endpoint": None}], {}, "missing-required", ".models[0].endpoint", ".models[0].endpoint"),
        ("partial-endpoint", [{"profile": "fast", "endpoint": "partial"}], {"envy.llm.endpoints.partial.protocol": "openai-compatible"}, "missing-required", ".models[0].endpoint", "envy.llm.endpoints.partial.baseUrl"),
        ("partial-credential", [{"profile": "fast", "credential": "partial"}], {"envy.llm.credentials.partial.kind": "api-key"}, "missing-required", ".models[0].credential", "envy.llm.credentials.partial.secretPath"),
        ("conflict", [{"profile": "fast"}, {"profile": "fast", "maxTokens": 123}], {}, "invalid-value", ".models[1]", ".models[0]"),
    ]
    with tempfile.TemporaryDirectory(prefix="envy-model-diagnostics-") as temporary:
        env = {"PATH": os.environ["PATH"], "HOME": temporary, "XDG_CONFIG_HOME": temporary,
               "NIX_PATH": "", "NIX_CONFIG": "experimental-features = nix-command\n"}
        for name, models, overrides, code, path, detail in cases:
            current = values | overrides
            config = {"model": None, "models": models} if models else {
                "model": "test-model", "models": [], "endpoint": "demo", "credential": "missing"}
            payload = {"agent": config, "profiles": {}, "endpoints": {}, "credentials": {}}
            for namespace, key in (("agentProfiles", "profiles"), ("llm.endpoints", "endpoints"), ("llm.credentials", "credentials")):
                prefix = f"envy.{namespace}."
                for field, value in current.items():
                    if field.startswith(prefix):
                        item, attribute = field[len(prefix):].split(".")
                        payload[key].setdefault(item, {})[attribute] = value
            expression = (f"let lib = import {lib_source}; selected = import {model_source} {{ inherit lib; }} "
                + "(builtins.fromJSON " + json.dumps(json.dumps(payload)) + "); in builtins.deepSeq selected.bindings true")
            result = subprocess.run(["nix", "eval", "--json", "--impure", "--expr", expression], env=env, text=True, capture_output=True, timeout=30)
            assert result.returncode != 0, (name, result.stdout)
            issue = parse_nix_error(result.stderr)
            assert issue is not None, (name, result.stderr)
            assert issue.code.value == code, (name, issue)
            assert issue.path == "envy.agents.pi" + path, (name, issue)
            assert issue.message in result.stderr, (name, result.stderr)
            assert "Location: " + issue.path in result.stderr, (name, result.stderr)
            assert "Next: " + issue.hint in result.stderr, (name, result.stderr)
            assert result.stderr.index("Location: ") < result.stderr.index("Next: "), (name, result.stderr)
            assert detail in issue.message + issue.hint, (name, issue)
            try:
                model_bindings(current, config | {"adapter": "pi"})
            except ConfigValidationError as exc:
                assert exc.issue.code == issue.code and exc.issue.path == issue.path, (name, exc.issue, issue)
                assert detail in str(exc), (name, str(exc))
            else:
                raise AssertionError(f"Python accepted invalid Nix case: {name}")
        print(f"Pi model diagnostics: {len(cases)} Nix/Python failure cases matched")


if __name__ == "__main__":
    main()

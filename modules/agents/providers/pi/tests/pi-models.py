# Ensure activation and live profile switches generate identical model metadata.
import json
import subprocess
import sys
from pathlib import Path

from envy.agent import _runtime_pi_models


def check_model_list(fixture) -> None:
    from envy.agent import _runtime_env
    from envy.agent_models import model_bindings

    values, agent = fixture["values"], fixture["agent"]
    assert agent["models"] == fixture["requestedModels"], "Omitted and explicit null fields must remain distinct"
    actual = _runtime_pi_models(values, agent)
    assert actual == fixture["data"], fixture["name"]
    providers = actual["providers"]
    assert len(providers) == fixture["expectedProviders"], fixture["name"]
    assert sum(len(item["models"]) for item in providers.values()) == fixture["expectedModels"]
    if "expectedLimits" in fixture:
        models = {model["id"]: model for provider in providers.values() for model in provider["models"]}
        assert set(models) == set(fixture["expectedLimits"]), fixture["name"]
        for model_id, limits in fixture["expectedLimits"].items():
            for field, value in limits.items():
                if value is None:
                    assert field not in models[model_id], (fixture["name"], model_id, field)
                else:
                    assert models[model_id][field] == value, (fixture["name"], model_id, field)
    bindings = model_bindings(values, agent)
    default = bindings[0]
    assert default["provider"] == fixture["defaultProvider"]
    assert default["selection"]["model"] == fixture["defaultModel"] == fixture["expectedDefault"]
    assert fixture["managedKeyPrefixes"] == {"providers": "envy-pi"}
    reference = f"{default['provider']}/{default['selection']['model']}"
    assert reference in fixture["wrapper"], fixture["name"]
    assert "ENVY_AGENT_PROVIDER:-envy-pi" in fixture["wrapper"]
    subprocess.run(["bash", "-n"], input=fixture["wrapper"], text=True, check=True)
    env = _runtime_env(values, agent, {})
    assert f"export ENVY_AGENT_PROVIDER={default['provider']}" in env
    for entry in bindings:
        if entry["selection"]["credential"] is not None and agent["models"]:
            assert f"export {entry['keyVariable']}=" in fixture["template"]
            assert f"export {entry['keyVariable']}=''" in env


def main() -> None:
    fixtures = json.loads(Path(sys.argv[1]).read_text())
    mismatches = []
    for fixture in fixtures:
        if fixture.get("modelListFixture"):
            check_model_list(fixture)
            continue
        provider = fixture["data"]["providers"]["envy-pi"]
        values = {
            "envy.llm.endpoints.test.baseUrl": provider["baseUrl"],
            "envy.llm.endpoints.test.protocol": (
                "anthropic-compatible" if provider["api"] == "anthropic-messages" else "openai-compatible"
            ),
        }
        if _runtime_pi_models(values, fixture["agent"]) != fixture["data"]:
            mismatches.append(fixture["name"])
    assert not mismatches, f"Runtime/Nix model mismatches: {mismatches}"
    print(f"Pi model runtime parity: {len(fixtures)} fixtures matched")


if __name__ == "__main__":
    main()

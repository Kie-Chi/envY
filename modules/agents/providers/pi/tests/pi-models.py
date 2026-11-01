# Ensure activation and live profile switches generate identical model metadata.
import json
import sys
from pathlib import Path

from envy.agent import _runtime_pi_models


def main() -> None:
    fixtures = json.loads(Path(sys.argv[1]).read_text())
    mismatches = []
    for fixture in fixtures:
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

"""Build Codex catalogs using the installed client's own model instructions."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from envy.secure_io import atomic_write_text


CATALOG_NAME = "envy-models.json"


def model_catalog(selections: list[dict[str, Any]], *, executable: str = "codex") -> dict[str, Any]:
    """Read bundled metadata offline, without loading the user's config or auth."""
    with tempfile.TemporaryDirectory(prefix="envy-codex-catalog-") as directory:
        result = subprocess.run(
            [executable, "debug", "models", "--bundled"],
            env=dict(os.environ, CODEX_HOME=directory),
            capture_output=True, text=True, timeout=30, check=False,
        )
    if result.returncode:
        raise ValueError("Codex could not export its bundled model catalog; update Codex before configuring models")
    try:
        bundled = json.loads(result.stdout)["models"]
        if not isinstance(bundled, list) or not bundled or not all(isinstance(item, dict) and item.get("slug") for item in bundled):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError("Codex returned an invalid bundled model catalog") from None
    models = []
    for index, selection in enumerate(selections):
        model = selection["model"]
        matches = [item for item in bundled if model == item["slug"] or model.startswith(item["slug"] + "-")]
        template = max(matches, key=lambda item: len(item["slug"])) if matches else bundled[0]
        entry = deepcopy(template)
        entry.update(slug=model, display_name=model, description=model,
                     visibility="list", supported_in_api=True, priority=index,
                     upgrade=None, availability_nux=None)
        # Unknown model IDs retain native instructions, without claiming native capabilities.
        if not matches:
            entry.update(default_reasoning_level=None, supported_reasoning_levels=[],
                         support_verbosity=False, default_verbosity=None,
                         context_window=None, max_context_window=None,
                         input_modalities=["text"], experimental_supported_tools=[],
                         supports_search_tool=False, supports_experimental_context=False,
                         supports_image_detail_original=False, comp_hash=None,
                         use_responses_lite=False, multi_agent_reasoning_effort=None,
                         supports_reasoning_effort_updates=False,
                         additional_speed_tiers=[], service_tiers=[])
        if selection.get("contextWindow") is not None:
            entry["context_window"] = selection["contextWindow"]
            entry["max_context_window"] = selection["contextWindow"]
        models.append(entry)
    if not models:
        raise ValueError("Codex model catalog must contain at least one model")
    return {"models": models}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    catalog = model_catalog(json.loads(args.source.read_text()), executable=args.codex)
    atomic_write_text(args.target, json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

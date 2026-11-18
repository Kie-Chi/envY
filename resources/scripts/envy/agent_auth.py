"""Merge an envY-managed API credential into Codex's own auth.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from envy.secure_io import atomic_write_text, ensure_private_directory


def update_auth(target: Path, api_key: str, *, auth_mode: str = "apikey") -> bool:
    """Update only the API credential and preserve Codex-owned auth fields."""
    key = api_key.strip()
    if not key:
        raise ValueError("an empty API key cannot be written to auth.json")
    existing: dict = {}
    if target.exists():
        text = target.read_text()
        if text.strip():
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("auth.json root must be an object")
            existing = parsed
    updated = dict(existing)
    updated["auth_mode"] = auth_mode
    updated["OPENAI_API_KEY"] = key
    if updated == existing and target.exists():
        return False
    ensure_private_directory(target.parent)
    atomic_write_text(target, json.dumps(updated, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        update_auth(args.target, args.secret_file.read_text())
    except (OSError, ValueError) as exc:
        print(f"envY could not update Codex credentials: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

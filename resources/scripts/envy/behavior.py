"""Value-limited semantic snapshots and declared activation previews."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Any

from envy.sources.model import Source, SourceError
from envy.structured_config import ConflictError, prepare_update

REPORT_VERSION = 1
ROLLBACK_BOUNDARY = (
    "Generation rollback restores a Nix/Home Manager generation; it does not generally undo "
    "Homebrew, npm, uv, native installations, system preferences, or external and writable application state."
)


def section(status: str, reason: str | None = None, **data: Any) -> dict:
    return {"status": status, "reason": reason, **data}


def _public_key(key: Any) -> bool:
    return isinstance(key, str) and not re.search(r"password|token|secret|credential|api.?key|authorization", key, re.I)


def semantic_snapshot(manifest: dict | None) -> dict:
    """Keep only public manifest fields; never retain runtime bodies or environment."""
    if not isinstance(manifest, dict):
        return {}
    snapshot = {}
    settings = manifest.get("settings")
    if isinstance(settings, dict):
        snapshot["settings"] = {key: value for key, value in settings.items()
                                if _public_key(key) and key.startswith("envy.")
                                and isinstance(value, (str, int, float, bool))}
    software = manifest.get("software")
    if isinstance(software, dict):
        groups = software.get("groups")
        if isinstance(groups, dict):
            selected = {}
            for key, group in groups.items():
                selection = group.get("selection") if isinstance(group, dict) else None
                if not isinstance(selection, dict) or not all(isinstance(selection.get(field), list)
                                                              for field in ("include", "exclude", "effective")):
                    continue
                selected[key] = {}
                for field in ("include", "exclude", "effective"):
                    selected[key][field] = [
                        {k: item[k] for k in ("id", "name", "ref", "sourceId", "version", "resolvedVersion")
                         if k in item and isinstance(item[k], (str, type(None)))} if isinstance(item, dict) else item
                        for item in selection[field] if isinstance(item, (dict, str))
                    ]
            if len(selected) == len(groups):
                snapshot["software"] = selected
        sources = software.get("sources")
        if isinstance(sources, dict):
            public = {}
            for key, raw in sources.items():
                try:
                    source = Source.from_dict(key, raw)
                    public[key] = {"type": source.type, "ecosystem": source.ecosystem,
                                   "owner": source.owner, "platforms": source.platforms,
                                   "scope": source.scope, "parameters": source.parameters}
                except (SourceError, ValueError, TypeError):
                    continue
            if len(public) == len(sources):
                snapshot["sources"] = public
    behavior = manifest.get("behavior")
    if isinstance(behavior, dict) and isinstance(behavior.get("actions"), list):
        actions = []
        for action in behavior["actions"]:
            if not isinstance(action, dict):
                continue
            public = {key: action[key] for key in ("id", "owner", "manager", "summary", "verification", "repeatability", "rollbackBoundary")
                      if isinstance(action.get(key), str)}
            if not all(key in public for key in ("id", "owner", "manager", "summary")):
                continue
            public["effects"] = [item for item in action.get("effects", []) if isinstance(item, str)]
            declaration = action.get("merge")
            public["merge"] = ({key: declaration[key] for key in
                                ("target", "format", "source", "managedKeys", "managedKeyPrefixes") if key in declaration}
                               if isinstance(declaration, dict) else None)
            actions.append(public)
        if len(actions) == len(behavior["actions"]):
            snapshot["behavior"] = {"actions": actions, "coverage": "partial",
                                    "unknowns": [item for item in behavior.get("unknowns", []) if isinstance(item, str)]}
    return snapshot


def comparison_report(before: dict | None, after: dict | None) -> dict:
    report = {}
    for name in ("settings", "software", "sources", "behavior"):
        if not after or name not in after:
            report[name] = section("unavailable", "Target semantic evidence is unavailable")
        elif not before or name not in before:
            report[name] = section("unknown", "No verified semantic baseline is bound to this generation",
                                   after=after[name])
        else:
            report[name] = section("compared", changed=before[name] != after[name],
                                   before=before[name], after=after[name])
    return report


def preview_merge(declaration: dict, *, source_root: Path = Path("/nix/store")) -> dict:
    """Load one declared public artifact; emit operations and key paths only."""
    try:
        source = Path(declaration["source"])
        target = Path(declaration["target"])
        if not source.is_absolute() or not target.is_absolute() or ".." in source.parts:
            return section("unavailable", "Merge paths must be absolute")
        if (not source.is_relative_to(source_root) or source.is_symlink()
                or not source.resolve(strict=True).is_relative_to(source_root.resolve(strict=True))):
            return section("unavailable", "Merge source is not an immutable declared artifact")
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            return section("unavailable", "Merge source is not a supported JSON artifact")
        declared = json.loads(source.read_text())
        prepared = prepare_update(target, declared, format=declaration["format"],
                                  managed_keys=declaration.get("managedKeys", []),
                                  managed_key_prefixes=declaration.get("managedKeyPrefixes", {}))
        return section("compared", **prepared.preview())
    except ConflictError:
        return section("unknown", "Configuration changed concurrently; original retained")
    except (OSError, ValueError, TypeError, KeyError):
        return section("unavailable", "Cannot safely inspect declared merge source or target")


def activation_preview(snapshot: dict, *, inspect_runtime: bool = False) -> dict:
    behavior = snapshot.get("behavior")
    if not isinstance(behavior, dict):
        return section("unavailable", "Activation declarations are unavailable", coverage="partial", actions=[])
    actions = []
    for action in behavior["actions"]:
        preview = dict(action)
        preview["realized"] = "unknown"
        preview["mergePreview"] = (
            preview_merge(action["merge"]) if inspect_runtime and action.get("merge") else
            section("unknown", "Runtime inspection was not requested" if action.get("merge") else
                    "Declaration only; runtime effects are not simulated")
        )
        actions.append(preview)
    return section("compared", "Declared actions are not proof of installation or activation",
                   coverage="partial", actions=actions, unknowns=behavior["unknowns"], rollbackBoundary=ROLLBACK_BOUNDARY)


def emit_report(command: str, payload: dict, *, ok: bool = True, error: str | None = None) -> None:
    """Versioned envelope plus legacy top-level aliases through the plain emitter."""
    from envy.jsonio import write_document

    document = {**payload, "schemaVersion": 1, "command": command, "ok": ok,
                "data": payload, "warnings": []}
    if error is not None:
        # Legacy callers used a string error. The envelope exposes the same diagnostic.
        document["error"] = {"code": "workflow_failed", "message": error}
    write_document(document)

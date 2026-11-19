"""Freeze a path flake and retain evidence for the exact built source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from envy.evaluation import informative_nix_error
from envy.process import run_process
from envy.utils import ENVY_ROOT, machine_build_attr, machine_manifest_attr


class SnapshotError(RuntimeError):
    def __init__(self, message: str, *, code: int = 1, partial: dict | None = None):
        super().__init__(message)
        self.code = code
        self.partial = partial or {}


def _json_command(command: list[str], root: Path, *, stream_stderr: bool = False) -> Any:
    result = run_process(command, cwd=root, capture=True, stream_stderr=stream_stderr, check=False)
    if result.returncode:
        raise SnapshotError(informative_nix_error(result.stderr or "", fallback="plan build failed"),
                            code=result.returncode)
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise SnapshotError("Nix returned invalid JSON") from None


def _git_evidence(root: Path) -> dict[str, Any]:
    values = []
    for arguments in (["rev-parse", "HEAD"], ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                      ["diff", "HEAD", "--binary", "--no-ext-diff"]):
        result = run_process(["git", *arguments], cwd=root, capture=True, check=False)
        values.append(result.stdout if result.returncode == 0 else None)
    revision, status, diff = values
    return {"gitRevision": revision.strip() if revision else None,
            "dirty": bool(status) if status is not None else None,
            "dirtyFingerprint": hashlib.sha256((status + "\0" + diff).encode()).hexdigest()
            if status is not None and diff is not None else None,
            "gitCoverage": "tracked content and untracked names; frozen NAR identifies all archived content"}


def _lock_digest(root: Path) -> str | None:
    try:
        return hashlib.sha256((root / "flake.lock").read_bytes()).hexdigest()
    except OSError:
        return None


def nar_hash(path: str, *, root: Path = ENVY_ROOT) -> str | None:
    """Accept both old path-keyed and new list path-info JSON."""
    try:
        value = _json_command(["nix", "path-info", "--json", path], root)
        if isinstance(value, list):
            row = next((row for row in value if isinstance(row, dict) and row.get("path") == path), {})
        elif isinstance(value, dict):
            info = value.get("info", value)
            row = info.get(path, info.get(Path(path).name, {})) if isinstance(info, dict) else {}
        else:
            row = {}
        result = row.get("narHash") if isinstance(row, dict) else None
        return result if isinstance(result, str) and result.startswith("sha256-") else None
    except (SnapshotError, OSError):
        return None


def derivation_output(value: Any, drv: str) -> str | None:
    """Normalize legacy derivations and versioned derivations with store basenames."""
    if not isinstance(value, dict):
        return None
    rows = value.get("derivations", value)
    if not isinstance(rows, dict):
        return None
    row = rows.get(drv, rows.get(Path(drv).name))
    outputs = row.get("outputs") if isinstance(row, dict) else None
    output = outputs.get("out") if isinstance(outputs, dict) else None
    path = output.get("path") if isinstance(output, dict) else output
    if not isinstance(path, str) or not path or ("/" in path and not path.startswith("/nix/store/")):
        return None
    return path if path.startswith("/nix/store/") else f"/nix/store/{path}"


def reverify_source(source: dict, target: str) -> bool:
    """Check immutable source, lock and derivation/output evidence without rebuilding."""
    frozen = source.get("frozenPath")
    drv = source.get("drvPath")
    if (not source.get("verified") or not isinstance(frozen, str) or not frozen.startswith("/nix/store/")
            or not isinstance(drv, str) or not drv.startswith("/nix/store/")
            or source.get("outputPath") != target or not source.get("narHash")
            or not source.get("lockFingerprint")):
        return False
    if nar_hash(frozen) != source["narHash"] or _lock_digest(Path(frozen)) != source["lockFingerprint"]:
        return False
    try:
        value = _json_command(["nix", "derivation", "show", drv], Path(frozen))
        return derivation_output(value, drv) == target
    except (SnapshotError, OSError):
        return False


def freeze_source(root: Path = ENVY_ROOT) -> dict[str, Any]:
    before = _git_evidence(root)
    lock = _lock_digest(root)
    # Freeze the root source without recursively fetching unused locked inputs.
    metadata = _json_command(["nix", "flake", "metadata", "--json", "--no-update-lock-file", f"path:{root}"], root)
    path = metadata.get("path") if isinstance(metadata, dict) else None
    if not isinstance(path, str) or not path.startswith("/nix/store/") or "#" in path:
        raise SnapshotError("Nix metadata did not return a frozen store source")
    source = {**before, "frozenPath": path, "narHash": nar_hash(path, root=root),
              "lockFingerprint": _lock_digest(Path(path)), "coverage": "path flake archive, including ignored files"}
    # Snapshot again, rather than using Git alone: path flakes include ignored files.
    again = _json_command(["nix", "flake", "metadata", "--json", "--no-update-lock-file", f"path:{root}"], root)
    if (not isinstance(again, dict) or again.get("path") != path or _git_evidence(root) != before
            or _lock_digest(root) != lock or source["lockFingerprint"] != lock):
        raise SnapshotError("Source changed while freezing; retry the preview", partial={"source": source})
    source["verified"] = source["narHash"] is not None and source["lockFingerprint"] is not None
    source["reason"] = None if source["verified"] else "Frozen source NAR or lock evidence is unavailable"
    return source


def build_snapshot(machine: str, *, root: Path = ENVY_ROOT) -> tuple[dict, dict | None, Path]:
    source = freeze_source(root)
    frozen = Path(source["frozenPath"])
    attr = machine_build_attr(machine, source=frozen)
    # Keep flake commands outside the store cwd so Nix resolves the full target.
    try:
        rows = _json_command(["nix", "build", "--no-link", "--json", "--impure", "--no-update-lock-file", attr], root, stream_stderr=True)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise SnapshotError("Nix build did not return one machine result")
        row = rows[0]
        outputs = row.get("outputs")
        output = outputs.get("out") if isinstance(outputs, dict) else None
        output = output.get("path") if isinstance(output, dict) else output
        drv = row.get("drvPath")
        if not isinstance(output, str) or not output.startswith("/nix/store/"):
            raise SnapshotError("Nix build returned no output path")
        source.update(outputPath=output, drvPath=drv if isinstance(drv, str) else None)
        if source["drvPath"] is None:
            source.update(verified=False, reason="Build derivation evidence is unavailable")
        else:
            try:
                drv_json = _json_command(["nix", "derivation", "show", source["drvPath"]], frozen)
                if derivation_output(drv_json, source["drvPath"]) != output:
                    source.update(verified=False, reason="Derivation output evidence does not match the build")
            except (SnapshotError, OSError):
                source.update(verified=False, reason="Derivation output evidence is unavailable")
    except SnapshotError as exc:
        exc.partial.update(source=source)
        raise
    manifest = None
    try:
        value = _json_command(["nix", "eval", "--impure", "--no-update-lock-file", machine_manifest_attr(machine, source=frozen), "--json"], root, stream_stderr=True)
        if not isinstance(value, dict) or value.get("id") != machine:
            raise SnapshotError("Frozen manifest identity is invalid")
        manifest = value
    except (SnapshotError, OSError):
        source["manifestReason"] = "Frozen manifest is unavailable"
    return source, manifest, Path(output)

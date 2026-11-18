"""Read-only inspection, separate from policy evaluation and lifecycle records."""

from __future__ import annotations

from pathlib import Path

from envy.storage.measurement import ScanBudget, measure
from envy.storage.model import StorageError
from envy.utils import PLATFORM


HOTSPOTS = (
    ("codex-runtime", ".cache/codex-runtimes", "runtime", "Keep the active runtime; size does not prove obsolescence."),
    ("codex-data", ".codex", "application-data", "Includes sessions and persistent state; report only."),
    ("nix-cache", ".cache/nix", "native-cache", "Nix tarball and evaluation caches; do not remove active databases."),
    ("nix-tarballs", ".cache/nix/tarball-cache-v2", "native-cache", "Nix-owned source cache; report only, with no GC or database mutation."),
    ("nix-evaluation", ".cache/nix/eval-cache-v6", "native-cache", "May be locked by Nix; report only."),
    ("envy-cache", ".cache/envy", "mixed-cache", "May include inspection evidence and recovery artifacts; report only."),
    ("uv-cache", ".cache/uv", "native-cache", "Use explicit native uv maintenance."),
    ("npm-cache", ".npm", "native-cache", "Use explicit native npm maintenance."),
    ("go-modules", "go/pkg/mod", "dependency-cache", "Removing module downloads may require network access to build again."),
)
DARWIN_HOTSPOTS = (
    ("wechat-data", "Library/Containers/com.tencent.xinWeChat", "application-data", "Includes messages, attachments and offline state; report only."),
    ("feishu-data", "Library/Application Support/LarkShell", "application-data", "Includes offline business data; use precise reviewed rules."),
    ("steam-data", "Library/Application Support/Steam", "installed-software", "Use Steam to manage games and workshop content."),
    ("vscode-extensions", ".vscode/extensions", "installed-software", "Manage installed extensions in VS Code."),
    ("vscode-data", "Library/Application Support/Code", "application-data", "Includes workspace state and extension caches."),
    ("vscode-updates", "Library/Caches/com.microsoft.VSCode.ShipIt", "update-staging", "Completion and updater activity must be checked before cleanup."),
    ("chrome-data", "Library/Application Support/Google/Chrome", "application-data", "Service Worker data may contain offline content."),
    ("orbstack-data", "Library/Group Containers/HUAQ24HBR6.dev.orbstack/data", "virtual-disk", "Sparse disk allocation is distinct from capacity and internal Docker usage."),
)


def item(identifier: str, path: Path, category: str, note: str, budget: ScanBudget) -> dict:
    return {"id": identifier, "path": str(path), "category": category, "action": "report",
            "note": note, "nativeReclaimableBytes": None, "policyEligibleBytes": None,
            **measure(path, budget)}


def annotate_containment(items: list[dict]) -> None:
    """Describe nesting without constructing a misleading additive total."""
    for entry in items:
        path = Path(entry["path"])
        parents = [other for other in items if other is not entry
                   and Path(other["path"]) in path.parents]
        entry["includedIn"] = max(parents, key=lambda value: len(Path(value["path"]).parts))["id"] if parents else None


def inspect(home: Path, *, platform: str = PLATFORM, budget: ScanBudget | None = None,
            projects: list[Path] | None = None) -> dict:
    budget = budget or ScanBudget()
    definitions = HOTSPOTS + (DARWIN_HOTSPOTS if platform == "darwin" else ())
    items = [item(identifier, home / relative, category, note, budget)
             for identifier, relative, category, note in definitions]
    from envy.storage import application_reports, docker, project_reports

    reports = {}
    operations = {
        "docker": lambda: docker.inspect(budget=budget),
        "applications": lambda: application_reports.inspect(home, budget=budget, platform=platform),
        "projects": lambda: project_reports.inspect(projects or [], home=home, budget=budget),
    }
    provider_warnings = []
    for name, operation in operations.items():
        try:
            report = operation()
        except (OSError, StorageError, ValueError):
            report = {"status": "unknown", "reason": "provider_failed", "items": [],
                      "warnings": [name + ": metadata probe failed; no cleanup was performed."]}
        reports[name] = report
        # Native Docker objects are not filesystem measurement rows.
        if name != "docker":
            items.extend(report.get("items", []))
        provider_warnings.extend(report.get("warnings", []))
    annotate_containment(items)
    warnings = ["Allocated bytes are estimates; APFS clones and snapshots may share blocks. "
                "Rows are not additive and do not promise reclaimed space."]
    warnings.extend(provider_warnings)
    warnings.extend(entry["id"] + ": " + ", ".join(entry["reasons"])
                    for entry in items if entry.get("reasons"))
    return {"readOnly": True, "home": str(home), "items": items, "warnings": warnings,
            "scanEntries": budget.entries, "totalsAdditive": False,
            "policyEvaluated": False, "reports": reports}

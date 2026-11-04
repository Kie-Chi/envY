"""Application doctor checks.

The app layer runs generic checkers (installed, running, state, login,
permissions) and custom checkers (e.g. vscode_sync) for each app declared
in schemas.apps.ALL_APP_SPECS.
"""

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from envy.doctor.checks.apps.checkers import TCC_DATABASE_CHECK
from envy.doctor.checks.apps.registry import run_single_app
from envy.doctor.model import SECTION_INSTALL, SECTION_SYSTEM, CheckResult, info
from envy.doctor.policy import app_policy, machine_manifest
from envy.doctor.probes import process
from envy.doctor.selection import DoctorSelection, app_keys_for_selection, parse_only
from envy.schemas.apps import ALL_APP_SPECS

# App checks mostly wait on short probe commands, so a few threads overlap them.
APP_CHECK_WORKERS = 8


def run_checks(selected: Optional[Iterable[str]] = None, selection: DoctorSelection | None = None) -> list[CheckResult]:
    active_selection = selection or parse_only(selected)
    process.reset()
    planned: list[str | CheckResult] = []

    for key in app_keys_for_selection(active_selection):
        enabled, reason = app_policy(ALL_APP_SPECS[key])
        if not enabled:
            result = info(
                SECTION_INSTALL,
                ALL_APP_SPECS[key].name,
                f"disabled for machine {(machine_manifest() or {}).get('id', 'current')}: {reason}",
            )
            result.details["app"] = key
            planned.append(result)
            continue
        planned.append(key)

    keys = [item for item in planned if isinstance(item, str)]
    with ThreadPoolExecutor(max_workers=max(1, min(APP_CHECK_WORKERS, len(keys)))) as pool:
        # map() yields in submission order, so output keeps the declared app order.
        checked = iter(list(pool.map(run_single_app, keys)))

    results: list[CheckResult] = []
    for item in planned:
        results.extend([item] if isinstance(item, CheckResult) else next(checked))
    return _first_tcc_report(results)


def _first_tcc_report(results: list[CheckResult]) -> list[CheckResult]:
    """Keep one unreadable-TCC warning, under the first app that needs TCC."""
    kept: list[CheckResult] = []
    reported = False
    for result in results:
        if result.section == SECTION_SYSTEM and result.name == TCC_DATABASE_CHECK:
            if reported:
                continue
            reported = True
        kept.append(result)
    return kept

"""Process and app-running probes."""

import re
import threading
from pathlib import Path
from typing import Iterable

from envy.doctor.probes.command import run
from envy.utils import platform_name

# One doctor run asks the same questions several times (the running and login
# checks both need the running state), so observations are kept until reset().
_names: set[str] | None = None
_names_lock = threading.Lock()
_running: dict[tuple[str | None, tuple[str, ...]], bool] = {}


def reset() -> None:
    """Forget earlier observations so the next probe sees current processes."""
    global _names
    with _names_lock:
        _names = None
    _running.clear()


def app_running(bundle_id: str | None = None, process_names: Iterable[str] = ()) -> bool:
    key = (bundle_id, tuple(process_names))
    if key not in _running:
        _running[key] = _probe_running(*key)
    return _running[key]


def _probe_running(bundle_id: str | None, process_names: tuple[str, ...]) -> bool:
    # The shared process list answers for most running apps; the per-app
    # probes below only run when it does not show one.
    running_names = names()
    if any(process_name in running_names for process_name in process_names):
        return True

    if bundle_id and platform_name() == "darwin":
        script = f'tell application id "{bundle_id}" to if it is running then return "true"'
        result = run(["osascript", "-e", script])
        if result.returncode == 0 and result.stdout.strip() == "true":
            return True

    for process_name in process_names:
        result = run(["pgrep", "-x", process_name])
        if result.returncode == 0:
            return True
    return False


def names() -> set[str]:
    global _names
    with _names_lock:
        if _names is None:
            _names = _process_names()
        return _names


def _process_names() -> set[str]:
    result = run(["ps", "ax", "-o", "comm="])
    if result.returncode != 0:
        return set()

    values: set[str] = set()
    for line in result.stdout.splitlines():
        command = line.strip()
        if not command:
            continue
        basename = Path(command).name
        values.add(basename)
        # Electron helpers can appear as "App Helper (Renderer)"; keep the app stem too.
        values.add(re.sub(r" Helper.*$", "", basename))
    return values

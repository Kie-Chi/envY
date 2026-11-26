"""Validated flake-input and Homebrew update workflows."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from envy import log
from envy.journal import operation_stage, record_operation
from envy.process import prime_flake_sources, run_process
from envy.transaction import FileTransaction
from envy.utils import ENVY_ROOT
from envy.workflows.check import check_or_exit


def _lock_evidence(path: Path) -> dict:
    try:
        return {"status": "present", "fingerprint": sha256(path.read_bytes()).hexdigest()}
    except FileNotFoundError:
        return {"status": "absent", "fingerprint": None}
    except OSError:
        return {"status": "unavailable", "fingerprint": None}


@record_operation(
    "update-inputs",
    detail=lambda input_name=None, **_: {"input": input_name or "all"},
)
def update_inputs(input_name: str | None = None, *, validate: bool = True) -> None:
    lock_file = ENVY_ROOT / "flake.lock"
    command = ["nix", "flake", "update"]
    if input_name:
        command.append(input_name)
    log.step("update", "updating flake input" if input_name else "updating all flake inputs",
             input=input_name or "all")
    before = _lock_evidence(lock_file)
    operation_stage("update-lock", "started", lockBefore=before)
    try:
        with FileTransaction([lock_file]) as transaction:
            run_process(
                command, cwd=ENVY_ROOT, check=True,
                activity="flake input update",
            )
            operation_stage("update-lock", "ok")
            prime_flake_sources(ENVY_ROOT)
            operation_stage("validate", "started")
            if validate:
                check_or_exit(all_machines=True)
            operation_stage("validate", "ok" if validate else "unknown")
            transaction.commit()
    except BaseException:
        restored = _lock_evidence(lock_file)
        operation_stage("restore-lock", "ok" if restored == before and restored["status"] != "unavailable" else "unknown")
        raise
    operation_stage("retain-lock", "ok", lockAfter=_lock_evidence(lock_file))
    log.ok("update", "flake inputs updated and validated", input=input_name or "all")


@record_operation("update-brew")
def update_homebrew() -> None:
    log.step("update", "updating Homebrew metadata")
    run_process(["brew", "update"], check=True, activity="Homebrew metadata update")
    log.ok("update", "Homebrew metadata updated")

"""Validated flake-input and Homebrew update workflows."""

from __future__ import annotations

from envy import log
from envy.journal import record_operation
from envy.process import prime_flake_sources, run_process
from envy.transaction import FileTransaction
from envy.utils import ENVY_ROOT
from envy.workflows.check import check_or_exit


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
    with FileTransaction([lock_file]) as transaction:
        run_process(
            command, cwd=ENVY_ROOT, check=True,
            activity="flake input update",
        )
        prime_flake_sources(ENVY_ROOT)
        if validate:
            check_or_exit(all_machines=True)
        transaction.commit()
    log.ok("update", "flake inputs updated and validated", input=input_name or "all")


@record_operation("update-brew")
def update_homebrew() -> None:
    log.step("update", "updating Homebrew metadata")
    run_process(["brew", "update"], check=True, activity="Homebrew metadata update")
    log.ok("update", "Homebrew metadata updated")

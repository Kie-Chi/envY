"""Expose shared habit verification through Doctor without additional probes."""

from envy.doctor.model import SECTION_RUNTIME, CheckResult
from envy.evaluation import machine_manifest


def run_checks() -> list[CheckResult]:
    # Delay the import: habit's fixed probes share the Doctor command boundary.
    from envy.habit import check_habits

    results, _ = check_habits(machine_manifest())
    return [
        CheckResult(
            section=SECTION_RUNTIME,
            name=f"habit/{result.habit}/{result.context}",
            status=result.status,
            message=result.message,
            details={"habit": result.habit, "context": result.context, **result.details},
        )
        for result in results
    ]

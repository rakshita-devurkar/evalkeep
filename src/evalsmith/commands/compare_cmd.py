"""``evalsmith compare``, ``runs`` and ``baseline`` -- reading two runs together."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from evalsmith.commands.detect_cmd import default_reviewer
from evalsmith.comparison import ComparisonReport, compare_results
from evalsmith.config import Project
from evalsmith.errors import CommandError
from evalsmith.runs import BaselinePromotion, CaseResult, EvaluationRun, Outcome
from evalsmith.storage import TraceStore
from evalsmith.storage.runs import AmbiguousRun
from evalsmith.targets import BASELINE, CANDIDATE

SUITE_DRIFT_HINT = (
    "The two runs covered different tests, so their pass rates are not "
    "comparable. Re-run both against the current suite, or pass "
    "--allow-suite-drift to compare only the tests they share."
)


@dataclass(frozen=True)
class RunSummary:
    run: EvaluationRun
    counts: dict[Outcome, int]
    is_baseline: bool = False


def compare(
    *,
    project_root: Path = Path(),
    baseline: str | None = None,
    candidate: str | None = None,
    allow_suite_drift: bool = False,
) -> ComparisonReport:
    """Compare two runs, refusing to compare incompatible suites."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        baseline_run = _resolve_run(store, baseline, default=BASELINE, role="baseline")
        candidate_run = _resolve_run(store, candidate, default=CANDIDATE, role="candidate")

        if baseline_run.run_id == candidate_run.run_id:
            raise CommandError(
                "The baseline and the candidate are the same run.",
                hint="Pass --baseline and --candidate explicitly.",
            )

        report = compare_results(
            baseline_run,
            store.runs.results(baseline_run.run_id),
            candidate_run,
            store.runs.results(candidate_run.run_id),
        )

    if not report.suite_compatible and not allow_suite_drift:
        raise CommandError(
            f"These runs used different test suites "
            f"({baseline_run.suite_hash} vs {candidate_run.suite_hash}).",
            hint=SUITE_DRIFT_HINT,
        )
    return report


def list_runs(*, project_root: Path = Path(), limit: int = 20) -> list[RunSummary]:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        promotion = store.runs.current_baseline()
        return [
            RunSummary(
                run=run,
                counts=store.runs.counts(run.run_id),
                is_baseline=promotion is not None and promotion.run_id == run.run_id,
            )
            for run in store.runs.recent(limit=limit)
        ]


def show_run(run_id: str, *, project_root: Path = Path()) -> tuple[EvaluationRun, list[CaseResult]]:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        run = _lookup(store, run_id)
        if run is None:
            raise CommandError(
                f"No run with ID {run_id.strip()!r}.",
                hint="Run 'evalsmith runs list' to see what exists.",
            )
        return run, store.runs.results(run.run_id)


def promote_baseline(
    run_id: str,
    *,
    project_root: Path = Path(),
    reviewer: str | None = None,
    reason: str | None = None,
) -> BaselinePromotion:
    """Make a run the reference point. Only ever an explicit, recorded decision."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        run = _lookup(store, run_id)
        if run is None:
            raise CommandError(
                f"No run with ID {run_id.strip()!r}.",
                hint="Run 'evalsmith runs list' to see what exists.",
            )
        errored = store.runs.counts(run.run_id).get(Outcome.ERROR, 0)
        if errored:
            raise CommandError(
                f"That run had {errored} test(s) that never executed, so it is not "
                "a sound reference point.",
                hint="Fix the target and re-run before promoting.",
            )
        promotion = BaselinePromotion(
            promotion_id=uuid.uuid4().hex,
            run_id=run.run_id,
            target_id=run.target_id,
            reviewer=reviewer or default_reviewer(),
            reason=reason,
        )
        store.runs.promote(promotion)
        return promotion


def current_baseline(
    *, project_root: Path = Path()
) -> tuple[BaselinePromotion, EvaluationRun] | None:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        promotion = store.runs.current_baseline()
        if promotion is None:
            return None
        run = store.runs.get(promotion.run_id)
        return (promotion, run) if run is not None else None


def _lookup(store: TraceStore, identifier: str) -> EvaluationRun | None:
    try:
        return store.runs.resolve(identifier)
    except AmbiguousRun as exc:
        raise CommandError(str(exc), hint="Use more characters of the run ID.") from exc


def _resolve_run(
    store: TraceStore, identifier: str | None, *, default: str, role: str
) -> EvaluationRun:
    """Accept a run ID, a target name, or nothing at all.

    With nothing given, the baseline is whichever run was explicitly promoted --
    falling back to the newest run for the conventional target name only when no
    promotion has ever been recorded.
    """
    if identifier is None and role == "baseline":
        promotion = store.runs.current_baseline()
        if promotion is not None:
            run = store.runs.get(promotion.run_id)
            if run is not None:
                return run

    name = (identifier or default).strip()
    run = _lookup(store, name) or store.runs.latest(name)
    if run is None:
        raise CommandError(
            f"No {role} run matching {name!r}.",
            hint="Run 'evalsmith runs list', or 'evalsmith run --target ...' first.",
        )
    return run

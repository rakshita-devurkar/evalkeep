"""``evalkeep detect`` and ``evalkeep failures`` -- evidence and review."""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path

from evalkeep.analysis import FailureAnalysis
from evalkeep.config import Project
from evalkeep.detection import DetectionReport, detect_failures
from evalkeep.errors import CommandError
from evalkeep.failures import Failure, FailureStatus, failure_id_for
from evalkeep.storage import FailureSummary, StoredTrace, TraceStore


@dataclass(frozen=True)
class FailureListing:
    summaries: list[FailureSummary]
    total: int
    counts: dict[FailureStatus, int]


@dataclass(frozen=True)
class FailureDetail:
    """A failure together with the trace it describes and how it was labelled."""

    failure: Failure
    trace: StoredTrace
    analysis: FailureAnalysis | None = None


def default_reviewer() -> str:
    """Who to record as the reviewer when the caller did not say."""
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getuser can fail on odd systems
        return "unknown"


def run_detection(*, project_root: Path = Path()) -> DetectionReport:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        if store.count() == 0:
            raise CommandError(
                "No traces have been ingested yet.",
                hint="Run 'evalkeep ingest traces.jsonl' first.",
            )
        return detect_failures(store)


def list_failures(
    *,
    project_root: Path = Path(),
    status: FailureStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> FailureListing:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        return FailureListing(
            summaries=store.failures.list(status=status, limit=limit, offset=offset),
            total=store.failures.count(status=status),
            counts=store.failures.counts_by_status(),
        )


def show_failure(identifier: str, *, project_root: Path = Path()) -> FailureDetail:
    """Look one up by failure ID or by the trace ID it belongs to."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        failure = resolve_failure(store, identifier)
        stored = store.get(failure.trace_id)
        if stored is None:  # pragma: no cover - the foreign key prevents this
            raise CommandError(f"Trace {failure.trace_id!r} is missing from the store.")
        return FailureDetail(
            failure=failure,
            trace=stored,
            analysis=store.failures.get_analysis(failure.failure_id),
        )


def review_failure(
    identifier: str,
    status: FailureStatus,
    *,
    project_root: Path = Path(),
    reviewer: str | None = None,
    reason: str | None = None,
) -> Failure:
    """Record a human decision on an existing failure."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        failure = resolve_failure(store, identifier)
        failure.review(status, reviewer=reviewer or default_reviewer(), reason=reason)
        store.failures.save(failure)
        return failure


def add_failure(
    trace_id: str,
    *,
    project_root: Path = Path(),
    reviewer: str | None = None,
    reason: str | None = None,
) -> Failure:
    """Mark a trace as a failure by hand, with no detector evidence."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        if store.get(trace_id) is None:
            raise CommandError(
                f"No stored trace with ID {trace_id.strip()!r}.",
                hint="Run 'evalkeep trace list' to see what has been ingested.",
            )
        existing = store.failures.get_by_trace(trace_id.strip())
        if existing is not None:
            raise CommandError(
                f"Trace {trace_id.strip()!r} already has failure {existing.failure_id} "
                f"({existing.status.value}).",
                hint=f"Use 'evalkeep failures confirm {existing.failure_id}' instead.",
            )
        failure = Failure.manual(
            trace_id.strip(), reviewer=reviewer or default_reviewer(), reason=reason
        )
        store.failures.save(failure)
        return failure


def resolve_failure(store: TraceStore, identifier: str) -> Failure:
    """Accept either a failure ID or the trace ID it was derived from."""
    cleaned = identifier.strip()
    failure = store.failures.get(cleaned) or store.failures.get_by_trace(cleaned)
    if failure is None and not cleaned.startswith("fail-"):
        failure = store.failures.get(failure_id_for(cleaned))
    if failure is None:
        raise CommandError(
            f"No failure matching {cleaned!r}.",
            hint="Run 'evalkeep failures list', or 'evalkeep detect' if you have not detected yet.",
        )
    return failure

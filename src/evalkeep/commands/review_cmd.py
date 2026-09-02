"""``evalkeep review`` and the non-interactive decision commands.

The interactive loop lives in the CLI; everything it can do is also reachable
one decision at a time, so a script or a CI job can record the same decisions
without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evalkeep.analysis import FailureAnalysis
from evalkeep.commands.dataset_cmd import show_test
from evalkeep.commands.detect_cmd import default_reviewer
from evalkeep.config import Project
from evalkeep.errors import CommandError
from evalkeep.failures import Failure
from evalkeep.regression import RegressionTest, ReviewStatus
from evalkeep.review import ReviewError, apply_edits, approve, reject, render_editable
from evalkeep.storage import StoredTrace, TraceStore


@dataclass(frozen=True)
class ReviewItem:
    """Everything a reviewer needs on screen to decide: guide 8H's first rule."""

    test: RegressionTest
    trace: StoredTrace
    failure: Failure
    analysis: FailureAnalysis | None


def pending_reviews(*, project_root: Path = Path(), limit: int | None = None) -> list[ReviewItem]:
    """Drafts awaiting a decision, with their source interaction and analysis."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        drafts = store.tests.list(status=ReviewStatus.DRAFT, limit=limit or 1000)
        return [item for test in drafts if (item := _assemble(store, test)) is not None]


def review_item(identifier: str, *, project_root: Path = Path()) -> ReviewItem:
    """One test, with its context, whatever its status."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        test = _resolve(store, identifier)
        item = _assemble(store, test)
        if item is None:  # pragma: no cover - foreign keys prevent this
            raise CommandError(f"Test {test.test_id!r} has lost its source trace.")
        return item


def approve_test(
    identifier: str,
    *,
    project_root: Path = Path(),
    reviewer: str | None = None,
    reason: str | None = None,
) -> RegressionTest:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        test = _resolve(store, identifier)
        try:
            approved = approve(test, reviewer=reviewer or default_reviewer(), reason=reason)
        except ReviewError as exc:
            raise CommandError(
                str(exc),
                hint="Fix it with 'evalkeep dataset edit <id>' and approve again.",
            ) from exc
        store.tests.save(approved)
        return approved


def reject_test(
    identifier: str,
    *,
    project_root: Path = Path(),
    reviewer: str | None = None,
    reason: str | None = None,
) -> RegressionTest:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        test = _resolve(store, identifier)
        rejected = reject(test, reviewer=reviewer or default_reviewer(), reason=reason)
        store.tests.save(rejected)
        return rejected


def editable_document(identifier: str, *, project_root: Path = Path()) -> str:
    """The YAML a reviewer edits for one test."""
    return render_editable(show_test(identifier, project_root=project_root))


def edit_test(
    identifier: str,
    document: str,
    *,
    project_root: Path = Path(),
    editor: str | None = None,
) -> RegressionTest:
    """Apply an edited document. Nothing is stored unless it is valid."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        test = _resolve(store, identifier)
        result = apply_edits(test, document, editor=editor or default_reviewer())
        if result.test is None:
            raise CommandError(
                "The edit was not applied:\n  - " + "\n  - ".join(result.errors),
                hint="The stored draft is unchanged.",
            )
        store.tests.save(result.test)
        return result.test


def _assemble(store: TraceStore, test: RegressionTest) -> ReviewItem | None:
    failure = store.failures.get(test.failure_id)
    stored = store.get(test.provenance.trace_id)
    if failure is None or stored is None:  # pragma: no cover - foreign keys prevent this
        return None
    return ReviewItem(
        test=test,
        trace=stored,
        failure=failure,
        analysis=store.failures.get_analysis(test.failure_id),
    )


def _resolve(store: TraceStore, identifier: str) -> RegressionTest:
    """Accept a test ID, a failure ID, or the trace the test came from."""
    cleaned = identifier.strip()
    test = store.tests.get(cleaned) or store.tests.get_by_failure(cleaned)
    if test is None:
        failure = store.failures.get_by_trace(cleaned)
        if failure is not None:
            test = store.tests.get_by_failure(failure.failure_id)
    if test is None:
        raise CommandError(
            f"No regression test matching {cleaned!r}.",
            hint="Run 'evalkeep dataset list' to see what exists.",
        )
    return test

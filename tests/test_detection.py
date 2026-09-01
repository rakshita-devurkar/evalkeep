"""Detection passes: idempotent, and never overwriting a human decision."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalsmith.cli import app
from evalsmith.commands.detect_cmd import (
    add_failure,
    list_failures,
    review_failure,
    run_detection,
    show_failure,
)
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.detection import detect_failures
from evalsmith.detectors import ExplicitStatusDetector, NegativeFeedbackDetector
from evalsmith.errors import CommandError, ExitCode
from evalsmith.failures import FailureOrigin, FailureStatus, failure_id_for
from evalsmith.storage import TraceStore
from evalsmith.trace import NormalizedTrace

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/refund-agent/traces.jsonl"


def trace_payload(trace_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "input": {"text": f"Question for {trace_id}."},
        "outcome": {"status": "failure"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore.open(tmp_path / "database.db") as opened:
        yield opened


@pytest.fixture
def ingested(initialized_project: Path) -> Path:
    """A project with the refund example ingested but not yet detected."""
    ingest_traces(EXAMPLE, project_root=initialized_project)
    return initialized_project


def store_trace(store: TraceStore, trace_id: str, **overrides: Any) -> None:
    store.add(NormalizedTrace.model_validate(trace_payload(trace_id, **overrides)))


class TestFirstPass:
    def test_creates_one_candidate_per_failing_trace(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        store_trace(store, "trace-2", outcome={"status": "success"})
        report = detect_failures(store)
        assert (report.traces, report.created, report.failures) == (2, 1, 1)
        assert store.failures.count() == 1

    def test_records_every_contributing_signal(self, store: TraceStore) -> None:
        store_trace(
            store,
            "trace-1",
            outcome={
                "status": "failure",
                "feedback": {"rating": "negative", "comment": "wrong order"},
            },
        )
        detect_failures(store)
        failure = store.failures.get_by_trace("trace-1")
        assert failure is not None
        assert failure.kinds == ["explicit_status", "negative_feedback"]
        assert len(failure.signals) == 2

    def test_new_failures_start_unreviewed(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        failure = store.failures.get_by_trace("trace-1")
        assert failure is not None
        assert failure.status is FailureStatus.CANDIDATE
        assert failure.origin is FailureOrigin.DETECTOR
        assert not failure.reviewed

    def test_the_failure_id_is_derived_from_the_trace(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        failure = store.failures.get_by_trace("trace-1")
        assert failure is not None
        assert failure.failure_id == failure_id_for("trace-1")

    def test_counts_signals_by_kind(self, store: TraceStore) -> None:
        store_trace(store, "trace-1", outcome={"status": "failure"})
        store_trace(
            store,
            "trace-2",
            outcome={"status": "failure", "feedback": {"rating": "negative"}},
        )
        report = detect_failures(store)
        assert {k.value: v for k, v in report.by_kind.items()} == {
            "explicit_status": 2,
            "negative_feedback": 1,
        }


class TestIdempotence:
    def test_a_second_pass_changes_nothing(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        second = detect_failures(store)
        assert (second.created, second.updated, second.unchanged) == (0, 0, 1)
        assert not second.changed
        assert store.failures.count() == 1

    def test_the_failure_id_is_stable_across_passes(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        first = store.failures.get_by_trace("trace-1")
        detect_failures(store)
        second = store.failures.get_by_trace("trace-1")
        assert first is not None and second is not None
        assert first.failure_id == second.failure_id
        assert first.detected_at == second.detected_at

    def test_new_evidence_updates_the_signals(self, store: TraceStore) -> None:
        store_trace(store, "trace-1", outcome={"status": "failure"})
        detect_failures(store, detectors=[ExplicitStatusDetector()])

        store._connection.execute(
            "UPDATE traces SET payload = ? WHERE trace_id = ?",
            (
                json.dumps(
                    trace_payload(
                        "trace-1",
                        outcome={"status": "failure", "feedback": {"rating": "negative"}},
                    )
                ),
                "trace-1",
            ),
        )
        store._connection.commit()

        report = detect_failures(store)
        assert (report.updated, report.created) == (1, 0)
        failure = store.failures.get_by_trace("trace-1")
        assert failure is not None
        assert failure.kinds == ["explicit_status", "negative_feedback"]


class TestReviewsSurvive:
    def test_a_confirmation_is_not_overwritten(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        failure = store.failures.get_by_trace("trace-1")
        assert failure is not None
        failure.review(FailureStatus.CONFIRMED, reviewer="alex", reason="real bug")
        store.failures.save(failure)

        report = detect_failures(store)

        after = store.failures.get_by_trace("trace-1")
        assert after is not None
        assert after.status is FailureStatus.CONFIRMED
        assert (after.reviewer, after.reason) == ("alex", "real bug")
        assert report.preserved_reviews == 1

    def test_a_dismissal_is_not_resurrected(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        failure = store.failures.get_by_trace("trace-1")
        assert failure is not None
        failure.review(FailureStatus.DISMISSED, reviewer="alex", reason="synthetic")
        store.failures.save(failure)

        detect_failures(store)

        after = store.failures.get_by_trace("trace-1")
        assert after is not None
        assert after.status is FailureStatus.DISMISSED

    def test_a_dismissed_failure_is_kept_for_audit(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        review_id = failure_id_for("trace-1")
        failure = store.failures.get(review_id)
        assert failure is not None
        failure.review(FailureStatus.DISMISSED, reviewer="alex", reason=None)
        store.failures.save(failure)
        assert store.failures.get(review_id) is not None


class TestWithdrawal:
    def test_an_unreviewed_candidate_without_evidence_is_withdrawn(self, store: TraceStore) -> None:
        """A detector was narrowed; the candidate it created is no longer backed."""
        store_trace(store, "trace-1")
        detect_failures(store)
        report = detect_failures(store, detectors=[NegativeFeedbackDetector()])
        assert report.withdrawn == 1
        assert store.failures.count() == 0

    def test_a_reviewed_failure_is_never_withdrawn(self, store: TraceStore) -> None:
        store_trace(store, "trace-1")
        detect_failures(store)
        failure = store.failures.get_by_trace("trace-1")
        assert failure is not None
        failure.review(FailureStatus.CONFIRMED, reviewer="alex", reason=None)
        store.failures.save(failure)

        report = detect_failures(store, detectors=[NegativeFeedbackDetector()])
        assert report.withdrawn == 0
        assert store.failures.count() == 1

    def test_a_manual_failure_is_never_withdrawn(self, store: TraceStore) -> None:
        store_trace(store, "trace-1", outcome={"status": "success"})
        from evalsmith.failures import Failure

        store.failures.save(Failure.manual("trace-1", reviewer="alex", reason="looks wrong"))
        report = detect_failures(store)
        assert report.withdrawn == 0
        assert store.failures.count() == 1


class TestManualActions:
    def test_confirm_records_reviewer_and_reason(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        failure = review_failure(
            "trace-1042",
            FailureStatus.CONFIRMED,
            project_root=ingested,
            reviewer="alex",
            reason="refunded the oldest order",
        )
        assert failure.status is FailureStatus.CONFIRMED
        assert failure.reviewer == "alex"
        assert failure.reason == "refunded the oldest order"

    def test_a_reviewer_defaults_to_the_current_user(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        failure = review_failure("trace-1042", FailureStatus.CONFIRMED, project_root=ingested)
        assert failure.reviewer

    def test_add_creates_a_manual_failure(self, ingested: Path) -> None:
        failure = add_failure(
            "trace-1060", project_root=ingested, reviewer="alex", reason="answer was stale"
        )
        assert failure.origin is FailureOrigin.MANUAL
        assert failure.status is FailureStatus.CONFIRMED
        assert failure.signals == []

    def test_add_refuses_an_unknown_trace(self, ingested: Path) -> None:
        with pytest.raises(CommandError, match="No stored trace"):
            add_failure("trace-nope", project_root=ingested)

    def test_add_refuses_to_shadow_an_existing_failure(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        with pytest.raises(CommandError, match="already has failure"):
            add_failure("trace-1042", project_root=ingested)

    def test_review_accepts_a_failure_id(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        identifier = failure_id_for("trace-1042")
        failure = review_failure(identifier, FailureStatus.CONFIRMED, project_root=ingested)
        assert failure.trace_id == "trace-1042"

    def test_an_unknown_identifier_is_a_command_error(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        with pytest.raises(CommandError, match="No failure matching"):
            review_failure("nope", FailureStatus.CONFIRMED, project_root=ingested)


class TestInspection:
    def test_show_returns_the_failure_and_its_trace(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        detail = show_failure("trace-1042", project_root=ingested)
        assert detail.failure.trace_id == "trace-1042"
        assert detail.trace.trace.trace_id == "trace-1042"
        assert len(detail.failure.signals) == 2

    def test_list_counts_by_status(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        review_failure("trace-1042", FailureStatus.CONFIRMED, project_root=ingested)
        listing = list_failures(project_root=ingested)
        assert listing.total == 3
        assert listing.counts == {FailureStatus.CANDIDATE: 2, FailureStatus.CONFIRMED: 1}

    def test_list_filters_by_status(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        review_failure("trace-1042", FailureStatus.DISMISSED, project_root=ingested)
        listing = list_failures(project_root=ingested, status=FailureStatus.DISMISSED)
        assert [s.trace_id for s in listing.summaries] == ["trace-1042"]

    def test_detection_needs_ingested_traces(self, initialized_project: Path) -> None:
        with pytest.raises(CommandError, match="No traces have been ingested"):
            run_detection(project_root=initialized_project)

    def test_detection_needs_a_project(self, tmp_path: Path) -> None:
        with pytest.raises(CommandError, match=r"evalsmith\.yaml"):
            run_detection(project_root=tmp_path)


class TestExampleDataset:
    def test_finds_exactly_the_three_seeded_failures(self, ingested: Path) -> None:
        report = run_detection(project_root=ingested)
        assert report.traces == 5
        assert report.created == 3
        listing = list_failures(project_root=ingested)
        assert {s.trace_id for s in listing.summaries} == {
            "trace-1042",
            "trace-1043",
            "trace-1051",
        }

    def test_the_clean_traces_are_not_flagged(self, ingested: Path) -> None:
        run_detection(project_root=ingested)
        listing = list_failures(project_root=ingested)
        flagged = {s.trace_id for s in listing.summaries}
        assert "trace-1060" not in flagged
        assert "trace-1061" not in flagged


class TestCli:
    def test_detect_reports_what_it_found(self, runner: CliRunner, ingested: Path) -> None:
        result = runner.invoke(app, ["detect", "-C", str(ingested)])
        assert result.exit_code == ExitCode.OK
        assert "traces examined" in result.stdout
        assert "explicit_status" in result.stdout

    def test_detect_without_traces_exits_two(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        result = runner.invoke(app, ["detect", "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_failures_list_shows_evidence_kinds(self, runner: CliRunner, ingested: Path) -> None:
        runner.invoke(app, ["detect", "-C", str(ingested)])
        result = runner.invoke(app, ["failures", "list", "-C", str(ingested)])
        assert "trace-1042" in result.stdout
        assert "explicit_status" in result.stdout

    def test_failures_list_when_empty(self, runner: CliRunner, ingested: Path) -> None:
        result = runner.invoke(app, ["failures", "list", "-C", str(ingested)])
        assert "No failure candidates" in result.stdout

    def test_failures_show_renders_evidence_and_trace(
        self, runner: CliRunner, ingested: Path
    ) -> None:
        runner.invoke(app, ["detect", "-C", str(ingested)])
        result = runner.invoke(app, ["failures", "show", "trace-1043", "-C", str(ingested)])
        assert result.exit_code == ExitCode.OK
        assert "failed_evaluator" in result.stdout
        assert "outcome.evaluations.0" in result.stdout

    def test_confirm_dismiss_and_add(self, runner: CliRunner, ingested: Path) -> None:
        runner.invoke(app, ["detect", "-C", str(ingested)])
        confirm = runner.invoke(
            app,
            ["failures", "confirm", "trace-1042", "-C", str(ingested), "--reviewer", "alex"],
        )
        dismiss = runner.invoke(
            app,
            ["failures", "dismiss", "trace-1051", "-C", str(ingested), "--reviewer", "alex"],
        )
        added = runner.invoke(
            app, ["failures", "add", "trace-1060", "-C", str(ingested), "--reviewer", "alex"]
        )
        assert confirm.exit_code == ExitCode.OK and "confirmed" in confirm.stdout
        assert dismiss.exit_code == ExitCode.OK and "dismissed" in dismiss.stdout
        assert added.exit_code == ExitCode.OK and "added" in added.stdout

        listing = list_failures(project_root=ingested)
        assert listing.counts == {
            FailureStatus.CANDIDATE: 1,
            FailureStatus.CONFIRMED: 2,
            FailureStatus.DISMISSED: 1,
        }

    def test_an_unknown_failure_exits_two(self, runner: CliRunner, ingested: Path) -> None:
        result = runner.invoke(app, ["failures", "show", "nope", "-C", str(ingested)])
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_a_manual_failure_says_it_has_no_evidence(
        self, runner: CliRunner, ingested: Path
    ) -> None:
        runner.invoke(app, ["failures", "add", "trace-1060", "-C", str(ingested)])
        result = runner.invoke(app, ["failures", "show", "trace-1060", "-C", str(ingested)])
        assert "added by hand" in result.stdout

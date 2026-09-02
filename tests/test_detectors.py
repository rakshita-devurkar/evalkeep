"""Detectors: evidence only, never inference (guide 8D)."""

from __future__ import annotations

from typing import Any

import pytest

from evalkeep.detectors import (
    DETECTORS,
    ExplicitStatusDetector,
    FailedEvaluatorDetector,
    FailureDetector,
    NegativeFeedbackDetector,
    Signal,
    SignalKind,
    detect_signals,
)
from evalkeep.trace import NormalizedTrace


def make_trace(**overrides: Any) -> NormalizedTrace:
    payload: dict[str, Any] = {
        "trace_id": "trace-1",
        "input": {"text": "Refund my latest order."},
    }
    payload.update(overrides)
    return NormalizedTrace.model_validate(payload)


class TestExplicitStatus:
    @pytest.mark.parametrize("status", ["failure", "error"])
    def test_fires_on_a_failing_status(self, status: str) -> None:
        (signal,) = list(ExplicitStatusDetector().detect(make_trace(outcome={"status": status})))
        assert signal.kind is SignalKind.EXPLICIT_STATUS
        assert signal.source == "outcome.status"
        assert signal.evidence["status"] == status

    @pytest.mark.parametrize("status", ["success", "unknown"])
    def test_stays_quiet_otherwise(self, status: str) -> None:
        assert list(ExplicitStatusDetector().detect(make_trace(outcome={"status": status}))) == []

    def test_a_trace_with_no_outcome_is_not_a_failure(self) -> None:
        assert list(ExplicitStatusDetector().detect(make_trace())) == []


class TestNegativeFeedback:
    def test_fires_on_a_negative_rating(self) -> None:
        trace = make_trace(
            outcome={"feedback": {"rating": "negative", "comment": "refunded the wrong order"}}
        )
        (signal,) = list(NegativeFeedbackDetector().detect(trace))
        assert signal.kind is SignalKind.NEGATIVE_FEEDBACK
        assert signal.summary == "refunded the wrong order"
        assert signal.evidence["comment"] == "refunded the wrong order"

    def test_falls_back_to_a_plain_summary_without_a_comment(self) -> None:
        trace = make_trace(outcome={"feedback": {"rating": "negative"}})
        (signal,) = list(NegativeFeedbackDetector().detect(trace))
        assert signal.summary == "feedback was rated negative"

    def test_positive_feedback_is_not_evidence(self) -> None:
        trace = make_trace(outcome={"feedback": {"rating": "positive", "comment": "great"}})
        assert list(NegativeFeedbackDetector().detect(trace)) == []

    def test_a_bare_score_is_not_evidence(self) -> None:
        """A score without its scale cannot be thresholded honestly."""
        trace = make_trace(outcome={"feedback": {"score": 1.0, "comment": "meh"}})
        assert list(NegativeFeedbackDetector().detect(trace)) == []

    def test_a_score_is_carried_along_when_the_rating_fires(self) -> None:
        trace = make_trace(outcome={"feedback": {"rating": "negative", "score": 0.2}})
        (signal,) = list(NegativeFeedbackDetector().detect(trace))
        assert signal.evidence["score"] == 0.2

    def test_no_feedback_is_not_evidence(self) -> None:
        assert list(NegativeFeedbackDetector().detect(make_trace())) == []


class TestFailedEvaluator:
    def test_fires_on_a_failed_outcome_evaluation(self) -> None:
        trace = make_trace(
            outcome={
                "evaluations": [
                    {"name": "refunds-newest", "passed": False, "reason": "got order-A"}
                ]
            }
        )
        (signal,) = list(FailedEvaluatorDetector().detect(trace))
        assert signal.kind is SignalKind.FAILED_EVALUATOR
        assert signal.source == "outcome.evaluations.0"
        assert signal.evidence["evaluator"] == "refunds-newest"

    def test_fires_on_a_failed_evaluation_event(self) -> None:
        trace = make_trace(
            events=[
                {"event_id": "e1", "type": "message", "role": "user", "content": "hi"},
                {"event_id": "e2", "type": "evaluation", "name": "grader", "passed": False},
            ]
        )
        (signal,) = list(FailedEvaluatorDetector().detect(trace))
        assert signal.source == "events.1"

    def test_passing_evaluations_are_not_evidence(self) -> None:
        trace = make_trace(outcome={"evaluations": [{"name": "ok", "passed": True}]})
        assert list(FailedEvaluatorDetector().detect(trace)) == []

    def test_an_unrecorded_verdict_is_not_evidence(self) -> None:
        """``passed`` absent means nobody decided, not that it failed."""
        trace = make_trace(outcome={"evaluations": [{"name": "ungraded", "score": 0.1}]})
        assert list(FailedEvaluatorDetector().detect(trace)) == []

    def test_every_failure_gets_its_own_signal(self) -> None:
        trace = make_trace(
            outcome={
                "evaluations": [
                    {"name": "one", "passed": False},
                    {"name": "two", "passed": True},
                    {"name": "three", "passed": False},
                ]
            }
        )
        signals = list(FailedEvaluatorDetector().detect(trace))
        assert [s.evidence["evaluator"] for s in signals] == ["one", "three"]


class TestCombining:
    def test_signals_accumulate_without_a_score(self) -> None:
        """Three signals means better documented, not 'more likely'."""
        trace = make_trace(
            outcome={
                "status": "failure",
                "feedback": {"rating": "negative"},
                "evaluations": [{"name": "grader", "passed": False}],
            }
        )
        signals = detect_signals(trace)
        assert [s.kind for s in signals] == [
            SignalKind.EXPLICIT_STATUS,
            SignalKind.NEGATIVE_FEEDBACK,
            SignalKind.FAILED_EVALUATOR,
        ]
        assert not any(hasattr(signal, "confidence") for signal in signals)

    def test_a_clean_trace_yields_nothing(self) -> None:
        assert detect_signals(make_trace(outcome={"status": "success"})) == []

    def test_detector_order_is_fixed(self) -> None:
        trace = make_trace(outcome={"status": "failure", "feedback": {"rating": "negative"}})
        assert detect_signals(trace) == detect_signals(trace)

    def test_a_custom_detector_set_can_be_used(self) -> None:
        trace = make_trace(outcome={"status": "failure", "feedback": {"rating": "negative"}})
        signals = detect_signals(trace, [NegativeFeedbackDetector()])
        assert [s.kind for s in signals] == [SignalKind.NEGATIVE_FEEDBACK]

    def test_the_registered_detectors_satisfy_the_protocol(self) -> None:
        assert len(DETECTORS) == 3
        for detector in DETECTORS:
            assert isinstance(detector, FailureDetector)
            assert detector.name and detector.description


class TestSignalSerialization:
    def test_round_trips(self) -> None:
        signal = Signal(
            detector="explicit_status",
            kind=SignalKind.EXPLICIT_STATUS,
            source="outcome.status",
            summary="marked failed",
            evidence={"status": "failure"},
        )
        assert Signal.from_dict(signal.to_dict()) == signal

    def test_tolerates_missing_evidence(self) -> None:
        payload = {
            "detector": "d",
            "kind": "explicit_status",
            "source": "outcome.status",
            "summary": "s",
        }
        assert Signal.from_dict(payload).evidence == {}

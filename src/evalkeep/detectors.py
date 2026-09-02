"""Failure detectors: evidence that something went wrong, never a guess.

A detector reads one stored (already redacted) trace and emits a
:class:`Signal` for each piece of explicit evidence it finds. Detectors do not
infer failure from the shape of an interaction, and they do not score their
confidence -- a signal either points at something a person recorded, or it does
not exist. Combining signals is therefore counting evidence, not adding
probabilities: a trace with three signals is better documented than one with a
single signal, not "more likely" to be a failure.

Adding a detector means implementing :class:`FailureDetector` and registering
it; nothing else in the pipeline changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from evalkeep.trace import EvaluationEvent, NormalizedTrace, OutcomeStatus


class SignalKind(StrEnum):
    """The category of evidence, independent of which detector found it."""

    EXPLICIT_STATUS = "explicit_status"
    NEGATIVE_FEEDBACK = "negative_feedback"
    FAILED_EVALUATOR = "failed_evaluator"


@dataclass(frozen=True)
class Signal:
    """One piece of evidence, with a pointer back to where it came from."""

    detector: str
    kind: SignalKind
    #: Path into the trace, so a reviewer can check the claim themselves.
    source: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "kind": self.kind.value,
            "source": self.source,
            "summary": self.summary,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Signal:
        return cls(
            detector=payload["detector"],
            kind=SignalKind(payload["kind"]),
            source=payload["source"],
            summary=payload["summary"],
            evidence=payload.get("evidence", {}),
        )


@runtime_checkable
class FailureDetector(Protocol):
    """Reads a trace, yields evidence. Never raises on well-formed input."""

    name: ClassVar[str]
    description: ClassVar[str]

    def detect(self, trace: NormalizedTrace) -> Iterable[Signal]:
        """Yield one signal per piece of evidence found in ``trace``."""
        ...


class ExplicitStatusDetector:
    """The recorded outcome says the interaction failed."""

    name: ClassVar[str] = "explicit_status"
    description: ClassVar[str] = "outcome.status is 'failure' or 'error'"

    _FAILING: ClassVar[dict[OutcomeStatus, str]] = {
        OutcomeStatus.FAILURE: "the trace is explicitly marked as failed",
        OutcomeStatus.ERROR: "the trace is explicitly marked as errored",
    }

    def detect(self, trace: NormalizedTrace) -> Iterator[Signal]:
        summary = self._FAILING.get(trace.outcome.status)
        if summary is None:
            return
        yield Signal(
            detector=self.name,
            kind=SignalKind.EXPLICIT_STATUS,
            source="outcome.status",
            summary=summary,
            evidence={"status": trace.outcome.status.value},
        )


class NegativeFeedbackDetector:
    """Structured feedback records a negative rating.

    Only an explicit ``rating`` of ``negative`` counts. A bare numeric score
    arrives without its scale -- 2 could be poor out of 5 or good out of 3 --
    and inventing a threshold would be exactly the kind of blind scoring this
    pipeline is meant to avoid.
    """

    name: ClassVar[str] = "negative_feedback"
    description: ClassVar[str] = "outcome.feedback.rating is 'negative'"

    def detect(self, trace: NormalizedTrace) -> Iterator[Signal]:
        feedback = trace.outcome.feedback
        if feedback is None or feedback.rating != "negative":
            return
        evidence: dict[str, Any] = {"rating": feedback.rating}
        if feedback.comment:
            evidence["comment"] = feedback.comment
        if feedback.score is not None:
            evidence["score"] = feedback.score
        yield Signal(
            detector=self.name,
            kind=SignalKind.NEGATIVE_FEEDBACK,
            source="outcome.feedback",
            summary=feedback.comment or "feedback was rated negative",
            evidence=evidence,
        )


class FailedEvaluatorDetector:
    """An evaluator recorded a failing verdict, in the outcome or in an event."""

    name: ClassVar[str] = "failed_evaluator"
    description: ClassVar[str] = "an evaluation recorded passed=false"

    def detect(self, trace: NormalizedTrace) -> Iterator[Signal]:
        for index, evaluation in enumerate(trace.outcome.evaluations):
            if evaluation.passed is False:
                yield self._signal(
                    source=f"outcome.evaluations.{index}",
                    name=evaluation.name,
                    reason=evaluation.reason,
                    score=evaluation.score,
                )
        for index, event in enumerate(trace.events):
            if isinstance(event, EvaluationEvent) and event.passed is False:
                yield self._signal(
                    source=f"events.{index}",
                    name=event.name,
                    reason=event.reason,
                    score=event.score,
                )

    def _signal(self, *, source: str, name: str, reason: str | None, score: float | None) -> Signal:
        evidence: dict[str, Any] = {"evaluator": name, "passed": False}
        if reason:
            evidence["reason"] = reason
        if score is not None:
            evidence["score"] = score
        return Signal(
            detector=self.name,
            kind=SignalKind.FAILED_EVALUATOR,
            source=source,
            summary=reason or f"evaluator {name!r} reported a failure",
            evidence=evidence,
        )


#: The detectors run by ``evalkeep detect``, in a fixed order so that the
#: signals persisted for a trace are reproducible.
DETECTORS: tuple[FailureDetector, ...] = (
    ExplicitStatusDetector(),
    NegativeFeedbackDetector(),
    FailedEvaluatorDetector(),
)


def detect_signals(
    trace: NormalizedTrace, detectors: Iterable[FailureDetector] = DETECTORS
) -> list[Signal]:
    """Every signal every detector finds in ``trace``, in detector order."""
    return [signal for detector in detectors for signal in detector.detect(trace)]

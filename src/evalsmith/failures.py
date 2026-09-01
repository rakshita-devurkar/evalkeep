"""Failure candidates: one per trace, carrying every contributing signal.

A failure is the durable record that a trace is worth attention. It holds the
evidence the detectors found and, once a person has looked, their decision. The
two are kept apart on purpose: signals are derived and can be recomputed, while
a review is a human judgement that automated detection must never overwrite.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from evalsmith.detectors import Signal

FAILURE_ID_PREFIX = "fail-"
_FAILURE_ID_LENGTH = 12


class FailureStatus(StrEnum):
    """Where a failure stands with its reviewer."""

    #: Detected automatically; nobody has looked yet.
    CANDIDATE = "candidate"
    #: A person confirmed this is a real failure.
    CONFIRMED = "confirmed"
    #: A person decided this is not a failure. Kept for audit, not deleted.
    DISMISSED = "dismissed"


class FailureOrigin(StrEnum):
    DETECTOR = "detector"
    MANUAL = "manual"


def failure_id_for(trace_id: str) -> str:
    """A stable failure ID derived from the trace it belongs to.

    Detection is idempotent because this is a pure function of the trace ID:
    re-running it addresses the same row rather than creating a second one.
    """
    digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:_FAILURE_ID_LENGTH]
    return f"{FAILURE_ID_PREFIX}{digest}"


@dataclass
class Failure:
    """One trace's failure record."""

    failure_id: str
    trace_id: str
    status: FailureStatus = FailureStatus.CANDIDATE
    origin: FailureOrigin = FailureOrigin.DETECTOR
    signals: list[Signal] = field(default_factory=list)
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reviewer: str | None = None
    reason: str | None = None

    @classmethod
    def from_signals(cls, trace_id: str, signals: list[Signal]) -> Failure:
        return cls(failure_id=failure_id_for(trace_id), trace_id=trace_id, signals=list(signals))

    @classmethod
    def manual(cls, trace_id: str, *, reviewer: str, reason: str | None = None) -> Failure:
        """A failure a person added by hand, with no detector evidence."""
        return cls(
            failure_id=failure_id_for(trace_id),
            trace_id=trace_id,
            status=FailureStatus.CONFIRMED,
            origin=FailureOrigin.MANUAL,
            reviewer=reviewer,
            reason=reason,
        )

    @property
    def reviewed(self) -> bool:
        """True once a person has decided, which detection must not undo."""
        return self.status is not FailureStatus.CANDIDATE

    @property
    def kinds(self) -> list[str]:
        """The distinct evidence kinds behind this failure, in order."""
        seen: dict[str, None] = {}
        for signal in self.signals:
            seen.setdefault(signal.kind.value, None)
        return list(seen)

    def review(self, status: FailureStatus, *, reviewer: str, reason: str | None) -> None:
        self.status = status
        self.reviewer = reviewer
        self.reason = reason
        self.updated_at = datetime.now(UTC)

"""The detection pass: turn stored traces into evidence-backed failure candidates.

Detection is idempotent, and the rules that make it so are the whole design:

* A failure's ID is a pure function of its trace ID, so a second pass addresses
  the same row instead of creating another one.
* Signals are *derived*. Every pass recomputes and replaces them, so changing or
  adding a detector updates the evidence on existing failures.
* A review is *not* derived. Once a person confirms or dismisses a failure,
  detection updates its signals and leaves the decision, reviewer and reason
  alone -- automated analysis never overwrites a human judgement.
* A candidate nobody has reviewed, whose evidence has since disappeared (a
  detector was removed or narrowed), is withdrawn. Nothing human is lost,
  because nothing human was there. Manually added failures are never withdrawn.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from evalkeep.detectors import DETECTORS, FailureDetector, SignalKind, detect_signals
from evalkeep.failures import Failure, FailureOrigin
from evalkeep.storage import TraceStore


@dataclass
class DetectionReport:
    """What one detection pass did."""

    traces: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    withdrawn: int = 0
    preserved_reviews: int = 0
    signals: int = 0
    by_kind: dict[SignalKind, int] = field(default_factory=dict)

    @property
    def failures(self) -> int:
        """Traces that came out of this pass carrying evidence."""
        return self.created + self.updated + self.unchanged

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.withdrawn)


def detect_failures(
    store: TraceStore, *, detectors: Iterable[FailureDetector] = DETECTORS
) -> DetectionReport:
    """Run every detector over every stored trace and persist the result."""
    detectors = tuple(detectors)
    report = DetectionReport()
    failures = store.failures

    for trace in store.iter_traces():
        report.traces += 1
        signals = detect_signals(trace, detectors)
        existing = failures.get_by_trace(trace.trace_id)

        if not signals:
            # No evidence. Withdraw an unreviewed detector candidate; leave
            # anything a person touched or created.
            if (
                existing is not None
                and not existing.reviewed
                and existing.origin is FailureOrigin.DETECTOR
            ):
                failures.delete(existing.failure_id)
                report.withdrawn += 1
            continue

        report.signals += len(signals)
        for signal in signals:
            report.by_kind[signal.kind] = report.by_kind.get(signal.kind, 0) + 1

        if existing is None:
            failures.save(Failure.from_signals(trace.trace_id, signals))
            report.created += 1
            continue

        if existing.reviewed:
            report.preserved_reviews += 1
        if [s.to_dict() for s in existing.signals] == [s.to_dict() for s in signals]:
            report.unchanged += 1
            continue

        existing.signals = signals
        failures.save(existing)
        report.updated += 1

    return report

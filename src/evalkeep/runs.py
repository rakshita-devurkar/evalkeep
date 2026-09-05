"""Evaluation runs and their per-test results.

The distinction that matters here is between a test that *failed* and a test
that never got to run. An assertion failure is information about the agent; a
timeout or a crashed provider is information about the harness. Conflating them
would let an outage look like a regression, which is exactly the mistake the
comparison stage exists to avoid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    #: Never evaluated: the harness or the target failed, not the agent's answer.
    ERROR = "error"


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class Verdict(StrEnum):
    """What a whole set of repetitions says about one case.

    The third value is the point of repeating at all: a case that sometimes
    passes has told you something a single execution cannot.
    """

    PASS = "pass"
    FAIL = "fail"
    #: Passed some repetitions and failed others.
    FLAKY = "flaky"
    #: Never actually evaluated.
    ERROR = "error"


@dataclass
class CaseResult:
    test_id: str
    outcome: Outcome
    error_kind: ErrorKind | None = None
    error: str | None = None
    latency_ms: int | None = None
    #: What the agent actually produced, redacted before storage.
    observation: str | None = None
    failed_assertions: list[str] = field(default_factory=list)
    #: Which execution of this case this was, zero-based.
    repetition: int = 0

    @property
    def comparable(self) -> bool:
        """Errors are reported separately, never counted as pass or fail."""
        return self.outcome is not Outcome.ERROR


@dataclass
class CaseSummary:
    """Every execution of one case in one run, taken together."""

    test_id: str
    passed: int = 0
    failed: int = 0
    errored: int = 0

    @property
    def repetitions(self) -> int:
        return self.passed + self.failed + self.errored

    @property
    def evaluated(self) -> int:
        """Repetitions that actually ran. Errors are not evidence either way."""
        return self.passed + self.failed

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.evaluated if self.evaluated else None

    @property
    def verdict(self) -> Verdict:
        if not self.evaluated:
            return Verdict.ERROR
        if self.passed == self.evaluated:
            return Verdict.PASS
        if self.passed == 0:
            return Verdict.FAIL
        return Verdict.FLAKY

    @property
    def flaky(self) -> bool:
        return self.verdict is Verdict.FLAKY

    @property
    def confidence(self) -> tuple[float, float] | None:
        """A Wilson interval on the pass rate, or ``None`` with nothing to go on.

        Wilson rather than the normal approximation because these counts are
        small and one-sided -- 20 passes out of 20 would otherwise produce an
        interval of zero width, which claims certainty no sample can give.
        """
        return wilson_interval(self.passed, self.evaluated)


@dataclass
class EvaluationRun:
    run_id: str
    target_id: str
    #: Identifies the exact set of approved tests that ran, so two runs are only
    #: compared when they answered the same questions.
    suite_hash: str
    tests: int = 0
    #: How many times each test was executed.
    repetitions: int = 1
    status: RunStatus = RunStatus.COMPLETED
    runner: str | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    output_dir: str | None = None


@dataclass
class BaselinePromotion:
    """A recorded decision that one run is now the reference point."""

    promotion_id: str
    run_id: str
    target_id: str
    reviewer: str
    reason: str | None = None
    promoted_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def suite_hash(test_ids: list[str]) -> str:
    """A fingerprint of which tests a run covered."""
    material = "\n".join(sorted(test_ids))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


#: 95% two-sided normal quantile, for the Wilson interval.
_Z = 1.959963984540054


def wilson_interval(successes: int, trials: int) -> tuple[float, float] | None:
    """A Wilson score interval for a proportion.

    Chosen over the Wald interval because it behaves at the edges, where these
    measurements live: an all-pass or all-fail case still gets an interval that
    reflects how few times it was tried.
    """
    if trials <= 0:
        return None
    rate = successes / trials
    denominator = 1 + _Z**2 / trials
    centre = (rate + _Z**2 / (2 * trials)) / denominator
    spread = _Z * ((rate * (1 - rate) / trials + _Z**2 / (4 * trials**2)) ** 0.5) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def summarize(results: list[CaseResult]) -> dict[str, CaseSummary]:
    """Group repeated executions by case."""
    summaries: dict[str, CaseSummary] = {}
    for result in results:
        summary = summaries.setdefault(result.test_id, CaseSummary(test_id=result.test_id))
        match result.outcome:
            case Outcome.PASS:
                summary.passed += 1
            case Outcome.FAIL:
                summary.failed += 1
            case Outcome.ERROR:
                summary.errored += 1
    return summaries

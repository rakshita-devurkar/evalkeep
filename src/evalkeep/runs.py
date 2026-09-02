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

    @property
    def comparable(self) -> bool:
        """Errors are reported separately, never counted as pass or fail."""
        return self.outcome is not Outcome.ERROR


@dataclass
class EvaluationRun:
    run_id: str
    target_id: str
    #: Identifies the exact set of approved tests that ran, so two runs are only
    #: compared when they answered the same questions.
    suite_hash: str
    tests: int = 0
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

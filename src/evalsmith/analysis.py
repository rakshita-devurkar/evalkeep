"""Structured failure analysis and the provider interface behind it.

Detection says *that* a trace failed and points at the evidence. Analysis says
*what kind* of failure it is, so that similar failures can be grouped and a
representative can be chosen. That description is a judgement, so every analysis
records who made it -- a person, or a named model at a named prompt version --
and keeps the raw response for audit.

The vocabularies below are deliberately closed. Free-text labels do not cluster:
"wrong order id", "refunded the wrong order" and "bad tool arg" are one failure
family that three different analysts would name three different ways.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from evalsmith.detectors import Signal
from evalsmith.trace import NormalizedTrace


class FailureType(StrEnum):
    """What went wrong."""

    WRONG_TOOL_ARGUMENT = "wrong_tool_argument"
    WRONG_TOOL_SELECTION = "wrong_tool_selection"
    MISSING_TOOL_CALL = "missing_tool_call"
    UNNECESSARY_ACTION = "unnecessary_action"
    INCORRECT_ANSWER = "incorrect_answer"
    INCOMPLETE_ANSWER = "incomplete_answer"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    FORMAT_VIOLATION = "format_violation"
    POLICY_VIOLATION = "policy_violation"
    UNWARRANTED_REFUSAL = "unwarranted_refusal"
    EXECUTION_ERROR = "execution_error"
    OTHER = "other"


class Component(StrEnum):
    """Where in the agent it went wrong."""

    PLANNING = "planning"
    TOOL_SELECTION = "tool_selection"
    TOOL_ARGUMENTS = "tool_arguments"
    TOOL_EXECUTION = "tool_execution"
    RETRIEVAL = "retrieval"
    RESPONSE_GENERATION = "response_generation"
    POLICY = "policy"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Ordered worst-first, for selecting high-severity representatives in 8F.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
)


@dataclass(frozen=True)
class ProviderAnalysis:
    """What a provider produced, before the pipeline stamps its provenance."""

    failure_type: FailureType
    component: Component
    severity: Severity
    summary: str
    raw_response: str | None = None


@dataclass
class FailureAnalysis:
    """A provider analysis plus the provenance that makes it auditable."""

    failure_type: FailureType
    component: Component
    severity: Severity
    summary: str
    #: Who decided: ``manual:alex``, ``anthropic:claude-opus-5``, ``stub``.
    analyzer: str
    prompt_version: int
    analyzed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Set when a person labelled by hand.
    labeler: str | None = None
    #: The provider's own words, redacted. Kept so a surprising label can be
    #: checked against what the model actually said.
    raw_response: str | None = None

    @property
    def manual(self) -> bool:
        return self.analyzer.startswith("manual")

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.index(self.severity)

    @classmethod
    def from_provider(
        cls,
        produced: ProviderAnalysis,
        *,
        analyzer: str,
        prompt_version: int,
    ) -> FailureAnalysis:
        return cls(
            failure_type=produced.failure_type,
            component=produced.component,
            severity=produced.severity,
            summary=produced.summary,
            analyzer=analyzer,
            prompt_version=prompt_version,
            raw_response=produced.raw_response,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_type": self.failure_type.value,
            "component": self.component.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "analyzer": self.analyzer,
            "prompt_version": self.prompt_version,
            "analyzed_at": self.analyzed_at.isoformat(),
            "labeler": self.labeler,
            "raw_response": self.raw_response,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FailureAnalysis:
        return cls(
            failure_type=FailureType(payload["failure_type"]),
            component=Component(payload["component"]),
            severity=Severity(payload["severity"]),
            summary=payload["summary"],
            analyzer=payload["analyzer"],
            prompt_version=int(payload["prompt_version"]),
            analyzed_at=datetime.fromisoformat(payload["analyzed_at"]),
            labeler=payload.get("labeler"),
            raw_response=payload.get("raw_response"),
        )


class AnalyzerError(Exception):
    """A provider could not analyze this failure. Never fatal to a run."""


@runtime_checkable
class AnalyzerProvider(Protocol):
    """Provider-independent analysis.

    ``identity`` is part of the cache key, so it must change whenever the thing
    producing the answer changes -- a different model is a different analyst.
    """

    name: ClassVar[str]
    description: ClassVar[str]

    @property
    def identity(self) -> str: ...

    def analyze_failure(self, trace: NormalizedTrace, signals: list[Signal]) -> ProviderAnalysis:
        """Describe one failure. Raises :class:`AnalyzerError` on failure."""
        ...

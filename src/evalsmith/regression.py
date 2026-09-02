"""Regression tests: what a failure must never do again.

A regression test preserves an observed failure as a permanent check. The shape
of one follows from what a trace can and cannot tell us:

* A trace shows exactly **what the agent did wrong**, so the forbidding half of
  a test -- "do not refund an older order" -- is derivable and deterministic.
* A trace does **not** show what the agent should have done instead. "Refund
  exactly the newest order" is a judgement about intent that no amount of
  reading the trace can supply.

So generation produces the forbidding half automatically and marks the test as
needing the positive half from a reviewer. That split is why drafts are never
exported without approval.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import combinations
from typing import Any

MAX_SLUG_WORDS = 6
MAX_SLUG_LENGTH = 48
_TEST_ID_DIGEST_LENGTH = 8

_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_REDACTION_PATTERN = re.compile(r"\[REDACTED:[a-z_]+\]", re.IGNORECASE)


class ExpectationType(StrEnum):
    OUTPUT_CONTAINS = "output_contains"
    OUTPUT_NOT_CONTAINS = "output_not_contains"
    OUTPUT_MATCHES = "output_matches"
    TOOL_CALLED = "tool_called"
    TOOL_NOT_CALLED = "tool_not_called"
    TOOL_ARGUMENT_EQUALS = "tool_argument_equals"
    TOOL_ARGUMENT_NOT_EQUALS = "tool_argument_not_equals"
    MAX_TOOL_CALLS = "max_tool_calls"
    #: The escape hatch for cases with no deterministic check. Costs an LLM
    #: judge at run time, so it is used only where nothing else applies.
    HUMAN_RUBRIC = "human_rubric"


#: Expectations that say what the agent *should* do. A test made only of the
#: others records a prohibition without an intent, which is half a test.
POSITIVE_TYPES: frozenset[ExpectationType] = frozenset(
    {
        ExpectationType.OUTPUT_CONTAINS,
        ExpectationType.OUTPUT_MATCHES,
        ExpectationType.TOOL_CALLED,
        ExpectationType.TOOL_ARGUMENT_EQUALS,
        ExpectationType.HUMAN_RUBRIC,
    }
)

#: Everything except the rubric can be checked without a model.
DETERMINISTIC_TYPES: frozenset[ExpectationType] = frozenset(
    set(ExpectationType) - {ExpectationType.HUMAN_RUBRIC}
)


class ReviewStatus(StrEnum):
    """The review lifecycle. Generation only ever writes ``DRAFT``."""

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Expectation:
    type: ExpectationType
    value: Any = None
    tool: str | None = None
    #: Argument path within a tool call, dotted for nested values.
    path: str | None = None

    @property
    def deterministic(self) -> bool:
        return self.type in DETERMINISTIC_TYPES

    @property
    def positive(self) -> bool:
        return self.type in POSITIVE_TYPES

    def describe(self) -> str:
        match self.type:
            case ExpectationType.TOOL_ARGUMENT_EQUALS:
                return f"{self.tool}.{self.path} == {self.value!r}"
            case ExpectationType.TOOL_ARGUMENT_NOT_EQUALS:
                return f"{self.tool}.{self.path} != {self.value!r}"
            case ExpectationType.TOOL_CALLED:
                return f"calls {self.tool}"
            case ExpectationType.TOOL_NOT_CALLED:
                return f"never calls {self.tool}"
            case ExpectationType.MAX_TOOL_CALLS:
                target = self.tool or "any tool"
                return f"at most {self.value} calls to {target}"
            case ExpectationType.HUMAN_RUBRIC:
                return f"rubric: {self.value}"
            case _:
                return f"{self.type.value} {self.value!r}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type.value, "value": self.value}
        if self.tool is not None:
            payload["tool"] = self.tool
        if self.path is not None:
            payload["path"] = self.path
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Expectation:
        return cls(
            type=ExpectationType(payload["type"]),
            value=payload.get("value"),
            tool=payload.get("tool"),
            path=payload.get("path"),
        )


@dataclass(frozen=True)
class Contradiction:
    """Two expectations that cannot both hold."""

    first: Expectation
    second: Expectation
    reason: str

    def describe(self) -> str:
        return f"{self.first.describe()} vs {self.second.describe()}: {self.reason}"


def validate_expectation(expectation: Expectation) -> str | None:
    """Reasons an expectation could never be evaluated, or ``None`` if it can."""
    match expectation.type:
        case ExpectationType.TOOL_ARGUMENT_EQUALS | ExpectationType.TOOL_ARGUMENT_NOT_EQUALS:
            if not expectation.tool or not expectation.path:
                return "an argument expectation needs both a tool and a path"
        case ExpectationType.TOOL_CALLED | ExpectationType.TOOL_NOT_CALLED:
            if not expectation.tool:
                return "a tool expectation needs a tool"
        case ExpectationType.MAX_TOOL_CALLS:
            if not isinstance(expectation.value, int) or isinstance(expectation.value, bool):
                return "max_tool_calls needs a whole number"
            if expectation.value < 0:
                return "max_tool_calls cannot be negative"
        case ExpectationType.OUTPUT_MATCHES:
            if not isinstance(expectation.value, str):
                return "output_matches needs a pattern"
            try:
                re.compile(expectation.value)
            except re.error as exc:
                return f"output_matches pattern does not compile: {exc}"
        case ExpectationType.OUTPUT_CONTAINS | ExpectationType.OUTPUT_NOT_CONTAINS:
            if not isinstance(expectation.value, str) or not expectation.value.strip():
                return f"{expectation.type.value} needs non-empty text"
        case ExpectationType.HUMAN_RUBRIC:
            if not isinstance(expectation.value, str) or not expectation.value.strip():
                return "human_rubric needs a description of what should happen"
    return None


def find_contradictions(expectations: list[Expectation]) -> list[Contradiction]:
    """Every pair of expectations that cannot both be satisfied.

    Run at generation time so a draft is never saved self-defeating, and again
    at review time so an edit cannot introduce one. A contradictory test fails
    on every agent, including a correct one, which makes it worse than no test:
    it reports a regression that is really a bug in the suite.
    """
    found: list[Contradiction] = []
    for first, second in combinations(expectations, 2):
        reason = _conflict(first, second) or _conflict(second, first)
        if reason is not None:
            found.append(Contradiction(first=first, second=second, reason=reason))
    return found


def _conflict(one: Expectation, two: Expectation) -> str | None:
    if (
        one.type is ExpectationType.TOOL_CALLED
        and two.type is ExpectationType.TOOL_NOT_CALLED
        and one.tool == two.tool
    ):
        return "the same tool cannot be both required and forbidden"

    if (
        one.type is ExpectationType.TOOL_NOT_CALLED
        and two.type
        in {ExpectationType.TOOL_ARGUMENT_EQUALS, ExpectationType.TOOL_ARGUMENT_NOT_EQUALS}
        and one.tool == two.tool
    ):
        return "an argument of a forbidden tool can never be checked"

    if (
        one.type is ExpectationType.TOOL_CALLED
        and two.type is ExpectationType.MAX_TOOL_CALLS
        and two.value == 0
        and (two.tool is None or two.tool == one.tool)
    ):
        return "a required tool cannot also be capped at zero calls"

    if (
        one.type is ExpectationType.TOOL_ARGUMENT_EQUALS
        and two.type is ExpectationType.TOOL_ARGUMENT_NOT_EQUALS
        and (one.tool, one.path) == (two.tool, two.path)
        and one.value == two.value
    ):
        return "the same argument cannot be required and forbidden to equal one value"

    if (
        one.type is two.type is ExpectationType.TOOL_ARGUMENT_EQUALS
        and (one.tool, one.path) == (two.tool, two.path)
        and one.value != two.value
    ):
        return "one argument cannot equal two different values"

    if (
        one.type is ExpectationType.OUTPUT_CONTAINS
        and two.type is ExpectationType.OUTPUT_NOT_CONTAINS
        and one.value == two.value
    ):
        return "the output cannot both contain and not contain the same text"

    if (
        one.type is two.type is ExpectationType.MAX_TOOL_CALLS
        and one.tool == two.tool
        and one.value != two.value
    ):
        return "two different call limits for the same tool"

    return None


@dataclass
class CaseInput:
    """What the test sends to the agent."""

    text: str | None = None
    messages: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "messages": self.messages}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CaseInput:
        return cls(text=payload.get("text"), messages=payload.get("messages", []))


@dataclass
class Fixture:
    """A tool result the original agent saw, so a replay can reproduce it."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "result": self.result,
            "error": self.error,
            "call_id": self.call_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Fixture:
        return cls(
            tool=payload["tool"],
            arguments=payload.get("arguments", {}),
            result=payload.get("result"),
            error=payload.get("error"),
            call_id=payload.get("call_id"),
        )


@dataclass
class Provenance:
    """Where a test came from, in enough detail to defend or discard it."""

    trace_id: str
    failure_id: str
    content_hash: str
    #: Recorded, but never part of the test ID: clusters are rebuilt and
    #: relabelled, and an ID that moved with them would not be stable.
    cluster_id: str | None = None
    cluster_label: str | None = None
    representative_roles: list[str] = field(default_factory=list)
    failure_type: str | None = None
    severity: str | None = None
    analyzer: str | None = None
    analysis_summary: str | None = None
    evidence: list[str] = field(default_factory=list)
    generator_version: int = 1
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            key: getattr(self, key)
            for key in (
                "trace_id",
                "failure_id",
                "content_hash",
                "cluster_id",
                "cluster_label",
                "representative_roles",
                "failure_type",
                "severity",
                "analyzer",
                "analysis_summary",
                "evidence",
                "generator_version",
            )
        }
        payload["generated_at"] = self.generated_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Provenance:
        data = dict(payload)
        generated_at = data.pop("generated_at", None)
        return cls(
            **data,
            generated_at=(
                datetime.fromisoformat(generated_at) if generated_at else datetime.now(UTC)
            ),
        )


@dataclass
class RegressionTest:
    test_id: str
    failure_id: str
    input: CaseInput
    provenance: Provenance
    status: ReviewStatus = ReviewStatus.DRAFT
    fixtures: list[Fixture] = field(default_factory=list)
    expectations: list[Expectation] = field(default_factory=list)
    #: Things a reviewer must decide, recorded rather than guessed at.
    warnings: list[str] = field(default_factory=list)
    reviewer: str | None = None
    review_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def reviewed(self) -> bool:
        return self.status is not ReviewStatus.DRAFT

    @property
    def deterministic_expectations(self) -> list[Expectation]:
        return [item for item in self.expectations if item.deterministic]

    @property
    def has_positive_expectation(self) -> bool:
        return any(item.positive for item in self.expectations)

    @property
    def contradictions(self) -> list[Contradiction]:
        return find_contradictions(self.expectations)


def slugify(text: str) -> str:
    """A short, readable, stable stem for a test ID."""
    cleaned = _REDACTION_PATTERN.sub(" ", text or "").lower()
    words = _WORD_PATTERN.findall(cleaned)[:MAX_SLUG_WORDS]
    return "_".join(words)[:MAX_SLUG_LENGTH].strip("_")


def make_test_id(trace_id: str, input_text: str) -> str:
    """A readable, stable test ID derived only from immutable facts.

    Deliberately *not* derived from the cluster label or the analysis: both are
    mutable. A reviewer renaming a cluster, or a re-analysis changing a failure
    type, must not rename a test that is already committed to Git and referenced
    by past run results.

    The stem comes from the trace's own input, which never changes once stored,
    and the suffix from the trace ID, which guarantees uniqueness.
    """
    digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:_TEST_ID_DIGEST_LENGTH]
    stem = slugify(input_text)
    return f"{stem}_{digest}" if stem else f"test_{digest}"

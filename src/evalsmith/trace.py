"""The normalized trace schema.

Every adapter converts a provider's format into these models, so the rest of
Evalsmith -- redaction, failure detection, clustering, test generation -- only
ever sees one shape.

Two deliberate strictnesses:

* Structural models reject unknown fields. A typo like ``trace-id`` is a defect
  worth reporting, not data worth keeping, and every field that survives here
  must be one the redactor knows how to scrub. Arbitrary provider data belongs
  under ``metadata.extra``, which redaction walks recursively.
* Identifiers are stripped and must be non-empty, so a whitespace-only
  ``trace_id`` fails validation rather than becoming a silent duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

#: Bumped when the trace schema changes in a way stored traces must migrate for.
SCHEMA_VERSION = 1

MAX_IDENTIFIER_LENGTH = 256
MAX_TOOL_NAME_LENGTH = 128

#: Identifiers are trimmed and must survive trimming.
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_IDENTIFIER_LENGTH),
]

#: Tool names must look like callable identifiers: no spaces, no path separators,
#: nothing that could be mistaken for a shell fragment when exported to a runner.
ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_TOOL_NAME_LENGTH,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$",
    ),
]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"
    UNKNOWN = "unknown"


class _Strict(BaseModel):
    """Base for every trace model: unknown fields are a validation error."""

    model_config = ConfigDict(extra="forbid")


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC so orderings stay comparable."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class Message(_Strict):
    role: Role
    content: str


class TraceInput(_Strict):
    """What the agent was asked to do."""

    text: str | None = None
    messages: list[Message] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_content(self) -> TraceInput:
        if not (self.text or "").strip() and not self.messages:
            raise ValueError("input must provide non-empty 'text' or at least one message")
        return self


class TraceOutput(_Strict):
    """What the agent produced. Absent for traces captured before a response."""

    text: str | None = None
    messages: list[Message] = Field(default_factory=list)


class _BaseEvent(_Strict):
    event_id: Identifier
    timestamp: datetime | None = None

    _normalize_timestamp = field_validator("timestamp")(_as_utc)


class MessageEvent(_BaseEvent):
    type: Literal["message"] = "message"
    role: Role
    content: str


class ToolCallEvent(_BaseEvent):
    type: Literal["tool_call"] = "tool_call"
    tool: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: Identifier | None = None


class ToolResultEvent(_BaseEvent):
    type: Literal["tool_result"] = "tool_result"
    tool: ToolName
    call_id: Identifier | None = None
    result: Any = None
    error: str | None = None


class EvaluationEvent(_BaseEvent):
    """An evaluator's verdict recorded alongside the interaction."""

    type: Literal["evaluation"] = "evaluation"
    name: NonEmptyStr
    passed: bool | None = None
    score: float | None = None
    reason: str | None = None


Event = Annotated[
    MessageEvent | ToolCallEvent | ToolResultEvent | EvaluationEvent,
    Field(discriminator="type"),
]


class Feedback(_Strict):
    """Human or automated feedback attached to the interaction."""

    rating: Literal["positive", "negative"] | None = None
    score: float | None = None
    comment: str | None = None


class Evaluation(_Strict):
    name: NonEmptyStr
    passed: bool | None = None
    score: float | None = None
    reason: str | None = None


class Outcome(_Strict):
    """The three evidence sources the failure detectors read in guide 8D."""

    status: OutcomeStatus = OutcomeStatus.UNKNOWN
    feedback: Feedback | None = None
    evaluations: list[Evaluation] = Field(default_factory=list)


class TraceMetadata(_Strict):
    recorded_at: datetime | None = None
    source: str | None = None
    agent: str | None = None
    model: str | None = None
    tags: list[str] = Field(default_factory=list)
    #: The one open field. Provider-specific data goes here and is redacted
    #: recursively; nothing else in the schema accepts unknown keys.
    extra: dict[str, Any] = Field(default_factory=dict)

    _normalize_recorded_at = field_validator("recorded_at")(_as_utc)


class NormalizedTrace(_Strict):
    """One recorded interaction, in the only shape Evalsmith stores."""

    schema_version: int = SCHEMA_VERSION
    trace_id: Identifier
    input: TraceInput
    output: TraceOutput | None = None
    events: list[Event] = Field(default_factory=list)
    outcome: Outcome = Field(default_factory=Outcome)
    metadata: TraceMetadata = Field(default_factory=TraceMetadata)

    @model_validator(mode="after")
    def _validate_event_sequence(self) -> NormalizedTrace:
        """Events must be uniquely identified, ordered, and internally consistent."""
        seen_event_ids: set[str] = set()
        open_calls: set[str] = set()
        previous: datetime | None = None

        for index, event in enumerate(self.events):
            where = f"events[{index}]"
            if event.event_id in seen_event_ids:
                raise ValueError(f"{where}: duplicate event_id {event.event_id!r}")
            seen_event_ids.add(event.event_id)

            if event.timestamp is not None:
                if previous is not None and event.timestamp < previous:
                    raise ValueError(
                        f"{where}: timestamp {event.timestamp.isoformat()} is earlier than "
                        f"the preceding event's {previous.isoformat()}; events must be ordered"
                    )
                previous = event.timestamp

            if isinstance(event, ToolCallEvent) and event.call_id is not None:
                if event.call_id in open_calls:
                    raise ValueError(f"{where}: duplicate call_id {event.call_id!r}")
                open_calls.add(event.call_id)
            elif isinstance(event, ToolResultEvent) and event.call_id is not None:
                if event.call_id not in open_calls:
                    raise ValueError(
                        f"{where}: call_id {event.call_id!r} does not match an earlier tool_call"
                    )

        return self

    @property
    def tool_calls(self) -> list[ToolCallEvent]:
        """Observed tool calls, in order. Used by detection and test generation."""
        return [event for event in self.events if isinstance(event, ToolCallEvent)]

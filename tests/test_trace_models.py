"""The normalized trace schema (guide 8B)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from evalkeep.trace import (
    SCHEMA_VERSION,
    NormalizedTrace,
    OutcomeStatus,
    ToolCallEvent,
)

MINIMAL: dict[str, Any] = {
    "trace_id": "trace-1042",
    "input": {"text": "Refund my latest order."},
    "outcome": {"status": "failure"},
}


def trace(**overrides: Any) -> dict[str, Any]:
    return {**MINIMAL, **overrides}


class TestMinimumTrace:
    def test_the_documented_minimum_trace_validates(self) -> None:
        parsed = NormalizedTrace.model_validate(MINIMAL)
        assert parsed.trace_id == "trace-1042"
        assert parsed.outcome.status is OutcomeStatus.FAILURE
        assert parsed.schema_version == SCHEMA_VERSION
        assert parsed.events == []

    def test_outcome_defaults_to_unknown(self) -> None:
        parsed = NormalizedTrace.model_validate({"trace_id": "t1", "input": {"text": "hello"}})
        assert parsed.outcome.status is OutcomeStatus.UNKNOWN

    def test_round_trips_through_json(self) -> None:
        parsed = NormalizedTrace.model_validate(MINIMAL)
        assert NormalizedTrace.model_validate_json(parsed.model_dump_json()) == parsed


class TestIdentifiers:
    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_trace_id_must_survive_trimming(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="trace_id"):
            NormalizedTrace.model_validate(trace(trace_id=bad))

    def test_trace_id_is_trimmed(self) -> None:
        assert NormalizedTrace.model_validate(trace(trace_id="  t1  ")).trace_id == "t1"

    def test_trace_id_is_required(self) -> None:
        with pytest.raises(ValidationError, match="trace_id"):
            NormalizedTrace.model_validate({"input": {"text": "hi"}})

    def test_event_id_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError, match="event_id"):
            NormalizedTrace.model_validate(
                trace(events=[{"event_id": " ", "type": "message", "role": "user", "content": "x"}])
            )

    def test_duplicate_event_ids_are_rejected(self) -> None:
        events = [
            {"event_id": "e1", "type": "message", "role": "user", "content": "a"},
            {"event_id": "e1", "type": "message", "role": "assistant", "content": "b"},
        ]
        with pytest.raises(ValidationError, match="duplicate event_id"):
            NormalizedTrace.model_validate(trace(events=events))


class TestInput:
    def test_input_is_required(self) -> None:
        with pytest.raises(ValidationError, match="input"):
            NormalizedTrace.model_validate({"trace_id": "t1"})

    def test_input_needs_text_or_messages(self) -> None:
        with pytest.raises(ValidationError, match="text' or at least one message"):
            NormalizedTrace.model_validate(trace(input={}))

    def test_whitespace_only_text_is_not_content(self) -> None:
        with pytest.raises(ValidationError, match="text' or at least one message"):
            NormalizedTrace.model_validate(trace(input={"text": "   "}))

    def test_messages_alone_are_enough(self) -> None:
        parsed = NormalizedTrace.model_validate(
            trace(input={"messages": [{"role": "user", "content": "Refund it."}]})
        )
        assert parsed.input.messages[0].content == "Refund it."

    def test_unknown_role_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="role"):
            NormalizedTrace.model_validate(
                trace(input={"messages": [{"role": "wizard", "content": "hi"}]})
            )


class TestToolEvents:
    def _with_tool(self, **fields: Any) -> dict[str, Any]:
        event = {"event_id": "e1", "type": "tool_call", "tool": "refund_order", **fields}
        return trace(events=[event])

    def test_a_tool_call_carries_its_arguments(self) -> None:
        parsed = NormalizedTrace.model_validate(self._with_tool(arguments={"order_id": "order-A"}))
        call = parsed.events[0]
        assert isinstance(call, ToolCallEvent)
        assert call.arguments["order_id"] == "order-A"
        assert parsed.tool_calls == [call]

    @pytest.mark.parametrize(
        "bad_name",
        ["", "  ", "refund order", "refund/order", "9refund", "rm -rf /", "a" * 200],
    )
    def test_invalid_tool_names_are_rejected(self, bad_name: str) -> None:
        with pytest.raises(ValidationError, match="tool"):
            NormalizedTrace.model_validate(self._with_tool(tool=bad_name))

    @pytest.mark.parametrize("good_name", ["refund_order", "orders.list", "get-order", "_private"])
    def test_identifier_like_tool_names_are_accepted(self, good_name: str) -> None:
        parsed = NormalizedTrace.model_validate(self._with_tool(tool=good_name))
        assert parsed.tool_calls[0].tool == good_name

    def test_unknown_event_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="type"):
            NormalizedTrace.model_validate(trace(events=[{"event_id": "e1", "type": "telepathy"}]))

    def test_tool_result_must_follow_its_call(self) -> None:
        events = [
            {"event_id": "e1", "type": "tool_result", "tool": "refund_order", "call_id": "c1"},
        ]
        with pytest.raises(ValidationError, match="does not match an earlier tool_call"):
            NormalizedTrace.model_validate(trace(events=events))

    def test_matching_call_and_result_are_accepted(self) -> None:
        events = [
            {"event_id": "e1", "type": "tool_call", "tool": "refund_order", "call_id": "c1"},
            {"event_id": "e2", "type": "tool_result", "tool": "refund_order", "call_id": "c1"},
        ]
        assert len(NormalizedTrace.model_validate(trace(events=events)).events) == 2

    def test_duplicate_call_ids_are_rejected(self) -> None:
        events = [
            {"event_id": "e1", "type": "tool_call", "tool": "refund_order", "call_id": "c1"},
            {"event_id": "e2", "type": "tool_call", "tool": "refund_order", "call_id": "c1"},
        ]
        with pytest.raises(ValidationError, match="duplicate call_id"):
            NormalizedTrace.model_validate(trace(events=events))


class TestTimestamps:
    def _events(self, first: str, second: str) -> list[dict[str, Any]]:
        return [
            {
                "event_id": "e1",
                "type": "message",
                "role": "user",
                "content": "a",
                "timestamp": first,
            },
            {
                "event_id": "e2",
                "type": "message",
                "role": "assistant",
                "content": "b",
                "timestamp": second,
            },
        ]

    def test_out_of_order_timestamps_are_rejected(self) -> None:
        events = self._events("2026-08-14T09:12:05Z", "2026-08-14T09:12:04Z")
        with pytest.raises(ValidationError, match="events must be ordered"):
            NormalizedTrace.model_validate(trace(events=events))

    def test_equal_timestamps_are_allowed_for_parallel_calls(self) -> None:
        events = self._events("2026-08-14T09:12:05Z", "2026-08-14T09:12:05Z")
        assert len(NormalizedTrace.model_validate(trace(events=events)).events) == 2

    def test_malformed_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timestamp"):
            NormalizedTrace.model_validate(
                trace(
                    events=[
                        {
                            "event_id": "e1",
                            "type": "message",
                            "role": "user",
                            "content": "a",
                            "timestamp": "last tuesday",
                        }
                    ]
                )
            )

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        parsed = NormalizedTrace.model_validate(
            trace(
                events=[
                    {
                        "event_id": "e1",
                        "type": "message",
                        "role": "user",
                        "content": "a",
                        "timestamp": "2026-08-14T09:12:05",
                    }
                ]
            )
        )
        assert parsed.events[0].timestamp == datetime(2026, 8, 14, 9, 12, 5, tzinfo=UTC)

    def test_timestamps_are_optional(self) -> None:
        parsed = NormalizedTrace.model_validate(
            trace(events=[{"event_id": "e1", "type": "message", "role": "user", "content": "a"}])
        )
        assert parsed.events[0].timestamp is None


class TestUnknownFields:
    def test_unknown_top_level_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            NormalizedTrace.model_validate(trace(session_id="s-1"))

    def test_a_misspelled_field_does_not_pass_silently(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedTrace.model_validate({"trace-id": "t1", "input": {"text": "hi"}})

    def test_arbitrary_data_is_accepted_under_metadata_extra(self) -> None:
        parsed = NormalizedTrace.model_validate(
            trace(metadata={"extra": {"session_id": "s-1", "nested": {"k": [1, 2]}}})
        )
        assert parsed.metadata.extra["nested"] == {"k": [1, 2]}


class TestOutcomeEvidence:
    def test_negative_feedback_is_preserved(self) -> None:
        parsed = NormalizedTrace.model_validate(
            trace(
                outcome={
                    "status": "failure",
                    "feedback": {"rating": "negative", "comment": "wrong order"},
                }
            )
        )
        assert parsed.outcome.feedback is not None
        assert parsed.outcome.feedback.rating == "negative"

    def test_failed_evaluations_are_preserved(self) -> None:
        parsed = NormalizedTrace.model_validate(
            trace(outcome={"evaluations": [{"name": "refunds-newest", "passed": False}]})
        )
        assert parsed.outcome.evaluations[0].passed is False

    def test_unknown_status_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            NormalizedTrace.model_validate(trace(outcome={"status": "sort-of-ok"}))

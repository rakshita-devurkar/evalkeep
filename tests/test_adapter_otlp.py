"""Reading OpenTelemetry traces exported as OTLP JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evalkeep.adapters import AdapterRecord, get_adapter
from evalkeep.adapters.otlp import OtlpAdapter
from evalkeep.adapters.semconv import (
    decode_attributes,
    decode_value,
    indexed,
    messages,
    tool_calls,
)
from evalkeep.trace import NormalizedTrace, ToolCallEvent, ToolResultEvent

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        # Protobuf renders 64-bit integers as JSON strings.
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def span(
    span_id: str,
    *,
    parent: str | None = None,
    name: str = "span",
    attributes: dict[str, Any] | None = None,
    status: int = 1,
    message: str | None = None,
    start: int = 1_755_000_000_000_000_000,
) -> dict[str, Any]:
    return {
        "traceId": TRACE,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 1_000_000_000),
        "attributes": [attribute(k, v) for k, v in (attributes or {}).items()],
        "status": {"code": status, **({"message": message} if message else {})},
    }


def document(*spans: dict[str, Any], service: str = "shopping-agent") -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [attribute("service.name", service)]},
                "scopeSpans": [{"spans": list(spans)}],
            }
        ]
    }


def read(tmp_path: Path, payload: Any, *, name: str = "otlp.json") -> list[AdapterRecord]:
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return list(OtlpAdapter().read(path))


def one(tmp_path: Path, *spans: dict[str, Any]) -> NormalizedTrace:
    (record,) = read(tmp_path, document(*spans))
    trace = record.trace
    assert trace is not None, record.issues
    return trace


AGENT = {
    "openinference.span.kind": "AGENT",
    "input.value": "Refund my latest order.",
    "output.value": "I've refunded order order-A.",
}
LLM_WITH_CALL = {
    "openinference.span.kind": "LLM",
    "llm.model_name": "gpt-4o",
    "llm.output_messages.0.message.role": "assistant",
    "llm.output_messages.0.message.tool_calls.0.tool_call.function.name": "refund_order",
    "llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments": (
        '{"order_id": "order-A"}'
    ),
}
TOOL = {
    "openinference.span.kind": "TOOL",
    "tool.name": "refund_order",
    "tool.parameters": '{"order_id": "order-A"}',
    "output.value": '{"status": "refunded"}',
}


class TestAttributeDecoding:
    def test_every_any_value_shape(self) -> None:
        assert decode_value({"stringValue": "x"}) == "x"
        assert decode_value({"boolValue": True}) is True
        assert decode_value({"doubleValue": 1.5}) == 1.5
        assert decode_value({"intValue": "42"}) == 42
        assert decode_value({"arrayValue": {"values": [{"stringValue": "a"}]}}) == ["a"]

    def test_a_nested_kvlist_decodes(self) -> None:
        value = {"kvlistValue": {"values": [attribute("inner", "x")]}}
        assert decode_value(value) == {"inner": "x"}

    def test_an_unknown_shape_is_none_rather_than_an_error(self) -> None:
        assert decode_value({"somethingNew": 1}) is None

    def test_a_missing_attribute_list_is_empty(self) -> None:
        assert decode_attributes(None) == {}
        assert decode_attributes("not a list") == {}


class TestUnflattening:
    def test_indexed_keys_become_a_list(self) -> None:
        attributes = {
            "llm.input_messages.0.message.role": "user",
            "llm.input_messages.0.message.content": "hello",
            "llm.input_messages.1.message.role": "assistant",
            "llm.input_messages.1.message.content": "hi",
        }
        assert indexed(attributes, "llm.input_messages") == [
            {"message.role": "user", "message.content": "hello"},
            {"message.role": "assistant", "message.content": "hi"},
        ]

    def test_order_follows_the_index_not_the_attribute_order(self) -> None:
        attributes = {
            "m.10.message.content": "eleventh",
            "m.2.message.content": "third",
        }
        assert [entry["message.content"] for entry in indexed(attributes, "m")] == [
            "third",
            "eleventh",
        ]

    def test_unrelated_keys_are_ignored(self) -> None:
        assert indexed({"other.0.x": 1}, "llm.input_messages") == []

    def test_messages_come_back_as_role_and_content(self) -> None:
        attributes = {
            "llm.input_messages.0.message.role": "user",
            "llm.input_messages.0.message.content": "Refund it.",
        }
        assert messages(attributes) == [{"role": "user", "content": "Refund it."}]

    def test_gen_ai_messages_are_read_as_a_fallback(self) -> None:
        attributes = {
            "gen_ai.input.messages": json.dumps([{"role": "user", "content": "Refund it."}])
        }
        assert messages(attributes) == [{"role": "user", "content": "Refund it."}]

    def test_gen_ai_content_parts_are_flattened(self) -> None:
        attributes = {
            "gen_ai.input.messages": json.dumps(
                [{"role": "user", "content": [{"text": "Refund "}, {"text": "it."}]}]
            )
        }
        assert messages(attributes) == [{"role": "user", "content": "Refund it."}]

    def test_openinference_wins_when_both_are_present(self) -> None:
        """The more specific convention, and the only one that models tool calls."""
        attributes = {
            "llm.input_messages.0.message.role": "user",
            "llm.input_messages.0.message.content": "from openinference",
            "gen_ai.input.messages": json.dumps([{"role": "user", "content": "from gen_ai"}]),
        }
        assert messages(attributes)[0]["content"] == "from openinference"

    def test_nested_tool_calls_are_extracted(self) -> None:
        assert tool_calls(LLM_WITH_CALL) == [("refund_order", {"order_id": "order-A"})]


class TestGrouping:
    def test_one_otel_trace_becomes_one_evalkeep_trace(self, tmp_path: Path) -> None:
        records = read(tmp_path, document(span("a1", attributes=AGENT), span("b2", parent="a1")))
        assert len(records) == 1
        assert records[0].trace is not None
        assert records[0].trace.trace_id == TRACE

    def test_separate_traces_stay_separate(self, tmp_path: Path) -> None:
        other = {**span("z9", attributes=AGENT), "traceId": "second"}
        records = read(tmp_path, document(span("a1", attributes=AGENT), other))
        assert {r.trace.trace_id for r in records if r.trace} == {TRACE, "second"}

    def test_the_root_is_the_span_nothing_parents(self, tmp_path: Path) -> None:
        trace = one(
            tmp_path,
            span("b2", parent="a1", attributes={"input.value": "child"}),
            span("a1", attributes=AGENT),
        )
        assert trace.input.text == "Refund my latest order."

    def test_an_orphan_parent_still_yields_a_root(self, tmp_path: Path) -> None:
        """Exports get truncated; a dangling parent must not lose the trace."""
        trace = one(tmp_path, span("b2", parent="missing", attributes=AGENT))
        assert trace.input.text == "Refund my latest order."

    def test_spans_are_ordered_by_start_time(self, tmp_path: Path) -> None:
        late = span("c3", attributes=TOOL, start=2_000_000_000_000_000_000)
        early = span(
            "b2",
            attributes={**TOOL, "tool.name": "list_orders"},
            start=1_000_000_000_000_000_000,
        )
        trace = one(tmp_path, span("a1", attributes=AGENT), late, early)
        assert [call.tool for call in trace.tool_calls] == ["list_orders", "refund_order"]


class TestEvents:
    def test_a_tool_span_becomes_a_call_and_a_result(self, tmp_path: Path) -> None:
        trace = one(tmp_path, span("a1", attributes=AGENT), span("c3", attributes=TOOL))
        call, result = trace.events
        assert isinstance(call, ToolCallEvent)
        assert isinstance(result, ToolResultEvent)
        assert call.tool == "refund_order"
        assert call.arguments == {"order_id": "order-A"}
        assert result.result == {"status": "refunded"}
        assert call.call_id == result.call_id

    def test_a_declared_call_with_no_span_is_kept(self, tmp_path: Path) -> None:
        """A tool the agent asked for and never ran is a real observation."""
        trace = one(tmp_path, span("a1", attributes=AGENT), span("b2", attributes=LLM_WITH_CALL))
        assert [call.tool for call in trace.tool_calls] == ["refund_order"]
        assert not any(isinstance(event, ToolResultEvent) for event in trace.events)

    def test_a_declared_call_matched_by_a_span_is_not_doubled(self, tmp_path: Path) -> None:
        """The LLM declares the intent and the tool span records it running."""
        trace = one(
            tmp_path,
            span("a1", attributes=AGENT),
            span("b2", attributes=LLM_WITH_CALL),
            span("c3", attributes=TOOL),
        )
        assert len(trace.tool_calls) == 1

    def test_two_identical_calls_with_one_span_keep_one_intent(self, tmp_path: Path) -> None:
        """Deduplication is one-for-one, not by name."""
        twice = {
            **LLM_WITH_CALL,
            "llm.output_messages.0.message.tool_calls.1.tool_call.function.name": "refund_order",
            "llm.output_messages.0.message.tool_calls.1.tool_call.function.arguments": (
                '{"order_id": "order-A"}'
            ),
        }
        trace = one(
            tmp_path,
            span("a1", attributes=AGENT),
            span("b2", attributes=twice),
            span("c3", attributes=TOOL),
        )
        assert len(trace.tool_calls) == 2

    def test_an_errored_tool_span_records_the_message(self, tmp_path: Path) -> None:
        failing = span("c3", attributes=TOOL, status=2, message="refunded the wrong order")
        trace = one(tmp_path, span("a1", attributes=AGENT), failing)
        result = trace.events[-1]
        assert isinstance(result, ToolResultEvent)
        assert result.error == "refunded the wrong order"

    def test_a_gen_ai_tool_span_is_recognized(self, tmp_path: Path) -> None:
        attributes = {"gen_ai.tool.name": "refund_order", "input.value": '{"order_id": "x"}'}
        trace = one(tmp_path, span("a1", attributes=AGENT), span("c3", attributes=attributes))
        assert trace.tool_calls[0].tool == "refund_order"

    def test_a_free_form_span_name_is_normalized(self, tmp_path: Path) -> None:
        """Instrumentation names spans freely; rejecting a trace over that is worse."""
        attributes = {"openinference.span.kind": "TOOL"}
        trace = one(
            tmp_path,
            span("a1", attributes=AGENT),
            span("c3", name="Tool: refund order!", attributes=attributes),
        )
        assert trace.tool_calls[0].tool == "Tool__refund_order"


class TestOutcome:
    def test_an_errored_span_is_evidence(self, tmp_path: Path) -> None:
        failing = span("c3", attributes=TOOL, status=2, message="boom")
        trace = one(tmp_path, span("a1", attributes=AGENT), failing)
        assert trace.outcome.status.value == "error"
        assert trace.outcome.evaluations[0].passed is False
        assert trace.outcome.evaluations[0].reason == "boom"

    def test_no_error_is_unknown_not_success(self, tmp_path: Path) -> None:
        """OTel records what happened, never whether the answer was any good."""
        trace = one(tmp_path, span("a1", attributes=AGENT))
        assert trace.outcome.status.value == "unknown"

    def test_an_ok_status_is_still_unknown(self, tmp_path: Path) -> None:
        trace = one(tmp_path, span("a1", attributes=AGENT, status=1))
        assert trace.outcome.status.value == "unknown"


class TestMetadataAndInput:
    def test_the_service_name_becomes_the_agent(self, tmp_path: Path) -> None:
        trace = one(tmp_path, span("a1", attributes=AGENT))
        assert trace.metadata.agent == "shopping-agent"
        assert trace.metadata.source == "opentelemetry"

    def test_the_model_is_found_on_a_child_span(self, tmp_path: Path) -> None:
        trace = one(tmp_path, span("a1", attributes=AGENT), span("b2", attributes=LLM_WITH_CALL))
        assert trace.metadata.model == "gpt-4o"

    def test_the_start_time_is_recorded(self, tmp_path: Path) -> None:
        trace = one(tmp_path, span("a1", attributes=AGENT))
        assert trace.metadata.recorded_at is not None

    def test_the_input_falls_back_to_a_child_span(self, tmp_path: Path) -> None:
        """Instrumentation often records the prompt on the first LLM span."""
        child = {
            "llm.input_messages.0.message.role": "user",
            "llm.input_messages.0.message.content": "Refund it.",
        }
        trace = one(tmp_path, span("a1"), span("b2", parent="a1", attributes=child))
        assert trace.input.messages[0].content == "Refund it."

    def test_a_trace_with_no_input_at_all_still_validates(self, tmp_path: Path) -> None:
        trace = one(tmp_path, span("a1", name="RefundAgent"))
        assert trace.input.text == "RefundAgent"


class TestFileShapes:
    def test_a_single_json_document(self, tmp_path: Path) -> None:
        assert read(tmp_path, document(span("a1", attributes=AGENT)))[0].ok

    def test_one_document_per_line(self, tmp_path: Path) -> None:
        payload = "\n".join(json.dumps(document(span("a1", attributes=AGENT))) for _ in range(1))
        assert read(tmp_path, payload)[0].ok

    def test_a_malformed_line_is_reported_not_raised(self, tmp_path: Path) -> None:
        payload = "{not json\n" + json.dumps(document(span("a1", attributes=AGENT)))
        records = read(tmp_path, payload)
        assert any(not r.ok for r in records)
        assert any(r.ok for r in records)

    def test_an_empty_file_yields_nothing(self, tmp_path: Path) -> None:
        assert read(tmp_path, "") == []

    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        (record,) = list(OtlpAdapter().read(tmp_path / "absent.json"))
        assert not record.ok
        assert "could not read" in record.issues[0].message

    def test_an_envelope_with_no_spans_yields_nothing(self, tmp_path: Path) -> None:
        assert read(tmp_path, {"resourceSpans": []}) == []


class TestRegistry:
    def test_the_adapter_is_registered(self) -> None:
        assert get_adapter("otlp").name == "otlp"

    def test_it_satisfies_the_protocol(self) -> None:
        from evalkeep.adapters import TraceAdapter

        assert isinstance(OtlpAdapter(), TraceAdapter)


class TestBundledExample:
    def test_the_example_export_reads_cleanly(self) -> None:
        example = (
            Path(__file__).resolve().parents[1] / "src/evalkeep/examples/opentelemetry/spans.json"
        )
        records = list(OtlpAdapter().read(example))
        assert records, "the bundled example should contain traces"
        assert all(record.ok for record in records), [
            issue.message for r in records for issue in r.issues
        ]
        assert any(record.trace and record.trace.tool_calls for record in records)


@pytest.mark.parametrize("status", [0, 1])
def test_non_error_statuses_never_invent_success(tmp_path: Path, status: int) -> None:
    trace = one(tmp_path, span("a1", attributes=AGENT, status=status))
    assert trace.outcome.status.value == "unknown"

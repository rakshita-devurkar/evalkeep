"""Reading LangSmith runs exported to JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evalkeep.adapters import AdapterRecord, get_adapter
from evalkeep.adapters.langsmith import LangSmithAdapter
from evalkeep.trace import NormalizedTrace, ToolCallEvent, ToolResultEvent

TRACE = "trace-9f2c"


def run(
    run_id: str,
    run_type: str = "chain",
    *,
    parent: str | None = None,
    name: str = "RefundAgent",
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": run_id,
        "trace_id": TRACE,
        "parent_run_id": parent,
        "name": name,
        "run_type": run_type,
        "status": "success",
        "start_time": "2026-08-14T09:12:03Z",
        "end_time": "2026-08-14T09:12:08Z",
        "inputs": inputs or {},
        "outputs": outputs or {},
    }
    payload.update(extra)
    return payload


def read(tmp_path: Path, payload: Any, *, name: str = "runs.jsonl") -> list[AdapterRecord]:
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload)
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        path.write_text("".join(json.dumps(item) + "\n" for item in payload))
    else:
        path.write_text(json.dumps(payload))
    return list(LangSmithAdapter().read(path))


def one(tmp_path: Path, *runs: dict[str, Any]) -> NormalizedTrace:
    (record,) = read(tmp_path, list(runs))
    trace = record.trace
    assert trace is not None, record.issues
    return trace


ROOT = run(
    "r1",
    inputs={"input": "Refund my latest order."},
    outputs={"output": "I've refunded order order-A."},
    extra={"metadata": {"ls_model_name": "gpt-4o", "ls_project_name": "shopping-agent"}},
    tags=["prod"],
)
LLM = run(
    "r2",
    "llm",
    parent="r1",
    name="ChatOpenAI",
    inputs={"messages": [[{"role": "human", "content": "Refund my latest order."}]]},
    outputs={
        "generations": [
            [
                {
                    "message": {
                        "kwargs": {
                            "content": "",
                            "tool_calls": [
                                {"name": "refund_order", "args": {"order_id": "order-A"}}
                            ],
                        }
                    }
                }
            ]
        ]
    },
)
TOOL = run(
    "r3",
    "tool",
    parent="r1",
    name="refund_order",
    inputs={"order_id": "order-A"},
    outputs={"status": "refunded"},
)


class TestGrouping:
    def test_a_run_tree_becomes_one_trace(self, tmp_path: Path) -> None:
        records = read(tmp_path, [ROOT, LLM, TOOL])
        assert len(records) == 1
        assert records[0].trace is not None
        assert records[0].trace.trace_id == TRACE

    def test_separate_traces_stay_separate(self, tmp_path: Path) -> None:
        other = {**ROOT, "id": "z", "trace_id": "second"}
        records = read(tmp_path, [ROOT, other])
        assert {r.trace.trace_id for r in records if r.trace} == {TRACE, "second"}

    def test_a_run_without_a_trace_id_is_its_own_trace(self, tmp_path: Path) -> None:
        """Some export shapes omit trace_id on a root run."""
        lone = {k: v for k, v in ROOT.items() if k != "trace_id"}
        (record,) = read(tmp_path, [lone])
        assert record.trace is not None
        assert record.trace.trace_id == "r1"

    def test_runs_are_ordered_by_start_time(self, tmp_path: Path) -> None:
        late = {**TOOL, "id": "late", "name": "refund_order", "start_time": "2026-08-14T09:99:00Z"}
        early = {
            **TOOL,
            "id": "early",
            "name": "list_orders",
            "start_time": "2026-08-14T09:00:00Z",
            "inputs": {"customer_id": "cust-77"},
        }
        trace = one(tmp_path, ROOT, late, early)
        assert [call.tool for call in trace.tool_calls] == ["list_orders", "refund_order"]


class TestInputAndOutput:
    def test_the_root_input_is_used(self, tmp_path: Path) -> None:
        assert one(tmp_path, ROOT).input.text == "Refund my latest order."

    def test_langchain_roles_are_translated(self, tmp_path: Path) -> None:
        """LangChain says human and ai; the trace schema says user and assistant."""
        bare = run("r1", inputs={"messages": [[{"role": "human", "content": "Refund it."}]]})
        trace = one(tmp_path, bare)
        assert trace.input.messages[0].role.value == "user"

    def test_a_type_field_is_accepted_as_a_role(self, tmp_path: Path) -> None:
        bare = run("r1", inputs={"messages": [{"type": "ai", "content": "Sure."}]})
        assert one(tmp_path, bare).input.messages[0].role.value == "assistant"

    def test_common_input_keys_are_tried_in_order(self, tmp_path: Path) -> None:
        bare = run("r1", inputs={"question": "Where is my order?"})
        assert one(tmp_path, bare).input.text == "Where is my order?"

    def test_an_unrecognized_shape_is_kept_rather_than_dropped(self, tmp_path: Path) -> None:
        bare = run("r1", inputs={"custom_field": "Refund it."})
        assert "Refund it." in (one(tmp_path, bare).input.text or "")

    def test_the_input_falls_back_to_a_child_run(self, tmp_path: Path) -> None:
        bare = run("r1", inputs={})
        trace = one(tmp_path, bare, LLM)
        assert trace.input.messages[0].content == "Refund my latest order."

    def test_the_output_is_read_from_the_root(self, tmp_path: Path) -> None:
        assert one(tmp_path, ROOT).output is not None


class TestEvents:
    def test_a_tool_run_becomes_a_call_and_a_result(self, tmp_path: Path) -> None:
        trace = one(tmp_path, ROOT, TOOL)
        call, result = trace.events
        assert isinstance(call, ToolCallEvent)
        assert isinstance(result, ToolResultEvent)
        assert call.tool == "refund_order"
        assert call.arguments == {"order_id": "order-A"}
        assert result.result == {"status": "refunded"}

    def test_a_declared_call_matched_by_a_tool_run_is_not_doubled(self, tmp_path: Path) -> None:
        assert len(one(tmp_path, ROOT, LLM, TOOL).tool_calls) == 1

    def test_a_declared_call_with_no_tool_run_is_kept(self, tmp_path: Path) -> None:
        trace = one(tmp_path, ROOT, LLM)
        assert [call.tool for call in trace.tool_calls] == ["refund_order"]

    def test_openai_style_function_calls_are_read(self, tmp_path: Path) -> None:
        openai_style = run(
            "r2",
            "llm",
            parent="r1",
            outputs={
                "generations": [
                    [
                        {
                            "message": {
                                "kwargs": {
                                    "tool_calls": [
                                        {
                                            "function": {
                                                "name": "refund_order",
                                                "arguments": '{"order_id": "order-A"}',
                                            }
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                ]
            },
        )
        trace = one(tmp_path, ROOT, openai_style)
        assert trace.tool_calls[0].arguments == {"order_id": "order-A"}

    def test_a_failing_tool_run_records_its_error(self, tmp_path: Path) -> None:
        failing = {**TOOL, "status": "error", "error": "refunded the wrong order"}
        trace = one(tmp_path, ROOT, failing)
        result = trace.events[-1]
        assert isinstance(result, ToolResultEvent)
        assert result.error == "refunded the wrong order"

    def test_a_free_form_run_name_is_normalized(self, tmp_path: Path) -> None:
        odd = {**TOOL, "name": "Tool: refund order!"}
        assert one(tmp_path, ROOT, odd).tool_calls[0].tool == "Tool__refund_order"


class TestOutcome:
    def test_an_error_is_evidence(self, tmp_path: Path) -> None:
        failing = {**ROOT, "status": "error", "error": "refunded the wrong order"}
        trace = one(tmp_path, failing)
        assert trace.outcome.status.value == "error"
        assert trace.outcome.evaluations[0].reason == "refunded the wrong order"

    def test_negative_feedback_is_evidence(self, tmp_path: Path) -> None:
        rated = {
            **ROOT,
            "feedback": [{"key": "user_score", "score": 0, "comment": "Wrong order."}],
        }
        trace = one(tmp_path, rated)
        assert trace.outcome.status.value == "failure"
        assert trace.outcome.feedback is not None
        assert trace.outcome.feedback.rating == "negative"
        assert trace.outcome.feedback.comment == "Wrong order."

    def test_a_positive_score_is_not_evidence(self, tmp_path: Path) -> None:
        rated = {**ROOT, "feedback": [{"key": "user_score", "score": 1}]}
        assert one(tmp_path, rated).outcome.status.value == "unknown"

    def test_an_unscored_comment_is_left_to_a_person(self, tmp_path: Path) -> None:
        """A comment without a score has no direction; guessing one would be wrong."""
        rated = {**ROOT, "feedback": [{"key": "note", "comment": "interesting"}]}
        assert one(tmp_path, rated).outcome.status.value == "unknown"

    def test_an_explicit_thumbs_down_counts(self, tmp_path: Path) -> None:
        rated = {**ROOT, "feedback": [{"key": "user", "value": "thumbs_down"}]}
        assert one(tmp_path, rated).outcome.status.value == "failure"

    def test_success_is_never_invented(self, tmp_path: Path) -> None:
        assert one(tmp_path, ROOT).outcome.status.value == "unknown"


class TestMetadata:
    def test_project_and_model_are_carried(self, tmp_path: Path) -> None:
        trace = one(tmp_path, ROOT)
        assert trace.metadata.source == "langsmith"
        assert trace.metadata.agent == "shopping-agent"
        assert trace.metadata.model == "gpt-4o"

    def test_tags_survive(self, tmp_path: Path) -> None:
        assert one(tmp_path, ROOT).metadata.tags == ["prod"]

    def test_a_naive_timestamp_is_read_as_utc(self, tmp_path: Path) -> None:
        naive = {**ROOT, "start_time": "2026-08-14T09:12:03"}
        trace = one(tmp_path, naive)
        assert trace.metadata.recorded_at is not None
        assert trace.metadata.recorded_at.tzinfo is not None


class TestFileShapes:
    def test_jsonl(self, tmp_path: Path) -> None:
        assert read(tmp_path, [ROOT, TOOL])[0].ok

    def test_a_json_array(self, tmp_path: Path) -> None:
        path = tmp_path / "runs.json"
        path.write_text(json.dumps([ROOT, TOOL]))
        assert next(iter(LangSmithAdapter().read(path))).ok

    def test_a_malformed_line_is_reported_not_raised(self, tmp_path: Path) -> None:
        payload = "{not json\n" + json.dumps(ROOT)
        records = read(tmp_path, payload)
        assert any(not r.ok for r in records) and any(r.ok for r in records)

    def test_an_empty_file_yields_nothing(self, tmp_path: Path) -> None:
        assert read(tmp_path, "") == []

    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        (record,) = list(LangSmithAdapter().read(tmp_path / "absent.jsonl"))
        assert not record.ok

    def test_a_run_without_an_id_is_skipped(self, tmp_path: Path) -> None:
        assert read(tmp_path, [{"name": "no id"}]) == []


class TestRegistry:
    def test_the_adapter_is_registered(self) -> None:
        assert get_adapter("langsmith").name == "langsmith"

    def test_it_satisfies_the_protocol(self) -> None:
        from evalkeep.adapters import TraceAdapter

        assert isinstance(LangSmithAdapter(), TraceAdapter)


class TestBundledExample:
    def test_the_example_export_reads_cleanly(self) -> None:
        example = Path(__file__).resolve().parents[1] / "src/evalkeep/examples/langsmith/runs.jsonl"
        records = list(LangSmithAdapter().read(example))
        assert len(records) == 5
        assert all(record.ok for record in records)

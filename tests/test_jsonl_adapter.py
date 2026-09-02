"""The generic JSONL adapter: normalize, or fail with a usable issue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evalkeep.adapters import AdapterRecord, IssueKind, JsonlAdapter, TraceAdapter, get_adapter
from evalkeep.errors import CommandError

MINIMAL: dict[str, Any] = {
    "trace_id": "trace-1",
    "input": {"text": "Refund my latest order."},
    "outcome": {"status": "failure"},
}


def records(*lines: str) -> list[AdapterRecord]:
    return list(JsonlAdapter().read_lines(lines))


def line_for(**overrides: Any) -> str:
    return json.dumps({**MINIMAL, **overrides})


class TestParsing:
    def test_reads_one_trace_per_line(self) -> None:
        parsed = records(line_for(trace_id="t1"), line_for(trace_id="t2"))
        assert [r.trace.trace_id for r in parsed if r.trace] == ["t1", "t2"]
        assert all(r.ok for r in parsed)

    def test_line_numbers_are_one_based_and_include_bad_lines(self) -> None:
        parsed = records(line_for(trace_id="t1"), "{oops", line_for(trace_id="t3"))
        assert [r.line for r in parsed] == [1, 2, 3]

    def test_blank_lines_are_not_records(self) -> None:
        parsed = records("", "   \n", line_for(), "\n")
        assert len(parsed) == 1
        assert parsed[0].line == 3

    def test_a_leading_byte_order_mark_is_tolerated(self) -> None:
        assert records("﻿" + line_for())[0].ok


class TestMalformedJson:
    def test_unparseable_line_is_rejected_not_raised(self) -> None:
        (record,) = records('{"trace_id": "t1", ')
        assert not record.ok
        assert record.issues[0].kind is IssueKind.JSON
        assert "invalid JSON" in record.issues[0].message

    def test_one_bad_line_does_not_stop_the_stream(self) -> None:
        parsed = records("not json", line_for(trace_id="t2"))
        assert [r.ok for r in parsed] == [False, True]

    def test_a_json_array_is_not_a_trace(self) -> None:
        (record,) = records('[{"trace_id": "t1"}]')
        assert record.issues[0].kind is IssueKind.JSON
        assert "expected a JSON object, got list" in record.issues[0].message

    def test_a_bare_scalar_is_not_a_trace(self) -> None:
        (record,) = records("42")
        assert "got int" in record.issues[0].message

    def test_undecodable_bytes_are_rejected_as_one_record(self) -> None:
        parsed = list(
            JsonlAdapter().read_binary_lines([b"\xff\xfe not utf-8", line_for().encode()])
        )
        assert parsed[0].issues[0].kind is IssueKind.ENCODING
        assert parsed[1].ok


class TestSchemaIssues:
    def test_missing_required_field_names_the_field(self) -> None:
        (record,) = records(json.dumps({"trace_id": "t1"}))
        assert record.issues[0].kind is IssueKind.SCHEMA
        assert record.issues[0].field == "input"

    def test_every_field_error_becomes_its_own_issue(self) -> None:
        (record,) = records(json.dumps({"input": {}, "outcome": {"status": "nope"}}))
        fields = {issue.field for issue in record.issues}
        assert {"trace_id", "outcome.status"} <= fields
        assert len(record.issues) >= 3

    def test_nested_event_errors_are_reported_by_path(self) -> None:
        line = line_for(
            events=[
                {"event_id": "e1", "type": "message", "role": "user", "content": "a"},
                {"event_id": "e2", "type": "tool_call", "tool": "not a tool name"},
            ]
        )
        (record,) = records(line)
        assert any(issue.field and issue.field.endswith("tool") for issue in record.issues)
        assert any("events.1" in (issue.field or "") for issue in record.issues)
        assert all("tool_call" not in (issue.field or "") for issue in record.issues)

    def test_field_paths_point_into_the_users_own_json(self) -> None:
        """No union tag: 'events.1.tool_call.tool' is not a path in the document."""
        line = line_for(
            events=[
                {"event_id": "e1", "type": "message", "role": "user", "content": "a"},
                {"event_id": "e2", "type": "tool_call", "tool": "not a tool name"},
            ]
        )
        (record,) = records(line)
        assert [i.field for i in record.issues] == ["events.1.tool"]

    def test_a_missing_field_is_still_named(self) -> None:
        line = line_for(events=[{"event_id": "e1", "type": "tool_call"}])
        (record,) = records(line)
        assert [i.field for i in record.issues] == ["events.0.tool"]

    def test_the_trace_id_is_reported_even_when_the_record_is_invalid(self) -> None:
        (record,) = records(json.dumps({"trace_id": "trace-99", "input": {}}))
        assert record.issues[0].trace_id == "trace-99"

    def test_an_unknown_field_suggests_metadata_extra(self) -> None:
        (record,) = records(line_for(session_id="s-1"))
        issue = next(i for i in record.issues if i.field == "session_id")
        assert issue.hint is not None
        assert "metadata.extra" in issue.hint


class TestFileReading:
    def test_streams_a_file_from_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "traces.jsonl"
        path.write_text(line_for(trace_id="t1") + "\n" + line_for(trace_id="t2") + "\n")
        assert [r.trace.trace_id for r in JsonlAdapter().read(path) if r.trace] == ["t1", "t2"]

    def test_reads_the_bundled_refund_example(self) -> None:
        example = Path(__file__).resolve().parents[1] / "examples/refund-agent/traces.jsonl"
        parsed = list(JsonlAdapter().read(example))
        assert len(parsed) == 5
        assert all(record.ok for record in parsed)


class TestStreaming:
    def test_records_are_yielded_lazily(self) -> None:
        """A 100k-trace file must not be read into memory to be validated."""
        produced = 0

        def endless() -> Any:
            nonlocal produced
            while True:
                produced += 1
                yield line_for(trace_id=f"t{produced}")

        stream = JsonlAdapter().read_lines(endless())
        first, second = next(stream), next(stream)

        assert first.trace is not None and second.trace is not None
        assert produced == 2


class TestRegistry:
    def test_the_default_format_resolves(self) -> None:
        adapter = get_adapter("jsonl")
        assert adapter.name == "jsonl"
        assert isinstance(adapter, TraceAdapter)

    def test_an_unknown_format_lists_the_known_ones(self) -> None:
        try:
            get_adapter("parquet")
        except CommandError as exc:
            assert "parquet" in exc.message
            assert exc.hint is not None and "jsonl" in exc.hint
        else:  # pragma: no cover - the call above must raise
            raise AssertionError("expected a CommandError")


class TestIssueSerialization:
    def test_omits_absent_fields(self) -> None:
        (record,) = records("not json")
        payload = record.issues[0].to_dict()
        assert payload == {"line": 1, "kind": "json", "message": payload["message"]}

    def test_includes_field_and_trace_id_when_known(self) -> None:
        (record,) = records(json.dumps({"trace_id": "t1", "input": {}}))
        payload = record.issues[0].to_dict()
        assert payload["trace_id"] == "t1"
        assert payload["field"].startswith("input")

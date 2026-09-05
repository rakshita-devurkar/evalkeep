"""The same five interactions, read through all three adapters.

The bundled examples record one story in three formats. Comparing them is how a
mapping mistake surfaces: an adapter that quietly drops a tool call or invents an
outcome will disagree with the other two here.

The one place they are *expected* to disagree is documented and asserted, because
a difference that nobody wrote down looks exactly like a bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evalkeep.adapters import get_adapter
from evalkeep.detectors import detect_signals
from evalkeep.trace import NormalizedTrace

ROOT = Path(__file__).resolve().parents[1]

SOURCES = {
    "jsonl": ROOT / "examples/refund-agent/traces.jsonl",
    "otlp": ROOT / "examples/opentelemetry/spans.json",
    "langsmith": ROOT / "examples/langsmith/runs.jsonl",
}

#: Tool calls per interaction, in the order the examples record them.
EXPECTED_TOOL_CALLS = [2, 2, 3, 1, 0]


def traces(source: str) -> list[NormalizedTrace]:
    records = list(get_adapter(source).read(SOURCES[source]))
    assert all(record.ok for record in records), [
        issue.message for record in records for issue in record.issues
    ]
    return [record.trace for record in records if record.trace]


@pytest.mark.parametrize("source", sorted(SOURCES))
class TestEveryAdapter:
    def test_reads_all_five_interactions(self, source: str) -> None:
        assert len(traces(source)) == 5

    def test_records_the_same_tool_calls(self, source: str) -> None:
        """A doubled or dropped tool call would show up here."""
        assert [len(trace.tool_calls) for trace in traces(source)] == EXPECTED_TOOL_CALLS

    def test_never_invents_a_success(self, source: str) -> None:
        assert all(
            trace.outcome.status.value != "success"
            for trace in traces(source)
            if not trace.outcome.evaluations and trace.outcome.feedback is None
        )

    def test_the_clean_interactions_produce_no_evidence(self, source: str) -> None:
        """Whatever the format, a normal interaction must not be flagged."""
        assert detect_signals(traces(source)[-1]) == []

    def test_names_its_source(self, source: str) -> None:
        assert all(trace.metadata.source for trace in traces(source))

    def test_the_refund_arguments_survive_the_round_trip(self, source: str) -> None:
        refunds = [call for call in traces(source)[0].tool_calls if call.tool == "refund_order"]
        assert [call.arguments["order_id"] for call in refunds] == ["order-A"]


class TestWhereTheyDiverge:
    """OpenTelemetry cannot express that a person was unhappy.

    The over-refund interaction succeeded technically -- every call returned OK --
    and its only evidence is user feedback. LangSmith carries feedback, so it is
    detected there; OTel has nowhere to put it, so the adapter reports finding
    nothing rather than inventing a signal. That is the correct answer, and it is
    asserted so nobody later 'fixes' it into a guess.
    """

    def _failures(self, source: str) -> int:
        return sum(1 for trace in traces(source) if detect_signals(trace))

    def test_formats_carrying_feedback_find_all_three(self) -> None:
        assert self._failures("jsonl") == 3
        assert self._failures("langsmith") == 3

    def test_opentelemetry_finds_only_what_it_can_see(self) -> None:
        assert self._failures("otlp") == 2

    def test_the_missing_one_is_the_feedback_only_interaction(self) -> None:
        by_source = {
            source: {trace.trace_id for trace in traces(source) if detect_signals(trace)}
            for source in ("otlp", "langsmith")
        }
        # The LangSmith trace IDs carry the original numbering; OTel's are hex.
        assert "trace-1051" in by_source["langsmith"]
        assert len(by_source["otlp"]) == 2

    def test_feedback_reaches_detection_where_it_exists(self) -> None:
        kinds = {
            signal.kind.value for trace in traces("langsmith") for signal in detect_signals(trace)
        }
        assert "negative_feedback" in kinds
        assert "negative_feedback" not in {
            signal.kind.value for trace in traces("otlp") for signal in detect_signals(trace)
        }

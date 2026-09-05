"""Reading OpenTelemetry traces exported as OTLP JSON.

This is the adapter that matters most for adoption, because OTel is the hub
rather than another vendor: Langfuse, Braintrust and Phoenix all ingest OTLP, so
an application instrumented for any of them can be pointed here without changing
its instrumentation.

**One OTel trace becomes one Evalkeep trace.** Spans sharing a trace ID are a
single interaction, and Evalkeep's unit is the interaction, so they are grouped
rather than emitted one by one.

That grouping is the one place this adapter differs from the JSONL one, which
streams a trace per line. Spans of a trace can appear anywhere in an export, so
they are held until the file ends. Memory is therefore proportional to the file,
not constant -- see the measured figures in `docs/pipeline.md`. Split very large
exports by time range.

**What it does not do.** OTel records what an application did, not whether the
answer was any good. A span with an ERROR status is real evidence and becomes an
``error`` outcome; everything else becomes ``unknown``, and it is detection's job
to say it found nothing. Inventing a `success` here would be inventing evidence.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from evalkeep.adapters.base import AdapterRecord, IssueKind, TraceIssue
from evalkeep.adapters.semconv import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_ARGUMENTS,
    GEN_AI_TOOL_NAME,
    INPUT_VALUE,
    KIND_TOOL,
    LLM_MODEL_NAME,
    OUTPUT_VALUE,
    SERVICE_NAME,
    SPAN_KIND,
    TOOL_NAME,
    TOOL_PARAMETERS,
    decode_attributes,
    json_object,
    messages,
    text,
    tool_calls,
)
from evalkeep.trace import NormalizedTrace

#: OTLP status codes. 2 is ERROR; 0 is unset and 1 is OK.
_STATUS_ERROR = 2

_NANOSECONDS = 1_000_000_000


@dataclass
class Span:
    """One OTel span, decoded far enough to be useful."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_nanos: int
    end_nanos: int
    attributes: dict[str, Any] = field(default_factory=dict)
    resource: dict[str, Any] = field(default_factory=dict)
    status_code: int = 0
    status_message: str | None = None

    @property
    def errored(self) -> bool:
        return self.status_code == _STATUS_ERROR

    @property
    def kind(self) -> str:
        value = self.attributes.get(SPAN_KIND)
        return value.upper() if isinstance(value, str) else ""

    @property
    def started_at(self) -> datetime | None:
        if not self.start_nanos:
            return None
        return datetime.fromtimestamp(self.start_nanos / _NANOSECONDS, tz=UTC)


class OtlpAdapter:
    """Reads OTLP JSON, in either of the two shapes exporters produce."""

    name: ClassVar[str] = "otlp"
    description: ClassVar[str] = (
        "OpenTelemetry spans as OTLP JSON (OpenInference or gen_ai conventions)"
    )

    def read(self, path: Path) -> Iterator[AdapterRecord]:
        try:
            documents = list(_documents(path))
        except (OSError, UnicodeDecodeError) as exc:
            yield AdapterRecord.rejected(
                1,
                TraceIssue(
                    line=1, kind=IssueKind.ENCODING, message=f"could not read {path}: {exc}"
                ),
            )
            return

        spans: list[Span] = []
        for line, document in documents:
            if isinstance(document, Exception):
                yield AdapterRecord.rejected(
                    line,
                    TraceIssue(
                        line=line,
                        kind=IssueKind.JSON,
                        message=f"invalid JSON: {document}",
                    ),
                )
                continue
            spans.extend(_spans(document))

        yield from self._group(spans)

    def _group(self, spans: list[Span]) -> Iterator[AdapterRecord]:
        """One record per OTel trace, in first-seen order."""
        grouped: dict[str, list[Span]] = {}
        for span in spans:
            grouped.setdefault(span.trace_id, []).append(span)

        for line, (trace_id, group) in enumerate(grouped.items(), start=1):
            group.sort(key=lambda span: (span.start_nanos, span.span_id))
            try:
                yield AdapterRecord.valid(line, _build(trace_id, group))
            except ValidationError as exc:
                yield AdapterRecord.rejected(
                    line,
                    *[
                        TraceIssue(
                            line=line,
                            kind=IssueKind.SCHEMA,
                            message=detail["msg"],
                            trace_id=trace_id,
                            field=".".join(str(part) for part in detail["loc"]) or None,
                        )
                        for detail in exc.errors()
                    ],
                )


def _documents(path: Path) -> Iterable[tuple[int, Any]]:
    """Yield each JSON document in the file, with the line it started on.

    Exporters produce either one JSON document per file or one per line; both
    are common enough that requiring the right one would just be a papercut.
    """
    raw = path.read_text(encoding="utf-8")
    stripped = raw.strip()
    if not stripped:
        return

    try:
        yield 1, json.loads(stripped)
        return
    except json.JSONDecodeError:
        pass

    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            yield line_number, json.loads(line)
        except json.JSONDecodeError as exc:
            yield line_number, exc


def _spans(document: Any) -> Iterator[Span]:
    """Walk the OTLP envelope down to individual spans."""
    if not isinstance(document, dict):
        return
    for resource_spans in document.get("resourceSpans") or []:
        if not isinstance(resource_spans, dict):
            continue
        resource = decode_attributes((resource_spans.get("resource") or {}).get("attributes"))
        for scope_spans in resource_spans.get("scopeSpans") or []:
            if not isinstance(scope_spans, dict):
                continue
            for raw in scope_spans.get("spans") or []:
                span = _span(raw, resource)
                if span is not None:
                    yield span


def _span(raw: Any, resource: dict[str, Any]) -> Span | None:
    if not isinstance(raw, dict):
        return None
    trace_id = raw.get("traceId")
    span_id = raw.get("spanId")
    if not isinstance(trace_id, str) or not trace_id:
        return None
    status = raw.get("status") or {}
    return Span(
        trace_id=trace_id,
        span_id=span_id if isinstance(span_id, str) else "",
        parent_span_id=raw.get("parentSpanId") or None,
        name=str(raw.get("name") or ""),
        start_nanos=_nanos(raw.get("startTimeUnixNano")),
        end_nanos=_nanos(raw.get("endTimeUnixNano")),
        attributes=decode_attributes(raw.get("attributes")),
        resource=resource,
        status_code=int(status.get("code") or 0),
        status_message=status.get("message") or None,
    )


def _nanos(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build(trace_id: str, spans: list[Span]) -> NormalizedTrace:
    """Assemble one Evalkeep trace from the spans of one OTel trace."""
    root = _root(spans)
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "input": _input(root, spans),
        "events": _events(spans),
        "outcome": _outcome(spans),
        "metadata": _metadata(root, spans),
    }
    output = _output(root)
    if output is not None:
        payload["output"] = output
    return NormalizedTrace.model_validate(payload)


def _root(spans: list[Span]) -> Span:
    """The span nothing else in this trace parents, or the earliest one."""
    known = {span.span_id for span in spans}
    for span in spans:
        if not span.parent_span_id or span.parent_span_id not in known:
            return span
    return spans[0]


def _input(root: Span, spans: list[Span]) -> dict[str, Any]:
    """What the interaction was asked to do.

    Falls back through the trace when the root span carries nothing, because
    instrumentation often records the prompt on the first LLM span rather than
    on the enclosing agent span.
    """
    for span in [root, *spans]:
        conversation = messages(span.attributes)
        if conversation:
            return {"messages": conversation}
        value = text(span.attributes, INPUT_VALUE)
        if value:
            return {"text": value}
    return {"text": root.name or "(no recorded input)"}


def _output(root: Span) -> dict[str, Any] | None:
    conversation = messages(root.attributes, output=True)
    if conversation:
        return {"messages": conversation}
    value = text(root.attributes, OUTPUT_VALUE)
    return {"text": value} if value else None


def _events(spans: list[Span]) -> list[dict[str, Any]]:
    """Tool spans become a call and its result; unexecuted intents become calls.

    A tool span records both the request and what came back, so it produces two
    events -- which is what lets an expectation assert on the arguments and a
    fixture replay the result.

    The same call is usually recorded twice: the LLM span declares the intent in
    `message.tool_calls`, and a sibling tool span records the execution. Emitting
    both would double every tool call, so an intent that a tool span accounts for
    is dropped, one for one. An intent with no matching span is kept -- a tool
    the agent asked for and never ran is a real observation, and often the
    interesting one.
    """
    executed = Counter(
        _call_key(_safe_tool_name(name), _tool_arguments(span))
        for span in spans
        if (name := _tool_name(span)) is not None
    )

    events: list[dict[str, Any]] = []
    for span in spans:
        name = _tool_name(span)
        if name is not None:
            events.extend(_tool_span_events(span, _safe_tool_name(name), len(events)))
            continue

        for index, (tool, arguments) in enumerate(tool_calls(span.attributes)):
            key = _call_key(_safe_tool_name(tool), arguments)
            if executed.get(key, 0) > 0:
                executed[key] -= 1
                continue
            events.append(
                {
                    "event_id": f"{span.span_id or len(events)}-tool-{index}",
                    "type": "tool_call",
                    "tool": _safe_tool_name(tool),
                    "arguments": arguments,
                    "timestamp": _timestamp(span.start_nanos),
                }
            )
    return events


def _call_key(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}:{json.dumps(arguments, sort_keys=True, default=str)}"


def _tool_span_events(span: Span, tool: str, position: int) -> list[dict[str, Any]]:
    call_id = span.span_id or f"call-{position}"
    events: list[dict[str, Any]] = [
        {
            "event_id": f"{span.span_id or position}-call",
            "type": "tool_call",
            "tool": tool,
            "call_id": call_id,
            "arguments": _tool_arguments(span),
            "timestamp": _timestamp(span.start_nanos),
        }
    ]
    result = text(span.attributes, OUTPUT_VALUE)
    if result is not None or span.errored:
        events.append(
            {
                "event_id": f"{span.span_id or position}-result",
                "type": "tool_result",
                "tool": tool,
                "call_id": call_id,
                "result": _maybe_json(result),
                "error": span.status_message if span.errored else None,
                "timestamp": _timestamp(span.end_nanos or span.start_nanos),
            }
        )
    return events


def _tool_name(span: Span) -> str | None:
    """The tool this span invoked, if it is a tool span at all."""
    for key in (TOOL_NAME, GEN_AI_TOOL_NAME):
        value = span.attributes.get(key)
        if isinstance(value, str) and value:
            return value
    if span.kind == KIND_TOOL:
        return span.name or None
    return None


def _tool_arguments(span: Span) -> dict[str, Any]:
    for key in (TOOL_PARAMETERS, GEN_AI_TOOL_ARGUMENTS, INPUT_VALUE):
        arguments = json_object(span.attributes, key)
        if arguments:
            return arguments
    return {}


def _safe_tool_name(name: str) -> str:
    """Coerce a span name into something the trace schema accepts as a tool.

    Instrumentation names tool spans freely -- "Tool: refund order" is common --
    and the schema requires an identifier, so the alternative to normalizing is
    rejecting traces over a cosmetic difference.
    """
    cleaned = "".join(character if character.isalnum() else "_" for character in name.strip())
    cleaned = cleaned.strip("_") or "tool"
    if not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"_{cleaned}"
    return cleaned[:128]


def _outcome(spans: list[Span]) -> dict[str, Any]:
    """An errored span is evidence. Everything else is silence, not success."""
    failed = [span for span in spans if span.errored]
    if not failed:
        return {"status": "unknown"}
    return {
        "status": "error",
        "evaluations": [
            {
                "name": span.name or "span",
                "passed": False,
                "reason": span.status_message or "the span reported an error status",
            }
            for span in failed
        ],
    }


def _metadata(root: Span, spans: list[Span]) -> dict[str, Any]:
    model = text(root.attributes, LLM_MODEL_NAME) or text(root.attributes, GEN_AI_REQUEST_MODEL)
    if model is None:
        for span in spans:
            model = text(span.attributes, LLM_MODEL_NAME) or text(
                span.attributes, GEN_AI_REQUEST_MODEL
            )
            if model:
                break
    started = root.started_at
    return {
        "source": "opentelemetry",
        "agent": text(root.resource, SERVICE_NAME),
        "model": model,
        "recorded_at": started.isoformat() if started else None,
        "extra": {
            "spans": len(spans),
            "root_span": root.name,
            "gen_ai_system": text(root.attributes, GEN_AI_SYSTEM),
        },
    }


def _timestamp(nanos: int) -> str | None:
    if not nanos:
        return None
    return datetime.fromtimestamp(nanos / _NANOSECONDS, tz=UTC).isoformat()


def _maybe_json(value: str | None) -> Any:
    """Tool results are often JSON in a string; keep the structure when so."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value

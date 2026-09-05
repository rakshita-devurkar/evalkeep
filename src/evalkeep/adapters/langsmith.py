"""Reading LangSmith runs exported to JSON.

LangSmith is the one widely-used platform that does not speak OTLP on the way
out: its traces are trees of `Run` objects in its own shape. That is precisely
why it earns an adapter -- the OTLP adapter covers the platforms that ingest
OpenTelemetry, and this covers the biggest one that does not.

**It reads a file, never the API.** LangSmith's bulk export is a paid tier, so an
API-based adapter would lock out everyone below it. A file works from any export
route -- the UI download, a `list_runs` script, or bulk export -- and keeps
credentials out of Evalkeep entirely.

Export runs however you like, as JSONL (one run per line) or a JSON array::

    from langsmith import Client
    with open("runs.jsonl", "w") as handle:
        for run in Client().list_runs(project_name="my-project"):
            handle.write(run.json() + "\\n")

**One LangSmith trace becomes one Evalkeep trace**, assembled from every run
sharing a `trace_id`, so runs are grouped rather than emitted one by one.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from evalkeep.adapters.base import AdapterRecord, IssueKind, TraceIssue
from evalkeep.trace import NormalizedTrace

#: Where a prompt usually lives in a LangChain run's inputs, most specific
#: first. Tried in order rather than guessed at, and documented so a project
#: with a different shape knows what to rename.
INPUT_KEYS = ("input", "question", "query", "prompt", "text", "content")
OUTPUT_KEYS = ("output", "answer", "result", "text", "content", "generations")

TOOL_RUN = "tool"
LLM_RUN = "llm"


@dataclass
class Run:
    """One LangSmith run, decoded far enough to be useful."""

    run_id: str
    trace_id: str
    parent_run_id: str | None
    name: str
    run_type: str
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.error) or (self.status or "").lower() == "error"

    @property
    def is_root(self) -> bool:
        return not self.parent_run_id


class LangSmithAdapter:
    """Reads exported LangSmith runs, as JSONL or a JSON array."""

    name: ClassVar[str] = "langsmith"
    description: ClassVar[str] = "LangSmith runs exported as JSONL or a JSON array"

    def read(self, path: Path) -> Iterator[AdapterRecord]:
        runs: list[Run] = []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            yield AdapterRecord.rejected(
                1,
                TraceIssue(
                    line=1, kind=IssueKind.ENCODING, message=f"could not read {path}: {exc}"
                ),
            )
            return

        for line, payload in _documents(raw):
            if isinstance(payload, Exception):
                yield AdapterRecord.rejected(
                    line,
                    TraceIssue(line=line, kind=IssueKind.JSON, message=f"invalid JSON: {payload}"),
                )
                continue
            run = _run(payload)
            if run is not None:
                runs.append(run)

        yield from self._group(runs)

    def _group(self, runs: list[Run]) -> Iterator[AdapterRecord]:
        grouped: dict[str, list[Run]] = {}
        for run in runs:
            grouped.setdefault(run.trace_id, []).append(run)

        for line, (trace_id, group) in enumerate(grouped.items(), start=1):
            group.sort(key=lambda run: (run.start_time or "", run.run_id))
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


def _documents(raw: str) -> Iterator[tuple[int, Any]]:
    """Each run in the export, whether the file is JSONL or a JSON array."""
    stripped = raw.strip()
    if not stripped:
        return

    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            yield 1, exc
            return
        yield from enumerate(parsed, start=1)
        return

    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            yield line_number, json.loads(line)
        except json.JSONDecodeError as exc:
            yield line_number, exc


def _run(payload: Any) -> Run | None:
    if not isinstance(payload, dict):
        return None
    run_id = payload.get("id") or payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    # A run without a trace_id is its own trace; LangSmith omits it on roots in
    # some export shapes.
    trace_id = payload.get("trace_id") or run_id
    return Run(
        run_id=run_id,
        trace_id=str(trace_id),
        parent_run_id=payload.get("parent_run_id") or None,
        name=str(payload.get("name") or ""),
        run_type=str(payload.get("run_type") or "").lower(),
        inputs=_mapping(payload.get("inputs")),
        outputs=_mapping(payload.get("outputs")),
        error=payload.get("error") or None,
        status=payload.get("status"),
        start_time=payload.get("start_time"),
        end_time=payload.get("end_time"),
        extra=_mapping(payload.get("extra")),
        tags=[str(tag) for tag in payload.get("tags") or []],
        feedback=[f for f in payload.get("feedback") or [] if isinstance(f, dict)],
    )


def _mapping(value: Any) -> dict[str, Any]:
    """A dictionary, or an empty one. Export shapes vary; nothing here raises."""
    return value if isinstance(value, dict) else {}


def _build(trace_id: str, runs: list[Run]) -> NormalizedTrace:
    root = next((run for run in runs if run.is_root), runs[0])
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "input": _input(root, runs),
        "events": _events(runs),
        "outcome": _outcome(runs),
        "metadata": _metadata(root, runs),
    }
    output = _output(root)
    if output is not None:
        payload["output"] = output
    return NormalizedTrace.model_validate(payload)


def _input(root: Run, runs: list[Run]) -> dict[str, Any]:
    for run in [root, *runs]:
        conversation = _messages(run.inputs)
        if conversation:
            return {"messages": conversation}
        value = _first_text(run.inputs, INPUT_KEYS)
        if value:
            return {"text": value}
    return {"text": root.name or "(no recorded input)"}


def _output(root: Run) -> dict[str, Any] | None:
    conversation = _messages(root.outputs)
    if conversation:
        return {"messages": conversation}
    value = _first_text(root.outputs, OUTPUT_KEYS)
    return {"text": value} if value else None


def _messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Role/content pairs from a LangChain messages list, if there is one."""
    raw = payload.get("messages")
    if not isinstance(raw, list):
        return []
    found: list[dict[str, str]] = []
    for item in raw:
        # LangChain nests one level deeper for chat model runs.
        entry = item[0] if isinstance(item, list) and item else item
        if not isinstance(entry, dict):
            continue
        role = entry.get("role") or entry.get("type")
        content = entry.get("content")
        if isinstance(role, str) and isinstance(content, str) and content:
            found.append({"role": _role(role), "content": content})
    return found


def _role(value: str) -> str:
    """LangChain says 'human' and 'ai' where the trace schema says user/assistant."""
    return {"human": "user", "ai": "assistant", "chat": "assistant"}.get(
        value.lower(), value.lower()
    )


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict | list) and value:
            return json.dumps(value, default=str)
    if payload:
        return json.dumps(payload, default=str)
    return None


def _events(runs: list[Run]) -> list[dict[str, Any]]:
    """Tool runs become a call and its result; unexecuted intents become calls.

    As with OpenTelemetry, a call is usually recorded twice -- once as the
    model's declared `tool_calls` and once as the child tool run that executed
    it -- so an intent a tool run accounts for is dropped, one for one.
    """
    executed = Counter(
        _call_key(_safe_tool_name(run.name), run.inputs) for run in runs if run.run_type == TOOL_RUN
    )

    events: list[dict[str, Any]] = []
    for run in runs:
        if run.run_type == TOOL_RUN:
            tool = _safe_tool_name(run.name)
            events.append(
                {
                    "event_id": f"{run.run_id}-call",
                    "type": "tool_call",
                    "tool": tool,
                    "call_id": run.run_id,
                    "arguments": run.inputs,
                    "timestamp": _timestamp(run.start_time),
                }
            )
            events.append(
                {
                    "event_id": f"{run.run_id}-result",
                    "type": "tool_result",
                    "tool": tool,
                    "call_id": run.run_id,
                    "result": run.outputs or None,
                    "error": run.error,
                    "timestamp": _timestamp(run.end_time or run.start_time),
                }
            )
            continue

        for index, (tool, arguments) in enumerate(_declared_tool_calls(run)):
            key = _call_key(_safe_tool_name(tool), arguments)
            if executed.get(key, 0) > 0:
                executed[key] -= 1
                continue
            events.append(
                {
                    "event_id": f"{run.run_id}-tool-{index}",
                    "type": "tool_call",
                    "tool": _safe_tool_name(tool),
                    "arguments": arguments,
                    "timestamp": _timestamp(run.start_time),
                }
            )
    return events


def _declared_tool_calls(run: Run) -> list[tuple[str, dict[str, Any]]]:
    """Tool calls a model asked for, wherever LangChain put them this time."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for message in _iter_messages(run.outputs):
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = call.get("name") or (call.get("function") or {}).get("name")
            arguments = call.get("args")
            if arguments is None:
                arguments = (call.get("function") or {}).get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"arguments": arguments}
            if isinstance(name, str) and name:
                calls.append((name, arguments if isinstance(arguments, dict) else {}))
    return calls


def _iter_messages(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Walk the generations/messages nesting LangChain outputs come in."""
    for generation_list in payload.get("generations") or []:
        entries = generation_list if isinstance(generation_list, list) else [generation_list]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            if isinstance(message, dict):
                yield {**message, **(message.get("kwargs") or {})}
    for entry in payload.get("messages") or []:
        item = entry[0] if isinstance(entry, list) and entry else entry
        if isinstance(item, dict):
            yield item


def _outcome(runs: list[Run]) -> dict[str, Any]:
    """Errors and explicit feedback are evidence; silence is not success."""
    failed = [run for run in runs if run.failed]
    negative = [item for run in runs for item in run.feedback if _is_negative(item)]

    if not failed and not negative:
        return {"status": "unknown"}

    outcome: dict[str, Any] = {"status": "error" if failed else "failure"}
    if failed:
        outcome["evaluations"] = [
            {
                "name": run.name or run.run_type or "run",
                "passed": False,
                "reason": run.error or "the run reported an error status",
            }
            for run in failed
        ]
    if negative:
        comment = next((str(item.get("comment")) for item in negative if item.get("comment")), None)
        outcome["feedback"] = {"rating": "negative", "comment": comment}
    return outcome


def _is_negative(item: dict[str, Any]) -> bool:
    """Only an unambiguous negative counts.

    A score is meaningless without its scale, so only a numeric zero -- which
    LangSmith's thumbs-down records -- or an explicitly negative value is read
    as evidence. Anything else is left for a person to judge.
    """
    score = item.get("score")
    if isinstance(score, bool):
        return score is False
    if isinstance(score, int | float):
        return float(score) <= 0.0
    value = item.get("value")
    return isinstance(value, str) and value.lower() in {"negative", "thumbs_down", "bad"}


def _metadata(root: Run, runs: list[Run]) -> dict[str, Any]:
    metadata = root.extra.get("metadata") if isinstance(root.extra, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    model = metadata.get("ls_model_name") or metadata.get("model")
    if not model:
        for run in runs:
            extra = run.extra.get("metadata") if isinstance(run.extra, dict) else {}
            if isinstance(extra, dict) and extra.get("ls_model_name"):
                model = extra["ls_model_name"]
                break
    return {
        "source": "langsmith",
        "agent": str(metadata.get("ls_project_name") or root.name or "") or None,
        "model": str(model) if model else None,
        "recorded_at": _timestamp(root.start_time),
        "tags": root.tags,
        "extra": {"runs": len(runs), "run_type": root.run_type},
    }


def _safe_tool_name(name: str) -> str:
    """Coerce a run name into something the trace schema accepts as a tool."""
    cleaned = "".join(character if character.isalnum() else "_" for character in name.strip())
    cleaned = cleaned.strip("_") or "tool"
    if not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"_{cleaned}"
    return cleaned[:128]


def _call_key(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}:{json.dumps(arguments, sort_keys=True, default=str)}"


def _timestamp(value: str | None) -> str | None:
    """LangSmith timestamps are ISO 8601, sometimes without a zone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()

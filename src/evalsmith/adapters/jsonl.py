"""The generic JSONL adapter: one JSON object per line, already normalized.

This is the format Evalsmith documents for users who export traces themselves.
Provider-specific adapters (Langfuse, Opik, OpenTelemetry) arrive in 0.2 and
map onto the same contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from evalsmith.adapters.base import AdapterRecord, IssueKind, TraceIssue
from evalsmith.trace import NormalizedTrace

EXTRA_FIELD_HINT = "Unknown fields belong under 'metadata.extra'."


class JsonlAdapter:
    """Reads newline-delimited JSON, one normalized trace per line."""

    name: ClassVar[str] = "jsonl"
    description: ClassVar[str] = "Generic newline-delimited JSON (one trace object per line)"

    def read(self, path: Path) -> Iterator[AdapterRecord]:
        """Stream ``path`` without loading it into memory.

        Opened in binary so that a single undecodable line becomes one rejected
        record instead of aborting the whole file.
        """
        with path.open("rb") as handle:
            yield from self.read_binary_lines(handle)

    def read_binary_lines(self, lines: Iterable[bytes]) -> Iterator[AdapterRecord]:
        for line_number, raw in enumerate(lines, start=1):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                yield AdapterRecord.rejected(
                    line_number,
                    TraceIssue(
                        line=line_number,
                        kind=IssueKind.ENCODING,
                        message=f"line is not valid UTF-8: {exc.reason}",
                    ),
                )
                continue
            record = self._parse(line_number, text)
            if record is not None:
                yield record

    def read_lines(self, lines: Iterable[str]) -> Iterator[AdapterRecord]:
        """Same contract as :meth:`read`, over already-decoded lines."""
        for line_number, text in enumerate(lines, start=1):
            record = self._parse(line_number, text)
            if record is not None:
                yield record

    def _parse(self, line_number: int, text: str) -> AdapterRecord | None:
        """Return a record, or ``None`` for a blank line (not a record at all)."""
        stripped = text.lstrip("\ufeff").strip()
        if not stripped:
            return None

        try:
            payload: Any = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return AdapterRecord.rejected(
                line_number,
                TraceIssue(
                    line=line_number,
                    kind=IssueKind.JSON,
                    message=f"invalid JSON: {exc.msg} at column {exc.colno}",
                ),
            )

        if not isinstance(payload, dict):
            return AdapterRecord.rejected(
                line_number,
                TraceIssue(
                    line=line_number,
                    kind=IssueKind.JSON,
                    message=f"expected a JSON object, got {type(payload).__name__}",
                ),
            )

        try:
            trace = NormalizedTrace.model_validate(payload)
        except ValidationError as exc:
            return AdapterRecord.rejected(
                line_number, *_issues_from_validation_error(line_number, payload, exc)
            )
        return AdapterRecord.valid(line_number, trace)


def _issues_from_validation_error(
    line_number: int, payload: dict[str, Any], error: ValidationError
) -> list[TraceIssue]:
    """One issue per field error, so the error JSONL is directly actionable."""
    trace_id = _best_effort_trace_id(payload)
    issues: list[TraceIssue] = []
    for detail in error.errors():
        location = _format_location(detail["loc"], payload)
        issues.append(
            TraceIssue(
                line=line_number,
                kind=IssueKind.SCHEMA,
                message=detail["msg"],
                trace_id=trace_id,
                field=location or None,
                hint=EXTRA_FIELD_HINT if detail["type"] == "extra_forbidden" else None,
            )
        )
    return issues


def _format_location(location: tuple[int | str, ...], payload: Any) -> str:
    """Render a Pydantic error location as a path into the user's own JSON.

    Pydantic inserts the union tag for a discriminated union, so a bad tool name
    arrives as ``('events', 0, 'tool_call', 'tool')``. There is no ``tool_call``
    key in the document, and telling someone to look at a path that does not
    exist wastes their time. Any non-final segment that cannot be resolved
    against the payload is dropped; the final segment is always kept, since a
    missing required field is exactly the one that will not resolve.
    """
    parts: list[str] = []
    current: Any = payload
    last_index = len(location) - 1
    for index, part in enumerate(location):
        resolved = _descend(current, part)
        if resolved is _UNRESOLVED and index != last_index:
            continue
        parts.append(str(part))
        current = None if resolved is _UNRESOLVED else resolved
    return ".".join(parts)


_UNRESOLVED = object()


def _descend(container: Any, part: int | str) -> Any:
    """One step into ``container``, or ``_UNRESOLVED`` if the step is not there."""
    if isinstance(container, dict) and part in container:
        return container[part]
    if (
        isinstance(container, list)
        and isinstance(part, int)
        and -len(container) <= part < len(container)
    ):
        return container[part]
    return _UNRESOLVED


def _best_effort_trace_id(payload: dict[str, Any]) -> str | None:
    """Name the offending record even when the record failed validation."""
    candidate = payload.get("trace_id")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return None

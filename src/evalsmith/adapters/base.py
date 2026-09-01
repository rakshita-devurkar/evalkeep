"""The adapter contract: every adapter normalizes or fails, never both.

An adapter turns one provider's records into :class:`AdapterRecord` values,
streaming them so that a 100k-trace file never has to fit in memory. A record
either carries a validated trace or carries the issues explaining why it could
not be validated -- an adapter never raises on bad input data. Exceptions are
reserved for the file itself being unusable, which the CLI reports as a command
error.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from evalsmith.trace import NormalizedTrace


class IssueKind(StrEnum):
    """Why a record was rejected. Written verbatim into the error JSONL."""

    ENCODING = "encoding"
    JSON = "json"
    SCHEMA = "schema"
    DUPLICATE_ID = "duplicate_id"


@dataclass(frozen=True)
class TraceIssue:
    """One reason one record is not usable, addressed to whoever must fix it."""

    line: int
    kind: IssueKind
    message: str
    trace_id: str | None = None
    field: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The structured error-JSONL record."""
        record: dict[str, Any] = {
            "line": self.line,
            "kind": self.kind.value,
            "message": self.message,
        }
        if self.trace_id is not None:
            record["trace_id"] = self.trace_id
        if self.field is not None:
            record["field"] = self.field
        if self.hint is not None:
            record["hint"] = self.hint
        return record


@dataclass(frozen=True)
class AdapterRecord:
    """One source record: either a trace, or the issues that rejected it."""

    line: int
    trace: NormalizedTrace | None = None
    issues: tuple[TraceIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.trace is not None

    @classmethod
    def valid(cls, line: int, trace: NormalizedTrace) -> AdapterRecord:
        return cls(line=line, trace=trace)

    @classmethod
    def rejected(cls, line: int, *issues: TraceIssue) -> AdapterRecord:
        if not issues:
            raise ValueError("a rejected record must carry at least one issue")
        return cls(line=line, issues=tuple(issues))


@runtime_checkable
class TraceAdapter(Protocol):
    """Converts a provider format into normalized traces."""

    name: ClassVar[str]
    description: ClassVar[str]

    def read(self, path: Path) -> Iterator[AdapterRecord]:
        """Stream records from ``path``, one per source record."""
        ...

"""The trace store: redacted traces in SQLite, never silently overwritten.

The full redacted trace is stored as JSON on ``traces`` and is the source of
truth. Events are also written as rows, as a derived index so that detection can
ask "which traces called ``refund_order``" without deserializing every trace;
both are written in the same transaction, so the index cannot drift.

Storing a trace ID that already exists is never an overwrite. It is either a
no-op (the stored trace has the same content, so re-ingesting a file is safe) or
a conflict the caller must resolve (same ID, different content).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from evalsmith.errors import CommandError
from evalsmith.hashing import content_hash
from evalsmith.redaction import RedactionSummary
from evalsmith.storage.failures import FailureStore
from evalsmith.storage.migrations import apply_migrations
from evalsmith.trace import NormalizedTrace, ToolCallEvent, ToolResultEvent


class StoreResult(StrEnum):
    """What happened -- or, for a dry run, what would have happened."""

    STORED = "stored"
    #: Same trace_id, same content: already ingested, nothing to do.
    ALREADY_STORED = "already_stored"
    #: Same trace_id, different content: refused, the caller must decide.
    ID_CONFLICT = "id_conflict"
    #: Different trace_id, same content: the interaction is already covered.
    CONTENT_DUPLICATE = "content_duplicate"


@dataclass(frozen=True)
class StoreOutcome:
    result: StoreResult
    trace_id: str
    content_hash: str
    #: For a content duplicate, the trace already holding this content.
    existing_trace_id: str | None = None

    @property
    def written(self) -> bool:
        return self.result is StoreResult.STORED


@dataclass(frozen=True)
class StoredTrace:
    """A trace as it came back out of the database."""

    trace: NormalizedTrace
    content_hash: str
    ingested_at: str
    redactions: int
    redaction_summary: dict[str, int]


@dataclass(frozen=True)
class TraceSummary:
    """One row of ``evalsmith trace list``."""

    trace_id: str
    status: str
    source: str | None
    recorded_at: str | None
    events: int
    redactions: int


class TraceStore:
    """Read/write access to one project's trace database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    @contextmanager
    def open(cls, path: Path) -> Iterator[TraceStore]:
        """Open (creating and migrating as needed) and always close cleanly."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path)
        except (OSError, sqlite3.Error) as exc:
            raise CommandError(f"Could not open the database {path}: {exc}") from exc

        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            apply_migrations(connection)
            yield cls(connection)
        finally:
            connection.close()

    @property
    def failures(self) -> FailureStore:
        """Failure candidates, sharing this store's connection."""
        return FailureStore(self._connection)

    # -- writing ---------------------------------------------------------

    def add(
        self, trace: NormalizedTrace, *, redaction: RedactionSummary | None = None
    ) -> StoreOutcome:
        """Store a redacted trace, or explain why it was not stored."""
        outcome = self.classify(trace)
        if not outcome.written:
            return outcome

        summary = redaction or RedactionSummary()
        payload = trace.model_dump(mode="json")
        now = datetime.now(UTC).isoformat()
        try:
            with self._connection:  # one transaction: trace and its events
                self._connection.execute(
                    """
                    INSERT INTO traces (
                        trace_id, content_hash, schema_version, status, source,
                        recorded_at, ingested_at, redactions, redaction_summary, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace.trace_id,
                        outcome.content_hash,
                        trace.schema_version,
                        trace.outcome.status.value,
                        trace.metadata.source,
                        _isoformat(trace.metadata.recorded_at),
                        now,
                        summary.total,
                        json.dumps(summary.to_dict(), sort_keys=True),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                self._connection.executemany(
                    """
                    INSERT INTO events (
                        trace_id, position, event_id, type, tool, call_id, timestamp, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    list(_event_rows(trace, payload)),
                )
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise CommandError(f"Could not store trace {trace.trace_id!r}: {exc}") from exc
        return outcome

    def classify(self, trace: NormalizedTrace) -> StoreOutcome:
        """Decide what storing ``trace`` would do, without writing anything."""
        digest = content_hash(trace)

        existing = self._connection.execute(
            "SELECT content_hash FROM traces WHERE trace_id = ?", (trace.trace_id,)
        ).fetchone()
        if existing is not None:
            result = (
                StoreResult.ALREADY_STORED
                if existing["content_hash"] == digest
                else StoreResult.ID_CONFLICT
            )
            return StoreOutcome(result, trace.trace_id, digest)

        same_content = self._connection.execute(
            "SELECT trace_id FROM traces WHERE content_hash = ? ORDER BY trace_id LIMIT 1",
            (digest,),
        ).fetchone()
        if same_content is not None:
            return StoreOutcome(
                StoreResult.CONTENT_DUPLICATE,
                trace.trace_id,
                digest,
                existing_trace_id=same_content["trace_id"],
            )

        return StoreOutcome(StoreResult.STORED, trace.trace_id, digest)

    # -- reading ---------------------------------------------------------

    def get(self, trace_id: str) -> StoredTrace | None:
        row = self._connection.execute(
            "SELECT * FROM traces WHERE trace_id = ?", (trace_id.strip(),)
        ).fetchone()
        if row is None:
            return None
        return StoredTrace(
            trace=NormalizedTrace.model_validate_json(row["payload"]),
            content_hash=row["content_hash"],
            ingested_at=row["ingested_at"],
            redactions=row["redactions"],
            redaction_summary=json.loads(row["redaction_summary"]),
        )

    def list(
        self, *, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[TraceSummary]:
        query = """
            SELECT t.trace_id, t.status, t.source, t.recorded_at, t.redactions,
                   (SELECT COUNT(*) FROM events e WHERE e.trace_id = t.trace_id) AS events
            FROM traces t
        """
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE t.status = ?"
            parameters.append(status)
        query += " ORDER BY t.ingested_at, t.trace_id LIMIT ? OFFSET ?"
        parameters += [limit, offset]

        return [
            TraceSummary(
                trace_id=row["trace_id"],
                status=row["status"],
                source=row["source"],
                recorded_at=row["recorded_at"],
                events=row["events"],
                redactions=row["redactions"],
            )
            for row in self._connection.execute(query, parameters)
        ]

    def iter_traces(self) -> Iterator[NormalizedTrace]:
        """Stream every stored trace in ingest order, without loading them all."""
        cursor = self._connection.execute(
            "SELECT payload FROM traces ORDER BY ingested_at, trace_id"
        )
        for row in cursor:
            yield NormalizedTrace.model_validate_json(row["payload"])

    def count(self, *, status: str | None = None) -> int:
        if status is None:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM traces").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM traces WHERE status = ?", (status,)
            ).fetchone()
        return int(row["n"])

    def event_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])


def _event_rows(trace: NormalizedTrace, payload: dict[str, Any]) -> Iterator[tuple[Any, ...]]:
    """Flatten events into index rows, keeping their recorded order."""
    for position, (event, dumped) in enumerate(zip(trace.events, payload["events"], strict=True)):
        tool = event.tool if isinstance(event, ToolCallEvent | ToolResultEvent) else None
        call_id = event.call_id if isinstance(event, ToolCallEvent | ToolResultEvent) else None
        yield (
            trace.trace_id,
            position,
            event.event_id,
            event.type,
            tool,
            call_id,
            _isoformat(event.timestamp),
            json.dumps(dumped, sort_keys=True),
        )


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None

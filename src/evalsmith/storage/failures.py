"""Persistence for failure candidates and their signals.

Shares the trace store's connection, so a failure and its signals are always
written in the same transaction as each other and under the same foreign keys
as the trace they describe.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evalsmith.detectors import Signal
from evalsmith.failures import Failure, FailureOrigin, FailureStatus


@dataclass(frozen=True)
class FailureSummary:
    """One row of ``evalsmith failures list``."""

    failure_id: str
    trace_id: str
    status: FailureStatus
    origin: FailureOrigin
    signals: int
    kinds: list[str]
    reviewer: str | None


class FailureStore:
    """Read/write access to the failure tables."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, failure: Failure) -> None:
        """Write a failure and replace its signals, in one transaction."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO failures (
                    failure_id, trace_id, status, origin, detected_at,
                    updated_at, reviewer, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(failure_id) DO UPDATE SET
                    status = excluded.status,
                    origin = excluded.origin,
                    updated_at = excluded.updated_at,
                    reviewer = excluded.reviewer,
                    reason = excluded.reason
                """,
                (
                    failure.failure_id,
                    failure.trace_id,
                    failure.status.value,
                    failure.origin.value,
                    failure.detected_at.isoformat(),
                    failure.updated_at.isoformat(),
                    failure.reviewer,
                    failure.reason,
                ),
            )
            self._connection.execute(
                "DELETE FROM failure_signals WHERE failure_id = ?", (failure.failure_id,)
            )
            self._connection.executemany(
                """
                INSERT INTO failure_signals (
                    failure_id, position, detector, kind, source, summary, evidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        failure.failure_id,
                        position,
                        signal.detector,
                        signal.kind.value,
                        signal.source,
                        signal.summary,
                        json.dumps(signal.evidence, sort_keys=True),
                    )
                    for position, signal in enumerate(failure.signals)
                ],
            )

    def delete(self, failure_id: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM failures WHERE failure_id = ?", (failure_id,))

    def get(self, failure_id: str) -> Failure | None:
        row = self._connection.execute(
            "SELECT * FROM failures WHERE failure_id = ?", (failure_id.strip(),)
        ).fetchone()
        return self._build(row) if row is not None else None

    def get_by_trace(self, trace_id: str) -> Failure | None:
        row = self._connection.execute(
            "SELECT * FROM failures WHERE trace_id = ?", (trace_id.strip(),)
        ).fetchone()
        return self._build(row) if row is not None else None

    def list(
        self, *, status: FailureStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[FailureSummary]:
        query = "SELECT * FROM failures"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status.value)
        query += " ORDER BY detected_at, failure_id LIMIT ? OFFSET ?"
        parameters += [limit, offset]

        summaries: list[FailureSummary] = []
        for row in self._connection.execute(query, parameters).fetchall():
            signals = _load_signals(self._connection, row["failure_id"])
            kinds: dict[str, None] = {}
            for signal in signals:
                kinds.setdefault(signal.kind.value, None)
            summaries.append(
                FailureSummary(
                    failure_id=row["failure_id"],
                    trace_id=row["trace_id"],
                    status=FailureStatus(row["status"]),
                    origin=FailureOrigin(row["origin"]),
                    signals=len(signals),
                    kinds=list(kinds),
                    reviewer=row["reviewer"],
                )
            )
        return summaries

    def count(self, *, status: FailureStatus | None = None) -> int:
        if status is None:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM failures").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM failures WHERE status = ?", (status.value,)
            ).fetchone()
        return int(row["n"])

    def counts_by_status(self) -> dict[FailureStatus, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS n FROM failures GROUP BY status"
        ).fetchall()
        return {FailureStatus(row["status"]): int(row["n"]) for row in rows}

    def iter_all(self) -> Iterator[Failure]:
        for row in self._connection.execute("SELECT * FROM failures ORDER BY failure_id"):
            yield self._build(row)

    def _build(self, row: sqlite3.Row) -> Failure:
        return Failure(
            failure_id=row["failure_id"],
            trace_id=row["trace_id"],
            status=FailureStatus(row["status"]),
            origin=FailureOrigin(row["origin"]),
            signals=_load_signals(self._connection, row["failure_id"]),
            detected_at=datetime.fromisoformat(row["detected_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            reviewer=row["reviewer"],
            reason=row["reason"],
        )


def _load_signals(connection: sqlite3.Connection, failure_id: str) -> list[Signal]:
    rows = connection.execute(
        "SELECT * FROM failure_signals WHERE failure_id = ? ORDER BY position",
        (failure_id,),
    ).fetchall()
    return [
        Signal.from_dict(
            {
                "detector": row["detector"],
                "kind": row["kind"],
                "source": row["source"],
                "summary": row["summary"],
                "evidence": json.loads(row["evidence"]),
            }
        )
        for row in rows
    ]

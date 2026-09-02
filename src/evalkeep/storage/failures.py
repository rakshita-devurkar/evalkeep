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

from evalkeep.analysis import FailureAnalysis
from evalkeep.detectors import Signal
from evalkeep.failures import Failure, FailureOrigin, FailureStatus


@dataclass(frozen=True)
class FailureSummary:
    """One row of ``evalkeep failures list``."""

    failure_id: str
    trace_id: str
    status: FailureStatus
    origin: FailureOrigin
    signals: int
    kinds: list[str]
    reviewer: str | None
    failure_type: str | None = None
    severity: str | None = None


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

    def save_analysis(self, failure_id: str, analysis: FailureAnalysis) -> None:
        """Store the latest analysis for a failure, replacing any previous one."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO failure_analyses (
                    failure_id, failure_type, component, severity, summary,
                    analyzer, prompt_version, analyzed_at, labeler, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(failure_id) DO UPDATE SET
                    failure_type = excluded.failure_type,
                    component = excluded.component,
                    severity = excluded.severity,
                    summary = excluded.summary,
                    analyzer = excluded.analyzer,
                    prompt_version = excluded.prompt_version,
                    analyzed_at = excluded.analyzed_at,
                    labeler = excluded.labeler,
                    raw_response = excluded.raw_response
                """,
                (
                    failure_id,
                    analysis.failure_type.value,
                    analysis.component.value,
                    analysis.severity.value,
                    analysis.summary,
                    analysis.analyzer,
                    analysis.prompt_version,
                    analysis.analyzed_at.isoformat(),
                    analysis.labeler,
                    analysis.raw_response,
                ),
            )

    def get_analysis(self, failure_id: str) -> FailureAnalysis | None:
        row = self._connection.execute(
            "SELECT * FROM failure_analyses WHERE failure_id = ?", (failure_id,)
        ).fetchone()
        if row is None:
            return None
        return FailureAnalysis.from_dict(
            {
                "failure_type": row["failure_type"],
                "component": row["component"],
                "severity": row["severity"],
                "summary": row["summary"],
                "analyzer": row["analyzer"],
                "prompt_version": row["prompt_version"],
                "analyzed_at": row["analyzed_at"],
                "labeler": row["labeler"],
                "raw_response": row["raw_response"],
            }
        )

    def counts_by_type(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT failure_type, COUNT(*) AS n FROM failure_analyses GROUP BY failure_type"
        ).fetchall()
        return {row["failure_type"]: int(row["n"]) for row in rows}

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
        query = """
            SELECT f.*, a.failure_type AS analysis_type, a.severity AS analysis_severity
            FROM failures f
            LEFT JOIN failure_analyses a ON a.failure_id = f.failure_id
        """
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE f.status = ?"
            parameters.append(status.value)
        query += " ORDER BY f.detected_at, f.failure_id LIMIT ? OFFSET ?"
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
                    failure_type=row["analysis_type"],
                    severity=row["analysis_severity"],
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

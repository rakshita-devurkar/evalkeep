"""Persistence for evaluation runs and their results."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from evalkeep.runs import (
    BaselinePromotion,
    CaseResult,
    ErrorKind,
    EvaluationRun,
    Outcome,
    RunStatus,
)


class AmbiguousRun(Exception):
    """A run prefix matched more than one run."""


class RunStore:
    """Read/write access to the run tables."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, run: EvaluationRun, results: list[CaseResult]) -> None:
        """Write a run and its results together, so neither exists alone."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO evaluation_runs (
                    run_id, target_id, suite_hash, tests, status, runner,
                    environment, started_at, finished_at, output_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    tests = excluded.tests,
                    finished_at = excluded.finished_at,
                    output_dir = excluded.output_dir
                """,
                (
                    run.run_id,
                    run.target_id,
                    run.suite_hash,
                    run.tests,
                    run.status.value,
                    run.runner,
                    json.dumps(run.environment, sort_keys=True),
                    run.started_at.isoformat(),
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.output_dir,
                ),
            )
            self._connection.execute("DELETE FROM test_results WHERE run_id = ?", (run.run_id,))
            self._connection.executemany(
                """
                INSERT INTO test_results (
                    run_id, test_id, outcome, error_kind, error, latency_ms,
                    observation, failed_assertions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run.run_id,
                        result.test_id,
                        result.outcome.value,
                        result.error_kind.value if result.error_kind else None,
                        result.error,
                        result.latency_ms,
                        result.observation,
                        json.dumps(result.failed_assertions),
                    )
                    for result in results
                ],
            )

    def get(self, run_id: str) -> EvaluationRun | None:
        row = self._connection.execute(
            "SELECT * FROM evaluation_runs WHERE run_id = ?", (run_id.strip(),)
        ).fetchone()
        return _build_run(row) if row is not None else None

    def resolve(self, identifier: str) -> EvaluationRun | None:
        """Find a run by its full ID or an unambiguous prefix.

        Run IDs are 32 hex characters, which nobody types. Listings show a
        prefix, so a prefix has to be usable -- an identifier a tool prints and
        will not accept back is a bug, not a nicety.
        """
        cleaned = identifier.strip()
        if not cleaned:
            return None
        exact = self.get(cleaned)
        if exact is not None:
            return exact

        rows = self._connection.execute(
            "SELECT * FROM evaluation_runs WHERE run_id LIKE ? || '%' ORDER BY run_id",
            (cleaned,),
        ).fetchall()
        if len(rows) > 1:
            matches = ", ".join(row["run_id"][:12] for row in rows)
            raise AmbiguousRun(f"{cleaned!r} matches several runs: {matches}.")
        return _build_run(rows[0]) if rows else None

    def latest(self, target_id: str) -> EvaluationRun | None:
        row = self._connection.execute(
            """
            SELECT * FROM evaluation_runs WHERE target_id = ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (target_id.strip(),),
        ).fetchone()
        return _build_run(row) if row is not None else None

    def recent(self, *, limit: int = 20) -> list[EvaluationRun]:
        return [
            _build_run(row)
            for row in self._connection.execute(
                "SELECT * FROM evaluation_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        ]

    def results(self, run_id: str) -> list[CaseResult]:
        return [
            _build_result(row)
            for row in self._connection.execute(
                "SELECT * FROM test_results WHERE run_id = ? ORDER BY test_id",
                (run_id,),
            )
        ]

    def promote(self, promotion: BaselinePromotion) -> None:
        """Record that a run is now the baseline. Never inferred, always decided."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO baseline_promotions (
                    promotion_id, run_id, target_id, promoted_at, reviewer, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    promotion.promotion_id,
                    promotion.run_id,
                    promotion.target_id,
                    promotion.promoted_at.isoformat(),
                    promotion.reviewer,
                    promotion.reason,
                ),
            )

    def current_baseline(self) -> BaselinePromotion | None:
        row = self._connection.execute(
            "SELECT * FROM baseline_promotions ORDER BY promoted_at DESC LIMIT 1"
        ).fetchone()
        return _build_promotion(row) if row is not None else None

    def promotions(self, *, limit: int = 20) -> list[BaselinePromotion]:
        return [
            _build_promotion(row)
            for row in self._connection.execute(
                "SELECT * FROM baseline_promotions ORDER BY promoted_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def counts(self, run_id: str) -> dict[Outcome, int]:
        rows = self._connection.execute(
            "SELECT outcome, COUNT(*) AS n FROM test_results WHERE run_id = ? GROUP BY outcome",
            (run_id,),
        ).fetchall()
        return {Outcome(row["outcome"]): int(row["n"]) for row in rows}


def _build_promotion(row: sqlite3.Row) -> BaselinePromotion:
    return BaselinePromotion(
        promotion_id=row["promotion_id"],
        run_id=row["run_id"],
        target_id=row["target_id"],
        reviewer=row["reviewer"],
        reason=row["reason"],
        promoted_at=datetime.fromisoformat(row["promoted_at"]),
    )


def _build_run(row: sqlite3.Row) -> EvaluationRun:
    return EvaluationRun(
        run_id=row["run_id"],
        target_id=row["target_id"],
        suite_hash=row["suite_hash"],
        tests=row["tests"],
        status=RunStatus(row["status"]),
        runner=row["runner"],
        environment=json.loads(row["environment"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=(datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None),
        output_dir=row["output_dir"],
    )


def _build_result(row: sqlite3.Row) -> CaseResult:
    return CaseResult(
        test_id=row["test_id"],
        outcome=Outcome(row["outcome"]),
        error_kind=ErrorKind(row["error_kind"]) if row["error_kind"] else None,
        error=row["error"],
        latency_ms=row["latency_ms"],
        observation=row["observation"],
        failed_assertions=list(json.loads(row["failed_assertions"])),
    )

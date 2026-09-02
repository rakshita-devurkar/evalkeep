"""Persistence for regression-test drafts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime

from evalkeep.regression import (
    CaseInput,
    Expectation,
    Fixture,
    Provenance,
    RegressionTest,
    ReviewStatus,
)


class RegressionStore:
    """Read/write access to the regression-test table."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, test: RegressionTest) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO regression_tests (
                    test_id, failure_id, cluster_id, status, input, fixtures,
                    expectations, warnings, provenance, reviewer, review_reason,
                    reviewed_at, edited, edited_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(test_id) DO UPDATE SET
                    cluster_id = excluded.cluster_id,
                    status = excluded.status,
                    input = excluded.input,
                    fixtures = excluded.fixtures,
                    expectations = excluded.expectations,
                    warnings = excluded.warnings,
                    provenance = excluded.provenance,
                    reviewer = excluded.reviewer,
                    review_reason = excluded.review_reason,
                    reviewed_at = excluded.reviewed_at,
                    edited = excluded.edited,
                    edited_by = excluded.edited_by,
                    updated_at = excluded.updated_at
                """,
                (
                    test.test_id,
                    test.failure_id,
                    test.provenance.cluster_id,
                    test.status.value,
                    json.dumps(test.input.to_dict(), sort_keys=True),
                    json.dumps([f.to_dict() for f in test.fixtures], sort_keys=True),
                    json.dumps([e.to_dict() for e in test.expectations], sort_keys=True),
                    json.dumps(test.warnings),
                    json.dumps(test.provenance.to_dict(), sort_keys=True),
                    test.reviewer,
                    test.review_reason,
                    test.reviewed_at.isoformat() if test.reviewed_at else None,
                    int(test.edited),
                    test.edited_by,
                    test.created_at.isoformat(),
                    test.updated_at.isoformat(),
                ),
            )

    def get(self, test_id: str) -> RegressionTest | None:
        row = self._connection.execute(
            "SELECT * FROM regression_tests WHERE test_id = ?", (test_id.strip(),)
        ).fetchone()
        return _build(row) if row is not None else None

    def get_by_failure(self, failure_id: str) -> RegressionTest | None:
        row = self._connection.execute(
            "SELECT * FROM regression_tests WHERE failure_id = ?", (failure_id.strip(),)
        ).fetchone()
        return _build(row) if row is not None else None

    def list(
        self, *, status: ReviewStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[RegressionTest]:
        query = "SELECT * FROM regression_tests"
        parameters: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status.value)
        query += " ORDER BY created_at, test_id LIMIT ? OFFSET ?"
        parameters += [limit, offset]
        return [_build(row) for row in self._connection.execute(query, parameters)]

    def iter_all(self) -> Iterator[RegressionTest]:
        for row in self._connection.execute("SELECT * FROM regression_tests ORDER BY test_id"):
            yield _build(row)

    def count(self, *, status: ReviewStatus | None = None) -> int:
        if status is None:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM regression_tests").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM regression_tests WHERE status = ?",
                (status.value,),
            ).fetchone()
        return int(row["n"])

    def counts_by_status(self) -> dict[ReviewStatus, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS n FROM regression_tests GROUP BY status"
        ).fetchall()
        return {ReviewStatus(row["status"]): int(row["n"]) for row in rows}

    def delete(self, test_id: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM regression_tests WHERE test_id = ?", (test_id,))


def _build(row: sqlite3.Row) -> RegressionTest:
    return RegressionTest(
        test_id=row["test_id"],
        failure_id=row["failure_id"],
        status=ReviewStatus(row["status"]),
        input=CaseInput.from_dict(json.loads(row["input"])),
        fixtures=[Fixture.from_dict(item) for item in json.loads(row["fixtures"])],
        expectations=[Expectation.from_dict(item) for item in json.loads(row["expectations"])],
        warnings=list(json.loads(row["warnings"])),
        provenance=Provenance.from_dict(json.loads(row["provenance"])),
        reviewer=row["reviewer"],
        review_reason=row["review_reason"],
        reviewed_at=(datetime.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None),
        edited=bool(row["edited"]),
        edited_by=row["edited_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )

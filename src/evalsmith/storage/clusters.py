"""Persistence for clusterings, clusters and their members."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from evalsmith.clusters import Cluster, ClusteringRun, ClusterMember, MemberRole


class ClusterStore:
    """Read/write access to the clustering tables.

    Only one clustering is current at a time: ``discover`` writes a new run and
    the previous one is replaced. Keeping several would mean every downstream
    command had to ask which grouping it meant.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def replace_run(self, run: ClusteringRun, clusters: list[Cluster]) -> None:
        """Install a clustering, atomically, discarding any previous one."""
        with self._connection:
            self._connection.execute("DELETE FROM clustering_runs")
            self._connection.execute(
                """
                INSERT INTO clustering_runs (
                    run_id, created_at, embedder, dimensions, parameters, failures
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.created_at.isoformat(),
                    run.embedder,
                    run.dimensions,
                    json.dumps(run.parameters, sort_keys=True),
                    run.failures,
                ),
            )
            for cluster in clusters:
                self._insert_cluster(cluster, run.run_id)

    def current_run(self) -> ClusteringRun | None:
        row = self._connection.execute(
            "SELECT * FROM clustering_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return ClusteringRun(
            run_id=row["run_id"],
            embedder=row["embedder"],
            dimensions=row["dimensions"],
            parameters=json.loads(row["parameters"]),
            failures=row["failures"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def save(self, cluster: Cluster) -> None:
        """Update one cluster in place, replacing its membership."""
        run = self.current_run()
        if run is None:  # pragma: no cover - callers check first
            raise ValueError("no clustering to update")
        with self._connection:
            self._connection.execute(
                "DELETE FROM clusters WHERE cluster_id = ?", (cluster.cluster_id,)
            )
            self._insert_cluster(cluster, run.run_id)

    def delete(self, cluster_id: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM clusters WHERE cluster_id = ?", (cluster_id,))

    def get(self, cluster_id: str) -> Cluster | None:
        row = self._connection.execute(
            "SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id.strip(),)
        ).fetchone()
        return self._build(row) if row is not None else None

    def list(self, *, include_dismissed: bool = True) -> list[Cluster]:
        query = "SELECT * FROM clusters"
        if not include_dismissed:
            query += " WHERE dismissed = 0"
        rows = self._connection.execute(query).fetchall()
        clusters = [self._build(row) for row in rows]
        clusters.sort(key=lambda cluster: (-cluster.size, cluster.cluster_id))
        return clusters

    def count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) AS n FROM clusters").fetchone()["n"])

    def find_by_failure(self, failure_id: str) -> Cluster | None:
        row = self._connection.execute(
            """
            SELECT c.* FROM clusters c
            JOIN cluster_members m ON m.cluster_id = c.cluster_id
            WHERE m.failure_id = ?
            """,
            (failure_id,),
        ).fetchone()
        return self._build(row) if row is not None else None

    def _insert_cluster(self, cluster: Cluster, run_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO clusters (
                cluster_id, run_id, label, labelled_by, dismissed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cluster.cluster_id,
                run_id,
                cluster.label,
                cluster.labelled_by,
                int(cluster.dismissed),
                cluster.created_at.isoformat(),
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO cluster_members (cluster_id, failure_id, distance, roles)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    cluster.cluster_id,
                    member.failure_id,
                    member.distance,
                    json.dumps([role.value for role in member.roles]),
                )
                for member in cluster.members
            ],
        )

    def _build(self, row: sqlite3.Row) -> Cluster:
        return Cluster(
            cluster_id=row["cluster_id"],
            label=row["label"],
            members=_load_members(self._connection, row["cluster_id"]),
            labelled_by=row["labelled_by"],
            dismissed=bool(row["dismissed"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


def _load_members(connection: sqlite3.Connection, cluster_id: str) -> list[ClusterMember]:
    rows = connection.execute(
        "SELECT * FROM cluster_members WHERE cluster_id = ? ORDER BY distance, failure_id",
        (cluster_id,),
    ).fetchall()
    members: list[ClusterMember] = []
    for row in rows:
        raw: Any = json.loads(row["roles"])
        members.append(
            ClusterMember(
                failure_id=row["failure_id"],
                distance=row["distance"],
                roles=[MemberRole(value) for value in raw],
            )
        )
    return members

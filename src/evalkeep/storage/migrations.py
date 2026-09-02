"""SQLite migrations, applied in order and recorded in ``schema_migrations``.

Each migration runs inside one transaction together with the row that records
it, so a database is never left half-migrated. Migrations are append-only: to
change the schema, add a new one rather than editing an applied one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from evalkeep.errors import CommandError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="traces and events",
        statements=(
            """
            CREATE TABLE traces (
                trace_id       TEXT PRIMARY KEY,
                content_hash   TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                status         TEXT NOT NULL,
                source         TEXT,
                recorded_at    TEXT,
                ingested_at    TEXT NOT NULL,
                redactions     INTEGER NOT NULL DEFAULT 0,
                redaction_summary TEXT NOT NULL DEFAULT '{}',
                payload        TEXT NOT NULL
            )
            """,
            "CREATE INDEX traces_content_hash ON traces(content_hash)",
            "CREATE INDEX traces_status ON traces(status)",
            """
            CREATE TABLE events (
                trace_id  TEXT NOT NULL
                          REFERENCES traces(trace_id) ON DELETE CASCADE,
                position  INTEGER NOT NULL,
                event_id  TEXT NOT NULL,
                type      TEXT NOT NULL,
                tool      TEXT,
                call_id   TEXT,
                timestamp TEXT,
                payload   TEXT NOT NULL,
                PRIMARY KEY (trace_id, position)
            )
            """,
            "CREATE INDEX events_tool ON events(tool)",
        ),
    ),
    Migration(
        version=2,
        name="failure candidates and signals",
        statements=(
            # UNIQUE on trace_id is the schema-level form of "one failure
            # candidate per trace"; detection cannot create a second one.
            """
            CREATE TABLE failures (
                failure_id  TEXT PRIMARY KEY,
                trace_id    TEXT NOT NULL UNIQUE
                            REFERENCES traces(trace_id) ON DELETE CASCADE,
                status      TEXT NOT NULL,
                origin      TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                reviewer    TEXT,
                reason      TEXT
            )
            """,
            "CREATE INDEX failures_status ON failures(status)",
            """
            CREATE TABLE failure_signals (
                failure_id TEXT NOT NULL
                           REFERENCES failures(failure_id) ON DELETE CASCADE,
                position   INTEGER NOT NULL,
                detector   TEXT NOT NULL,
                kind       TEXT NOT NULL,
                source     TEXT NOT NULL,
                summary    TEXT NOT NULL,
                evidence   TEXT NOT NULL,
                PRIMARY KEY (failure_id, position)
            )
            """,
            "CREATE INDEX failure_signals_kind ON failure_signals(kind)",
        ),
    ),
    Migration(
        version=3,
        name="failure analysis",
        statements=(
            # One analysis per failure: the latest replaces the previous one,
            # and the provenance columns say who produced it.
            """
            CREATE TABLE failure_analyses (
                failure_id     TEXT PRIMARY KEY
                               REFERENCES failures(failure_id) ON DELETE CASCADE,
                failure_type   TEXT NOT NULL,
                component      TEXT NOT NULL,
                severity       TEXT NOT NULL,
                summary        TEXT NOT NULL,
                analyzer       TEXT NOT NULL,
                prompt_version INTEGER NOT NULL,
                analyzed_at    TEXT NOT NULL,
                labeler        TEXT,
                raw_response   TEXT
            )
            """,
            "CREATE INDEX failure_analyses_type ON failure_analyses(failure_type)",
            "CREATE INDEX failure_analyses_severity ON failure_analyses(severity)",
        ),
    ),
    Migration(
        version=4,
        name="clusters and representatives",
        statements=(
            # One row per `discover`, holding everything needed to reproduce
            # the grouping it produced.
            """
            CREATE TABLE clustering_runs (
                run_id     TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                embedder   TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                parameters TEXT NOT NULL,
                failures   INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE clusters (
                cluster_id  TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL
                            REFERENCES clustering_runs(run_id) ON DELETE CASCADE,
                label       TEXT NOT NULL,
                labelled_by TEXT,
                dismissed   INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            )
            """,
            "CREATE INDEX clusters_run ON clusters(run_id)",
            """
            CREATE TABLE cluster_members (
                cluster_id TEXT NOT NULL
                           REFERENCES clusters(cluster_id) ON DELETE CASCADE,
                failure_id TEXT NOT NULL
                           REFERENCES failures(failure_id) ON DELETE CASCADE,
                distance   REAL NOT NULL,
                roles      TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (cluster_id, failure_id)
            )
            """,
            "CREATE INDEX cluster_members_failure ON cluster_members(failure_id)",
        ),
    ),
    Migration(
        version=5,
        name="regression test drafts",
        statements=(
            # cluster_id is a plain column, not a foreign key: clusters are
            # rebuilt by every `discover`, and a test must outlive the grouping
            # that suggested it.
            """
            CREATE TABLE regression_tests (
                test_id       TEXT PRIMARY KEY,
                failure_id    TEXT NOT NULL UNIQUE
                              REFERENCES failures(failure_id) ON DELETE CASCADE,
                cluster_id    TEXT,
                status        TEXT NOT NULL,
                input         TEXT NOT NULL,
                fixtures      TEXT NOT NULL,
                expectations  TEXT NOT NULL,
                warnings      TEXT NOT NULL,
                provenance    TEXT NOT NULL,
                reviewer      TEXT,
                review_reason TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """,
            "CREATE INDEX regression_tests_status ON regression_tests(status)",
            "CREATE INDEX regression_tests_cluster ON regression_tests(cluster_id)",
        ),
    ),
    Migration(
        version=6,
        name="review audit trail",
        statements=(
            "ALTER TABLE regression_tests ADD COLUMN reviewed_at TEXT",
            "ALTER TABLE regression_tests ADD COLUMN edited INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE regression_tests ADD COLUMN edited_by TEXT",
        ),
    ),
    Migration(
        version=7,
        name="evaluation runs and results",
        statements=(
            """
            CREATE TABLE evaluation_runs (
                run_id      TEXT PRIMARY KEY,
                target_id   TEXT NOT NULL,
                suite_hash  TEXT NOT NULL,
                tests       INTEGER NOT NULL,
                status      TEXT NOT NULL,
                runner      TEXT,
                environment TEXT NOT NULL DEFAULT '{}',
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                output_dir  TEXT
            )
            """,
            "CREATE INDEX evaluation_runs_target ON evaluation_runs(target_id)",
            """
            CREATE TABLE test_results (
                run_id            TEXT NOT NULL
                                  REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
                test_id           TEXT NOT NULL,
                outcome           TEXT NOT NULL,
                error_kind        TEXT,
                error             TEXT,
                latency_ms        INTEGER,
                observation       TEXT,
                failed_assertions TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (run_id, test_id)
            )
            """,
            "CREATE INDEX test_results_outcome ON test_results(outcome)",
        ),
    ),
    Migration(
        version=8,
        name="baseline promotions",
        statements=(
            # An append-only record rather than a flag: which run was the
            # baseline, when, and who decided, is exactly the kind of history a
            # regression argument later depends on.
            """
            CREATE TABLE baseline_promotions (
                promotion_id TEXT PRIMARY KEY,
                run_id       TEXT NOT NULL
                             REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
                target_id    TEXT NOT NULL,
                promoted_at  TEXT NOT NULL,
                reviewer     TEXT NOT NULL,
                reason       TEXT
            )
            """,
            "CREATE INDEX baseline_promotions_time ON baseline_promotions(promoted_at)",
        ),
    ),
)

LATEST_VERSION = max(migration.version for migration in MIGRATIONS)

_SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def applied_version(connection: sqlite3.Connection) -> int:
    """The highest migration recorded in this database, or 0 for a new one."""
    connection.execute(_SCHEMA_MIGRATIONS_TABLE)
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def apply_migrations(connection: sqlite3.Connection) -> list[Migration]:
    """Bring the database up to :data:`LATEST_VERSION`. Returns what it ran."""
    current = applied_version(connection)
    if current > LATEST_VERSION:
        raise CommandError(
            f"The database is at schema version {current}, but this build only "
            f"understands {LATEST_VERSION}.",
            hint="Upgrade evalkeep.",
        )

    applied: list[Migration] = []
    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        with connection:  # one transaction per migration, including its record
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
        applied.append(migration)
    return applied

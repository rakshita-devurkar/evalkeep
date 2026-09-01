"""SQLite migrations, applied in order and recorded in ``schema_migrations``.

Each migration runs inside one transaction together with the row that records
it, so a database is never left half-migrated. Migrations are append-only: to
change the schema, add a new one rather than editing an applied one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from evalsmith.errors import CommandError


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
            hint="Upgrade evalsmith.",
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

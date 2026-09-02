"""``evalkeep trace`` -- inspect stored traces.

Safe by construction: the database only ever holds redacted traces, so there is
no unredacted view to expose here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evalkeep.config import Project
from evalkeep.errors import CommandError
from evalkeep.storage import StoredTrace, TraceStore, TraceSummary


@dataclass(frozen=True)
class TraceListing:
    summaries: list[TraceSummary]
    total: int
    offset: int


def show_trace(trace_id: str, *, project_root: Path = Path()) -> StoredTrace:
    """Load one stored trace, or explain that it is not there."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        stored = store.get(trace_id)
        if stored is None:
            raise CommandError(
                f"No stored trace with ID {trace_id.strip()!r}.",
                hint="Run 'evalkeep trace list' to see what has been ingested.",
            )
        return stored


def list_traces(
    *,
    project_root: Path = Path(),
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> TraceListing:
    """Page through stored traces, newest ingest last."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        return TraceListing(
            summaries=store.list(limit=limit, offset=offset, status=status),
            total=store.count(status=status),
            offset=offset,
        )

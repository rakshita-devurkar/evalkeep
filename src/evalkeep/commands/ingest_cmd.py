"""``evalkeep ingest`` -- validate, redact, deduplicate and store traces."""

from __future__ import annotations

from pathlib import Path

from evalkeep.adapters import DEFAULT_ADAPTER, get_adapter
from evalkeep.config import Project
from evalkeep.errors import CommandError
from evalkeep.ingest import DEFAULT_SAMPLE_LIMIT, IngestReport, ingest_file
from evalkeep.redaction import Redactor
from evalkeep.storage import TraceStore


def ingest_traces(
    path: Path,
    *,
    project_root: Path = Path(),
    adapter_name: str = DEFAULT_ADAPTER,
    validate_only: bool = False,
    dry_run: bool = False,
    error_path: Path | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> IngestReport:
    """Run the ingest pipeline over ``path``."""
    adapter = get_adapter(adapter_name)
    if validate_only and dry_run:
        raise CommandError(
            "--validate-only and --dry-run cannot be combined.",
            hint="--validate-only checks the file alone; --dry-run also checks it "
            "against the stored traces.",
        )

    path = path.expanduser()
    resolved_errors = error_path.expanduser() if error_path is not None else None

    if validate_only:
        return ingest_file(path, adapter, error_path=resolved_errors, sample_limit=sample_limit)

    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        return ingest_file(
            path,
            adapter,
            store=store,
            redactor=Redactor(project.config.redaction),
            dry_run=dry_run,
            error_path=resolved_errors,
            sample_limit=sample_limit,
        )

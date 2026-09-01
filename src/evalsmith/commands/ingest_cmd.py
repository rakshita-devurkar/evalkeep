"""``evalsmith ingest`` -- validate a trace file (and, from guide 8C, store it).

Redaction and storage are the next phase's work. Until they exist this command
refuses to pretend it stored anything: without ``--validate-only`` it fails
loudly rather than exiting zero on a file it never persisted.
"""

from __future__ import annotations

from pathlib import Path

from evalsmith.adapters import DEFAULT_ADAPTER, get_adapter
from evalsmith.errors import CommandError
from evalsmith.ingest import DEFAULT_SAMPLE_LIMIT, ValidationReport, validate_file

STORAGE_NOT_IMPLEMENTED = (
    "Storing traces is not implemented yet; redaction and storage arrive with "
    "the next phase (guide 8C)."
)


def ingest_traces(
    path: Path,
    *,
    adapter_name: str = DEFAULT_ADAPTER,
    validate_only: bool = False,
    error_path: Path | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> ValidationReport:
    """Validate ``path`` and report what was found."""
    adapter = get_adapter(adapter_name)
    if not validate_only:
        raise CommandError(
            STORAGE_NOT_IMPLEMENTED,
            hint="Re-run with --validate-only to check the file without storing it.",
        )
    return validate_file(
        path.expanduser(),
        adapter,
        error_path=error_path.expanduser() if error_path is not None else None,
        sample_limit=sample_limit,
    )

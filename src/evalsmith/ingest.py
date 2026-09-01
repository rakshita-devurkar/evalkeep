"""Streaming validation of a trace file.

The pipeline layers cross-record checks (duplicate trace IDs) on top of an
adapter's per-record parsing, and reports as it goes: issues are written to the
error JSONL the moment they are found, and only a bounded sample is kept for
terminal display. Nothing here holds the file in memory, so the cost of
validating 100k traces is the set of trace IDs seen so far.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from evalsmith.adapters import AdapterRecord, IssueKind, TraceAdapter, TraceIssue
from evalsmith.errors import CommandError, ExitCode

#: Issues kept for terminal display; the rest go to the error JSONL.
DEFAULT_SAMPLE_LIMIT = 20


@dataclass
class ValidationReport:
    """What a validation pass found. Counts are exact; ``sample`` is bounded."""

    path: Path
    adapter: str
    records: int = 0
    valid: int = 0
    invalid: int = 0
    issue_count: int = 0
    duplicate_ids: int = 0
    error_path: Path | None = None
    sample: list[TraceIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.invalid == 0

    @property
    def truncated(self) -> int:
        """Issues found but not kept in the sample."""
        return max(0, self.issue_count - len(self.sample))

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.ok else ExitCode.RECORD_ERRORS


def iter_records(path: Path, adapter: TraceAdapter) -> Iterator[AdapterRecord]:
    """Adapter records plus the duplicate-ID check the adapter cannot make.

    An adapter sees one record at a time and so cannot know that a trace ID has
    already appeared. A duplicate is rejected rather than merged: the guide's
    storage rules forbid silently overwriting a trace ID, and accepting the
    second copy here would decide that question in the wrong place.
    """
    seen: set[str] = set()
    for record in adapter.read(path):
        if record.trace is None:
            yield record
            continue
        trace_id = record.trace.trace_id
        if trace_id in seen:
            yield AdapterRecord.rejected(
                record.line,
                TraceIssue(
                    line=record.line,
                    kind=IssueKind.DUPLICATE_ID,
                    message=f"trace_id {trace_id!r} already appeared earlier in this file",
                    trace_id=trace_id,
                    hint="Trace IDs must be unique; the first occurrence is kept.",
                ),
            )
            continue
        seen.add(trace_id)
        yield record


def validate_file(
    path: Path,
    adapter: TraceAdapter,
    *,
    error_path: Path | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> ValidationReport:
    """Validate every record in ``path``, writing issues to ``error_path``."""
    _check_readable_file(path)
    report = ValidationReport(path=path, adapter=adapter.name, error_path=error_path)

    with ExitStack() as stack:
        errors: TextIO | None = None
        if error_path is not None:
            errors = stack.enter_context(_open_error_file(error_path))

        for record in iter_records(path, adapter):
            report.records += 1
            if record.ok:
                report.valid += 1
                continue

            report.invalid += 1
            for issue in record.issues:
                report.issue_count += 1
                if issue.kind is IssueKind.DUPLICATE_ID:
                    report.duplicate_ids += 1
                if len(report.sample) < sample_limit:
                    report.sample.append(issue)
                if errors is not None:
                    errors.write(json.dumps(issue.to_dict(), sort_keys=True) + "\n")

    return report


def _open_error_file(error_path: Path) -> TextIO:
    try:
        error_path.parent.mkdir(parents=True, exist_ok=True)
        return error_path.open("w", encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"Could not write the error file {error_path}: {exc}") from exc


def _check_readable_file(path: Path) -> None:
    if not path.exists():
        raise CommandError(f"{path} does not exist.")
    if path.is_dir():
        raise CommandError(f"{path} is a directory, not a trace file.")
    if not path.is_file():
        raise CommandError(f"{path} is not a regular file.")

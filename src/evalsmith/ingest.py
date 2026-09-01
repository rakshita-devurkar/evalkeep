"""The ingest pipeline: validate, redact, hash, store -- in that order.

Redaction happens between validation and storage, in memory, so a raw value
never reaches the database. The pipeline streams: issues are written to the
error JSONL as they are found and only a bounded sample is kept for display, so
validating 100k traces costs the set of trace IDs seen so far, not the file.

Three modes share one implementation:

* **validate** (no store) -- parse and check only. Needs no project.
* **dry run** -- redact and ask the store what *would* happen, writing nothing.
* **ingest** -- the same, then commit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from evalsmith.adapters import AdapterRecord, IssueKind, TraceAdapter, TraceIssue
from evalsmith.errors import CommandError, ExitCode
from evalsmith.redaction import RedactionSummary, Redactor
from evalsmith.storage import StoreResult, TraceStore

#: Issues kept for terminal display; the rest go to the error JSONL.
DEFAULT_SAMPLE_LIMIT = 20


class IngestMode(StrEnum):
    VALIDATE = "validate"
    DRY_RUN = "dry-run"
    STORE = "ingest"


@dataclass
class IngestReport:
    """What a pass over a trace file found. Counts are exact; ``sample`` is bounded."""

    path: Path
    adapter: str
    mode: IngestMode = IngestMode.VALIDATE

    # Validation
    records: int = 0
    valid: int = 0
    invalid: int = 0
    issue_count: int = 0
    duplicate_ids: int = 0

    # Storage
    stored: int = 0
    already_stored: int = 0
    content_duplicates: int = 0
    id_conflicts: int = 0

    # Redaction
    redactions: int = 0
    redacted_traces: int = 0
    redaction_summary: RedactionSummary = field(default_factory=RedactionSummary)

    error_path: Path | None = None
    sample: list[TraceIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Records that could not be handled at all -- duplicates are not failures."""
        return self.invalid == 0 and self.id_conflicts == 0

    @property
    def truncated(self) -> int:
        return max(0, self.issue_count - len(self.sample))

    @property
    def skipped(self) -> int:
        """Valid traces the store already knew about."""
        return self.already_stored + self.content_duplicates

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.ok else ExitCode.RECORD_ERRORS


def iter_records(path: Path, adapter: TraceAdapter) -> Iterator[AdapterRecord]:
    """Adapter records plus the duplicate-ID check the adapter cannot make.

    An adapter sees one record at a time and so cannot know that a trace ID has
    already appeared in this file. The second occurrence is rejected rather than
    merged: accepting it would decide, in the wrong place, which copy wins.
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


def ingest_file(
    path: Path,
    adapter: TraceAdapter,
    *,
    store: TraceStore | None = None,
    redactor: Redactor | None = None,
    dry_run: bool = False,
    error_path: Path | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> IngestReport:
    """Run the pipeline over ``path``. Without ``store``, validation only."""
    _check_readable_file(path)
    mode = (
        IngestMode.VALIDATE
        if store is None
        else (IngestMode.DRY_RUN if dry_run else IngestMode.STORE)
    )
    report = IngestReport(path=path, adapter=adapter.name, mode=mode, error_path=error_path)
    redactor = redactor or Redactor()

    with ExitStack() as stack:
        errors: TextIO | None = None
        if error_path is not None:
            errors = stack.enter_context(_open_error_file(error_path))

        def report_issue(issue: TraceIssue) -> None:
            report.issue_count += 1
            if issue.kind is IssueKind.DUPLICATE_ID:
                report.duplicate_ids += 1
            if len(report.sample) < sample_limit:
                report.sample.append(issue)
            if errors is not None:
                errors.write(json.dumps(issue.to_dict(), sort_keys=True) + "\n")

        for record in iter_records(path, adapter):
            report.records += 1
            if record.trace is None:
                report.invalid += 1
                for issue in record.issues:
                    report_issue(issue)
                continue

            report.valid += 1
            if store is None:
                continue

            # Redaction sits here on purpose: between a trace being valid and it
            # touching the database, with no path around it.
            redacted, summary = redactor.redact(record.trace)
            report.redactions += summary.total
            report.redacted_traces += 1 if summary.total else 0
            report.redaction_summary.merge(summary)

            outcome = (
                store.classify(redacted) if dry_run else store.add(redacted, redaction=summary)
            )
            _count_outcome(report, outcome.result)
            if outcome.result is StoreResult.ID_CONFLICT:
                report_issue(
                    TraceIssue(
                        line=record.line,
                        kind=IssueKind.ID_CONFLICT,
                        message=(
                            f"trace_id {redacted.trace_id!r} is already stored with "
                            "different content"
                        ),
                        trace_id=redacted.trace_id,
                        hint="Give the new trace a different ID, or remove the stored one.",
                    )
                )

    return report


def _count_outcome(report: IngestReport, result: StoreResult) -> None:
    match result:
        case StoreResult.STORED:
            report.stored += 1
        case StoreResult.ALREADY_STORED:
            report.already_stored += 1
        case StoreResult.CONTENT_DUPLICATE:
            report.content_duplicates += 1
        case StoreResult.ID_CONFLICT:
            report.id_conflicts += 1


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

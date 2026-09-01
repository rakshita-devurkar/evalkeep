"""The Evalsmith command line.

The CLI coordinates commands and renders output; the business logic lives in
:mod:`evalsmith.commands` so it can be tested without a terminal.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import typer
from rich.console import Console
from rich.table import Table

from evalsmith import __version__
from evalsmith.adapters import DEFAULT_ADAPTER, available_adapters
from evalsmith.commands.detect_cmd import (
    FailureDetail,
    add_failure,
    list_failures,
    review_failure,
    run_detection,
    show_failure,
)
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.commands.init_cmd import Action, initialize_project
from evalsmith.commands.trace_cmd import list_traces, show_trace
from evalsmith.detection import DetectionReport
from evalsmith.errors import EvalsmithError, ExitCode
from evalsmith.failures import FailureStatus
from evalsmith.ingest import DEFAULT_SAMPLE_LIMIT, IngestMode, IngestReport
from evalsmith.storage import StoredTrace

T = TypeVar("T")

app = typer.Typer(
    name="evalsmith",
    help="Turn real AI-agent failures into reviewed regression tests.",
    no_args_is_help=True,
    add_completion=False,
)
trace_app = typer.Typer(name="trace", help="Inspect stored traces.", no_args_is_help=True)
app.add_typer(trace_app)
failures_app = typer.Typer(
    name="failures", help="Inspect and review failure candidates.", no_args_is_help=True
)
app.add_typer(failures_app)

console = Console()
err_console = Console(stderr=True)

ACTION_STYLES: dict[Action, str] = {
    Action.CREATED: "green",
    Action.UPDATED: "green",
    Action.OVERWRITTEN: "yellow",
    Action.EXISTS: "dim",
}

PROJECT_OPTION = typer.Option(Path(), "--project", "-C", metavar="DIR", help="Project directory.")


def _version_callback(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit(ExitCode.OK)


@app.callback()
def cli(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the Evalsmith version and exit.",
    ),
) -> None:
    """Evalsmith turns recorded failures into a trustworthy regression suite."""


@app.command()
def version() -> None:
    """Print the Evalsmith version."""
    console.print(__version__)


@app.command()
def init(
    directory: Path = typer.Argument(Path(), help="Project directory to initialize."),
    name: str | None = typer.Option(
        None, "--name", help="Project name to record in the configuration."
    ),
    force: bool = typer.Option(False, "--force", help="Rewrite an existing configuration file."),
) -> None:
    """Create a safe local project structure. Safe to re-run."""
    report = _run(lambda: initialize_project(directory, project_name=name, force=force))

    table = Table(box=None, pad_edge=False)
    table.add_column("action")
    table.add_column("path")
    table.add_column("", style="dim")
    root = report.project.root
    for step in report.steps:
        try:
            shown = step.path.relative_to(root)
        except ValueError:
            shown = step.path
        table.add_row(
            f"[{ACTION_STYLES[step.action]}]{step.action.value}[/]", str(shown), step.detail
        )
    console.print(table)

    if report.changed:
        name_shown = report.project.config.project_name
        console.print(f"\n[bold green]Initialized[/] {name_shown} in {root}")
    else:
        console.print(f"\n[dim]Already initialized:[/] {root}")
    console.print("Next: [bold]evalsmith ingest traces.jsonl[/]")


@app.command()
def ingest(
    path: Path = typer.Argument(..., help="Trace file to read."),
    validate_only: bool = typer.Option(
        False,
        "--validate-only",
        help="Check the file alone. Needs no project, stores nothing.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Redact and check against stored traces without writing anything.",
    ),
    trace_format: str = typer.Option(
        DEFAULT_ADAPTER,
        "--format",
        "-f",
        help=f"Trace format. One of: {', '.join(sorted(available_adapters()))}.",
    ),
    project: Path = PROJECT_OPTION,
    errors: Path | None = typer.Option(
        None, "--errors", metavar="PATH", help="Write one JSON object per issue to this file."
    ),
    sample_limit: int = typer.Option(
        DEFAULT_SAMPLE_LIMIT, "--show", min=0, help="How many issues to print."
    ),
) -> None:
    """Validate, redact, deduplicate and store traces."""
    report = _run(
        lambda: ingest_traces(
            path,
            project_root=project,
            adapter_name=trace_format,
            validate_only=validate_only,
            dry_run=dry_run,
            error_path=errors,
            sample_limit=sample_limit,
        )
    )
    _render_ingest(report)
    raise typer.Exit(report.exit_code)


@trace_app.command("list")
def trace_list(
    project: Path = PROJECT_OPTION,
    limit: int = typer.Option(50, "--limit", min=1, help="Rows to show."),
    offset: int = typer.Option(0, "--offset", min=0, help="Rows to skip."),
    status: str | None = typer.Option(None, "--status", help="Filter by outcome status."),
) -> None:
    """List stored traces."""
    listing = _run(
        lambda: list_traces(project_root=project, limit=limit, offset=offset, status=status)
    )
    if not listing.summaries:
        console.print("[dim]No stored traces.[/]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("trace_id", style="cyan")
    table.add_column("status")
    table.add_column("events", justify="right")
    table.add_column("redactions", justify="right")
    table.add_column("source", style="dim")
    table.add_column("recorded", style="dim")
    for summary in listing.summaries:
        table.add_row(
            summary.trace_id,
            _status_markup(summary.status),
            str(summary.events),
            str(summary.redactions),
            summary.source or "",
            summary.recorded_at or "",
        )
    console.print(table)

    shown = len(listing.summaries)
    console.print(f"\n[dim]{shown} of {listing.total} traces[/]")


@trace_app.command("show")
def trace_show(
    trace_id: str = typer.Argument(..., help="Trace to inspect."),
    project: Path = PROJECT_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Print the stored trace as JSON."),
) -> None:
    """Inspect one stored trace. Stored traces are always redacted."""
    stored = _run(lambda: show_trace(trace_id, project_root=project))
    if as_json:
        console.print_json(stored.trace.model_dump_json())
        return
    _render_trace(stored)


def _render_trace(stored: StoredTrace) -> None:
    trace = stored.trace
    header = Table(box=None, pad_edge=False, show_header=False)
    header.add_column(style="bold")
    header.add_column()
    header.add_row("trace_id", trace.trace_id)
    header.add_row("status", _status_markup(trace.outcome.status.value))
    header.add_row("content", stored.content_hash)
    header.add_row("ingested", stored.ingested_at)
    if stored.redactions:
        detail = ", ".join(f"{rule} x{count}" for rule, count in stored.redaction_summary.items())
        header.add_row("redacted", f"{stored.redactions} values ({detail})")
    console.print(header)

    if trace.input.text:
        console.print(f"\n[bold]input[/]\n{trace.input.text}")
    for message in trace.input.messages:
        console.print(f"\n[bold]input:{message.role.value}[/]\n{message.content}")
    if trace.output is not None and trace.output.text:
        console.print(f"\n[bold]output[/]\n{trace.output.text}")

    if trace.events:
        console.print("\n[bold]events[/]")
        events = Table(box=None, pad_edge=False)
        events.add_column("#", justify="right", style="dim")
        events.add_column("type")
        events.add_column("detail")
        for position, event in enumerate(trace.events):
            events.add_row(str(position), event.type, _event_detail(event))
        console.print(events)

    if trace.outcome.feedback is not None:
        feedback = trace.outcome.feedback
        console.print(
            f"\n[bold]feedback[/] {feedback.rating or ''} {feedback.comment or ''}".rstrip()
        )
    for evaluation in trace.outcome.evaluations:
        verdict = {True: "[green]pass[/]", False: "[red]fail[/]", None: "[dim]-[/]"}[
            evaluation.passed
        ]
        console.print(
            f"[bold]eval[/] {evaluation.name} {verdict} {evaluation.reason or ''}".rstrip()
        )


def _event_detail(event: object) -> str:
    tool = getattr(event, "tool", None)
    if tool is not None:
        arguments = getattr(event, "arguments", None)
        if arguments is not None:
            return f"{tool}({json.dumps(arguments, sort_keys=True)})"
        result = getattr(event, "result", None)
        return f"{tool} -> {json.dumps(result, sort_keys=True, default=str)}"
    content = getattr(event, "content", None)
    if content is not None:
        return f"{getattr(event, 'role', '')}: {content}"
    return getattr(event, "name", "")


def _status_markup(status: str) -> str:
    styles = {"failure": "red", "success": "green", "error": "yellow"}
    style = styles.get(status)
    return f"[{style}]{status}[/]" if style else f"[dim]{status}[/]"


@app.command()
def detect(project: Path = PROJECT_OPTION) -> None:
    """Create evidence-backed failure candidates."""
    report = _run(lambda: run_detection(project_root=project))
    _render_detection(report)


@failures_app.command("list")
def failures_list(
    project: Path = PROJECT_OPTION,
    status: FailureStatus | None = typer.Option(None, "--status", help="Filter by status."),
    limit: int = typer.Option(50, "--limit", min=1, help="Rows to show."),
    offset: int = typer.Option(0, "--offset", min=0, help="Rows to skip."),
) -> None:
    """List failure candidates."""
    listing = _run(
        lambda: list_failures(project_root=project, status=status, limit=limit, offset=offset)
    )
    if not listing.summaries:
        console.print("[dim]No failure candidates.[/]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("failure_id", style="cyan")
    table.add_column("trace_id")
    table.add_column("status")
    table.add_column("evidence")
    table.add_column("reviewer", style="dim")
    for summary in listing.summaries:
        evidence = ", ".join(summary.kinds) if summary.kinds else "[dim]none (manual)[/]"
        table.add_row(
            summary.failure_id,
            summary.trace_id,
            _failure_status_markup(summary.status),
            evidence,
            summary.reviewer or "",
        )
    console.print(table)

    breakdown = ", ".join(
        f"{count} {status.value}" for status, count in sorted(listing.counts.items())
    )
    console.print(f"\n[dim]{len(listing.summaries)} of {listing.total} ({breakdown})[/]")


@failures_app.command("show")
def failures_show(
    identifier: str = typer.Argument(..., metavar="ID", help="Failure ID or trace ID."),
    project: Path = PROJECT_OPTION,
) -> None:
    """Inspect the evidence behind one failure."""
    detail = _run(lambda: show_failure(identifier, project_root=project))
    _render_failure(detail)


@failures_app.command("confirm")
def failures_confirm(
    identifier: str = typer.Argument(..., metavar="ID", help="Failure ID or trace ID."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
    reason: str | None = typer.Option(None, "--reason", help="Why."),
) -> None:
    """Confirm a failure candidate is a real failure."""
    failure = _run(
        lambda: review_failure(
            identifier,
            FailureStatus.CONFIRMED,
            project_root=project,
            reviewer=reviewer,
            reason=reason,
        )
    )
    console.print(
        f"[bold green]confirmed[/] {failure.failure_id} ({failure.trace_id}) by {failure.reviewer}"
    )


@failures_app.command("dismiss")
def failures_dismiss(
    identifier: str = typer.Argument(..., metavar="ID", help="Failure ID or trace ID."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
    reason: str | None = typer.Option(None, "--reason", help="Why."),
) -> None:
    """Dismiss a failure candidate. The record is kept for audit."""
    failure = _run(
        lambda: review_failure(
            identifier,
            FailureStatus.DISMISSED,
            project_root=project,
            reviewer=reviewer,
            reason=reason,
        )
    )
    console.print(
        f"[bold yellow]dismissed[/] {failure.failure_id} ({failure.trace_id}) by {failure.reviewer}"
    )


@failures_app.command("add")
def failures_add(
    trace_id: str = typer.Argument(..., help="Trace to mark as a failure."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
    reason: str | None = typer.Option(None, "--reason", help="Why."),
) -> None:
    """Mark a trace as a failure by hand, with no detector evidence."""
    failure = _run(
        lambda: add_failure(trace_id, project_root=project, reviewer=reviewer, reason=reason)
    )
    console.print(
        f"[bold green]added[/] {failure.failure_id} for {failure.trace_id} by {failure.reviewer}"
    )


def _failure_status_markup(status: FailureStatus) -> str:
    styles = {
        FailureStatus.CONFIRMED: "red",
        FailureStatus.CANDIDATE: "yellow",
        FailureStatus.DISMISSED: "dim",
    }
    return f"[{styles[status]}]{status.value}[/]"


def _render_detection(report: DetectionReport) -> None:
    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("traces examined", str(report.traces))
    summary.add_row("failures", f"[red]{report.failures}[/]" if report.failures else "0")
    summary.add_row("new", f"[green]{report.created}[/]")
    if report.updated:
        summary.add_row("updated", str(report.updated))
    if report.unchanged:
        summary.add_row("unchanged", str(report.unchanged))
    if report.withdrawn:
        summary.add_row("withdrawn", str(report.withdrawn))
    if report.preserved_reviews:
        summary.add_row("reviews kept", str(report.preserved_reviews))
    summary.add_row("signals", str(report.signals))
    console.print(summary)

    if report.by_kind:
        detail = ", ".join(
            f"{kind.value} x{count}" for kind, count in sorted(report.by_kind.items())
        )
        console.print(f"[dim]{detail}[/]")
    console.print("\nNext: [bold]evalsmith failures list[/]")


def _render_failure(detail: FailureDetail) -> None:
    failure = detail.failure
    header = Table(box=None, pad_edge=False, show_header=False)
    header.add_column(style="bold")
    header.add_column()
    header.add_row("failure_id", failure.failure_id)
    header.add_row("trace_id", failure.trace_id)
    header.add_row("status", _failure_status_markup(failure.status))
    header.add_row("origin", failure.origin.value)
    header.add_row("detected", failure.detected_at.isoformat())
    if failure.reviewer:
        header.add_row("reviewer", failure.reviewer)
    if failure.reason:
        header.add_row("reason", failure.reason)
    console.print(header)

    if failure.signals:
        console.print("\n[bold]evidence[/]")
        signals = Table(box=None, pad_edge=False)
        signals.add_column("kind")
        signals.add_column("source", style="magenta")
        signals.add_column("detail")
        for signal in failure.signals:
            signals.add_row(signal.kind.value, signal.source, signal.summary)
        console.print(signals)
    else:
        console.print("\n[dim]No detector evidence; this failure was added by hand.[/]")

    console.print("\n[bold]trace[/]")
    _render_trace(detail.trace)


def _render_ingest(report: IngestReport) -> None:
    if report.sample:
        issues = Table(box=None, pad_edge=False)
        issues.add_column("line", justify="right", style="cyan")
        issues.add_column("kind")
        issues.add_column("field", style="magenta")
        issues.add_column("problem")
        for issue in report.sample:
            problem = (
                issue.message if issue.hint is None else f"{issue.message} [dim]({issue.hint})[/]"
            )
            issues.add_row(str(issue.line), issue.kind.value, issue.field or "", problem)
        console.print(issues)
        if report.truncated:
            hint = (
                "re-run with --errors PATH to capture them all"
                if report.error_path is None
                else f"see {report.error_path}"
            )
            console.print(f"[dim]... and {report.truncated} more ({hint})[/]")
        console.print()

    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("records", str(report.records))
    summary.add_row("valid", f"[green]{report.valid}[/]")
    summary.add_row("invalid", f"[red]{report.invalid}[/]" if report.invalid else "0")
    if report.duplicate_ids:
        summary.add_row("duplicate ids", f"[red]{report.duplicate_ids}[/]")
    if report.mode is not IngestMode.VALIDATE:
        label = "would store" if report.mode is IngestMode.DRY_RUN else "stored"
        summary.add_row(label, f"[green]{report.stored}[/]")
        if report.already_stored:
            summary.add_row("already stored", str(report.already_stored))
        if report.content_duplicates:
            summary.add_row("duplicate content", str(report.content_duplicates))
        if report.id_conflicts:
            summary.add_row("id conflicts", f"[red]{report.id_conflicts}[/]")
        summary.add_row("redacted values", str(report.redactions))
    console.print(summary)

    if report.redactions:
        detail = ", ".join(
            f"{rule} x{count}" for rule, count in report.redaction_summary.to_dict().items()
        )
        console.print(f"[dim]{detail}[/]")
    if report.error_path is not None and report.issue_count:
        console.print(f"\n[dim]{report.issue_count} issues written to {report.error_path}[/]")

    if report.mode is IngestMode.DRY_RUN:
        console.print("\n[bold yellow]Dry run[/] - nothing was written.")
    elif not report.ok:
        console.print(f"\n[bold red]Invalid[/] {report.path}")
    elif report.mode is IngestMode.VALIDATE:
        console.print(f"\n[bold green]Valid[/] {report.path}")
    else:
        console.print(f"\n[bold green]Ingested[/] {report.stored} traces from {report.path}")


def _run(action: Callable[[], T]) -> T:
    """Run a command function, turning EvalsmithError into a clean exit."""
    try:
        return action()
    except EvalsmithError as exc:
        err_console.print(f"[bold red]error:[/] {exc.message}")
        if exc.hint:
            err_console.print(f"[dim]hint:[/] {exc.hint}")
        raise typer.Exit(exc.exit_code) from exc


def main() -> None:
    app()

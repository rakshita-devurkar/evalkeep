"""The Evalsmith command line.

The CLI coordinates commands and renders output; the business logic lives in
:mod:`evalsmith.commands` so it can be tested without a terminal.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import click
import typer
from rich.console import Console
from rich.table import Table

from evalsmith import __version__
from evalsmith.adapters import DEFAULT_ADAPTER, available_adapters
from evalsmith.analysis import Component, FailureType, Severity
from evalsmith.analysis_run import AnalysisReport
from evalsmith.commands.analyze_cmd import label_failure, run_analysis
from evalsmith.commands.compare_cmd import (
    compare,
    current_baseline,
    list_runs,
    promote_baseline,
    show_run,
)
from evalsmith.commands.dataset_cmd import BuildReport, build_dataset, list_tests, show_test
from evalsmith.commands.detect_cmd import (
    FailureDetail,
    add_failure,
    list_failures,
    review_failure,
    run_detection,
    show_failure,
)
from evalsmith.commands.discover_cmd import (
    ClusterDetail,
    dismiss_cluster,
    list_clusters,
    merge_clusters,
    rename_cluster,
    restore_cluster,
    run_discovery,
    show_cluster,
    split_cluster,
)
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.commands.init_cmd import Action, initialize_project
from evalsmith.commands.review_cmd import (
    ReviewItem,
    approve_test,
    edit_test,
    editable_document,
    pending_reviews,
    reject_test,
    review_item,
)
from evalsmith.commands.run_cmd import ExportResult, export_suite, run_suite
from evalsmith.commands.target_cmd import add_target, list_targets, remove_target, show_target
from evalsmith.commands.trace_cmd import list_traces, show_trace
from evalsmith.comparison import Classification, ComparisonReport
from evalsmith.detection import DetectionReport
from evalsmith.discovery import DiscoveryReport
from evalsmith.errors import CommandError, EvalsmithError, ExitCode
from evalsmith.exporters import parse_format
from evalsmith.failures import FailureStatus
from evalsmith.ingest import DEFAULT_SAMPLE_LIMIT, IngestMode, IngestReport
from evalsmith.regression import RegressionTest, ReviewStatus
from evalsmith.review import ReviewDecision, ReviewOutcome
from evalsmith.runner import RunOutcome
from evalsmith.runs import CaseResult, EvaluationRun, Outcome
from evalsmith.storage import StoredTrace
from evalsmith.targets import TargetKind

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
clusters_app = typer.Typer(
    name="clusters", help="Inspect and edit failure families.", no_args_is_help=True
)
app.add_typer(clusters_app)
dataset_app = typer.Typer(
    name="dataset", help="Generate and inspect regression tests.", no_args_is_help=True
)
app.add_typer(dataset_app)
targets_app = typer.Typer(
    name="targets", help="Configure the agents under test.", no_args_is_help=True
)
app.add_typer(targets_app)
runs_app = typer.Typer(name="runs", help="Inspect evaluation runs.", no_args_is_help=True)
app.add_typer(runs_app)
baseline_app = typer.Typer(
    name="baseline", help="The run everything is compared against.", no_args_is_help=True
)
app.add_typer(baseline_app)

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
    # A count, not the kinds: seven columns of prose does not fit a terminal,
    # and 'failures show' is where the evidence itself belongs.
    table.add_column("evidence", justify="right")
    table.add_column("type")
    table.add_column("severity")
    table.add_column("reviewer", style="dim")
    for summary in listing.summaries:
        table.add_row(
            summary.failure_id,
            summary.trace_id,
            _failure_status_markup(summary.status),
            str(summary.signals) if summary.signals else "[dim]manual[/]",
            summary.failure_type or "[dim]-[/]",
            _severity_markup(summary.severity) if summary.severity else "[dim]-[/]",
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


@app.command()
def analyze(
    project: Path = PROJECT_OPTION,
    reanalyze: bool = typer.Option(
        False, "--reanalyze", help="Re-run over existing machine analyses."
    ),
    overwrite_manual: bool = typer.Option(
        False,
        "--overwrite-manual",
        help="Also replace hand-written labels. Off by default.",
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Stop after N failures."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore the analyzer cache."),
) -> None:
    """Describe failures with the configured analyzer provider."""
    report = _run(
        lambda: run_analysis(
            project_root=project,
            reanalyze=reanalyze,
            overwrite_manual=overwrite_manual,
            limit=limit,
            use_cache=not no_cache,
        )
    )
    _render_analysis(report)


@failures_app.command("label")
def failures_label(
    identifier: str = typer.Argument(..., metavar="ID", help="Failure ID or trace ID."),
    failure_type: FailureType = typer.Option(..., "--type", help="What went wrong."),
    component: Component = typer.Option(..., "--component", help="Where it went wrong."),
    severity: Severity = typer.Option(..., "--severity", help="How much it matters."),
    summary: str = typer.Option(..., "--summary", help="One sentence naming the mistake."),
    project: Path = PROJECT_OPTION,
    labeler: str | None = typer.Option(None, "--labeler", help="Who is labelling."),
) -> None:
    """Describe a failure by hand. Works with no analyzer provider configured."""
    analysis = _run(
        lambda: label_failure(
            identifier,
            failure_type=failure_type,
            component=component,
            severity=severity,
            summary=summary,
            project_root=project,
            labeler=labeler,
        )
    )
    console.print(
        f"[bold green]labelled[/] {analysis.failure_type.value} / "
        f"{analysis.component.value} / {analysis.severity.value} by {analysis.labeler}"
    )


def _render_analysis(report: AnalysisReport) -> None:
    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("analyzer", report.analyzer)
    summary.add_row("prompt version", str(report.prompt_version))
    summary.add_row("considered", str(report.considered))
    summary.add_row("analyzed", f"[green]{report.analyzed}[/]")
    if report.from_cache:
        summary.add_row("from cache", str(report.from_cache))
    if report.skipped:
        summary.add_row("already analyzed", str(report.skipped))
    if report.manual_kept:
        summary.add_row("hand labels kept", str(report.manual_kept))
    if report.failed:
        summary.add_row("failed", f"[red]{report.failed}[/]")
    if report.redactions:
        summary.add_row("redacted values", str(report.redactions))
    console.print(summary)

    if report.by_type:
        detail = ", ".join(f"{name} x{count}" for name, count in sorted(report.by_type.items()))
        console.print(f"[dim]{detail}[/]")

    for failure_id, message in report.errors:
        err_console.print(f"[yellow]could not analyze[/] {failure_id}: {message}")

    console.print("\nNext: [bold]evalsmith failures list[/]")


def _failure_status_markup(status: FailureStatus) -> str:
    styles = {
        FailureStatus.CONFIRMED: "red",
        FailureStatus.CANDIDATE: "yellow",
        FailureStatus.DISMISSED: "dim",
    }
    return f"[{styles[status]}]{status.value}[/]"


@app.command()
def discover(
    project: Path = PROJECT_OPTION,
    skip_analysis: bool = typer.Option(
        False, "--skip-analysis", help="Group what is already analyzed, analyzing nothing."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-cluster even if it discards reviewer edits."
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore the embedding cache."),
) -> None:
    """Analyze, embed, cluster and select representatives."""
    report = _run(
        lambda: run_discovery(
            project_root=project,
            analyze=not skip_analysis,
            force=force,
            use_cache=not no_cache,
        )
    )
    _render_discovery(report)


@clusters_app.command("list")
def clusters_list(
    project: Path = PROJECT_OPTION,
    include_dismissed: bool = typer.Option(
        True, "--dismissed/--no-dismissed", help="Include dismissed families."
    ),
) -> None:
    """List failure families."""
    clusters = _run(
        lambda: list_clusters(project_root=project, include_dismissed=include_dismissed)
    )
    if not clusters:
        console.print("[dim]No clusters. Run 'evalsmith discover'.[/]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("cluster_id", style="cyan")
    table.add_column("size", justify="right")
    table.add_column("reps", justify="right")
    table.add_column("label")
    table.add_column("state", style="dim")
    for cluster in clusters:
        state = "dismissed" if cluster.dismissed else ("renamed" if cluster.labelled_by else "")
        table.add_row(
            cluster.cluster_id,
            str(cluster.size),
            str(len(cluster.representatives)),
            f"[dim]{cluster.label}[/]" if cluster.dismissed else cluster.label,
            state,
        )
    console.print(table)

    total = sum(cluster.size for cluster in clusters)
    console.print(f"\n[dim]{len(clusters)} clusters covering {total} failures[/]")


@clusters_app.command("show")
def clusters_show(
    identifier: str = typer.Argument(..., metavar="ID", help="Cluster, failure or trace ID."),
    project: Path = PROJECT_OPTION,
) -> None:
    """Inspect one family and its representatives."""
    detail = _run(lambda: show_cluster(identifier, project_root=project))
    _render_cluster(detail)


@clusters_app.command("rename")
def clusters_rename(
    identifier: str = typer.Argument(..., metavar="ID", help="Cluster, failure or trace ID."),
    label: str = typer.Argument(..., help="New label."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is renaming."),
) -> None:
    """Rename a family."""
    cluster = _run(
        lambda: rename_cluster(identifier, label, project_root=project, reviewer=reviewer)
    )
    console.print(f"[bold green]renamed[/] {cluster.cluster_id} to {cluster.label!r}")


@clusters_app.command("dismiss")
def clusters_dismiss(
    identifier: str = typer.Argument(..., metavar="ID", help="Cluster, failure or trace ID."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
) -> None:
    """Mark a family as not worth regression coverage. It is kept, not deleted."""
    cluster = _run(lambda: dismiss_cluster(identifier, project_root=project, reviewer=reviewer))
    console.print(f"[bold yellow]dismissed[/] {cluster.cluster_id} ({cluster.label})")


@clusters_app.command("restore")
def clusters_restore(
    identifier: str = typer.Argument(..., metavar="ID", help="Cluster, failure or trace ID."),
    project: Path = PROJECT_OPTION,
) -> None:
    """Undo a dismissal."""
    cluster = _run(lambda: restore_cluster(identifier, project_root=project))
    console.print(f"[bold green]restored[/] {cluster.cluster_id} ({cluster.label})")


@clusters_app.command("merge")
def clusters_merge(
    identifiers: list[str] = typer.Argument(..., metavar="ID...", help="Clusters to merge."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
) -> None:
    """Combine several families into one."""
    cluster = _run(lambda: merge_clusters(identifiers, project_root=project, reviewer=reviewer))
    console.print(
        f"[bold green]merged[/] into {cluster.cluster_id} "
        f"({cluster.size} failures, {cluster.label!r})"
    )


@clusters_app.command("split")
def clusters_split(
    identifier: str = typer.Argument(..., metavar="ID", help="Cluster to split."),
    failure: list[str] = typer.Option(..., "--failure", help="Failure to move out. Repeatable."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
) -> None:
    """Move some members of a family into a new family of their own."""
    remainder, extracted = _run(
        lambda: split_cluster(identifier, failure, project_root=project, reviewer=reviewer)
    )
    console.print(
        f"[bold green]split[/] {extracted.cluster_id} ({extracted.size}) "
        f"out of {remainder.cluster_id} ({remainder.size})"
    )


def _render_discovery(report: DiscoveryReport) -> None:
    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("embedder", report.embedder)
    summary.add_row("failures", str(report.considered))
    if report.unanalyzed:
        summary.add_row("not analyzed", f"[yellow]{report.unanalyzed}[/]")
    summary.add_row("clusters", f"[green]{report.clusters}[/]")
    summary.add_row("largest", str(report.largest))
    summary.add_row("singletons", str(report.singletons))
    summary.add_row("representatives", str(report.representatives))
    summary.add_row("embedded", str(report.embedded))
    if report.from_cache:
        summary.add_row("from cache", str(report.from_cache))
    if report.kept_labels:
        summary.add_row("labels kept", str(report.kept_labels))
    if report.discarded_edits:
        summary.add_row("edits discarded", f"[red]{report.discarded_edits}[/]")
    console.print(summary)

    parameters = ", ".join(f"{key}={value}" for key, value in sorted(report.parameters.items()))
    console.print(f"[dim]{parameters}[/]")
    console.print("\nNext: [bold]evalsmith clusters list[/]")


def _render_cluster(detail: ClusterDetail) -> None:
    cluster = detail.cluster
    header = Table(box=None, pad_edge=False, show_header=False)
    header.add_column(style="bold")
    header.add_column()
    header.add_row("cluster_id", cluster.cluster_id)
    header.add_row("label", cluster.label)
    header.add_row("size", str(cluster.size))
    if cluster.labelled_by:
        header.add_row("edited by", cluster.labelled_by)
    if cluster.dismissed:
        header.add_row("state", "[yellow]dismissed[/]")
    console.print(header)

    console.print("\n[bold]members[/]")
    table = Table(box=None, pad_edge=False)
    table.add_column("failure_id", style="cyan")
    table.add_column("trace_id")
    table.add_column("dist", justify="right")
    table.add_column("role")
    table.add_column("severity")
    table.add_column("summary")
    for member in cluster.members:
        analysis = detail.analyses.get(member.failure_id)
        table.add_row(
            member.failure_id,
            detail.trace_ids.get(member.failure_id, ""),
            f"{member.distance:.2f}",
            ", ".join(role.value for role in member.roles) or "[dim]-[/]",
            _severity_markup(analysis.severity.value) if analysis else "",
            analysis.summary if analysis else "",
        )
    console.print(table)


@dataset_app.command("build")
def dataset_build(
    project: Path = PROJECT_OPTION,
    all_failures: bool = typer.Option(
        False, "--all", help="Cover every failure, not just cluster representatives."
    ),
    regenerate: bool = typer.Option(
        False, "--regenerate", help="Rewrite existing drafts. Never touches reviewed tests."
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Stop after N tests."),
) -> None:
    """Generate pending regression-test drafts."""
    report = _run(
        lambda: build_dataset(
            project_root=project,
            representatives_only=not all_failures,
            regenerate=regenerate,
            limit=limit,
        )
    )
    _render_build(report)


@dataset_app.command("list")
def dataset_list(
    project: Path = PROJECT_OPTION,
    status: ReviewStatus | None = typer.Option(None, "--status", help="Filter by status."),
    limit: int = typer.Option(50, "--limit", min=1, help="Rows to show."),
    offset: int = typer.Option(0, "--offset", min=0, help="Rows to skip."),
) -> None:
    """List regression tests."""
    listing = _run(
        lambda: list_tests(project_root=project, status=status, limit=limit, offset=offset)
    )
    if not listing.tests:
        console.print("[dim]No regression tests. Run 'evalsmith dataset build'.[/]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("test_id", style="cyan", overflow="fold")
    table.add_column("status")
    table.add_column("checks", justify="right")
    table.add_column("reviewer", style="dim")
    table.add_column("needs", style="yellow")
    for test in listing.tests:
        needs = []
        if not test.has_positive_expectation:
            needs.append("expectation")
        if test.contradictions:
            needs.append("conflict")
        table.add_row(
            test.test_id,
            _test_status_markup(test.status),
            str(len(test.deterministic_expectations)),
            f"{test.reviewer} (edited)" if test.edited else (test.reviewer or ""),
            ", ".join(needs),
        )
    console.print(table)

    breakdown = ", ".join(
        f"{count} {status.value}" for status, count in sorted(listing.counts.items())
    )
    console.print(f"\n[dim]{len(listing.tests)} of {listing.total} ({breakdown})[/]")


@dataset_app.command("show")
def dataset_show(
    identifier: str = typer.Argument(..., metavar="ID", help="Test, failure or trace ID."),
    project: Path = PROJECT_OPTION,
) -> None:
    """Inspect one regression test and its provenance."""
    test = _run(lambda: show_test(identifier, project_root=project))
    _render_test(test)


@app.command()
def review(
    identifier: str | None = typer.Argument(
        None, metavar="[ID]", help="Review one test instead of every draft."
    ),
    project: Path = PROJECT_OPTION,
    limit: int | None = typer.Option(None, "--limit", min=1, help="Review at most N drafts."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is reviewing."),
) -> None:
    """Approve, edit, reject or skip drafts."""
    if identifier is not None:
        items = [_run(lambda: review_item(identifier, project_root=project))]
    else:
        items = _run(lambda: pending_reviews(project_root=project, limit=limit))

    if not items:
        console.print("[dim]Nothing to review. Run 'evalsmith dataset build'.[/]")
        return

    who = reviewer or _default_reviewer()
    outcome = ReviewOutcome(remaining=len(items))
    for position, item in enumerate(items, start=1):
        console.rule(f"[bold]{position} of {len(items)}[/]  {item.test.test_id}")
        if not _review_one(item, project=project, reviewer=who, outcome=outcome):
            break
        outcome.remaining -= 1

    _render_review_outcome(outcome)


def _review_one(item: ReviewItem, *, project: Path, reviewer: str, outcome: ReviewOutcome) -> bool:
    """Show one draft and act on the decision. Returns False to stop the session."""
    current = item
    while True:
        _render_review_item(current)
        decision = _ask_decision()

        match decision:
            case ReviewDecision.APPROVE:
                try:
                    approve_test(
                        current.test.test_id,
                        project_root=project,
                        reviewer=reviewer,
                        reason=_ask_reason("Why approve? (optional)"),
                    )
                except EvalsmithError as exc:
                    err_console.print(f"[bold red]error:[/] {exc.message}")
                    continue
                outcome.reviewed += 1
                outcome.approved += 1
                if current.test.edited:
                    outcome.edited += 1
                console.print("[bold green]approved[/]")
                return True

            case ReviewDecision.REJECT:
                reject_test(
                    current.test.test_id,
                    project_root=project,
                    reviewer=reviewer,
                    reason=_ask_reason("Why reject?"),
                )
                outcome.reviewed += 1
                outcome.rejected += 1
                console.print("[bold yellow]rejected[/] [dim](kept for audit)[/]")
                return True

            case ReviewDecision.EDIT:
                if not _edit_in_place(current.test.test_id, project=project, reviewer=reviewer):
                    continue
                current = review_item(current.test.test_id, project_root=project)
                continue

            case _:
                outcome.skipped += 1
                console.print("[dim]skipped[/]")
                return True


def _edit_in_place(test_id: str, *, project: Path, reviewer: str) -> bool:
    """Open the document in $EDITOR and store it if it is valid."""
    document = editable_document(test_id, project_root=project)
    try:
        edited = click.edit(document, extension=".yaml")
    except Exception as exc:  # no editor configured, or it failed to launch
        err_console.print(f"[bold red]error:[/] could not open an editor: {exc}")
        err_console.print("[dim]hint:[/] set $EDITOR, or use 'evalsmith dataset edit'.")
        return False

    if edited is None or edited.strip() == document.strip():
        console.print("[dim]no changes[/]")
        return False

    try:
        edit_test(test_id, edited, project_root=project, editor=reviewer)
    except EvalsmithError as exc:
        err_console.print(f"[bold red]error:[/] {exc.message}")
        if exc.hint:
            err_console.print(f"[dim]hint:[/] {exc.hint}")
        return False

    console.print("[bold green]edited[/]")
    return True


def _ask_decision() -> ReviewDecision:
    answer = typer.prompt("approve / edit / reject / skip", default="skip", show_default=True)
    letter = answer.strip().lower()[:1]
    return {
        "a": ReviewDecision.APPROVE,
        "e": ReviewDecision.EDIT,
        "r": ReviewDecision.REJECT,
    }.get(letter, ReviewDecision.SKIP)


def _ask_reason(prompt: str) -> str | None:
    answer = typer.prompt(prompt, default="", show_default=False)
    return answer.strip() or None


def _default_reviewer() -> str:
    from evalsmith.commands.detect_cmd import default_reviewer

    return default_reviewer()


def _render_review_item(item: ReviewItem) -> None:
    """Guide 8H: the source interaction, the analysis, then the proposed test."""
    console.print("\n[bold]the interaction[/]")
    _render_trace(item.trace)

    if item.analysis is not None:
        analysis = item.analysis
        console.print(
            f"\n[bold]analysis[/] {analysis.failure_type.value} / "
            f"{analysis.component.value} / {_severity_markup(analysis.severity.value)}"
        )
        console.print(analysis.summary)
        console.print(f"[dim]by {analysis.analyzer}[/]")

    console.print("\n[bold]evidence[/]")
    for signal in item.failure.signals:
        console.print(f"  [dim]({signal.kind.value})[/] {signal.summary}")

    console.print("\n[bold]proposed test[/]")
    _render_test(item.test)


def _render_review_outcome(outcome: ReviewOutcome) -> None:
    console.rule()
    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("approved", f"[green]{outcome.approved}[/]")
    if outcome.edited:
        summary.add_row("of those, edited", str(outcome.edited))
    summary.add_row("rejected", str(outcome.rejected))
    summary.add_row("skipped", str(outcome.skipped))
    if outcome.remaining:
        summary.add_row("left to review", str(outcome.remaining))
    console.print(summary)
    console.print("\n[dim]Only approved tests are exported.[/]")


@dataset_app.command("approve")
def dataset_approve(
    identifier: str = typer.Argument(..., metavar="ID", help="Test, failure or trace ID."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
    reason: str | None = typer.Option(None, "--reason", help="Why."),
) -> None:
    """Approve a test without the interactive loop."""
    test = _run(
        lambda: approve_test(identifier, project_root=project, reviewer=reviewer, reason=reason)
    )
    console.print(f"[bold green]approved[/] {test.test_id} by {test.reviewer}")


@dataset_app.command("reject")
def dataset_reject(
    identifier: str = typer.Argument(..., metavar="ID", help="Test, failure or trace ID."),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
    reason: str | None = typer.Option(None, "--reason", help="Why."),
) -> None:
    """Reject a test. The record is kept for audit."""
    test = _run(
        lambda: reject_test(identifier, project_root=project, reviewer=reviewer, reason=reason)
    )
    console.print(f"[bold yellow]rejected[/] {test.test_id} by {test.reviewer}")


@dataset_app.command("edit")
def dataset_edit(
    identifier: str = typer.Argument(..., metavar="ID", help="Test, failure or trace ID."),
    project: Path = PROJECT_OPTION,
    from_file: Path | None = typer.Option(
        None, "--file", help="Read the edited document from a file instead of $EDITOR."
    ),
    show: bool = typer.Option(False, "--show", help="Print the editable document and exit."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is editing."),
) -> None:
    """Edit a test's input and expectations."""
    if show:
        console.print(_run(lambda: editable_document(identifier, project_root=project)))
        return

    if from_file is not None:
        document = _run(lambda: _read_document(from_file))
    else:
        original = _run(lambda: editable_document(identifier, project_root=project))
        edited = click.edit(original, extension=".yaml")
        if edited is None or edited.strip() == original.strip():
            console.print("[dim]no changes[/]")
            return
        document = edited

    test = _run(lambda: edit_test(identifier, document, project_root=project, editor=reviewer))
    console.print(
        f"[bold green]edited[/] {test.test_id} "
        f"({len(test.expectations)} expectations, {len(test.warnings)} warnings)"
    )


def _read_document(path: Path) -> str:
    try:
        return path.expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"Could not read {path}: {exc}") from exc


@targets_app.command("add")
def targets_add(
    target_id: str = typer.Argument(..., metavar="NAME", help="baseline, candidate, ..."),
    kind: TargetKind = typer.Option(..., "--type", help="How the agent is reached."),
    project: Path = PROJECT_OPTION,
    description: str | None = typer.Option(None, "--description"),
    url: str | None = typer.Option(None, "--url", help="http: the endpoint."),
    method: str = typer.Option("POST", "--method", help="http: the HTTP method."),
    header: list[str] = typer.Option(
        [], "--header", metavar="NAME=VALUE", help="http: repeatable. Use ${ENV} for secrets."
    ),
    body: str | None = typer.Option(
        None, "--body", help="http: request body as JSON. Use {{input}} for the test input."
    ),
    path: str | None = typer.Option(None, "--path", help="python/javascript: the script."),
    function: str | None = typer.Option(None, "--function", help="python: the entry point."),
    provider: str | None = typer.Option(None, "--provider", help="model: the provider id."),
    output_path: str | None = typer.Option(
        None, "--output-path", help="http: where the answer is, e.g. json.reply."
    ),
    tool_calls_path: str | None = typer.Option(
        None, "--tool-calls-path", help="http: where the tool calls are."
    ),
    replace: bool = typer.Option(False, "--replace", help="Overwrite an existing target."),
) -> None:
    """Record how to reach an agent. Secrets must be ${ENV_VAR} references."""
    target = _run(
        lambda: add_target(
            target_id,
            kind,
            project_root=project,
            description=description,
            url=url,
            method=method,
            headers=_parse_pairs(header),
            body=_parse_json(body, "--body"),
            path=path,
            function=function,
            provider=provider,
            output_path=output_path,
            tool_calls_path=tool_calls_path,
            replace=replace,
        )
    )
    console.print(f"[bold green]added[/] target {target.target_id} ({target.kind.value})")


@targets_app.command("list")
def targets_list(project: Path = PROJECT_OPTION) -> None:
    """List configured targets."""
    targets = _run(lambda: list_targets(project_root=project))
    if not targets:
        console.print("[dim]No targets. Add one with 'evalsmith targets add'.[/]")
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("target", style="cyan")
    table.add_column("type")
    table.add_column("where", overflow="fold")
    table.add_column("description", style="dim")
    for target in targets:
        table.add_row(
            target.target_id,
            target.kind.value,
            target.url or target.path or target.provider or "",
            target.description or "",
        )
    console.print(table)


@targets_app.command("show")
def targets_show(
    target_id: str = typer.Argument(..., metavar="NAME"),
    project: Path = PROJECT_OPTION,
) -> None:
    """Inspect a target and the environment it needs."""
    target, environment = _run(lambda: show_target(target_id, project_root=project))
    console.print_json(target.model_dump_json(exclude_none=True))
    if environment:
        console.print("\n[bold]environment[/]")
        for name, present in sorted(environment.items()):
            mark = "[green]set[/]" if present else "[red]missing[/]"
            console.print(f"  {name} {mark}")


@targets_app.command("remove")
def targets_remove(
    target_id: str = typer.Argument(..., metavar="NAME"),
    project: Path = PROJECT_OPTION,
) -> None:
    """Forget a target."""
    _run(lambda: remove_target(target_id, project_root=project))
    console.print(f"[bold yellow]removed[/] target {target_id}")


@app.command()
def export(
    project: Path = PROJECT_OPTION,
    export_format: str = typer.Option("promptfoo", "--format", "-f", help="promptfoo or jsonl."),
    target: str | None = typer.Option(None, "--target", help="Which target to export for."),
    out: Path | None = typer.Option(None, "--out", help="Directory to write into."),
) -> None:
    """Create runner-compatible files from the approved suite."""
    result = _run(
        lambda: export_suite(
            project_root=project,
            export_format=parse_format(export_format),
            target_id=target,
            out=out,
        )
    )
    _render_export(result)


@app.command()
def run(
    project: Path = PROJECT_OPTION,
    target: str = typer.Option(..., "--target", help="Which target to run against."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Run at most N tests."),
) -> None:
    """Delegate execution of the approved suite to Promptfoo."""
    outcome = _run(lambda: run_suite(project_root=project, target_id=target, limit=limit))
    _render_run(outcome)
    raise typer.Exit(ExitCode.OK)


def _parse_pairs(pairs: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator:
            raise CommandError(f"Expected NAME=VALUE, got {pair!r}.")
        parsed[name.strip()] = value.strip()
    return parsed


def _parse_json(raw: str | None, option: str) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError(f"{option} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CommandError(f"{option} must be a JSON object.")
    return parsed


def _render_export(result: ExportResult) -> None:
    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column()
    summary.add_row("format", result.format.value)
    if result.target_id:
        summary.add_row("target", result.target_id)
    summary.add_row("tests", str(result.tests))
    summary.add_row("written", str(result.path))
    console.print(summary)
    console.print("\n[dim]Approved tests only.[/]")


def _render_run(outcome: RunOutcome) -> None:
    counts = outcome.counts
    passed = counts.get(Outcome.PASS, 0)
    failed = counts.get(Outcome.FAIL, 0)
    errored = counts.get(Outcome.ERROR, 0)

    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("run", outcome.run.run_id)
    summary.add_row("target", outcome.run.target_id)
    summary.add_row("runner", outcome.run.runner or "unknown")
    summary.add_row("tests", str(outcome.run.tests))
    summary.add_row("passed", f"[green]{passed}[/]")
    summary.add_row("failed", f"[red]{failed}[/]" if failed else "0")
    summary.add_row("errors", f"[yellow]{errored}[/]" if errored else "0")
    console.print(summary)

    for result in outcome.results:
        if result.outcome is Outcome.ERROR:
            console.print(
                f"[yellow]error[/] {result.test_id} "
                f"[dim]({result.error_kind.value if result.error_kind else 'unknown'})[/]"
            )
        elif result.outcome is Outcome.FAIL:
            reason = result.failed_assertions[0] if result.failed_assertions else ""
            console.print(f"[red]fail[/] {result.test_id} [dim]{reason.splitlines()[0:1]}[/]")

    for message in outcome.messages:
        err_console.print(f"[dim]{message}[/]")

    if errored:
        console.print(
            "\n[dim]Errors are reported separately: a test that never ran is not "
            "a test that failed.[/]"
        )
    console.print(f"\n[dim]Results stored under {outcome.run.output_dir}[/]")


@app.command("compare")
def compare_runs(
    project: Path = PROJECT_OPTION,
    baseline: str | None = typer.Option(
        None, "--baseline", help="Run ID or target name. Defaults to the promoted baseline."
    ),
    candidate: str | None = typer.Option(
        None, "--candidate", help="Run ID or target name. Defaults to the newest candidate run."
    ),
    allow_suite_drift: bool = typer.Option(
        False, "--allow-suite-drift", help="Compare only the tests both runs share."
    ),
    fail_on_regression: bool = typer.Option(
        False, "--fail-on-regression", help="Exit non-zero when any test regressed."
    ),
) -> None:
    """Compare baseline and candidate result sets."""
    report = _run(
        lambda: compare(
            project_root=project,
            baseline=baseline,
            candidate=candidate,
            allow_suite_drift=allow_suite_drift,
        )
    )
    _render_comparison(report)
    if fail_on_regression and report.regressions:
        raise typer.Exit(ExitCode.RECORD_ERRORS)


@runs_app.command("list")
def runs_list(
    project: Path = PROJECT_OPTION,
    limit: int = typer.Option(20, "--limit", min=1, help="Rows to show."),
) -> None:
    """List evaluation runs, newest first."""
    summaries = _run(lambda: list_runs(project_root=project, limit=limit))
    if not summaries:
        console.print("[dim]No runs. Run 'evalsmith run --target baseline'.[/]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("run", style="cyan", overflow="fold")
    table.add_column("target")
    table.add_column("pass", justify="right")
    table.add_column("fail", justify="right")
    table.add_column("error", justify="right")
    table.add_column("suite", style="dim")
    table.add_column("", style="dim")
    for summary in summaries:
        table.add_row(
            summary.run.run_id[:12],
            summary.run.target_id,
            str(summary.counts.get(Outcome.PASS, 0)),
            str(summary.counts.get(Outcome.FAIL, 0)),
            str(summary.counts.get(Outcome.ERROR, 0)),
            summary.run.suite_hash.removeprefix("sha256:")[:8],
            "baseline" if summary.is_baseline else "",
        )
    console.print(table)


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., metavar="RUN_ID"),
    project: Path = PROJECT_OPTION,
) -> None:
    """Inspect one run and its per-test results."""
    run, results = _run(lambda: show_run(run_id, project_root=project))
    _render_run_detail(run, results)


@baseline_app.command("promote")
def baseline_promote(
    run_id: str = typer.Argument(..., metavar="RUN_ID"),
    project: Path = PROJECT_OPTION,
    reviewer: str | None = typer.Option(None, "--reviewer", help="Who is deciding."),
    reason: str | None = typer.Option(None, "--reason", help="Why."),
) -> None:
    """Make a run the reference point. Never automatic."""
    promotion = _run(
        lambda: promote_baseline(run_id, project_root=project, reviewer=reviewer, reason=reason)
    )
    console.print(
        f"[bold green]promoted[/] {promotion.run_id[:12]} "
        f"({promotion.target_id}) by {promotion.reviewer}"
    )


@baseline_app.command("show")
def baseline_show(project: Path = PROJECT_OPTION) -> None:
    """Show which run is currently the baseline."""
    current = _run(lambda: current_baseline(project_root=project))
    if current is None:
        console.print(
            "[dim]No baseline has been promoted. Comparisons fall back to the "
            "newest run for the 'baseline' target.[/]"
        )
        return
    promotion, run = current
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("run", run.run_id)
    table.add_row("target", run.target_id)
    table.add_row("suite", run.suite_hash)
    table.add_row("promoted", promotion.promoted_at.isoformat())
    table.add_row("by", promotion.reviewer)
    if promotion.reason:
        table.add_row("reason", promotion.reason)
    console.print(table)


_CLASSIFICATION_STYLES: dict[Classification, str] = {
    Classification.UNCHANGED_PASS: "green",
    Classification.FIXED: "bold green",
    Classification.REGRESSION: "bold red",
    Classification.UNCHANGED_FAILURE: "red",
    Classification.NOT_COMPARABLE: "yellow",
    Classification.MISSING: "yellow",
}


def _render_run_detail(run: EvaluationRun, results: list[CaseResult]) -> None:
    header = Table(box=None, pad_edge=False, show_header=False)
    header.add_column(style="bold")
    header.add_column()
    header.add_row("run", run.run_id)
    header.add_row("target", run.target_id)
    header.add_row("suite", run.suite_hash)
    header.add_row("runner", run.runner or "unknown")
    header.add_row("started", run.started_at.isoformat())
    console.print(header)

    table = Table(box=None, pad_edge=False)
    table.add_column("test_id", style="cyan", overflow="fold")
    table.add_column("outcome")
    table.add_column("detail", style="dim")
    for result in results:
        style = {"pass": "green", "fail": "red", "error": "yellow"}[result.outcome.value]
        detail = ""
        if result.outcome is Outcome.ERROR:
            detail = result.error_kind.value if result.error_kind else "error"
        elif result.failed_assertions:
            detail = result.failed_assertions[0].splitlines()[0]
        table.add_row(result.test_id, f"[{style}]{result.outcome.value}[/]", detail)
    console.print("\n")
    console.print(table)


def _render_comparison(report: ComparisonReport) -> None:
    counts = report.counts
    header = Table(box=None, pad_edge=False, show_header=False)
    header.add_column(style="bold")
    header.add_column()
    header.add_row(
        "baseline", f"{report.baseline_run.run_id[:12]} ({report.baseline_run.target_id})"
    )
    header.add_row(
        "candidate", f"{report.candidate_run.run_id[:12]} ({report.candidate_run.target_id})"
    )
    if not report.suite_compatible:
        header.add_row("suite", "[yellow]differs; comparing shared tests only[/]")
    console.print(header)

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style="bold")
    table.add_column(justify="right")
    for classification in Classification:
        count = counts.get(classification, 0)
        if not count and classification in (
            Classification.NOT_COMPARABLE,
            Classification.MISSING,
        ):
            continue
        label = classification.value.replace("_", " ")
        style = _CLASSIFICATION_STYLES[classification]
        table.add_row(label, f"[{style}]{count}[/]" if count else "0")
    console.print("\n")
    console.print(table)

    for comparison in report.regressions:
        console.print(f"[bold red]regression[/] {comparison.test_id}")
    for comparison in report.fixes:
        console.print(f"[bold green]fixed[/] {comparison.test_id}")
    for comparison in report.excluded:
        console.print(f"[yellow]excluded[/] {comparison.test_id} [dim]({comparison.reason})[/]")

    _render_statistics(report)


def _render_statistics(report: ComparisonReport) -> None:
    baseline_rate = report.baseline_pass_rate
    candidate_rate = report.candidate_pass_rate
    if baseline_rate is None or candidate_rate is None:
        console.print("\n[yellow]No comparable tests.[/] Nothing can be concluded.")
        return

    statistics = report.statistics
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("compared", str(len(report.comparable)))
    table.add_row("baseline pass rate", f"{baseline_rate:.1%}")
    table.add_row("candidate pass rate", f"{candidate_rate:.1%}")
    if statistics is not None:
        table.add_row("difference", f"{statistics.difference:+.1%}")
        table.add_row("p-value", f"{statistics.p_value:.4f}")
        if statistics.interval is not None:
            low, high = statistics.interval
            table.add_row("95% interval", f"{low:+.1%} to {high:+.1%}")
    console.print("\n")
    console.print(table)

    if statistics is None:
        return
    if statistics.note:
        console.print(f"[dim]{statistics.note}[/]")
    elif statistics.significant:
        console.print("[dim]McNemar's exact test: the change is unlikely to be chance.[/]")
    else:
        console.print(
            "[dim]McNemar's exact test: this difference is within what chance "
            "would produce. Not evidence of no change -- evidence of not enough "
            "evidence.[/]"
        )

    if report.excluded:
        console.print(
            f"\n[yellow]{len(report.excluded)} test(s) excluded[/] and not counted "
            "in any rate above."
        )


def _test_status_markup(status: ReviewStatus) -> str:
    styles = {
        ReviewStatus.DRAFT: "yellow",
        ReviewStatus.APPROVED: "green",
        ReviewStatus.REJECTED: "dim",
    }
    return f"[{styles[status]}]{status.value}[/]"


def _render_build(report: BuildReport) -> None:
    summary = Table(box=None, pad_edge=False, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("considered", str(report.considered))
    summary.add_row("drafts created", f"[green]{report.created}[/]")
    if report.regenerated:
        summary.add_row("regenerated", str(report.regenerated))
    if report.skipped:
        summary.add_row("already drafted", str(report.skipped))
    if report.reviewed_kept:
        summary.add_row("reviewed, kept", str(report.reviewed_kept))
    if report.unanalyzed:
        summary.add_row("not analyzed", f"[yellow]{report.unanalyzed}[/]")
    if report.needs_expectation:
        summary.add_row("need an expectation", f"[yellow]{report.needs_expectation}[/]")
    if report.contradictions:
        summary.add_row("contradictions", f"[red]{report.contradictions}[/]")
    console.print(summary)

    for test_id, warning in report.warnings[:10]:
        console.print(f"[yellow]{test_id}[/] [dim]{warning}[/]")
    if len(report.warnings) > 10:
        console.print(f"[dim]... and {len(report.warnings) - 10} more warnings[/]")

    console.print("\n[dim]Drafts are pending: nothing is exported until it is approved.[/]")
    console.print("Next: [bold]evalsmith dataset list[/]")


def _render_test(test: RegressionTest) -> None:
    header = Table(box=None, pad_edge=False, show_header=False)
    header.add_column(style="bold")
    header.add_column()
    header.add_row("test_id", test.test_id)
    header.add_row("status", _test_status_markup(test.status))
    header.add_row("trace_id", test.provenance.trace_id)
    header.add_row("failure_id", test.failure_id)
    if test.provenance.cluster_label:
        header.add_row("cluster", test.provenance.cluster_label)
    if test.provenance.representative_roles:
        header.add_row("selected as", ", ".join(test.provenance.representative_roles))
    if test.provenance.failure_type:
        header.add_row("failure type", test.provenance.failure_type)
    if test.provenance.severity:
        header.add_row("severity", _severity_markup(test.provenance.severity))
    header.add_row("analyzer", test.provenance.analyzer or "")
    header.add_row("generator", f"v{test.provenance.generator_version}")
    if test.reviewer:
        header.add_row("reviewer", test.reviewer)
    if test.reviewed_at:
        header.add_row("reviewed", test.reviewed_at.isoformat())
    if test.review_reason:
        header.add_row("reason", test.review_reason)
    if test.edited:
        header.add_row("edited by", test.edited_by or "")
    console.print(header)

    if test.input.text:
        console.print(f"\n[bold]input[/]\n{test.input.text}")
    for message in test.input.messages:
        console.print(f"\n[bold]input:{message['role']}[/]\n{message['content']}")

    console.print("\n[bold]expectations[/]")
    table = Table(box=None, pad_edge=False)
    table.add_column("type")
    table.add_column("check")
    table.add_column("kind", style="dim")
    for expectation in test.expectations:
        table.add_row(
            expectation.type.value,
            expectation.describe(),
            "deterministic" if expectation.deterministic else "needs a judge",
        )
    console.print(table)

    if test.fixtures:
        console.print("\n[bold]fixtures[/] [dim](tool results the original agent saw)[/]")
        fixtures = Table(box=None, pad_edge=False)
        fixtures.add_column("tool")
        fixtures.add_column("arguments")
        fixtures.add_column("result")
        for fixture in test.fixtures:
            fixtures.add_row(
                fixture.tool,
                json.dumps(fixture.arguments, sort_keys=True),
                json.dumps(fixture.result, sort_keys=True, default=str),
            )
        console.print(fixtures)

    for warning in test.warnings:
        console.print(f"\n[yellow]needs review:[/] {warning}")


def _severity_markup(severity: str) -> str:
    styles = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "dim"}
    style = styles.get(severity)
    return f"[{style}]{severity}[/]" if style else severity


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

    if detail.analysis is not None:
        analysis = detail.analysis
        console.print("\n[bold]analysis[/]")
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("type", analysis.failure_type.value)
        table.add_row("component", analysis.component.value)
        table.add_row("severity", _severity_markup(analysis.severity.value))
        table.add_row("summary", analysis.summary)
        table.add_row("analyzer", analysis.analyzer)
        if not analysis.manual:
            table.add_row("prompt version", str(analysis.prompt_version))
        table.add_row("analyzed", analysis.analyzed_at.isoformat())
        console.print(table)
    else:
        console.print("\n[dim]Not analyzed yet. Run 'evalsmith analyze', or label by hand.[/]")

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

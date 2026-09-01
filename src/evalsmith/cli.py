"""The Evalsmith command line.

The CLI coordinates commands and renders output; the business logic lives in
:mod:`evalsmith.commands` so it can be tested without a terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import typer
from rich.console import Console
from rich.table import Table

from evalsmith import __version__
from evalsmith.adapters import DEFAULT_ADAPTER, available_adapters
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.commands.init_cmd import Action, initialize_project
from evalsmith.errors import EvalsmithError, ExitCode
from evalsmith.ingest import DEFAULT_SAMPLE_LIMIT, ValidationReport

T = TypeVar("T")

app = typer.Typer(
    name="evalsmith",
    help="Turn real AI-agent failures into reviewed regression tests.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

ACTION_STYLES: dict[Action, str] = {
    Action.CREATED: "green",
    Action.UPDATED: "green",
    Action.OVERWRITTEN: "yellow",
    Action.EXISTS: "dim",
}


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
    directory: Path = typer.Argument(
        Path("."),
        help="Project directory to initialize.",
    ),
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
        name = report.project.config.project_name
        console.print(f"\n[bold green]Initialized[/] {name} in {root}")
    else:
        console.print(f"\n[dim]Already initialized:[/] {root}")
    console.print("Next: [bold]evalsmith ingest traces.jsonl[/]")


@app.command()
def ingest(
    path: Path = typer.Argument(..., help="Trace file to read."),
    validate_only: bool = typer.Option(
        False,
        "--validate-only",
        help="Check the file without redacting or storing anything.",
    ),
    trace_format: str = typer.Option(
        DEFAULT_ADAPTER,
        "--format",
        "-f",
        help=f"Trace format. One of: {', '.join(sorted(available_adapters()))}.",
    ),
    errors: Path | None = typer.Option(
        None,
        "--errors",
        metavar="PATH",
        help="Write one JSON object per issue to this file.",
    ),
    sample_limit: int = typer.Option(
        DEFAULT_SAMPLE_LIMIT,
        "--show",
        min=0,
        help="How many issues to print.",
    ),
) -> None:
    """Validate, redact, deduplicate and store traces."""
    report = _run(
        lambda: ingest_traces(
            path,
            adapter_name=trace_format,
            validate_only=validate_only,
            error_path=errors,
            sample_limit=sample_limit,
        )
    )
    _render_validation(report)
    raise typer.Exit(report.exit_code)


def _render_validation(report: ValidationReport) -> None:
    if report.sample:
        issues = Table(title=None, box=None, pad_edge=False)
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
    console.print(summary)

    if report.error_path is not None and report.issue_count:
        console.print(f"\n[dim]{report.issue_count} issues written to {report.error_path}[/]")
    if report.ok:
        console.print(f"\n[bold green]Valid[/] {report.path}")
    else:
        console.print(f"\n[bold red]Invalid[/] {report.path}")


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

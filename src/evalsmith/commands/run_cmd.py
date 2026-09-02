"""``evalsmith export`` and ``evalsmith run`` -- hand the suite to the runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from evalsmith.config import Project
from evalsmith.errors import CommandError
from evalsmith.exporters import ExportFormat, build_config, to_jsonl
from evalsmith.redaction import Redactor
from evalsmith.regression import RegressionTest, ReviewStatus
from evalsmith.runner import RunOutcome, execute
from evalsmith.storage import TraceStore
from evalsmith.targets import Target, get_target

NOTHING_APPROVED = "No approved tests to export."
NOTHING_APPROVED_HINT = "Only approved tests leave the database. Run 'evalsmith review' first."


@dataclass(frozen=True)
class ExportResult:
    format: ExportFormat
    path: Path
    tests: int
    target_id: str | None = None


def approved_tests(*, project_root: Path = Path()) -> list[RegressionTest]:
    """The suite: every approved test, and nothing else."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        return store.tests.list(status=ReviewStatus.APPROVED, limit=10_000)


def export_suite(
    *,
    project_root: Path = Path(),
    export_format: ExportFormat = ExportFormat.PROMPTFOO,
    target_id: str | None = None,
    out: Path | None = None,
) -> ExportResult:
    """Write the approved suite in the requested format."""
    project = Project.load(project_root.expanduser().resolve())
    tests = approved_tests(project_root=project_root)
    if not tests:
        raise CommandError(NOTHING_APPROVED, hint=NOTHING_APPROVED_HINT)

    directory = (out or project.subdir("exports")).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    if export_format is ExportFormat.JSONL:
        path = directory / "tests.jsonl"
        path.write_text(to_jsonl(tests), encoding="utf-8")
        return ExportResult(format=export_format, path=path, tests=len(tests))

    target = _target(project, target_id)
    path = directory / f"promptfooconfig.{target.target_id}.yaml"
    path.write_text(
        yaml.safe_dump(build_config(tests, target), sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return ExportResult(
        format=export_format, path=path, tests=len(tests), target_id=target.target_id
    )


def run_suite(
    *,
    project_root: Path = Path(),
    target_id: str,
    limit: int | None = None,
) -> RunOutcome:
    """Delegate execution of the approved suite to the configured runner."""
    project = Project.load(project_root.expanduser().resolve())
    target = get_target(project.root, target_id)
    target.validate_shape()

    tests = approved_tests(project_root=project_root)
    if not tests:
        raise CommandError(NOTHING_APPROVED, hint="Run 'evalsmith review' first.")
    if limit is not None:
        tests = tests[:limit]

    outcome = execute(
        tests,
        target,
        directory=project.subdir("runs") / f"{target.target_id}-pending",
        command=list(project.config.runner.command),
        timeout_seconds=project.config.runner.timeout_seconds,
        # Relative paths in a target are relative to the project, not to
        # wherever the command happened to be typed.
        working_directory=project.root,
        redactor=Redactor(project.config.redaction),
    )

    # Name the directory after the run only once the run has an identity.
    final = project.subdir("runs") / outcome.run.run_id
    pending = Path(outcome.run.output_dir or "")
    if pending.is_dir() and not final.exists():
        pending.rename(final)
        outcome.run.output_dir = str(final)

    with TraceStore.open(project.database_path) as store:
        store.runs.save(outcome.run, outcome.results)
    return outcome


def _target(project: Project, target_id: str | None) -> Target:
    if target_id is not None:
        return get_target(project.root, target_id)
    from evalsmith.targets import load_targets

    targets = load_targets(project.root)
    if len(targets.targets) == 1:
        return next(iter(targets.targets.values()))
    raise CommandError(
        "Which target should this be exported for?",
        hint="Pass --target, or add one with 'evalsmith targets add'.",
    )

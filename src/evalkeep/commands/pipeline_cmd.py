"""``evalkeep from-traces`` -- the whole pipeline up to the review gate.

The stages exist because each is a real decision with its own evidence, and
anyone tuning a suite will end up running them one at a time. But five commands
and their options is a lot to understand before seeing whether the tool is worth
anything, so this runs them in order and reports what came out.

It deliberately stops at review. Everything before it is derived and can be
re-run; approving a test is a judgement, and a command that quietly approved
things on your behalf would defeat the point of the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from evalkeep.adapters import DEFAULT_ADAPTER
from evalkeep.commands.analyze_cmd import run_analysis
from evalkeep.commands.dataset_cmd import build_dataset
from evalkeep.commands.detect_cmd import run_detection
from evalkeep.commands.discover_cmd import run_discovery
from evalkeep.commands.ingest_cmd import ingest_traces
from evalkeep.config import Project
from evalkeep.discovery import CLUSTERABLE
from evalkeep.errors import CommandError
from evalkeep.regression import ReviewStatus
from evalkeep.storage import TraceStore


@dataclass
class PipelineReport:
    """What one end-to-end pass produced, in the order it happened."""

    traces: int = 0
    already_known: int = 0
    invalid: int = 0
    failures: int = 0
    evidence: dict[str, int] = field(default_factory=dict)
    analyzed: int = 0
    described: bool = False
    families: int = 0
    representatives: int = 0
    drafts: int = 0
    ready: int = 0
    needs_expectation: int = 0
    pending_review: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def found_nothing(self) -> bool:
        return self.failures == 0


def from_traces(
    path: Path,
    *,
    project_root: Path = Path(),
    adapter_name: str = DEFAULT_ADAPTER,
    limit: int | None = None,
) -> PipelineReport:
    """Ingest a trace file and carry it as far as the review queue."""
    project = Project.load(project_root.expanduser().resolve())
    report = PipelineReport()

    ingested = ingest_traces(path, project_root=project_root, adapter_name=adapter_name)
    report.traces = ingested.stored
    report.already_known = ingested.already_stored + ingested.content_duplicates
    report.invalid = ingested.invalid
    if ingested.identifier_risks:
        report.notes.append(
            f"{ingested.identifier_risks} trace(s) have identifiers that look like "
            "they carry personal data; see redaction.pseudonymize_identifiers."
        )

    if report.traces == 0 and report.already_known == 0:
        raise CommandError(
            "No traces were stored, so there is nothing to work with.",
            hint="Check the file, or pass --format if it is not Evalkeep's own JSONL.",
        )

    detected = run_detection(project_root=project_root)
    report.failures = detected.failures
    report.evidence = {kind.value: count for kind, count in detected.by_kind.items()}
    if report.found_nothing:
        return report

    # Describing failures is what lets them be grouped by *what they are*. With
    # no provider configured that is a person's job, so the run continues on
    # observed behaviour and says so rather than stopping.
    if project.config.analyzer.provider != "manual":
        analysis = run_analysis(project_root=project_root)
        report.analyzed = analysis.analyzed + analysis.from_cache + analysis.skipped
        if analysis.failed:
            # Configured is not the same as working. Saying which provider
            # refused, and why, beats a later error about nothing to group.
            report.notes.append(
                f"{analysis.failed} failure(s) could not be described by "
                f"{analysis.analyzer}: {analysis.errors[0][1] if analysis.errors else 'unknown'}"
            )

    # Ask the store, not the analyzer: descriptions also arrive from
    # 'evalkeep failures label', and a run after that should not be told it has
    # none. Anything still undescribed is grouped on observed behaviour rather
    # than dropped, which is the only way a first run reaches the review queue.
    undescribed = _undescribed(project)
    report.described = undescribed == 0
    if undescribed:
        report.notes.append(
            f"{undescribed} failure(s) were grouped by what was observed, not by what "
            "they are. Describe them with 'evalkeep failures label', or set a working "
            "analyzer.provider in evalkeep.yaml, and re-run for tighter families."
        )

    discovered = run_discovery(
        project_root=project_root,
        analyze=False,
        force=True,
        group_undescribed=undescribed > 0,
    )
    report.families = discovered.clusters
    report.representatives = discovered.representatives

    # Regenerate: everything up to the review gate is derived, so a second run
    # after describing a failure should reflect it rather than leave the first
    # run's draft in place. Reviewed tests are kept regardless -- rebuilding a
    # draft is not the same as undoing a decision.
    built = build_dataset(project_root=project_root, limit=limit, regenerate=True)
    report.drafts = built.created + built.regenerated + built.skipped

    with TraceStore.open(project.database_path) as store:
        drafts = store.tests.list(status=ReviewStatus.DRAFT, limit=10_000)
        report.pending_review = len(drafts)
        # Enough evidence means the draft carries a check that fails when the
        # bug returns -- which is what a regression test is for, and what
        # drives the compare numbers even when all it does is forbid the
        # action that was observed. `needs_expectation` then qualifies that
        # same set rather than naming a separate one: those drafts cannot yet
        # confirm the agent did the right thing instead, only that it avoided
        # the one wrong thing. Reported as a remainder, it read as six
        # findings from three failures.
        report.ready = sum(1 for test in drafts if test.deterministic_expectations)
        report.needs_expectation = sum(1 for test in drafts if not test.has_positive_expectation)

    return report


def _undescribed(project: Project) -> int:
    """How many clusterable failures nobody has said anything about yet."""
    with TraceStore.open(project.database_path) as store:
        return sum(
            1
            for failure in store.failures.iter_all()
            if failure.status in CLUSTERABLE
            and store.failures.get_analysis(failure.failure_id) is None
        )

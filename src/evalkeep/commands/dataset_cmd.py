"""``evalkeep dataset`` -- generate and inspect regression-test drafts.

By default only cluster *representatives* get a test. That is the entire point
of clustering: a suite wants one good test per failure family, not forty copies
of the same bug. ``--all`` overrides it when you want coverage of every failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from evalkeep.clusters import Cluster
from evalkeep.config import Project
from evalkeep.errors import CommandError
from evalkeep.failures import Failure, FailureStatus
from evalkeep.generation import GENERATOR_VERSION, build_test
from evalkeep.regression import RegressionTest, ReviewStatus
from evalkeep.storage import TraceStore


@dataclass
class BuildReport:
    """What one ``dataset build`` did."""

    considered: int = 0
    created: int = 0
    regenerated: int = 0
    skipped: int = 0
    reviewed_kept: int = 0
    unanalyzed: int = 0
    needs_expectation: int = 0
    contradictions: int = 0
    generator_version: int = GENERATOR_VERSION
    warnings: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.regenerated)


@dataclass(frozen=True)
class DatasetListing:
    tests: list[RegressionTest]
    total: int
    counts: dict[ReviewStatus, int]


def build_dataset(
    *,
    project_root: Path = Path(),
    representatives_only: bool = True,
    regenerate: bool = False,
    limit: int | None = None,
) -> BuildReport:
    """Generate pending drafts for the failures worth covering."""
    project = Project.load(project_root.expanduser().resolve())
    report = BuildReport()

    with TraceStore.open(project.database_path) as store:
        clusters = store.clusters.list(include_dismissed=False)
        if representatives_only and not clusters:
            raise CommandError(
                "No clusters to draw representatives from.",
                hint="Run 'evalkeep discover' first, or pass --all to cover every failure.",
            )

        targets = _targets(store, clusters, representatives_only=representatives_only)
        if not targets:
            raise CommandError(
                "Nothing to generate tests from.",
                hint="Run 'evalkeep detect' and 'evalkeep analyze' first.",
            )

        for failure, cluster, roles in targets:
            if limit is not None and report.created + report.regenerated >= limit:
                break
            report.considered += 1

            existing = store.tests.get_by_failure(failure.failure_id)
            if existing is not None and not regenerate:
                report.skipped += 1
                continue
            if existing is not None and existing.reviewed:
                # Regeneration rewrites a draft; it does not undo a review.
                report.reviewed_kept += 1
                continue

            analysis = store.failures.get_analysis(failure.failure_id)
            if analysis is None:
                report.unanalyzed += 1
                continue
            stored = store.get(failure.trace_id)
            if stored is None:  # pragma: no cover - the foreign key prevents this
                continue

            test = build_test(
                stored.trace,
                failure,
                analysis,
                cluster_id=cluster.cluster_id if cluster else None,
                cluster_label=cluster.label if cluster else None,
                representative_roles=roles,
            )
            if existing is not None:
                test.created_at = existing.created_at
                report.regenerated += 1
            else:
                report.created += 1

            if not test.has_positive_expectation:
                report.needs_expectation += 1
            report.contradictions += len(test.contradictions)
            report.warnings.extend((test.test_id, warning) for warning in test.warnings)

            store.tests.save(test)

    return report


def list_tests(
    *,
    project_root: Path = Path(),
    status: ReviewStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DatasetListing:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        return DatasetListing(
            tests=store.tests.list(status=status, limit=limit, offset=offset),
            total=store.tests.count(status=status),
            counts=store.tests.counts_by_status(),
        )


def show_test(identifier: str, *, project_root: Path = Path()) -> RegressionTest:
    """Look one up by test ID, failure ID or the trace it came from."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        cleaned = identifier.strip()
        test = store.tests.get(cleaned) or store.tests.get_by_failure(cleaned)
        if test is None:
            failure = store.failures.get_by_trace(cleaned)
            if failure is not None:
                test = store.tests.get_by_failure(failure.failure_id)
        if test is None:
            raise CommandError(
                f"No regression test matching {cleaned!r}.",
                hint="Run 'evalkeep dataset list', or 'evalkeep dataset build' first.",
            )
        return test


def _targets(
    store: TraceStore, clusters: list[Cluster], *, representatives_only: bool
) -> list[tuple[Failure, Cluster | None, list[str]]]:
    """Which failures get a test, and what cluster context to record with each."""
    context: dict[str, tuple[Cluster, list[str]]] = {}
    for cluster in clusters:
        for member in cluster.members:
            context[member.failure_id] = (
                cluster,
                [role.value for role in member.roles],
            )

    targets: list[tuple[Failure, Cluster | None, list[str]]] = []
    for failure in store.failures.iter_all():
        if failure.status is FailureStatus.DISMISSED:
            continue
        entry = context.get(failure.failure_id)
        if representatives_only:
            if entry is None or not entry[1]:
                continue
            targets.append((failure, entry[0], entry[1]))
        elif entry is None:
            targets.append((failure, None, []))
        else:
            targets.append((failure, entry[0], entry[1]))
    return targets

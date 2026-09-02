"""``evalkeep discover`` and ``evalkeep clusters`` -- group failures and edit groups.

Editing a clustering is a first-class operation, not an escape hatch. A distance
metric over short summaries will always split a family that a person can see is
one, and merge two that a person can see are not; merge, split, rename and
dismiss are how that judgement gets recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evalkeep.analysis import FailureAnalysis
from evalkeep.cache import EmbeddingCache
from evalkeep.clustering import ClusterInput, assign_roles, derive_label
from evalkeep.clusters import Cluster, ClusterMember
from evalkeep.commands.analyze_cmd import run_analysis
from evalkeep.commands.detect_cmd import default_reviewer
from evalkeep.config import Project
from evalkeep.discovery import ClusterEditsWouldBeLost, DiscoveryReport, discover
from evalkeep.embeddings import get_embedder
from evalkeep.errors import CommandError
from evalkeep.storage import TraceStore

EDITS_WOULD_BE_LOST_HINT = "Re-run with --force to discard them, or leave the clustering as it is."


@dataclass(frozen=True)
class ClusterDetail:
    """A cluster together with what each member is."""

    cluster: Cluster
    analyses: dict[str, FailureAnalysis]
    trace_ids: dict[str, str]


def run_discovery(
    *,
    project_root: Path = Path(),
    analyze: bool = True,
    force: bool = False,
    use_cache: bool = True,
) -> DiscoveryReport:
    """Analyze (when a provider is configured), embed, cluster and select."""
    project = Project.load(project_root.expanduser().resolve())

    if analyze and project.config.analyzer.provider != "manual":
        run_analysis(project_root=project_root, use_cache=use_cache)

    embedder = get_embedder(project.config.clustering)
    cache = EmbeddingCache(project.subdir("cache"), enabled=use_cache)

    with TraceStore.open(project.database_path) as store:
        if store.failures.count() == 0:
            raise CommandError(
                "No failure candidates to group.", hint="Run 'evalkeep detect' first."
            )
        try:
            report = discover(store, embedder, cache, project.config.clustering, force=force)
        except ClusterEditsWouldBeLost as exc:
            names = ", ".join(cluster.label for cluster in exc.clusters)
            raise CommandError(
                f"Re-clustering would discard reviewer edits on {len(exc.clusters)} "
                f"cluster(s): {names}.",
                hint=EDITS_WOULD_BE_LOST_HINT,
            ) from exc

        if report.clusters == 0:
            raise CommandError(
                "No analyzed failures to group.",
                hint="Run 'evalkeep analyze', or label failures by hand with "
                "'evalkeep failures label'.",
            )
        return report


def list_clusters(*, project_root: Path = Path(), include_dismissed: bool = True) -> list[Cluster]:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        return store.clusters.list(include_dismissed=include_dismissed)


def show_cluster(cluster_id: str, *, project_root: Path = Path()) -> ClusterDetail:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        cluster = _resolve(store, cluster_id)
        return _detail(store, cluster)


def rename_cluster(
    cluster_id: str, label: str, *, project_root: Path = Path(), reviewer: str | None = None
) -> Cluster:
    cleaned = label.strip()
    if not cleaned:
        raise CommandError("A cluster label cannot be empty.")

    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        cluster = _resolve(store, cluster_id)
        cluster.label = cleaned
        cluster.labelled_by = reviewer or default_reviewer()
        store.clusters.save(cluster)
        return cluster


def dismiss_cluster(
    cluster_id: str, *, project_root: Path = Path(), reviewer: str | None = None
) -> Cluster:
    """Mark a family as not worth regression coverage. Kept, not deleted."""
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        cluster = _resolve(store, cluster_id)
        cluster.dismissed = True
        cluster.labelled_by = cluster.labelled_by or reviewer or default_reviewer()
        store.clusters.save(cluster)
        return cluster


def restore_cluster(cluster_id: str, *, project_root: Path = Path()) -> Cluster:
    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        cluster = _resolve(store, cluster_id)
        cluster.dismissed = False
        store.clusters.save(cluster)
        return cluster


def merge_clusters(
    cluster_ids: list[str], *, project_root: Path = Path(), reviewer: str | None = None
) -> Cluster:
    """Combine several families into one the reviewer says is really one."""
    if len(cluster_ids) < 2:
        raise CommandError("Merging needs at least two clusters.")

    project = Project.load(project_root.expanduser().resolve())
    with TraceStore.open(project.database_path) as store:
        clusters = [_resolve(store, cluster_id) for cluster_id in cluster_ids]
        if len({cluster.cluster_id for cluster in clusters}) != len(clusters):
            raise CommandError("Each cluster can only be merged once.")

        members = [member for cluster in clusters for member in cluster.members]
        named = next((cluster for cluster in clusters if cluster.labelled_by), None)
        merged = Cluster.build(
            label=named.label if named else _combined_label(store, members),
            members=_rerank(store, members),
        )
        # A merge is a judgement, so the result counts as reviewer-edited and
        # will not be silently rebuilt away by the next `discover`.
        merged.labelled_by = named.labelled_by if named else (reviewer or default_reviewer())

        for cluster in clusters:
            store.clusters.delete(cluster.cluster_id)
        store.clusters.save(merged)
        return merged


def split_cluster(
    cluster_id: str,
    failure_ids: list[str],
    *,
    project_root: Path = Path(),
    reviewer: str | None = None,
) -> tuple[Cluster, Cluster]:
    """Move some members out of a family into a new one of their own."""
    if not failure_ids:
        raise CommandError("Splitting needs at least one failure to move out.")

    project = Project.load(project_root.expanduser().resolve())
    who = reviewer or default_reviewer()
    with TraceStore.open(project.database_path) as store:
        cluster = _resolve(store, cluster_id)
        wanted = {identifier.strip() for identifier in failure_ids}
        moving = [member for member in cluster.members if member.failure_id in wanted]
        staying = [member for member in cluster.members if member.failure_id not in wanted]

        missing = wanted - {member.failure_id for member in moving}
        if missing:
            raise CommandError(
                f"Not in cluster {cluster.cluster_id}: {', '.join(sorted(missing))}.",
                hint="Run 'evalkeep clusters show <id>' to see its members.",
            )
        if not staying:
            raise CommandError(
                "Splitting out every member would leave the cluster empty.",
                hint="Nothing to do: the cluster already contains exactly these failures.",
            )

        store.clusters.delete(cluster.cluster_id)
        remainder = Cluster.build(
            label=_combined_label(store, staying), members=_rerank(store, staying)
        )
        remainder.labelled_by = who
        extracted = Cluster.build(
            label=_combined_label(store, moving), members=_rerank(store, moving)
        )
        extracted.labelled_by = who
        store.clusters.save(remainder)
        store.clusters.save(extracted)
        return remainder, extracted


def _rerank(store: TraceStore, members: list[ClusterMember]) -> list[ClusterMember]:
    """Re-mark representatives after an edit changed the membership.

    Distances to the old centroid are kept: recomputing one would need the
    vectors, and the ordering they induce is what the roles depend on. The roles
    themselves are re-derived through the same function the algorithm uses, so
    an edited cluster carries no stale marks and no missing ones.
    """
    ranked = sorted(members, key=lambda member: (member.distance, member.failure_id))
    fresh = [
        ClusterMember(failure_id=member.failure_id, distance=member.distance) for member in ranked
    ]
    severities = {}
    for member in fresh:
        analysis = store.failures.get_analysis(member.failure_id)
        if analysis is not None:
            severities[member.failure_id] = analysis.severity
    assign_roles(fresh, severities)
    return fresh


def _combined_label(store: TraceStore, members: list[ClusterMember]) -> str:
    inputs: list[ClusterInput] = []
    for member in members:
        analysis = store.failures.get_analysis(member.failure_id)
        if analysis is not None:
            inputs.append(ClusterInput(failure_id=member.failure_id, analysis=analysis))
    return derive_label(inputs) if inputs else "unlabelled"


def _detail(store: TraceStore, cluster: Cluster) -> ClusterDetail:
    analyses: dict[str, FailureAnalysis] = {}
    trace_ids: dict[str, str] = {}
    for member in cluster.members:
        analysis = store.failures.get_analysis(member.failure_id)
        if analysis is not None:
            analyses[member.failure_id] = analysis
        failure = store.failures.get(member.failure_id)
        if failure is not None:
            trace_ids[member.failure_id] = failure.trace_id
    return ClusterDetail(cluster=cluster, analyses=analyses, trace_ids=trace_ids)


def _resolve(store: TraceStore, identifier: str) -> Cluster:
    """Accept a cluster ID, or the ID of any failure or trace inside it."""
    cleaned = identifier.strip()
    cluster = store.clusters.get(cleaned)
    if cluster is not None:
        return cluster

    cluster = store.clusters.find_by_failure(cleaned)
    if cluster is not None:
        return cluster

    failure = store.failures.get_by_trace(cleaned)
    if failure is not None:
        cluster = store.clusters.find_by_failure(failure.failure_id)
        if cluster is not None:
            return cluster

    raise CommandError(
        f"No cluster matching {cleaned!r}.",
        hint="Run 'evalkeep clusters list', or 'evalkeep discover' first.",
    )

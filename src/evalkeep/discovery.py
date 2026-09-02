"""The discovery pass: embed analyzed failures, group them, pick representatives.

Discovery is derived from analysis, so it can always be recomputed. What cannot
be recomputed is what a reviewer did to the result -- renaming a family,
dismissing one, merging two that the distance metric kept apart. Those edits are
preserved where the group survives unchanged, and re-clustering refuses to
discard them without an explicit ``--force``.

Cluster identity is derived from membership, which is what makes that possible:
re-running on unchanged data produces the same cluster IDs, so labels stay
attached to the families they were written for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from evalkeep.cache import EmbeddingCache, embedding_key
from evalkeep.clustering import ClusterInput, build_clusters, clustering_parameters
from evalkeep.clusters import Cluster, ClusteringRun
from evalkeep.config import ClusteringConfig
from evalkeep.embeddings import EmbeddingProvider
from evalkeep.failures import FailureStatus
from evalkeep.storage import TraceStore

#: Dismissed failures are not part of any family worth covering.
CLUSTERABLE = (FailureStatus.CANDIDATE, FailureStatus.CONFIRMED)


@dataclass
class DiscoveryReport:
    """What one discovery pass did."""

    embedder: str
    considered: int = 0
    unanalyzed: int = 0
    clusters: int = 0
    singletons: int = 0
    representatives: int = 0
    embedded: int = 0
    from_cache: int = 0
    kept_labels: int = 0
    discarded_edits: int = 0
    largest: int = 0
    parameters: dict[str, object] = field(default_factory=dict)


class ClusterEditsWouldBeLost(Exception):
    """Re-clustering would discard reviewer edits that cannot be carried over."""

    def __init__(self, clusters: list[Cluster]) -> None:
        self.clusters = clusters
        super().__init__(f"{len(clusters)} edited clusters would be discarded")


def discover(
    store: TraceStore,
    embedder: EmbeddingProvider,
    cache: EmbeddingCache,
    config: ClusteringConfig,
    *,
    force: bool = False,
) -> DiscoveryReport:
    """Cluster analyzed failures and select representatives."""
    report = DiscoveryReport(embedder=embedder.identity)
    report.parameters = dict(clustering_parameters(config))

    inputs = _gather(store, report)
    vectors = _embed(inputs, embedder, cache, report)
    clusters = build_clusters(inputs, vectors, config)

    _carry_over_edits(store, clusters, report, force=force)

    run = ClusteringRun(
        run_id=uuid.uuid4().hex,
        embedder=embedder.identity,
        dimensions=embedder.dimensions,
        parameters=report.parameters,
        failures=len(inputs),
    )
    store.clusters.replace_run(run, clusters)

    report.clusters = len(clusters)
    report.singletons = sum(1 for cluster in clusters if cluster.size == 1)
    report.representatives = sum(len(cluster.representatives) for cluster in clusters)
    report.largest = max((cluster.size for cluster in clusters), default=0)
    return report


def _gather(store: TraceStore, report: DiscoveryReport) -> list[ClusterInput]:
    """Every analyzed, undismissed failure. Unanalyzed ones are reported, not guessed at."""
    inputs: list[ClusterInput] = []
    for failure in store.failures.iter_all():
        if failure.status not in CLUSTERABLE:
            continue
        report.considered += 1
        analysis = store.failures.get_analysis(failure.failure_id)
        if analysis is None:
            report.unanalyzed += 1
            continue
        inputs.append(ClusterInput(failure_id=failure.failure_id, analysis=analysis))
    # A stable input order keeps the clustering reproducible.
    inputs.sort(key=lambda item: item.failure_id)
    return inputs


def _embed(
    inputs: list[ClusterInput],
    embedder: EmbeddingProvider,
    cache: EmbeddingCache,
    report: DiscoveryReport,
) -> list[list[float]]:
    """Embed, reusing cached vectors for text that has not changed."""
    vectors: list[list[float] | None] = []
    pending: list[int] = []

    for index, item in enumerate(inputs):
        cached = cache.get_vector(embedding_key(item.text, embedder.identity))
        vectors.append(cached)
        if cached is None:
            pending.append(index)

    if pending:
        fresh = embedder.embed([inputs[index].text for index in pending])
        for index, vector in zip(pending, fresh, strict=True):
            vectors[index] = vector
            cache.put_vector(embedding_key(inputs[index].text, embedder.identity), vector)

    report.embedded = len(pending)
    report.from_cache = len(inputs) - len(pending)
    return [vector for vector in vectors if vector is not None]


def _carry_over_edits(
    store: TraceStore,
    clusters: list[Cluster],
    report: DiscoveryReport,
    *,
    force: bool,
) -> None:
    """Reattach reviewer edits to families that came back unchanged.

    A cluster ID is a function of its members, so a family whose membership did
    not change keeps its ID -- and with it, its name and its dismissal. A family
    whose membership *did* change is genuinely a different group, and its edits
    cannot be carried over honestly. Rather than dropping them quietly, the pass
    refuses until the caller says to proceed.
    """
    existing = {cluster.cluster_id: cluster for cluster in store.clusters.list()}
    if not existing:
        return

    rebuilt = {cluster.cluster_id: cluster for cluster in clusters}
    for cluster_id, previous in existing.items():
        if not previous.edited:
            continue
        survivor = rebuilt.get(cluster_id)
        if survivor is None:
            report.discarded_edits += 1
            continue
        survivor.label = previous.label
        survivor.labelled_by = previous.labelled_by
        survivor.dismissed = previous.dismissed
        report.kept_labels += 1

    if report.discarded_edits and not force:
        lost = [
            cluster
            for cluster_id, cluster in existing.items()
            if cluster.edited and cluster_id not in rebuilt
        ]
        raise ClusterEditsWouldBeLost(lost)

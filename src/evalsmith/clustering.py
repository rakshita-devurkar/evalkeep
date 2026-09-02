"""Grouping failures into families, and choosing who represents each family.

The algorithm is average-linkage agglomerative clustering over cosine distance,
cut at a configured distance threshold. It was chosen for three properties that
matter more here than raw clustering quality:

* **It is deterministic.** No initialisation, no random restarts: the same
  vectors and the same threshold always produce the same grouping. A seed is
  still recorded with every run, so swapping in a randomized algorithm later
  cannot quietly break reproducibility.
* **It does not need the number of clusters up front.** Nobody knows how many
  failure families a trace file contains.
* **The threshold means something.** It is a cosine distance, so it can be
  explained, tuned and written down, rather than being an opaque knob.

Average linkage rather than single linkage on purpose: single linkage chains, so
one ambiguous failure sitting between two families would merge both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.cluster import AgglomerativeClustering

from evalsmith.analysis import SEVERITY_ORDER, FailureAnalysis, Severity
from evalsmith.clusters import Cluster, ClusterMember, MemberRole
from evalsmith.config import ClusteringConfig


@dataclass(frozen=True)
class ClusterInput:
    """One failure, as clustering sees it."""

    failure_id: str
    analysis: FailureAnalysis

    @property
    def text(self) -> str:
        return cluster_text(self.analysis)


def cluster_text(analysis: FailureAnalysis) -> str:
    """The text representation a failure is embedded from.

    The structured labels lead, then the summary. Including the type and
    component means two failures sharing a family agree on those tokens before
    a single word of prose is compared, which is what keeps a well-labelled
    dataset grouping tightly even with a purely lexical embedder.
    """
    return f"{analysis.failure_type.value} in {analysis.component.value}: {analysis.summary}"


def clustering_parameters(config: ClusteringConfig) -> dict[str, Any]:
    """Everything needed to reproduce a grouping, stored with the run."""
    return {
        "algorithm": config.algorithm,
        "metric": config.metric,
        "linkage": config.linkage,
        "threshold": config.threshold,
        "seed": config.seed,
        "embedder": config.embedder,
        "dimensions": config.dimensions,
    }


def build_clusters(
    inputs: list[ClusterInput], vectors: list[list[float]], config: ClusteringConfig
) -> list[Cluster]:
    """Group ``inputs`` and choose representatives for each group."""
    if not inputs:
        return []
    if len(inputs) != len(vectors):  # pragma: no cover - callers pair these
        raise ValueError("inputs and vectors must be the same length")

    matrix = np.asarray(vectors, dtype=np.float64)
    assignments = _assign(matrix, config)

    clusters: list[Cluster] = []
    for group in sorted(set(assignments)):
        indices = [i for i, label in enumerate(assignments) if label == group]
        clusters.append(_build_one([inputs[i] for i in indices], matrix[indices]))

    # Largest first, then by ID: a stable order for humans and for tests.
    clusters.sort(key=lambda cluster: (-cluster.size, cluster.cluster_id))
    return clusters


def _assign(matrix: np.ndarray[Any, Any], config: ClusteringConfig) -> list[int]:
    if len(matrix) == 1:
        return [0]
    model = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=config.threshold,
        metric=config.metric,
        linkage=config.linkage,
    )
    return [int(label) for label in model.fit_predict(matrix)]


def _build_one(members: list[ClusterInput], vectors: np.ndarray[Any, Any]) -> Cluster:
    centroid = _centroid(vectors)
    # Vectors are L2-normalized, so a dot product is the cosine similarity.
    # Clamped because floating point can push a dot product just past 1.0,
    # which would surface as a distance of -0.00 in the member listing.
    distances = [min(2.0, max(0.0, float(1.0 - np.dot(vector, centroid)))) for vector in vectors]

    cluster_members = [
        ClusterMember(failure_id=item.failure_id, distance=distance)
        for item, distance in zip(members, distances, strict=True)
    ]
    assign_roles(
        cluster_members,
        {item.failure_id: item.analysis.severity for item in members},
    )
    return Cluster.build(label=derive_label(members), members=cluster_members)


def _centroid(vectors: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    centroid: np.ndarray[Any, Any] = vectors.mean(axis=0)
    magnitude = float(np.linalg.norm(centroid))
    return centroid / magnitude if magnitude else centroid


def assign_roles(
    members: list[ClusterMember], severities: dict[str, Severity]
) -> list[ClusterMember]:
    """Mark the central, boundary and worst-case members, in place.

    The three roles answer three different questions -- what this family
    typically looks like, how far it stretches, and how bad it gets -- so one
    failure can hold several. In a cluster of one it holds all three; roles
    accumulate on a member rather than partitioning the cluster.

    Shared with the editing commands on purpose: a cluster that a reviewer
    merged or split must end up with the same kind of representatives as one
    the algorithm produced, or the selection would silently differ depending on
    how the cluster came to exist.
    """
    for member in members:
        member.roles.clear()

    order = sorted(range(len(members)), key=lambda i: (members[i].distance, i))
    members[order[0]].roles.append(MemberRole.CENTRAL)
    if len(members) > 1:
        members[order[-1]].roles.append(MemberRole.BOUNDARY)

    if severities:
        worst = min(
            range(len(members)),
            key=lambda i: (
                _severity_rank(severities, members[i].failure_id),
                members[i].distance,
                i,
            ),
        )
        if MemberRole.HIGH_SEVERITY not in members[worst].roles:
            members[worst].roles.append(MemberRole.HIGH_SEVERITY)
    return members


def _severity_rank(severities: dict[str, Severity], failure_id: str) -> int:
    severity = severities.get(failure_id)
    # An unlabelled member cannot be the worst case; sort it last.
    return SEVERITY_ORDER.index(severity) if severity is not None else len(SEVERITY_ORDER)


def derive_label(inputs: list[ClusterInput]) -> str:
    """Name a family after what its members have in common.

    Derived rather than generated: it needs no provider, it is reproducible, and
    a reviewer can rename it. Guide 8G deliberately keeps this label out of test
    IDs for exactly that reason -- it is mutable.
    """
    types = _most_common(item.analysis.failure_type.value for item in inputs)
    components = _most_common(item.analysis.component.value for item in inputs)
    return f"{types} in {components}"


def _most_common(values: Any) -> str:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    # Ties break alphabetically so the label is a function of the members alone.
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

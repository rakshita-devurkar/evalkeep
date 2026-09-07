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

import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from evalkeep.analysis import SEVERITY_ORDER, FailureAnalysis, Severity
from evalkeep.clusters import Cluster, ClusterMember, MemberRole
from evalkeep.config import ClusteringConfig
from evalkeep.detectors import SignalKind
from evalkeep.errors import CommandError


@dataclass(frozen=True)
class ClusterInput:
    """One failure, as clustering sees it.

    A failure is normally grouped by its *description* -- the structured
    analysis someone or something wrote for it. Before anyone has described
    them, it can still be grouped by what was *observed*: which tools ran and
    what the evidence said. That is weaker, and the difference is tracked here
    rather than hidden, so a report can say which it did.
    """

    failure_id: str
    text: str
    severity: Severity | None = None
    failure_type: str | None = None
    component: str | None = None
    #: The tools this failure used, for naming a family nobody has described.
    behaviour: str | None = None
    #: False when this was grouped by observed behaviour rather than a description.
    described: bool = True

    @classmethod
    def from_analysis(cls, failure_id: str, analysis: FailureAnalysis) -> ClusterInput:
        return cls(
            failure_id=failure_id,
            text=cluster_text(analysis),
            severity=analysis.severity,
            failure_type=analysis.failure_type.value,
            component=analysis.component.value,
        )

    @classmethod
    def from_observation(
        cls, failure_id: str, text: str, *, behaviour: str | None = None
    ) -> ClusterInput:
        return cls(failure_id=failure_id, text=text, behaviour=behaviour, described=False)


def observation_text(tools: list[str], kinds: list[str], evidence: list[str]) -> str:
    """What a failure looks like before anyone has described it.

    Built from what the agent *did* -- which tools ran, what kind of evidence
    caught it -- and only then from the evidence's own words. Deliberately not
    from the request: two customers asking the same thing in different words are
    the same failure, and their phrasing would scatter them.

    The tool list is repeated because in a bag-of-words representation
    repetition *is* weight, and behaviour is the reliable signal here. Two
    reports of one bug are worded differently while the calls the agent made
    stay the same, so letting the wording dominate loses the family. Sublinear
    term weighting damps the repetition, so this is a nudge rather than a
    override.

    Boilerplate evidence is dropped: "explicitly marked as failed" appears on
    every explicit failure and so distinguishes none of them.
    """
    listed = ", ".join(sorted(set(tools)))
    informative = [line for line in evidence if line and not _boilerplate(line)]
    weighted = " ".join([listed] * 3) if listed else ""
    return f"{weighted} | {' '.join(sorted(set(kinds)))} | {' '.join(informative)}".strip(" |")


_BOILERPLATE = (
    "explicitly marked as failed",
    "explicitly marked as errored",
    "feedback was rated negative",
    "reported a failure",
)


def _boilerplate(line: str) -> bool:
    lowered = line.lower()
    return any(phrase in lowered for phrase in _BOILERPLATE)


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

    # A described failure is embedded from someone's account of what went wrong;
    # an undescribed one from the tools it called and the evidence that caught
    # it. Those are different kinds of text, so their distances sit on different
    # scales and no single threshold cuts both correctly -- and a distance
    # measured *between* the two populations does not mean anything at all.
    # So each is clustered against its own kind, at its own threshold, and the
    # groups are concatenated. Two records of one bug, one described and one
    # not, therefore land in separate families: that is the honest answer, since
    # nothing yet establishes they are the same.
    clusters: list[Cluster] = []
    for described in (True, False):
        indices = [i for i, item in enumerate(inputs) if item.described is described]
        if not indices:
            continue
        threshold = config.threshold if described else config.undescribed_threshold
        assignments = _assign(matrix[indices], config, threshold)
        for group in sorted(set(assignments)):
            rows = [
                index for index, label in zip(indices, assignments, strict=True) if label == group
            ]
            clusters.append(_build_one([inputs[i] for i in rows], matrix[rows]))

    # Largest first, then by ID: a stable order for humans and for tests.
    clusters.sort(key=lambda cluster: (-cluster.size, cluster.cluster_id))
    return clusters


def _assign(matrix: np.ndarray[Any, Any], config: ClusteringConfig, threshold: float) -> list[int]:
    if config.linkage != "average" or config.metric != "cosine":
        raise CommandError(
            f"Only average linkage over cosine distance is implemented, not "
            f"{config.linkage!r} over {config.metric!r}.",
            hint="Change clustering.linkage and clustering.metric in evalkeep.yaml.",
        )
    return average_linkage(matrix, threshold)


def average_linkage(vectors: np.ndarray[Any, Any], threshold: float) -> list[int]:
    """Average-linkage agglomerative clustering over cosine distance.

    Implemented here rather than pulled from scikit-learn, which would bring
    scipy with it -- 119 MB of install for one class, in a tool whose clustering
    is a few hundred vectors of lexical similarity. Verified against
    scikit-learn's implementation across 240 random datasets before that
    dependency was removed, and roughly thirty times faster at two thousand
    points, because a general implementation does far more than this one case
    needs.

    Cluster distances are updated by the Lance-Williams rule, and each row keeps
    its nearest neighbour so a merge costs a scan rather than a full search.
    """
    count = len(vectors)
    if count <= 1:
        return [0] * count

    # Vectors are L2-normalized, so cosine distance is 1 - the dot product.
    distances = np.clip(1.0 - vectors @ vectors.T, 0.0, 2.0)
    np.fill_diagonal(distances, np.inf)

    sizes = np.ones(count)
    alive = np.ones(count, dtype=bool)
    members: list[list[int]] = [[index] for index in range(count)]
    nearest = distances.argmin(axis=1)
    best = distances[np.arange(count), nearest]

    for _ in range(count - 1):
        candidates = np.where(alive, best, np.inf)
        first = int(candidates.argmin())
        if candidates[first] >= threshold:
            break
        second = int(nearest[first])
        if first > second:
            first, second = second, first

        total = sizes[first] + sizes[second]
        merged = (sizes[first] * distances[first] + sizes[second] * distances[second]) / total
        distances[first] = merged
        distances[:, first] = merged
        distances[first, first] = np.inf
        distances[second, :] = np.inf
        distances[:, second] = np.inf

        alive[second] = False
        sizes[first] = total
        members[first] += members[second]

        # Only rows whose nearest neighbour was one of the merged pair can have
        # changed, so the rest of the cache stays valid.
        stale = np.where(alive & ((nearest == first) | (nearest == second)))[0]
        for row in np.union1d(stale, [first]):
            if alive[row]:
                nearest[row] = int(distances[row].argmin())
                best[row] = distances[row, nearest[row]]

    labels = [0] * count
    for label, index in enumerate(np.where(alive)[0]):
        for point in members[index]:
            labels[point] = label
    return labels


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
        {item.failure_id: item.severity for item in members if item.severity is not None},
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

    An undescribed family is named for what it did, and says so, because a label
    that reads like an analysis when nobody analysed anything would be a lie.
    """
    described = [item for item in inputs if item.described and item.failure_type]
    if not described:
        behaviours = [item.behaviour for item in inputs if item.behaviour]
        if behaviours:
            return f"undescribed: {_most_common(iter(behaviours))}"
        # No tool calls to name it after -- a ledger of outcomes rather than a
        # trace of actions. Fall back to the words its members share, because a
        # listing where every family reads "undescribed failures" tells a
        # reviewer nothing about which one to open.
        shared = _shared_terms(inputs)
        return f"undescribed: {shared}" if shared else "undescribed failures"
    types = _most_common(item.failure_type or "" for item in described)
    components = _most_common(item.component or "" for item in described)
    return f"{types} in {components}"


#: Present on every family, so they name none of them.
_UNINFORMATIVE = frozenset({kind.value for kind in SignalKind} | {"none", "null", "true", "false"})


def _shared_terms(inputs: list[ClusterInput], limit: int = 3) -> str:
    """The words most of a family has in common, as a name for it.

    Document frequency within the family, not raw count: a term repeated many
    times in one long member says nothing about the family, while a term
    present in most members is what they share. Terms carrying digits are
    dropped -- "150" and "600" are what makes two instances of one contract
    breach different, not what makes them the same.
    """
    documents = [frozenset(_terms(item.text)) for item in inputs]
    documents = [document for document in documents if document]
    if not documents:
        return ""
    counts: dict[str, int] = {}
    for document in documents:
        for term in document:
            counts[term] = counts.get(term, 0) + 1
    needed = max(1, (len(documents) + 1) // 2)
    ranked = sorted(
        ((term, n) for term, n in counts.items() if n >= needed),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return ", ".join(term for term, _ in ranked[:limit])


def _terms(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"\w+", text.lower())
        if len(token) > 1
        and not any(char.isdigit() for char in token)
        and token not in _UNINFORMATIVE
    ]


def _most_common(values: Any) -> str:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    # Ties break alphabetically so the label is a function of the members alone.
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

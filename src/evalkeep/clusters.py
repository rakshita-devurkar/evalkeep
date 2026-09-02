"""Clusters: families of failures, and the representatives chosen from them.

A cluster's identity is derived from its members, so an unchanged clustering
re-computes to the same IDs and any labels you gave it survive. Change the
membership and the identity changes -- which is honest, because it is no longer
the same group.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

CLUSTER_ID_PREFIX = "cl-"
_CLUSTER_ID_LENGTH = 12


class MemberRole(StrEnum):
    """Why a member was selected as a representative, if it was.

    The three roles answer three different questions about a failure family:
    what it typically looks like, how far it stretches, and how bad it gets.
    """

    #: Closest to the centroid: the most typical example of this family.
    CENTRAL = "central"
    #: Furthest from the centroid: the edge case the family still contains.
    BOUNDARY = "boundary"
    #: The worst outcome in the family, whether or not it is typical.
    HIGH_SEVERITY = "high_severity"


@dataclass
class ClusterMember:
    failure_id: str
    #: Cosine distance to the cluster centroid.
    distance: float
    roles: list[MemberRole] = field(default_factory=list)

    @property
    def representative(self) -> bool:
        return bool(self.roles)


def cluster_id_for(failure_ids: list[str]) -> str:
    """A stable ID derived from membership, so re-clustering is idempotent."""
    material = "\n".join(sorted(failure_ids))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_CLUSTER_ID_LENGTH]
    return f"{CLUSTER_ID_PREFIX}{digest}"


@dataclass
class Cluster:
    cluster_id: str
    label: str
    members: list[ClusterMember] = field(default_factory=list)
    #: Set when a person renamed it, so re-clustering knows not to relabel.
    labelled_by: str | None = None
    #: A family the reviewer decided is not worth regression coverage.
    dismissed: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def build(cls, label: str, members: list[ClusterMember]) -> Cluster:
        return cls(
            cluster_id=cluster_id_for([member.failure_id for member in members]),
            label=label,
            members=members,
        )

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def failure_ids(self) -> list[str]:
        return [member.failure_id for member in self.members]

    @property
    def representatives(self) -> list[ClusterMember]:
        return [member for member in self.members if member.representative]

    @property
    def edited(self) -> bool:
        """True once a person has changed something automation would overwrite."""
        return self.labelled_by is not None or self.dismissed


@dataclass
class ClusteringRun:
    """One clustering, with everything needed to reproduce it."""

    run_id: str
    embedder: str
    dimensions: int
    parameters: dict[str, Any]
    failures: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

"""Embeddings, clustering and representative selection (guide 8F)."""

from __future__ import annotations

import math
from typing import Any, ClassVar

import pytest

from evalkeep.analysis import Component, FailureAnalysis, FailureType, Severity
from evalkeep.cache import EmbeddingCache, embedding_key
from evalkeep.clustering import (
    ClusterInput,
    assign_roles,
    build_clusters,
    cluster_text,
    clustering_parameters,
    derive_label,
)
from evalkeep.clusters import ClusterMember, MemberRole, cluster_id_for
from evalkeep.config import ClusteringConfig
from evalkeep.embeddings import EmbeddingProvider, HashingEmbedder, get_embedder
from evalkeep.errors import CommandError

# Two members of one family, plus two clearly different failures.
WRONG_ORDER_A = "Refunded the oldest order instead of the newest order."
WRONG_ORDER_B = "Refunded an older order instead of the newest order."
OVER_ACTION = "Refunded every order on the account when the user asked for one."
WRONG_COUNTRY = "Stated the wrong shipping country for the customer."


def analysis(
    summary: str,
    *,
    failure_type: FailureType = FailureType.WRONG_TOOL_ARGUMENT,
    component: Component = Component.TOOL_ARGUMENTS,
    severity: Severity = Severity.HIGH,
) -> FailureAnalysis:
    return FailureAnalysis(
        failure_type=failure_type,
        component=component,
        severity=severity,
        summary=summary,
        analyzer="manual:test",
        prompt_version=0,
    )


def make_input(failure_id: str, summary: str, **kwargs: Any) -> ClusterInput:
    return ClusterInput(failure_id=failure_id, analysis=analysis(summary, **kwargs))


def cosine(one: list[float], two: list[float]) -> float:
    return sum(a * b for a, b in zip(one, two, strict=True))


class TestHashingEmbedder:
    def test_vectors_are_the_configured_width(self) -> None:
        embedder = HashingEmbedder(dimensions=128)
        (vector,) = embedder.embed(["anything"])
        assert len(vector) == 128
        assert embedder.dimensions == 128

    def test_vectors_are_normalized(self) -> None:
        (vector,) = HashingEmbedder().embed([WRONG_ORDER_A])
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)

    def test_the_same_text_always_embeds_identically(self) -> None:
        """``hash()`` is randomized per process; this must not be."""
        first = HashingEmbedder().embed([WRONG_ORDER_A])
        second = HashingEmbedder().embed([WRONG_ORDER_A])
        assert first == second

    def test_similar_text_is_closer_than_unrelated_text(self) -> None:
        near, far = _pair_distances()
        assert near < far

    def test_obvious_duplicates_are_very_close(self) -> None:
        one, two = HashingEmbedder().embed([WRONG_ORDER_A, WRONG_ORDER_A])
        assert math.isclose(cosine(one, two), 1.0, rel_tol=1e-9)

    def test_unrelated_text_is_far_apart(self) -> None:
        one, two = HashingEmbedder().embed([WRONG_ORDER_A, WRONG_COUNTRY])
        assert 1 - cosine(one, two) > 0.55

    def test_a_different_seed_is_a_different_space(self) -> None:
        assert HashingEmbedder(seed=0).embed([WRONG_ORDER_A]) != HashingEmbedder(seed=1).embed(
            [WRONG_ORDER_A]
        )

    def test_the_identity_names_the_space(self) -> None:
        assert HashingEmbedder(dimensions=256, seed=7).identity == "hashing:256:7"

    def test_word_order_matters(self) -> None:
        """Bigrams: the same words rearranged are not the same description."""
        one, two = HashingEmbedder().embed(
            ["refunded the oldest order", "ordered the oldest refund"]
        )
        assert cosine(one, two) < 0.999

    def test_empty_text_does_not_divide_by_zero(self) -> None:
        (vector,) = HashingEmbedder().embed([""])
        assert all(value == 0.0 for value in vector)

    def test_a_repeated_word_cannot_swamp_the_description(self) -> None:
        """Sublinear weighting: a quoted string repeated 40 times must not turn a
        summary into a different failure family."""
        noisy, clean, unrelated = HashingEmbedder().embed(
            ["refund " * 40 + WRONG_ORDER_A, WRONG_ORDER_A, WRONG_COUNTRY]
        )
        assert cosine(noisy, clean) > cosine(noisy, unrelated)
        assert cosine(noisy, clean) > cosine(clean, unrelated)

    def test_too_few_dimensions_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 8"):
            HashingEmbedder(dimensions=4)

    def test_it_satisfies_the_provider_protocol(self) -> None:
        assert isinstance(HashingEmbedder(), EmbeddingProvider)


class TestEmbedderRegistry:
    def test_the_configured_embedder_resolves(self) -> None:
        embedder = get_embedder(ClusteringConfig(dimensions=64, seed=3))
        assert embedder.identity == "hashing:64:3"

    def test_an_unknown_embedder_is_a_command_error(self) -> None:
        with pytest.raises(CommandError, match="Unknown embedding provider"):
            get_embedder(ClusteringConfig(embedder="word2vec"))

    def test_a_replacement_provider_needs_only_the_protocol(self) -> None:
        """Proof that 'replaceable' is real: a five-line provider is enough."""

        class Constant:
            name: ClassVar[str] = "constant"
            description: ClassVar[str] = "test double"

            @property
            def identity(self) -> str:
                return "constant:1"

            @property
            def dimensions(self) -> int:
                return 2

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[1.0, 0.0] for _ in texts]

        assert isinstance(Constant(), EmbeddingProvider)


class TestTextRepresentation:
    def test_the_structured_labels_lead(self) -> None:
        text = cluster_text(analysis(WRONG_ORDER_A))
        assert text.startswith("wrong_tool_argument in tool_arguments:")
        assert WRONG_ORDER_A in text

    def test_the_type_pulls_same_family_failures_together(self) -> None:
        embedder = HashingEmbedder()
        same_type = [
            cluster_text(analysis(WRONG_ORDER_A)),
            cluster_text(analysis(WRONG_ORDER_B)),
        ]
        cross_type = [
            cluster_text(analysis(WRONG_ORDER_A)),
            cluster_text(
                analysis(
                    WRONG_ORDER_B,
                    failure_type=FailureType.INCORRECT_ANSWER,
                    component=Component.RETRIEVAL,
                )
            ),
        ]
        near = cosine(*embedder.embed(same_type))
        far = cosine(*embedder.embed(cross_type))
        assert near > far


class TestClustering:
    def _cluster(self, inputs: list[ClusterInput], **overrides: Any) -> Any:
        config = ClusteringConfig(**overrides)
        vectors = get_embedder(config).embed([item.text for item in inputs])
        return build_clusters(inputs, vectors, config)

    def test_obvious_duplicates_group(self) -> None:
        clusters = self._cluster([make_input("f1", WRONG_ORDER_A), make_input("f2", WRONG_ORDER_B)])
        assert len(clusters) == 1
        assert set(clusters[0].failure_ids) == {"f1", "f2"}

    def test_unrelated_failures_separate(self) -> None:
        clusters = self._cluster(
            [
                make_input("f1", WRONG_ORDER_A),
                make_input(
                    "f2",
                    WRONG_COUNTRY,
                    failure_type=FailureType.INCORRECT_ANSWER,
                    component=Component.RETRIEVAL,
                ),
            ]
        )
        assert len(clusters) == 2

    def test_a_mixed_set_splits_into_families(self) -> None:
        clusters = self._cluster(
            [
                make_input("f1", WRONG_ORDER_A),
                make_input("f2", WRONG_ORDER_B),
                make_input(
                    "f3",
                    OVER_ACTION,
                    failure_type=FailureType.UNNECESSARY_ACTION,
                    component=Component.PLANNING,
                ),
            ]
        )
        assert len(clusters) == 2
        assert clusters[0].size == 2  # largest first
        assert clusters[1].failure_ids == ["f3"]

    def test_the_same_input_always_produces_the_same_grouping(self) -> None:
        inputs = [
            make_input("f1", WRONG_ORDER_A),
            make_input("f2", WRONG_ORDER_B),
            make_input("f3", OVER_ACTION),
        ]
        first = [c.cluster_id for c in self._cluster(inputs)]
        second = [c.cluster_id for c in self._cluster(inputs)]
        assert first == second

    def test_input_order_does_not_change_the_grouping(self) -> None:
        inputs = [
            make_input("f1", WRONG_ORDER_A),
            make_input("f2", WRONG_ORDER_B),
            make_input("f3", OVER_ACTION),
        ]
        forward = {c.cluster_id for c in self._cluster(inputs)}
        backward = {c.cluster_id for c in self._cluster(list(reversed(inputs)))}
        assert forward == backward

    def test_a_tighter_threshold_splits_more(self) -> None:
        inputs = [make_input("f1", WRONG_ORDER_A), make_input("f2", WRONG_ORDER_B)]
        assert len(self._cluster(inputs, threshold=0.05)) == 2
        assert len(self._cluster(inputs, threshold=0.55)) == 1

    def test_no_inputs_is_no_clusters(self) -> None:
        assert build_clusters([], [], ClusteringConfig()) == []

    def test_a_single_input_is_one_cluster(self) -> None:
        clusters = self._cluster([make_input("f1", WRONG_ORDER_A)])
        assert len(clusters) == 1
        assert clusters[0].size == 1

    def test_cluster_ids_come_from_membership(self) -> None:
        clusters = self._cluster([make_input("f1", WRONG_ORDER_A), make_input("f2", WRONG_ORDER_B)])
        assert clusters[0].cluster_id == cluster_id_for(["f2", "f1"])

    def test_the_parameters_describe_the_run(self) -> None:
        parameters = clustering_parameters(ClusteringConfig(threshold=0.4, seed=9))
        assert parameters["algorithm"] == "agglomerative"
        assert parameters["metric"] == "cosine"
        assert parameters["linkage"] == "average"
        assert parameters["threshold"] == 0.4
        assert parameters["seed"] == 9


class TestRepresentatives:
    def _cluster(self, inputs: list[ClusterInput]) -> Any:
        config = ClusteringConfig()
        vectors = get_embedder(config).embed([item.text for item in inputs])
        return build_clusters(inputs, vectors, config)[0]

    def test_a_lone_failure_holds_every_role(self) -> None:
        cluster = self._cluster([make_input("f1", WRONG_ORDER_A)])
        assert set(cluster.members[0].roles) == {
            MemberRole.CENTRAL,
            MemberRole.HIGH_SEVERITY,
        }

    def test_central_and_boundary_are_different_members(self) -> None:
        cluster = self._cluster(
            [
                make_input("f1", WRONG_ORDER_A),
                make_input("f2", WRONG_ORDER_B),
                make_input("f3", "Refunded the oldest order rather than the latest one."),
            ]
        )
        central = [m for m in cluster.members if MemberRole.CENTRAL in m.roles]
        boundary = [m for m in cluster.members if MemberRole.BOUNDARY in m.roles]
        assert len(central) == 1 and len(boundary) == 1
        assert central[0].failure_id != boundary[0].failure_id

    def test_the_boundary_is_the_furthest_member(self) -> None:
        cluster = self._cluster(
            [
                make_input("f1", WRONG_ORDER_A),
                make_input("f2", WRONG_ORDER_B),
                make_input("f3", "Refunded the oldest order rather than the latest one."),
            ]
        )
        furthest = max(cluster.members, key=lambda m: m.distance)
        assert MemberRole.BOUNDARY in furthest.roles

    def test_the_worst_severity_is_marked_even_when_not_typical(self) -> None:
        cluster = self._cluster(
            [
                make_input("f1", WRONG_ORDER_A, severity=Severity.LOW),
                make_input("f2", WRONG_ORDER_B, severity=Severity.LOW),
                make_input(
                    "f3",
                    "Refunded the oldest order rather than the latest one.",
                    severity=Severity.CRITICAL,
                ),
            ]
        )
        worst = next(m for m in cluster.members if m.failure_id == "f3")
        assert MemberRole.HIGH_SEVERITY in worst.roles

    def test_every_cluster_has_at_least_one_representative(self) -> None:
        config = ClusteringConfig()
        inputs = [
            make_input("f1", WRONG_ORDER_A),
            make_input("f2", OVER_ACTION, failure_type=FailureType.UNNECESSARY_ACTION),
        ]
        vectors = get_embedder(config).embed([item.text for item in inputs])
        for cluster in build_clusters(inputs, vectors, config):
            assert cluster.representatives


class TestRoleAssignment:
    def test_roles_are_recomputed_not_appended(self) -> None:
        """An edited cluster must not carry stale marks from before the edit."""
        members = [
            ClusterMember("f1", 0.1, roles=[MemberRole.BOUNDARY]),
            ClusterMember("f2", 0.9, roles=[MemberRole.CENTRAL]),
        ]
        assign_roles(members, {})
        assert members[0].roles == [MemberRole.CENTRAL]
        assert members[1].roles == [MemberRole.BOUNDARY]

    def test_without_severities_only_position_roles_are_marked(self) -> None:
        members = [ClusterMember("f1", 0.1), ClusterMember("f2", 0.9)]
        assign_roles(members, {})
        assert not any(MemberRole.HIGH_SEVERITY in m.roles for m in members)

    def test_an_unlabelled_member_cannot_be_the_worst_case(self) -> None:
        members = [ClusterMember("f1", 0.1), ClusterMember("f2", 0.2)]
        assign_roles(members, {"f2": Severity.CRITICAL})
        worst = next(m for m in members if MemberRole.HIGH_SEVERITY in m.roles)
        assert worst.failure_id == "f2"


class TestLabels:
    def test_a_label_names_what_members_share(self) -> None:
        label = derive_label([make_input("f1", WRONG_ORDER_A), make_input("f2", WRONG_ORDER_B)])
        assert label == "wrong_tool_argument in tool_arguments"

    def test_the_majority_wins(self) -> None:
        label = derive_label(
            [
                make_input("f1", WRONG_ORDER_A),
                make_input("f2", WRONG_ORDER_B),
                make_input("f3", OVER_ACTION, failure_type=FailureType.UNNECESSARY_ACTION),
            ]
        )
        assert label.startswith("wrong_tool_argument")

    def test_ties_break_deterministically(self) -> None:
        inputs = [
            make_input("f1", WRONG_ORDER_A, failure_type=FailureType.INCORRECT_ANSWER),
            make_input("f2", WRONG_ORDER_B, failure_type=FailureType.WRONG_TOOL_ARGUMENT),
        ]
        assert derive_label(inputs) == derive_label(list(reversed(inputs)))


class TestEmbeddingCache:
    def test_a_vector_round_trips(self, tmp_path: Any) -> None:
        cache = EmbeddingCache(tmp_path / "cache")
        cache.put_vector("k", [0.1, 0.2])
        assert cache.get_vector("k") == [0.1, 0.2]

    def test_a_missing_key_is_a_miss(self, tmp_path: Any) -> None:
        assert EmbeddingCache(tmp_path / "cache").get_vector("nope") is None

    def test_a_malformed_entry_is_a_miss(self, tmp_path: Any) -> None:
        cache = EmbeddingCache(tmp_path / "cache")
        cache.put("k", {"vector": "not a list"})
        assert cache.get_vector("k") is None

    def test_the_key_covers_the_text_and_the_space(self) -> None:
        base = embedding_key("some text", "hashing:512:0")
        assert base != embedding_key("other text", "hashing:512:0")
        assert base != embedding_key("some text", "hashing:512:1")
        assert base == embedding_key("some text", "hashing:512:0")


def _pair_distances() -> tuple[float, float]:
    embedder = HashingEmbedder()
    a, b, c = embedder.embed([WRONG_ORDER_A, WRONG_ORDER_B, WRONG_COUNTRY])
    return 1 - cosine(a, b), 1 - cosine(a, c)

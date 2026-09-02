"""Embedding providers and the registry the project configuration resolves against."""

from __future__ import annotations

from evalsmith.config import ClusteringConfig
from evalsmith.embeddings.base import EmbeddingProvider
from evalsmith.embeddings.hashing import HashingEmbedder
from evalsmith.errors import CommandError

KNOWN_EMBEDDERS: dict[str, str] = {HashingEmbedder.name: HashingEmbedder.description}


def get_embedder(config: ClusteringConfig) -> EmbeddingProvider:
    """Build the configured embedding provider.

    A hosted embedding model plugs in here: implement
    :class:`~evalsmith.embeddings.base.EmbeddingProvider`, give it an identity
    that names the model, and register it below. Nothing downstream changes --
    the cache and the stored clustering parameters key off ``identity``, so old
    and new vectors can never be silently mixed.
    """
    if config.embedder == HashingEmbedder.name:
        return HashingEmbedder(dimensions=config.dimensions, seed=config.seed)
    known = ", ".join(sorted(KNOWN_EMBEDDERS))
    raise CommandError(
        f"Unknown embedding provider {config.embedder!r}.",
        hint=f"Known providers: {known}.",
    )


__all__ = ["KNOWN_EMBEDDERS", "EmbeddingProvider", "HashingEmbedder", "get_embedder"]

"""The embedding provider contract.

Embeddings turn failure descriptions into vectors so that similar failures land
near each other. The interface is deliberately tiny -- one method over a batch
of strings -- so a hosted embedding model can replace the built-in one without
anything else in the pipeline changing.

``identity`` is part of every cache key and is stored with every clustering run.
It must change whenever the vectors would change: a different model, a different
dimensionality or a different seed is a different embedding space, and mixing
two of them silently would produce meaningless distances.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: ClassVar[str]
    description: ClassVar[str]

    @property
    def identity(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one L2-normalized vector each."""
        ...

"""A deterministic, offline embedding provider using the hashing trick.

Tokens (and adjacent token pairs) are hashed to fixed positions in a vector with
a signed contribution, then the vector is L2-normalized so cosine similarity is
a dot product. This is feature hashing, a real technique -- not a stub -- but it
is worth being precise about what it does and does not capture:

* It captures **lexical** overlap. Two summaries describing the same mistake in
  similar words land close together. Bigrams mean word order carries some
  weight, so "refunded the oldest order" and "ordered the oldest refund" differ.
* It does **not** capture meaning. "refunded the wrong order" and "issued a
  credit for the incorrect purchase" share almost no tokens and will not group,
  where a trained embedding model would place them together.

That tradeoff is acceptable here because the text being embedded is not free
prose: it is a structured analysis whose failure type and component are part of
the string, written under a prompt that explicitly asks for wording that repeats
across the same failure family. Lexical similarity does real work on that input.

Determinism is the point. ``hash()`` is randomized per process in Python and
must never be used; BLAKE2b keyed by the configured seed gives the same vector
on every machine and every run, which is what makes a clustering reproducible.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise
from typing import ClassVar

DEFAULT_DIMENSIONS = 512
DEFAULT_SEED = 0

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    name: ClassVar[str] = "hashing"
    description: ClassVar[str] = "Deterministic offline feature hashing (lexical similarity)"

    def __init__(self, *, dimensions: int = DEFAULT_DIMENSIONS, seed: int = DEFAULT_SEED) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self._dimensions = dimensions
        self.seed = seed

    @property
    def identity(self) -> str:
        return f"{self.name}:{self._dimensions}:{self.seed}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for feature, weight in _features(text).items():
            index, sign = self._position(feature)
            vector[index] += sign * weight
        return _normalize(vector)

    def _position(self, feature: str) -> tuple[int, int]:
        """Hash a feature to a bucket and a sign.

        The sign halves the bias from collisions: two unrelated features landing
        in the same bucket are as likely to cancel as to reinforce.
        """
        digest = hashlib.blake2b(
            feature.encode("utf-8"), digest_size=9, key=str(self.seed).encode("utf-8")
        ).digest()
        value = int.from_bytes(digest, "big")
        return value % self._dimensions, 1 if (value >> 63) & 1 else -1


def _features(text: str) -> dict[str, float]:
    """Unigrams and bigrams, with sublinear term weighting."""
    tokens = _TOKEN_PATTERN.findall(text.lower())
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    for first, second in pairwise(tokens):
        bigram = f"{first}_{second}"
        counts[bigram] = counts.get(bigram, 0) + 1
    # 1 + log(count): a word repeated ten times matters more than once, but not
    # ten times more, so one long quoted string cannot dominate a summary.
    return {feature: 1.0 + math.log(count) for feature, count in counts.items()}


def _normalize(vector: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0.0:
        return vector
    return [value / magnitude for value in vector]

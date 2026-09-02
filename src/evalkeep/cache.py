"""On-disk cache for analyzer answers.

Keyed by the three things that can change the answer: the *content* of the trace
(already redacted), which analyst produced it, and which prompt version was
asked. Change any one and the key changes, so a prompt edit or a model swap
never serves a stale label -- and re-running analysis after a database reset
costs nothing.

The cache lives under ``.evalkeep/cache/``, which is not committed. It is a
speed and money optimisation, never a source of truth: deleting it loses
nothing that the database does not already hold.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ANALYSIS_DIRNAME = "analysis"
EMBEDDING_DIRNAME = "embeddings"


def cache_key(content_hash: str, analyzer_identity: str, prompt_version: int) -> str:
    """A stable key over the three inputs that determine an answer."""
    material = "\n".join([content_hash, analyzer_identity, str(prompt_version)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0


class JsonFileCache:
    """A content-addressed JSON file cache. Corrupt entries are treated as misses."""

    dirname: str = "cache"

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = root / self.dirname
        self.enabled = enabled
        self.stats = CacheStats()

    def path_for(self, key: str) -> Path:
        # Sharded by the first two characters so one directory never holds
        # a hundred thousand files.
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self.path_for(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.stats.misses += 1
            return None
        if not isinstance(payload, dict):
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return payload

    def put(self, key: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self.path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash cannot leave a half-written entry
            # that would later be read back as a valid answer.
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # A cache is an optimisation; failing to write one is not an error.
            return
        self.stats.writes += 1


class AnalysisCache(JsonFileCache):
    """Cached analyzer answers, keyed by trace content, analyst and prompt version."""

    dirname = ANALYSIS_DIRNAME


class EmbeddingCache(JsonFileCache):
    """Cached vectors, keyed by the embedded text and the embedding space.

    Embedding the same failure description twice is wasted work with a local
    provider and wasted money with a hosted one, and re-running ``discover``
    after editing one cluster should not re-embed everything.
    """

    dirname = EMBEDDING_DIRNAME

    def get_vector(self, key: str) -> list[float] | None:
        payload = self.get(key)
        if payload is None:
            return None
        vector = payload.get("vector")
        if not isinstance(vector, list) or not all(
            isinstance(value, int | float) for value in vector
        ):
            self.stats.hits -= 1
            self.stats.misses += 1
            return None
        return [float(value) for value in vector]

    def put_vector(self, key: str, vector: list[float]) -> None:
        self.put(key, {"vector": vector})


def embedding_key(text: str, embedder_identity: str) -> str:
    """A key over the exact text embedded and the space it was embedded into."""
    material = "\n".join([embedder_identity, text])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

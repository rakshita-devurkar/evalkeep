"""Stable content hashing, for detecting the same interaction twice.

Two traces are the same *interaction* when the agent was asked the same thing,
did the same things and got the same verdict -- regardless of when it was
recorded, what the exporter called the events, or which run it came from. The
hash therefore covers what failure analysis actually reads and deliberately
excludes:

* ``trace_id`` -- the question is whether a re-export under a new ID is the same
  interaction, so the ID cannot be part of the answer.
* ``metadata`` and every ``timestamp`` -- wall-clock time and provenance differ
  between exports of one interaction.
* ``event_id`` and ``call_id`` -- arbitrary handles, often regenerated per export.

Hashing runs on the *redacted* trace, so an identical interaction hashes the same
whether or not it happened to carry a customer's email address.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from evalkeep.trace import NormalizedTrace

HASH_ALGORITHM = "sha256"

#: Per-event keys that identify the recording rather than the interaction.
_VOLATILE_EVENT_KEYS = frozenset({"event_id", "call_id", "timestamp"})


def canonical_content(trace: NormalizedTrace) -> dict[str, Any]:
    """The subset of a trace that defines the interaction."""
    payload = trace.model_dump(mode="json")
    return {
        "input": payload["input"],
        "output": payload["output"],
        "outcome": payload["outcome"],
        "events": [
            {key: value for key, value in event.items() if key not in _VOLATILE_EVENT_KEYS}
            for event in payload["events"]
        ],
    }


def content_hash(trace: NormalizedTrace) -> str:
    """A stable ``sha256:<hex>`` fingerprint of the interaction."""
    canonical = json.dumps(
        canonical_content(trace),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.new(HASH_ALGORITHM, canonical.encode("utf-8")).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"

"""Trace adapters and the registry the CLI resolves ``--format`` against."""

from __future__ import annotations

from evalsmith.adapters.base import AdapterRecord, IssueKind, TraceAdapter, TraceIssue
from evalsmith.adapters.jsonl import JsonlAdapter
from evalsmith.errors import CommandError

DEFAULT_ADAPTER = "jsonl"

_ADAPTERS: dict[str, TraceAdapter] = {JsonlAdapter.name: JsonlAdapter()}


def available_adapters() -> dict[str, TraceAdapter]:
    return dict(_ADAPTERS)


def get_adapter(name: str) -> TraceAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        known = ", ".join(sorted(_ADAPTERS))
        raise CommandError(
            f"Unknown trace format {name!r}.", hint=f"Available formats: {known}."
        ) from None


__all__ = [
    "DEFAULT_ADAPTER",
    "AdapterRecord",
    "IssueKind",
    "JsonlAdapter",
    "TraceAdapter",
    "TraceIssue",
    "available_adapters",
    "get_adapter",
]

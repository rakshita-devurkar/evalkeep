"""Export formats for approved regression tests."""

from __future__ import annotations

from enum import StrEnum

from evalkeep.errors import CommandError
from evalkeep.exporters.generic import to_jsonl, to_record
from evalkeep.exporters.promptfoo import (
    FIXTURES_VAR,
    assertion,
    build_config,
    build_test_case,
    provider_for,
    replay_warnings,
)


class ExportFormat(StrEnum):
    PROMPTFOO = "promptfoo"
    JSONL = "jsonl"


def parse_format(name: str) -> ExportFormat:
    try:
        return ExportFormat(name)
    except ValueError:
        known = ", ".join(member.value for member in ExportFormat)
        raise CommandError(
            f"Unknown export format {name!r}.", hint=f"Available formats: {known}."
        ) from None


__all__ = [
    "FIXTURES_VAR",
    "ExportFormat",
    "assertion",
    "build_config",
    "build_test_case",
    "parse_format",
    "provider_for",
    "replay_warnings",
    "to_jsonl",
    "to_record",
]

"""Export formats for approved regression tests."""

from __future__ import annotations

from enum import StrEnum

from evalsmith.errors import CommandError
from evalsmith.exporters.generic import to_jsonl, to_record
from evalsmith.exporters.promptfoo import assertion, build_config, provider_for, test_case


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
    "ExportFormat",
    "assertion",
    "build_config",
    "parse_format",
    "provider_for",
    "test_case",
    "to_jsonl",
    "to_record",
]

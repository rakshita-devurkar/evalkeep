"""A deterministic analyzer for development, with no network and no key.

It exists so the analysis pipeline -- caching, storage, reporting -- can be
exercised end to end offline. It is not analysis: it reads the detector evidence
back to you and labels everything ``other``/``unknown``. Its output is stamped
``stub`` so nothing downstream can mistake it for a real judgement.
"""

from __future__ import annotations

from typing import ClassVar

from evalsmith.analysis import Component, FailureType, ProviderAnalysis, Severity
from evalsmith.detectors import Signal
from evalsmith.trace import NormalizedTrace


class StubAnalyzer:
    name: ClassVar[str] = "stub"
    description: ClassVar[str] = "Deterministic placeholder for offline development"

    @property
    def identity(self) -> str:
        return self.name

    def analyze_failure(self, trace: NormalizedTrace, signals: list[Signal]) -> ProviderAnalysis:
        summary = "; ".join(signal.summary for signal in signals) or "no evidence recorded"
        return ProviderAnalysis(
            failure_type=FailureType.OTHER,
            component=Component.UNKNOWN,
            severity=Severity.MEDIUM,
            summary=f"[stub] {summary}",
            raw_response=None,
        )

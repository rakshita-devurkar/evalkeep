"""The analysis pass: describe failures, cached, without ever losing a run.

Two properties matter more than speed here:

* **A provider failure is never fatal.** One trace the model chokes on must not
  abandon the other four hundred. Failures are counted and reported; the run
  continues.
* **The provider's own words are kept, redacted.** The model only ever sees a
  redacted trace, but its response is redacted again before storage -- a model
  can quote its input, and "it only saw redacted text" is an argument, not a
  guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from evalsmith.analysis import AnalyzerError, AnalyzerProvider, FailureAnalysis
from evalsmith.cache import AnalysisCache, cache_key
from evalsmith.failures import FailureStatus
from evalsmith.hashing import content_hash
from evalsmith.prompts import FAILURE_ANALYSIS_PROMPT_VERSION
from evalsmith.redaction import RedactionSummary, Redactor
from evalsmith.storage import TraceStore


@dataclass
class AnalysisReport:
    """What one analysis pass did."""

    analyzer: str
    prompt_version: int
    considered: int = 0
    analyzed: int = 0
    from_cache: int = 0
    skipped: int = 0
    manual_kept: int = 0
    failed: int = 0
    redactions: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    by_type: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.analyzed)


#: Analysis describes failures worth keeping; a dismissed one is not.
ANALYZABLE = (FailureStatus.CANDIDATE, FailureStatus.CONFIRMED)


def analyze_failures(
    store: TraceStore,
    provider: AnalyzerProvider,
    cache: AnalysisCache,
    *,
    redactor: Redactor | None = None,
    reanalyze: bool = False,
    overwrite_manual: bool = False,
    limit: int | None = None,
) -> AnalysisReport:
    """Analyze every failure that needs it, reusing cached answers."""
    redactor = redactor or Redactor()
    report = AnalysisReport(
        analyzer=provider.identity, prompt_version=FAILURE_ANALYSIS_PROMPT_VERSION
    )
    failures = store.failures

    for failure in failures.iter_all():
        if failure.status not in ANALYZABLE:
            continue
        if limit is not None and report.analyzed + report.from_cache >= limit:
            break

        report.considered += 1
        existing = failures.get_analysis(failure.failure_id)
        if existing is not None and not _needs_analysis(
            existing, provider, reanalyze=reanalyze, overwrite_manual=overwrite_manual
        ):
            report.skipped += 1
            if existing.manual:
                report.manual_kept += 1
            continue

        stored = store.get(failure.trace_id)
        if stored is None:  # pragma: no cover - the foreign key prevents this
            report.failed += 1
            report.errors.append((failure.failure_id, "trace is missing from the store"))
            continue

        key = cache_key(
            content_hash(stored.trace), provider.identity, FAILURE_ANALYSIS_PROMPT_VERSION
        )
        cached = cache.get(key)
        if cached is not None and not reanalyze:
            analysis = FailureAnalysis.from_dict(cached)
            analysis.analyzed_at = datetime.now(UTC)
            failures.save_analysis(failure.failure_id, analysis)
            report.from_cache += 1
            _count_type(report, analysis)
            continue

        try:
            produced = provider.analyze_failure(stored.trace, failure.signals)
        except AnalyzerError as exc:
            report.failed += 1
            report.errors.append((failure.failure_id, str(exc)))
            continue

        analysis = FailureAnalysis.from_provider(
            produced,
            analyzer=provider.identity,
            prompt_version=FAILURE_ANALYSIS_PROMPT_VERSION,
        )
        report.redactions += _redact_in_place(analysis, redactor)

        failures.save_analysis(failure.failure_id, analysis)
        cache.put(key, analysis.to_dict())
        report.analyzed += 1
        _count_type(report, analysis)

    return report


def _needs_analysis(
    existing: FailureAnalysis,
    provider: AnalyzerProvider,
    *,
    reanalyze: bool,
    overwrite_manual: bool,
) -> bool:
    """Re-analyze when forced, or when the analysis is stale -- never over a label.

    ``--reanalyze`` refreshes *machine* analyses. Replacing something a person
    wrote takes its own flag: refreshing model output after a prompt change is
    routine, and it must not quietly discard hand-written labels along the way.
    """
    if existing.manual:
        return overwrite_manual
    if reanalyze:
        return True
    return (
        existing.analyzer != provider.identity
        or existing.prompt_version != FAILURE_ANALYSIS_PROMPT_VERSION
    )


def _redact_in_place(analysis: FailureAnalysis, redactor: Redactor) -> int:
    """Redact what the provider wrote, before it is stored anywhere."""
    summary = RedactionSummary()
    analysis.summary = redactor.redact_text(analysis.summary, summary)
    if analysis.raw_response is not None:
        analysis.raw_response = redactor.redact_text(analysis.raw_response, summary)
    return summary.total


def _count_type(report: AnalysisReport, analysis: FailureAnalysis) -> None:
    key = analysis.failure_type.value
    report.by_type[key] = report.by_type.get(key, 0) + 1

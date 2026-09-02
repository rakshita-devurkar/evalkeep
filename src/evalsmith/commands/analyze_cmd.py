"""``evalsmith analyze`` and ``evalsmith failures label`` -- describe failures.

Manual labelling is a first-class path, not a fallback. With no provider
configured Evalsmith still produces a fully labelled dataset; it just asks a
person for the labels instead of a model.
"""

from __future__ import annotations

from pathlib import Path

from evalsmith.analysis import Component, FailureAnalysis, FailureType, Severity
from evalsmith.analysis_run import AnalysisReport, analyze_failures
from evalsmith.analyzers import MANUAL_PROVIDER, get_analyzer
from evalsmith.cache import AnalysisCache
from evalsmith.commands.detect_cmd import default_reviewer, resolve_failure
from evalsmith.config import Project
from evalsmith.errors import CommandError
from evalsmith.redaction import RedactionSummary, Redactor
from evalsmith.storage import TraceStore

#: Hand-written labels answer no prompt, so they carry no prompt version.
MANUAL_PROMPT_VERSION = 0

NO_PROVIDER_MESSAGE = (
    "No analyzer provider is configured, so there is nothing to run automatically."
)
NO_PROVIDER_HINT = (
    "Label failures by hand with 'evalsmith failures label <id> --type ... "
    "--component ... --severity ... --summary ...', or set analyzer.provider "
    "in evalsmith.yaml."
)


def run_analysis(
    *,
    project_root: Path = Path(),
    reanalyze: bool = False,
    overwrite_manual: bool = False,
    limit: int | None = None,
    use_cache: bool = True,
) -> AnalysisReport:
    """Analyze failures with the configured provider."""
    project = Project.load(project_root.expanduser().resolve())
    provider = get_analyzer(project.config.analyzer)
    if provider is None:
        raise CommandError(NO_PROVIDER_MESSAGE, hint=NO_PROVIDER_HINT)

    cache = AnalysisCache(project.subdir("cache"), enabled=use_cache)
    with TraceStore.open(project.database_path) as store:
        if store.failures.count() == 0:
            raise CommandError(
                "No failure candidates to analyze.",
                hint="Run 'evalsmith detect' first.",
            )
        return analyze_failures(
            store,
            provider,
            cache,
            redactor=Redactor(project.config.redaction),
            reanalyze=reanalyze,
            overwrite_manual=overwrite_manual,
            limit=limit,
        )


def label_failure(
    identifier: str,
    *,
    failure_type: FailureType,
    component: Component,
    severity: Severity,
    summary: str,
    project_root: Path = Path(),
    labeler: str | None = None,
) -> FailureAnalysis:
    """Record a hand-written analysis for one failure."""
    cleaned = summary.strip()
    if not cleaned:
        raise CommandError("A label needs a non-empty --summary.")

    project = Project.load(project_root.expanduser().resolve())
    who = labeler or default_reviewer()
    redactor = Redactor(project.config.redaction)

    with TraceStore.open(project.database_path) as store:
        failure = resolve_failure(store, identifier)
        analysis = FailureAnalysis(
            failure_type=failure_type,
            component=component,
            severity=severity,
            # A person can type a real email address into a summary; redact it
            # for the same reason the trace itself was redacted.
            summary=redactor.redact_text(cleaned, RedactionSummary()),
            analyzer=f"{MANUAL_PROVIDER}:{who}",
            prompt_version=MANUAL_PROMPT_VERSION,
            labeler=who,
        )
        store.failures.save_analysis(failure.failure_id, analysis)
        return analysis

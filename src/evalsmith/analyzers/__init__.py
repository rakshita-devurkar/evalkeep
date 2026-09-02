"""Analyzer providers and the registry the project configuration resolves against.

``manual`` is the default and is deliberately *not* a provider: it means nobody
analyzes automatically, and failures are labelled by hand. Resolving it returns
``None`` so callers must handle the offline case explicitly rather than silently
falling back to a machine label.
"""

from __future__ import annotations

from evalsmith.analysis import AnalyzerError, AnalyzerProvider
from evalsmith.analyzers.stub import StubAnalyzer
from evalsmith.config import AnalyzerConfig
from evalsmith.errors import CommandError

MANUAL_PROVIDER = "manual"

KNOWN_PROVIDERS: dict[str, str] = {
    MANUAL_PROVIDER: "No automatic analysis; label failures by hand",
    "anthropic": "Claude via the Anthropic Messages API (needs an API key)",
    StubAnalyzer.name: StubAnalyzer.description,
}


def get_analyzer(config: AnalyzerConfig) -> AnalyzerProvider | None:
    """Build the configured provider, or ``None`` for manual labelling."""
    if config.provider == MANUAL_PROVIDER:
        return None
    if config.provider == StubAnalyzer.name:
        return StubAnalyzer()
    if config.provider == "anthropic":
        # Imported lazily: the SDK is an optional dependency.
        from evalsmith.analyzers.anthropic import AnthropicAnalyzer

        return AnthropicAnalyzer(
            model=config.model, effort=config.effort, max_tokens=config.max_tokens
        )
    known = ", ".join(sorted(KNOWN_PROVIDERS))
    raise CommandError(
        f"Unknown analyzer provider {config.provider!r}.",
        hint=f"Known providers: {known}.",
    )


__all__ = [
    "KNOWN_PROVIDERS",
    "MANUAL_PROVIDER",
    "AnalyzerError",
    "AnalyzerProvider",
    "StubAnalyzer",
    "get_analyzer",
]

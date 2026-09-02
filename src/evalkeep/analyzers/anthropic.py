"""The Anthropic analyzer.

Uses the Messages API with structured outputs, so the model is constrained to
the JSON schema in :mod:`evalkeep.prompts` rather than asked politely for JSON
and parsed hopefully. The model only ever sees a redacted trace, and the
pipeline redacts the response again before storing it.

The ``anthropic`` package is an optional dependency: Evalkeep runs entirely
offline with manual labelling, and nothing in the core imports this module
unless the project configures ``provider: anthropic``.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from evalkeep.analysis import (
    AnalyzerError,
    Component,
    FailureType,
    ProviderAnalysis,
    Severity,
)
from evalkeep.detectors import Signal
from evalkeep.prompts import (
    FAILURE_ANALYSIS_SCHEMA,
    FAILURE_ANALYSIS_SYSTEM,
    failure_analysis_prompt,
)
from evalkeep.trace import NormalizedTrace

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_EFFORT = "medium"


class _AnalysisResponse(BaseModel):
    """The schema the model is constrained to, validated again on arrival."""

    failure_type: FailureType
    component: Component
    severity: Severity
    summary: str


class AnthropicAnalyzer:
    name: ClassVar[str] = "anthropic"
    description: ClassVar[str] = "Claude via the Anthropic Messages API (needs an API key)"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client

    @property
    def identity(self) -> str:
        """A different model is a different analyst, so it keys the cache."""
        return f"{self.name}:{self.model}"

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _build_client()
        return self._client

    def analyze_failure(self, trace: NormalizedTrace, signals: list[Signal]) -> ProviderAnalysis:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=FAILURE_ANALYSIS_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": FAILURE_ANALYSIS_SCHEMA},
                },
                messages=[{"role": "user", "content": failure_analysis_prompt(trace, signals)}],
            )
        except Exception as exc:  # SDK exception classes are not importable here
            raise AnalyzerError(f"{self.identity} request failed: {exc}") from exc

        return _parse(response, self.identity)


def _parse(response: Any, identity: str) -> ProviderAnalysis:
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        raise AnalyzerError(f"{identity} declined to analyze this trace")
    if stop_reason == "max_tokens":
        raise AnalyzerError(f"{identity} hit max_tokens before finishing")

    text = next(
        (block.text for block in response.content if getattr(block, "type", None) == "text"),
        None,
    )
    if not text:
        raise AnalyzerError(f"{identity} returned no text content")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalyzerError(f"{identity} returned text that is not JSON: {exc}") from exc

    try:
        parsed = _AnalysisResponse.model_validate(payload)
    except ValidationError as exc:
        raise AnalyzerError(
            f"{identity} returned JSON that does not match the schema:\n{exc}"
        ) from exc

    return ProviderAnalysis(
        failure_type=parsed.failure_type,
        component=parsed.component,
        severity=parsed.severity,
        summary=parsed.summary.strip(),
        raw_response=text,
    )


def _build_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise AnalyzerError(
            "The 'anthropic' package is not installed. "
            "Install it with: uv add 'evalkeep[anthropic]'"
        ) from exc
    try:
        return anthropic.Anthropic()
    except Exception as exc:
        raise AnalyzerError(
            f"Could not create an Anthropic client: {exc}. "
            "Set ANTHROPIC_API_KEY, or run 'ant auth login'."
        ) from exc

"""Versioned prompts and the strict JSON schema their answers must satisfy.

The version is part of every cache key and is stored with every analysis, so a
prompt change never silently mixes old and new labels in one dataset. Editing
the prompt text without bumping :data:`FAILURE_ANALYSIS_PROMPT_VERSION` is a
bug: it would serve stale cached answers for a question you no longer ask.
"""

from __future__ import annotations

import json
from typing import Any

from evalkeep.analysis import Component, FailureType, Severity
from evalkeep.detectors import Signal
from evalkeep.trace import (
    EvaluationEvent,
    MessageEvent,
    NormalizedTrace,
    ToolCallEvent,
    ToolResultEvent,
)

FAILURE_ANALYSIS_PROMPT_VERSION = 1

MAX_SUMMARY_LENGTH = 240

FAILURE_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "failure_type": {
            "type": "string",
            "enum": [member.value for member in FailureType],
            "description": "The kind of failure, from the fixed list.",
        },
        "component": {
            "type": "string",
            "enum": [member.value for member in Component],
            "description": "Where in the agent the failure originated.",
        },
        "severity": {
            "type": "string",
            "enum": [member.value for member in Severity],
            "description": "How much damage this failure does to a user.",
        },
        "summary": {
            "type": "string",
            "description": (
                "One sentence naming the specific mistake, in terms that would "
                "match other traces with the same underlying problem."
            ),
        },
    },
    "required": ["failure_type", "component", "severity", "summary"],
    "additionalProperties": False,
}

FAILURE_ANALYSIS_SYSTEM = """\
You classify failures in recorded AI-agent interactions so that similar failures \
can be grouped together.

Rules:
- Describe only what the recorded interaction shows. Do not speculate about code \
you cannot see, and do not propose fixes.
- Write the summary so that two traces with the same underlying problem would \
receive near-identical summaries. Name the mistake, not the customer, the order \
or the wording of this particular request.
- Values shown as [REDACTED:...] were removed before you saw them. Treat them as \
opaque; never guess what they were.
- Choose the most specific failure_type that fits. Use "other" only when nothing \
else applies.
- Severity is about user impact: critical means money, data or safety; low means \
cosmetic or easily noticed.\
"""


def failure_analysis_prompt(trace: NormalizedTrace, signals: list[Signal]) -> str:
    """The user-turn prompt describing one failing interaction."""
    sections = [
        "Here is a recorded interaction that has been marked as a failure.",
        "",
        "## Evidence that it failed",
    ]
    sections.extend(f"- ({signal.kind.value}) {signal.summary}" for signal in signals)
    sections += ["", "## The interaction", _render_trace(trace)]
    sections += [
        "",
        "Classify this failure. Respond with a JSON object matching the required schema.",
    ]
    return "\n".join(sections)


def _render_trace(trace: NormalizedTrace) -> str:
    """A compact, stable rendering. Stable matters: it feeds the cache key."""
    lines: list[str] = []
    if trace.input.text:
        lines.append(f"user: {trace.input.text}")
    for message in trace.input.messages:
        lines.append(f"{message.role.value}: {message.content}")

    for event in trace.events:
        if isinstance(event, ToolCallEvent):
            arguments = json.dumps(event.arguments, sort_keys=True)
            lines.append(f"tool_call: {event.tool}({arguments})")
        elif isinstance(event, ToolResultEvent):
            result = json.dumps(event.result, sort_keys=True, default=str)
            suffix = f" error={event.error}" if event.error else ""
            lines.append(f"tool_result: {event.tool} -> {result}{suffix}")
        elif isinstance(event, MessageEvent):
            lines.append(f"{event.role.value}: {event.content}")
        elif isinstance(event, EvaluationEvent):
            verdict = {True: "pass", False: "fail", None: "unrecorded"}[event.passed]
            lines.append(f"evaluation: {event.name} {verdict}")

    if trace.output is not None and trace.output.text:
        lines.append(f"assistant: {trace.output.text}")
    for message in trace.output.messages if trace.output else []:
        lines.append(f"{message.role.value}: {message.content}")

    if trace.outcome.feedback is not None and trace.outcome.feedback.comment:
        lines.append(f"feedback: {trace.outcome.feedback.comment}")
    for evaluation in trace.outcome.evaluations:
        if evaluation.passed is False:
            reason = f": {evaluation.reason}" if evaluation.reason else ""
            lines.append(f"failed evaluation: {evaluation.name}{reason}")

    return "\n".join(lines)

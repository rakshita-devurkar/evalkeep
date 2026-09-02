"""Turning a reviewed failure into a draft regression test.

Every expectation here is read off the recorded trace. Nothing is inferred about
what the agent *should* have done, because a trace does not contain that: it
shows an action and a verdict, not an intention. So the generator writes the
half it can defend -- "you called refund_order with order-A and that was wrong"
-- and records, as a warning, that a reviewer still owes the other half.

That is why drafts are the only thing this module produces.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from evalkeep.analysis import FailureAnalysis, FailureType
from evalkeep.failures import Failure
from evalkeep.hashing import content_hash
from evalkeep.regression import (
    CaseInput,
    Expectation,
    ExpectationType,
    Fixture,
    Provenance,
    RegressionTest,
    ReviewStatus,
    find_contradictions,
    make_test_id,
    validate_expectation,
)
from evalkeep.trace import NormalizedTrace, ToolCallEvent, ToolResultEvent

#: Bumped when generation would produce different tests from the same input.
GENERATOR_VERSION = 1

#: How deep to flatten nested tool arguments into dotted paths.
MAX_ARGUMENT_DEPTH = 3

NO_POSITIVE_EXPECTATION = (
    "No positive expectation: this test forbids the observed mistake but does "
    "not say what the agent should do instead. Add one before approving."
)
NO_TOOL_CALLS = (
    "The failure is about tool use but the trace records no tool calls, so no "
    "tool expectation could be derived."
)


def build_test(
    trace: NormalizedTrace,
    failure: Failure,
    analysis: FailureAnalysis,
    *,
    cluster_id: str | None = None,
    cluster_label: str | None = None,
    representative_roles: list[str] | None = None,
) -> RegressionTest:
    """Generate one pending draft from one analyzed failure."""
    expectations, warnings = derive_expectations(trace, analysis)

    for expectation in expectations:
        problem = validate_expectation(expectation)
        if problem is not None:  # pragma: no cover - generation should not emit these
            warnings.append(f"Invalid expectation ({expectation.describe()}): {problem}")

    # A contradictory draft fails on a correct agent too, so it is caught here
    # rather than surfacing later as a phantom regression.
    for contradiction in find_contradictions(expectations):
        warnings.append(f"Contradictory expectations: {contradiction.describe()}")

    if not any(expectation.positive for expectation in expectations):
        warnings.append(NO_POSITIVE_EXPECTATION)

    return RegressionTest(
        test_id=make_test_id(trace.trace_id, _input_text(trace)),
        failure_id=failure.failure_id,
        status=ReviewStatus.DRAFT,
        input=_build_input(trace),
        fixtures=_build_fixtures(trace),
        expectations=expectations,
        warnings=warnings,
        provenance=Provenance(
            trace_id=trace.trace_id,
            failure_id=failure.failure_id,
            content_hash=content_hash(trace),
            cluster_id=cluster_id,
            cluster_label=cluster_label,
            representative_roles=list(representative_roles or []),
            failure_type=analysis.failure_type.value,
            severity=analysis.severity.value,
            analyzer=analysis.analyzer,
            analysis_summary=analysis.summary,
            evidence=[signal.kind.value for signal in failure.signals],
            generator_version=GENERATOR_VERSION,
        ),
    )


def derive_expectations(
    trace: NormalizedTrace, analysis: FailureAnalysis
) -> tuple[list[Expectation], list[str]]:
    """Read expectations off the trace, deterministic ones first."""
    calls = trace.tool_calls
    warnings: list[str] = []
    expectations: list[Expectation] = []

    match analysis.failure_type:
        case FailureType.WRONG_TOOL_ARGUMENT:
            expectations += _forbid_observed_arguments(calls, warnings)
        case FailureType.WRONG_TOOL_SELECTION:
            expectations += _forbid_observed_tool(calls, warnings)
        case FailureType.UNNECESSARY_ACTION:
            expectations += _limit_repeated_calls(calls, warnings)
        case _:
            warnings.append(
                f"No deterministic expectation can be derived from a "
                f"{analysis.failure_type.value} failure; it needs a reviewer's check."
            )

    if not expectations:
        # The rubric is a last resort: it costs an LLM judge at run time, so it
        # only appears where nothing checkable could be read off the trace.
        expectations.append(
            Expectation(
                type=ExpectationType.HUMAN_RUBRIC,
                value=f"The agent must not repeat this failure: {analysis.summary}",
            )
        )
    return expectations, warnings


def _forbid_observed_arguments(
    calls: list[ToolCallEvent], warnings: list[str]
) -> list[Expectation]:
    """The last call is the action the outcome is about.

    A heuristic, and a documented one: earlier calls in a trace are usually
    lookups that succeeded, and asserting against their arguments would forbid
    correct behaviour. The reviewer can widen this during review.
    """
    if not calls:
        warnings.append(NO_TOOL_CALLS)
        return []

    implicated = calls[-1]
    arguments = _flatten(implicated.arguments)
    if not arguments:
        warnings.append(f"{implicated.tool} was called with no scalar arguments to assert against.")
        return []
    if len(calls) > 1:
        warnings.append(
            f"Assertions target the last tool call ({implicated.tool}); "
            f"{len(calls) - 1} earlier call(s) were treated as context."
        )
    return [
        Expectation(
            type=ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
            tool=implicated.tool,
            path=path,
            value=value,
        )
        for path, value in arguments.items()
    ]


def _forbid_observed_tool(calls: list[ToolCallEvent], warnings: list[str]) -> list[Expectation]:
    if not calls:
        warnings.append(NO_TOOL_CALLS)
        return []
    return [Expectation(type=ExpectationType.TOOL_NOT_CALLED, tool=calls[-1].tool)]


def _limit_repeated_calls(calls: list[ToolCallEvent], warnings: list[str]) -> list[Expectation]:
    """Cap the tool the agent over-used at one fewer than it managed.

    Deliberately a weak bound. The trace proves N calls was too many; it does
    not prove what the right number is, and picking one would be inventing the
    reviewer's judgement.
    """
    if not calls:
        warnings.append(NO_TOOL_CALLS)
        return []

    counts = Counter(call.tool for call in calls)
    tool, count = counts.most_common(1)[0]
    if count == 1:
        return [Expectation(type=ExpectationType.TOOL_NOT_CALLED, tool=tool)]

    warnings.append(
        f"{tool} was called {count} times; the limit is set to {count - 1}, "
        "which only proves the observed count was too many. Tighten it during review."
    )
    return [Expectation(type=ExpectationType.MAX_TOOL_CALLS, tool=tool, value=count - 1)]


def _build_input(trace: NormalizedTrace) -> CaseInput:
    return CaseInput(
        text=trace.input.text,
        messages=[
            {"role": message.role.value, "content": message.content}
            for message in trace.input.messages
        ],
    )


def _input_text(trace: NormalizedTrace) -> str:
    if trace.input.text:
        return trace.input.text
    for message in trace.input.messages:
        if message.role.value == "user":
            return message.content
    return trace.input.messages[0].content if trace.input.messages else ""


def _build_fixtures(trace: NormalizedTrace) -> list[Fixture]:
    """The tool results the original agent saw, paired with the calls that got them."""
    arguments_by_call: dict[str, dict[str, Any]] = {
        call.call_id: call.arguments for call in trace.tool_calls if call.call_id
    }
    return [
        Fixture(
            tool=event.tool,
            arguments=arguments_by_call.get(event.call_id or "", {}),
            result=event.result,
            error=event.error,
            call_id=event.call_id,
        )
        for event in trace.events
        if isinstance(event, ToolResultEvent)
    ]


def _flatten(arguments: dict[str, Any], prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Scalar arguments as dotted paths, so nested values are still assertable."""
    flat: dict[str, Any] = {}
    for key, value in arguments.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and depth < MAX_ARGUMENT_DEPTH:
            flat.update(_flatten(value, prefix=f"{path}.", depth=depth + 1))
        elif isinstance(value, str | int | float | bool) or value is None:
            flat[path] = value
    return flat

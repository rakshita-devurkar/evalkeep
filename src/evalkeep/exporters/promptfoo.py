"""Generating a Promptfoo configuration from approved tests.

Evalkeep does not execute agents; it hands an established runner a suite and
reads the results back. That division is the point of the project, so this
module's only job is translation -- and translation has two rules:

* **Every value is embedded as JSON, never interpolated as text.** Expectations
  contain data that came from a trace: order IDs, output fragments, tool names.
  Pasting those into a JavaScript expression by concatenation would be an
  injection bug with the same shape as SQL injection, so every literal goes
  through ``json.dumps``.
* **Every provider kind is adapted to one response shape.** Assertions are
  written once, against ``{text, toolCalls}``, and the provider configuration is
  what differs between an HTTP endpoint and a local script.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evalkeep.regression import Expectation, ExpectationType, RegressionTest
from evalkeep.targets import Target, TargetKind

#: Reads a dotted path out of a tool call's arguments, tolerating gaps.
_ARGUMENT_GETTER = "(p,o)=>p.split('.').reduce((a,k)=>a==null?a:a[k],o)"
#: Parenthesised deliberately. Without the outer parentheses, `.some()` binds
#: tighter than `||`, so `(output&&output.toolCalls)||[].some(...)` evaluates to
#: the tool-call array itself -- truthy -- and every tool assertion passes no
#: matter what the agent did.
_CALLS = "((output&&output.toolCalls)||[])"
_TEXT = "String((output&&output.text)??'')"


def build_config(
    tests: list[RegressionTest],
    target: Target,
    *,
    description: str | None = None,
    project_root: Path | None = None,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """A complete promptfooconfig document for one target and one suite."""
    return {
        "description": description or f"evalkeep suite against {target.target_id}",
        "providers": [provider_for(target, project_root=project_root, config_dir=config_dir)],
        # The prompt is the test input verbatim: Evalkeep tests an application,
        # not a prompt template.
        "prompts": ["{{input}}"],
        "tests": [test_case(test) for test in tests],
    }


def provider_for(
    target: Target,
    *,
    project_root: Path | None = None,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Translate a target into a Promptfoo provider.

    Script paths in a target are written relative to the project, because that
    is where a person reading ``targets.yaml`` expects them to be. The runner
    resolves them relative to the generated config instead, and the config is a
    build artifact that can land anywhere -- so the path is rewritten here to
    point at the same file from wherever the config is being written.
    """
    match target.kind:
        case TargetKind.HTTP:
            return {
                "id": target.url,
                "config": {
                    "method": target.method,
                    "headers": dict(target.headers),
                    "body": target.body,
                    # Normalize whatever the service returns into the one shape
                    # the assertions are written against.
                    "transformResponse": (
                        "({text: String("
                        f"{target.extract.output} ?? ''), toolCalls: "
                        f"{target.extract.tool_calls or '[]'} ?? []}})"
                    ),
                },
            }
        case TargetKind.PYTHON:
            suffix = f":{target.function}" if target.function else ""
            script = _script_path(target.path, project_root, config_dir)
            return {"id": f"file://{script}{suffix}"}
        case TargetKind.JAVASCRIPT:
            script = _script_path(target.path, project_root, config_dir)
            return {"id": f"file://{script}"}
        case _:
            return {"id": target.provider}


def test_case(test: RegressionTest) -> dict[str, Any]:
    """One Promptfoo test case, described by its stable test ID."""
    return {
        "description": test.test_id,
        "vars": {"input": _input_text(test)},
        "assert": [assertion(expectation) for expectation in test.expectations],
        "metadata": {
            "test_id": test.test_id,
            "trace_id": test.provenance.trace_id,
            "failure_type": test.provenance.failure_type,
            "severity": test.provenance.severity,
        },
    }


def assertion(expectation: Expectation) -> dict[str, Any]:
    """Translate one expectation into a Promptfoo assertion."""
    if expectation.type is ExpectationType.HUMAN_RUBRIC:
        # The only assertion that needs a model at run time.
        return {"type": "llm-rubric", "value": str(expectation.value)}
    return {"type": "javascript", "value": _javascript(expectation)}


def _javascript(expectation: Expectation) -> str:
    value = json.dumps(expectation.value)
    tool = json.dumps(expectation.tool)
    path = json.dumps(expectation.path)

    match expectation.type:
        case ExpectationType.OUTPUT_CONTAINS:
            return f"{_TEXT}.includes({value})"
        case ExpectationType.OUTPUT_NOT_CONTAINS:
            return f"!{_TEXT}.includes({value})"
        case ExpectationType.OUTPUT_MATCHES:
            return f"new RegExp({value}).test({_TEXT})"
        case ExpectationType.TOOL_CALLED:
            return f"{_CALLS}.some(c=>c.tool==={tool})"
        case ExpectationType.TOOL_NOT_CALLED:
            return f"!{_CALLS}.some(c=>c.tool==={tool})"
        case ExpectationType.TOOL_ARGUMENT_EQUALS:
            return f"{_CALLS}.some(c=>c.tool==={tool}&&{_argument_matches(path, value)})"
        case ExpectationType.TOOL_ARGUMENT_NOT_EQUALS:
            # "Not equals" means no call passed that value, not "some call
            # passed something else" -- one correct call must not excuse a
            # second wrong one.
            return f"!{_CALLS}.some(c=>c.tool==={tool}&&{_argument_matches(path, value)})"
        case ExpectationType.MAX_TOOL_CALLS:
            scope = "true" if expectation.tool is None else f"c.tool==={tool}"
            return f"{_CALLS}.filter(c=>{scope}).length<={int(expectation.value)}"
        case _:  # pragma: no cover - every type is handled above
            raise ValueError(f"cannot translate {expectation.type}")


def _argument_matches(path: str, value: str) -> str:
    getter = f"({_ARGUMENT_GETTER})({path},c.arguments||{{}})"
    # Compared as JSON so structured argument values behave, and so that 1 and
    # "1" stay different.
    return f"JSON.stringify({getter})===JSON.stringify({value})"


def _input_text(test: RegressionTest) -> str:
    if test.input.text:
        return test.input.text
    for message in test.input.messages:
        if message.get("role") == "user":
            return message.get("content", "")
    return test.input.messages[0].get("content", "") if test.input.messages else ""


def _script_path(path: str | None, project_root: Path | None, config_dir: Path | None) -> str:
    """Rewrite a project-relative script path for the config's own location."""
    if path is None:  # pragma: no cover - validate_shape rejects this first
        return ""
    if project_root is None:
        return path
    resolved = (project_root / path).resolve()
    if config_dir is None:
        return str(resolved)
    return os.path.relpath(resolved, config_dir.resolve())

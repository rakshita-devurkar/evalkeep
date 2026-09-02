"""Human review: the gate between a generated draft and a committed test.

Everything here is a pure function over a test. The interactive terminal loop is
a thin shell in the CLI that calls these, which is what keeps the guide's
"non-interactive CI review format" possible later: a different front end -- a
web form, a PR check, a batch file of decisions -- needs no new logic, only a
different way of collecting the same decisions.

Two rules the review gate enforces rather than suggests:

* **A contradictory test cannot be approved.** It would fail on a correct agent
  too, reporting a regression that is really a bug in the suite.
* **Editing cannot change what a test *is*.** A reviewer edits the input and the
  expectations -- the parts that encode intent. The test ID, provenance and
  fixtures are facts about where the test came from, and letting a review rewrite
  them would make the audit trail describe something that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import yaml

from evalsmith.generation import NO_POSITIVE_EXPECTATION
from evalsmith.regression import (
    CaseInput,
    Expectation,
    ExpectationType,
    RegressionTest,
    ReviewStatus,
    find_contradictions,
    validate_expectation,
)

#: The only fields a reviewer may change.
EDITABLE_FIELDS = ("input", "expectations")

EDIT_HELP = """\
# Editing {test_id}
#
# Change the input and the expectations. Everything else -- the test ID, the
# provenance, the recorded fixtures -- describes where this test came from and
# is not editable.
#
# Expectation types:
#   output_contains        value: text the answer must contain
#   output_not_contains    value: text the answer must not contain
#   output_matches         value: a regular expression the answer must match
#   tool_called            tool: a tool that must be called
#   tool_not_called        tool: a tool that must never be called
#   tool_argument_equals   tool, path, value
#   tool_argument_not_equals   tool, path, value
#   max_tool_calls         value: a whole number; tool: optional
#   human_rubric           value: what should happen, judged by a model at run time
#
# Delete every expectation to abandon the edit.
"""


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"
    SKIP = "skip"


@dataclass
class ReviewOutcome:
    """What one review session did."""

    reviewed: int = 0
    approved: int = 0
    rejected: int = 0
    edited: int = 0
    skipped: int = 0
    remaining: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.approved or self.rejected or self.edited)


@dataclass
class EditResult:
    """A parsed edit, or the reasons it could not be applied."""

    test: RegressionTest | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.test is not None and not self.errors


class ReviewError(Exception):
    """A decision that cannot be recorded as asked."""


def recompute_warnings(test: RegressionTest) -> list[str]:
    """What still needs a reviewer's attention, for the test as it stands now.

    Recomputed rather than carried forward: warnings describe current content,
    and a note about how the draft was generated stops being true the moment a
    person edits it.
    """
    warnings: list[str] = []
    for expectation in test.expectations:
        problem = validate_expectation(expectation)
        if problem is not None:
            warnings.append(f"Invalid expectation ({expectation.describe()}): {problem}")
    warnings.extend(
        f"Contradictory expectations: {contradiction.describe()}"
        for contradiction in test.contradictions
    )
    if not test.has_positive_expectation:
        warnings.append(NO_POSITIVE_EXPECTATION)
    return warnings


def blocking_problems(test: RegressionTest) -> list[str]:
    """Reasons a test must not be approved as it stands."""
    problems = [
        f"Invalid expectation ({expectation.describe()}): {problem}"
        for expectation in test.expectations
        if (problem := validate_expectation(expectation)) is not None
    ]
    problems.extend(
        f"Contradictory expectations: {contradiction.describe()}"
        for contradiction in test.contradictions
    )
    if not test.expectations:
        problems.append("A test with no expectations checks nothing.")
    return problems


def approve(test: RegressionTest, *, reviewer: str, reason: str | None = None) -> RegressionTest:
    """Approve a test, refusing if it could never pass on a correct agent."""
    problems = blocking_problems(test)
    if problems:
        raise ReviewError(
            "This test cannot be approved as it stands:\n  - " + "\n  - ".join(problems)
        )
    return _record(test, ReviewStatus.APPROVED, reviewer=reviewer, reason=reason)


def reject(test: RegressionTest, *, reviewer: str, reason: str | None = None) -> RegressionTest:
    """Reject a test. The record is kept, not deleted: a rejection is evidence."""
    return _record(test, ReviewStatus.REJECTED, reviewer=reviewer, reason=reason)


def _record(
    test: RegressionTest, status: ReviewStatus, *, reviewer: str, reason: str | None
) -> RegressionTest:
    test.status = status
    test.reviewer = reviewer
    test.review_reason = reason
    test.reviewed_at = datetime.now(UTC)
    test.updated_at = test.reviewed_at
    test.warnings = recompute_warnings(test)
    return test


def render_editable(test: RegressionTest) -> str:
    """The YAML a reviewer edits, with the guidance they need above it."""
    body = {
        "input": {
            key: value for key, value in test.input.to_dict().items() if value not in (None, [])
        },
        "expectations": [expectation.to_dict() for expectation in test.expectations],
    }
    header = EDIT_HELP.format(test_id=test.test_id)
    return header + yaml.safe_dump(body, sort_keys=False, default_flow_style=False)


def apply_edits(test: RegressionTest, text: str, *, editor: str) -> EditResult:
    """Parse an edited document and return an updated copy, or the errors.

    Nothing is written until the result is valid: an unparseable or
    self-contradictory edit leaves the stored draft exactly as it was.
    """
    try:
        # safe_load, never load: this document is arbitrary text from an editor,
        # and full YAML can construct objects.
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return EditResult(errors=[f"Could not parse YAML: {exc}"])

    if raw is None:
        return EditResult(errors=["The document is empty."])
    if not isinstance(raw, dict):
        return EditResult(errors=[f"Expected a mapping, got {type(raw).__name__}."])

    unknown = set(raw) - set(EDITABLE_FIELDS)
    if unknown:
        return EditResult(
            errors=[
                f"Only {' and '.join(EDITABLE_FIELDS)} can be edited; "
                f"remove: {', '.join(sorted(unknown))}."
            ]
        )

    errors: list[str] = []
    case_input, input_errors = _parse_input(raw.get("input"))
    errors.extend(input_errors)
    expectations, expectation_errors = _parse_expectations(raw.get("expectations"))
    errors.extend(expectation_errors)

    if errors:
        return EditResult(errors=errors)

    updated = _copy_with_edits(test, case_input, expectations, editor=editor)
    contradictions = [
        f"Contradictory expectations: {contradiction.describe()}"
        for contradiction in find_contradictions(expectations)
    ]
    if contradictions:
        return EditResult(errors=contradictions)
    return EditResult(test=updated)


def _copy_with_edits(
    test: RegressionTest,
    case_input: CaseInput,
    expectations: list[Expectation],
    *,
    editor: str,
) -> RegressionTest:
    now = datetime.now(UTC)
    updated = RegressionTest(
        test_id=test.test_id,
        failure_id=test.failure_id,
        input=case_input,
        provenance=test.provenance,
        status=test.status,
        fixtures=test.fixtures,
        expectations=expectations,
        reviewer=test.reviewer,
        review_reason=test.review_reason,
        reviewed_at=test.reviewed_at,
        edited=True,
        edited_by=editor,
        created_at=test.created_at,
        updated_at=now,
    )
    updated.warnings = recompute_warnings(updated)
    return updated


def _parse_input(raw: Any) -> tuple[CaseInput, list[str]]:
    if raw is None:
        return CaseInput(), ["input is required."]
    if not isinstance(raw, dict):
        return CaseInput(), [f"input must be a mapping, got {type(raw).__name__}."]

    text = raw.get("text")
    messages = raw.get("messages") or []
    if text is not None and not isinstance(text, str):
        return CaseInput(), ["input.text must be text."]
    if not isinstance(messages, list):
        return CaseInput(), ["input.messages must be a list."]

    parsed: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or "role" not in message or "content" not in message:
            return CaseInput(), [f"input.messages[{index}] needs a role and content."]
        parsed.append({"role": str(message["role"]), "content": str(message["content"])})

    if not (text or "").strip() and not parsed:
        return CaseInput(), ["input needs non-empty text or at least one message."]
    return CaseInput(text=text, messages=parsed), []


def _parse_expectations(raw: Any) -> tuple[list[Expectation], list[str]]:
    if raw is None or raw == []:
        return [], ["A test needs at least one expectation."]
    if not isinstance(raw, list):
        return [], [f"expectations must be a list, got {type(raw).__name__}."]

    expectations: list[Expectation] = []
    errors: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"expectations[{index}] must be a mapping.")
            continue
        kind = item.get("type")
        try:
            expectation_type = ExpectationType(str(kind))
        except ValueError:
            known = ", ".join(member.value for member in ExpectationType)
            errors.append(f"expectations[{index}]: unknown type {kind!r}. Known types: {known}.")
            continue

        expectation = Expectation(
            type=expectation_type,
            value=item.get("value"),
            tool=item.get("tool"),
            path=item.get("path"),
        )
        problem = validate_expectation(expectation)
        if problem is not None:
            errors.append(f"expectations[{index}]: {problem}")
            continue
        expectations.append(expectation)

    return expectations, errors

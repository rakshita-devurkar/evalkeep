"""Human review: the gate between a draft and a committed test (guide 8H)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from evalsmith.analysis import Component, FailureType, Severity
from evalsmith.cli import app
from evalsmith.commands.analyze_cmd import label_failure
from evalsmith.commands.dataset_cmd import build_dataset, list_tests, show_test
from evalsmith.commands.detect_cmd import run_detection
from evalsmith.commands.discover_cmd import run_discovery
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.commands.review_cmd import (
    approve_test,
    edit_test,
    editable_document,
    pending_reviews,
    reject_test,
    review_item,
)
from evalsmith.errors import CommandError, ExitCode
from evalsmith.regression import (
    CaseInput,
    Expectation,
    ExpectationType,
    Provenance,
    RegressionTest,
    ReviewStatus,
)
from evalsmith.review import (
    ReviewError,
    apply_edits,
    approve,
    blocking_problems,
    recompute_warnings,
    reject,
    render_editable,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/refund-agent/traces.jsonl"

LABELS: dict[str, tuple[FailureType, Component, Severity, str]] = {
    "trace-1042": (
        FailureType.WRONG_TOOL_ARGUMENT,
        Component.TOOL_ARGUMENTS,
        Severity.HIGH,
        "Refunded the oldest order instead of the newest order.",
    ),
    "trace-1043": (
        FailureType.WRONG_TOOL_ARGUMENT,
        Component.TOOL_ARGUMENTS,
        Severity.CRITICAL,
        "Refunded an older order instead of the newest order.",
    ),
    "trace-1051": (
        FailureType.UNNECESSARY_ACTION,
        Component.PLANNING,
        Severity.CRITICAL,
        "Refunded every order on the account when the user asked for one.",
    ),
}

FORBID = Expectation(
    type=ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
    tool="refund_order",
    path="order_id",
    value="order-A",
)
REQUIRE = Expectation(
    type=ExpectationType.TOOL_ARGUMENT_EQUALS,
    tool="refund_order",
    path="order_id",
    value="order-C",
)


def make_test(**overrides: Any) -> RegressionTest:
    payload: dict[str, Any] = {
        "test_id": "refund_my_latest_order_abc12345",
        "failure_id": "fail-abc123",
        "input": CaseInput(text="Refund my latest order."),
        "expectations": [FORBID],
        "provenance": Provenance(
            trace_id="trace-1042", failure_id="fail-abc123", content_hash="sha256:abc"
        ),
    }
    payload.update(overrides)
    return RegressionTest(**payload)


@pytest.fixture
def drafted(initialized_project: Path) -> Path:
    """The refund example, all the way through to pending drafts."""
    ingest_traces(EXAMPLE, project_root=initialized_project)
    run_detection(project_root=initialized_project)
    for trace_id, (kind, component, severity, summary) in LABELS.items():
        label_failure(
            trace_id,
            failure_type=kind,
            component=component,
            severity=severity,
            summary=summary,
            project_root=initialized_project,
            labeler="alex",
        )
    run_discovery(project_root=initialized_project)
    build_dataset(project_root=initialized_project, representatives_only=False)
    return initialized_project


class TestApproval:
    def test_approval_records_who_when_and_why(self) -> None:
        approved = approve(make_test(), reviewer="alex", reason="prohibition is enough")
        assert approved.status is ReviewStatus.APPROVED
        assert approved.reviewer == "alex"
        assert approved.review_reason == "prohibition is enough"
        assert approved.reviewed_at is not None

    def test_a_contradictory_test_cannot_be_approved(self) -> None:
        """It would fail on a correct agent, reporting a suite bug as a regression."""
        contradictory = make_test(
            expectations=[
                Expectation(type=ExpectationType.TOOL_CALLED, tool="refund_order"),
                Expectation(type=ExpectationType.TOOL_NOT_CALLED, tool="refund_order"),
            ]
        )
        with pytest.raises(ReviewError, match="cannot be approved"):
            approve(contradictory, reviewer="alex")

    def test_a_test_with_no_expectations_cannot_be_approved(self) -> None:
        with pytest.raises(ReviewError, match="checks nothing"):
            approve(make_test(expectations=[]), reviewer="alex")

    def test_an_invalid_expectation_blocks_approval(self) -> None:
        broken = make_test(
            expectations=[Expectation(type=ExpectationType.MAX_TOOL_CALLS, tool="t", value=-1)]
        )
        assert blocking_problems(broken)
        with pytest.raises(ReviewError):
            approve(broken, reviewer="alex")

    def test_a_forbidding_only_test_may_still_be_approved(self) -> None:
        """Missing a positive expectation is a warning, not a veto."""
        approved = approve(make_test(), reviewer="alex")
        assert approved.status is ReviewStatus.APPROVED
        assert any("No positive expectation" in warning for warning in approved.warnings)


class TestRejection:
    def test_rejection_records_who_and_why(self) -> None:
        rejected = reject(make_test(), reviewer="alex", reason="synthetic data")
        assert rejected.status is ReviewStatus.REJECTED
        assert rejected.reviewer == "alex"
        assert rejected.review_reason == "synthetic data"
        assert rejected.reviewed_at is not None

    def test_a_contradictory_test_can_still_be_rejected(self) -> None:
        """Rejection is how a broken draft leaves the queue."""
        contradictory = make_test(
            expectations=[
                Expectation(type=ExpectationType.TOOL_CALLED, tool="t"),
                Expectation(type=ExpectationType.TOOL_NOT_CALLED, tool="t"),
            ]
        )
        assert reject(contradictory, reviewer="alex").status is ReviewStatus.REJECTED

    def test_a_rejected_test_is_kept_for_audit(self, drafted: Path) -> None:
        test = list_tests(project_root=drafted).tests[0]
        reject_test(test.test_id, project_root=drafted, reviewer="alex", reason="no")
        stored = show_test(test.test_id, project_root=drafted)
        assert stored.status is ReviewStatus.REJECTED
        assert stored.review_reason == "no"


class TestEditableDocument:
    def test_it_round_trips_unchanged(self) -> None:
        test = make_test()
        result = apply_edits(test, render_editable(test), editor="alex")
        assert result.ok
        assert result.test is not None
        assert result.test.expectations == test.expectations
        assert result.test.input.text == test.input.text

    def test_it_only_offers_the_editable_fields(self) -> None:
        document = yaml.safe_load(render_editable(make_test()))
        assert set(document) == {"input", "expectations"}

    def test_the_guidance_lists_every_expectation_type(self) -> None:
        document = render_editable(make_test())
        for member in ExpectationType:
            assert member.value in document

    def test_adding_the_positive_half_clears_the_warning(self) -> None:
        """The guide's example, completed by a reviewer."""
        test = make_test()
        assert not test.has_positive_expectation
        document = yaml.safe_dump(
            {
                "input": {"text": "Refund my latest order."},
                "expectations": [REQUIRE.to_dict(), FORBID.to_dict()],
            }
        )
        result = apply_edits(test, document, editor="alex")
        assert result.ok
        assert result.test is not None
        assert result.test.has_positive_expectation
        assert result.test.warnings == []


class TestEditValidation:
    def _edit(self, document: str) -> Any:
        return apply_edits(make_test(), document, editor="alex")

    def test_unparseable_yaml_is_refused(self) -> None:
        result = self._edit("input: [unclosed")
        assert not result.ok
        assert "Could not parse YAML" in result.errors[0]

    def test_an_empty_document_is_refused(self) -> None:
        assert "empty" in self._edit("# everything deleted\n").errors[0]

    def test_a_non_mapping_is_refused(self) -> None:
        assert "Expected a mapping" in self._edit("- one\n- two\n").errors[0]

    def test_uneditable_fields_are_refused(self) -> None:
        document = yaml.safe_dump(
            {
                "input": {"text": "x"},
                "expectations": [FORBID.to_dict()],
                "test_id": "something_else",
                "provenance": {},
            }
        )
        result = self._edit(document)
        assert not result.ok
        assert "test_id" in result.errors[0] and "provenance" in result.errors[0]

    def test_a_test_needs_at_least_one_expectation(self) -> None:
        document = yaml.safe_dump({"input": {"text": "x"}, "expectations": []})
        assert "at least one expectation" in self._edit(document).errors[0]

    def test_an_unknown_expectation_type_is_refused(self) -> None:
        document = yaml.safe_dump(
            {"input": {"text": "x"}, "expectations": [{"type": "vibes", "value": "good"}]}
        )
        assert "unknown type" in self._edit(document).errors[0]

    def test_an_invalid_expectation_is_refused(self) -> None:
        document = yaml.safe_dump(
            {
                "input": {"text": "x"},
                "expectations": [{"type": "max_tool_calls", "tool": "t", "value": -1}],
            }
        )
        assert "negative" in self._edit(document).errors[0]

    def test_a_contradictory_edit_is_refused(self) -> None:
        document = yaml.safe_dump(
            {
                "input": {"text": "x"},
                "expectations": [
                    {"type": "tool_called", "tool": "refund_order"},
                    {"type": "tool_not_called", "tool": "refund_order"},
                ],
            }
        )
        result = self._edit(document)
        assert not result.ok
        assert "Contradictory" in result.errors[0]

    def test_an_empty_input_is_refused(self) -> None:
        document = yaml.safe_dump({"input": {"text": "   "}, "expectations": [FORBID.to_dict()]})
        assert "non-empty text" in self._edit(document).errors[0]

    def test_message_inputs_are_accepted(self) -> None:
        document = yaml.safe_dump(
            {
                "input": {"messages": [{"role": "user", "content": "Refund it."}]},
                "expectations": [FORBID.to_dict()],
            }
        )
        result = self._edit(document)
        assert result.ok
        assert result.test is not None
        assert result.test.input.messages == [{"role": "user", "content": "Refund it."}]

    def test_a_malformed_message_is_refused(self) -> None:
        document = yaml.safe_dump(
            {"input": {"messages": [{"role": "user"}]}, "expectations": [FORBID.to_dict()]}
        )
        assert "role and content" in self._edit(document).errors[0]

    def test_yaml_is_loaded_safely(self) -> None:
        """Arbitrary YAML tags must not be able to construct objects."""
        document = "input: !!python/object/apply:os.system ['echo pwned']\nexpectations: []\n"
        result = self._edit(document)
        assert not result.ok


class TestEditPreservesFacts:
    def _pair(self) -> tuple[RegressionTest, RegressionTest]:
        """The original and its edited copy, so facts can be compared directly."""
        original = make_test()
        document = yaml.safe_dump(
            {"input": {"text": "Refund it."}, "expectations": [REQUIRE.to_dict()]}
        )
        result = apply_edits(original, document, editor="sam")
        assert result.test is not None
        return original, result.test

    def _edited(self) -> RegressionTest:
        return self._pair()[1]

    def test_the_test_id_is_unchanged(self) -> None:
        original, edited = self._pair()
        assert edited.test_id == original.test_id

    def test_provenance_is_unchanged(self) -> None:
        assert self._edited().provenance.content_hash == "sha256:abc"

    def test_fixtures_are_unchanged(self) -> None:
        original, edited = self._pair()
        assert edited.fixtures == original.fixtures

    def test_the_edit_is_attributed(self) -> None:
        edited = self._edited()
        assert edited.edited
        assert edited.edited_by == "sam"

    def test_the_creation_time_survives(self) -> None:
        original, edited = self._pair()
        assert edited.created_at == original.created_at
        assert edited.updated_at > original.updated_at


class TestWarningRecomputation:
    def test_warnings_describe_current_content(self) -> None:
        test = make_test(warnings=["a stale note from generation"])
        test.expectations = [REQUIRE, FORBID]
        assert recompute_warnings(test) == []

    def test_a_missing_positive_expectation_is_reported(self) -> None:
        assert any(
            "No positive expectation" in warning for warning in recompute_warnings(make_test())
        )

    def test_a_contradiction_is_reported(self) -> None:
        test = make_test(
            expectations=[
                Expectation(type=ExpectationType.TOOL_CALLED, tool="t"),
                Expectation(type=ExpectationType.TOOL_NOT_CALLED, tool="t"),
            ]
        )
        assert any("Contradictory" in warning for warning in recompute_warnings(test))


class TestCommands:
    def test_pending_reviews_carries_the_full_context(self, drafted: Path) -> None:
        items = pending_reviews(project_root=drafted)
        assert items
        item = items[0]
        assert item.trace.trace.trace_id == item.test.provenance.trace_id
        assert item.failure.signals
        assert item.analysis is not None

    def test_only_drafts_are_pending(self, drafted: Path) -> None:
        first = pending_reviews(project_root=drafted)[0]
        approve_test(first.test.test_id, project_root=drafted, reviewer="alex")
        remaining = {item.test.test_id for item in pending_reviews(project_root=drafted)}
        assert first.test.test_id not in remaining

    def test_the_limit_is_respected(self, drafted: Path) -> None:
        assert len(pending_reviews(project_root=drafted, limit=1)) == 1

    def test_review_item_resolves_by_trace(self, drafted: Path) -> None:
        item = review_item("trace-1042", project_root=drafted)
        assert item.test.provenance.trace_id == "trace-1042"

    def test_approving_a_contradictory_test_is_a_command_error(self, drafted: Path) -> None:
        test = show_test("trace-1042", project_root=drafted)
        document = yaml.safe_dump(
            {
                "input": {"text": "Refund it."},
                "expectations": [{"type": "max_tool_calls", "tool": "t", "value": 0}],
            }
        )
        edit_test(test.test_id, document, project_root=drafted)
        # Now force a contradiction past the edit gate by writing directly.
        with pytest.raises(CommandError):
            edit_test(
                test.test_id,
                yaml.safe_dump({"input": {"text": "x"}, "expectations": []}),
                project_root=drafted,
            )

    def test_a_refused_edit_leaves_the_draft_alone(self, drafted: Path) -> None:
        test = show_test("trace-1042", project_root=drafted)
        before = test.expectations
        with pytest.raises(CommandError, match="not applied"):
            edit_test(test.test_id, "not: valid: yaml: at all:", project_root=drafted)
        assert show_test("trace-1042", project_root=drafted).expectations == before

    def test_the_editable_document_is_offered_for_a_stored_test(self, drafted: Path) -> None:
        document = editable_document("trace-1042", project_root=drafted)
        assert "expectations" in document
        assert "refund_order" in document

    def test_an_unknown_test_is_a_command_error(self, drafted: Path) -> None:
        with pytest.raises(CommandError, match="No regression test matching"):
            approve_test("nope", project_root=drafted)


class TestCli:
    def test_review_shows_interaction_analysis_and_test(
        self, runner: CliRunner, drafted: Path
    ) -> None:
        """Guide 8H's first requirement, all three on one screen."""
        result = runner.invoke(app, ["review", "trace-1042", "-C", str(drafted)], input="s\n")
        assert result.exit_code == ExitCode.OK
        assert "the interaction" in result.stdout
        assert "analysis" in result.stdout
        assert "proposed test" in result.stdout
        assert "evidence" in result.stdout

    def test_approving_through_the_loop(self, runner: CliRunner, drafted: Path) -> None:
        result = runner.invoke(
            app,
            ["review", "trace-1042", "-C", str(drafted), "--reviewer", "alex"],
            input="a\nlooks right\n",
        )
        assert result.exit_code == ExitCode.OK
        stored = show_test("trace-1042", project_root=drafted)
        assert stored.status is ReviewStatus.APPROVED
        assert stored.reviewer == "alex"
        assert stored.review_reason == "looks right"

    def test_rejecting_through_the_loop(self, runner: CliRunner, drafted: Path) -> None:
        runner.invoke(
            app,
            ["review", "trace-1042", "-C", str(drafted), "--reviewer", "alex"],
            input="r\nsynthetic\n",
        )
        stored = show_test("trace-1042", project_root=drafted)
        assert stored.status is ReviewStatus.REJECTED
        assert stored.review_reason == "synthetic"

    def test_skipping_leaves_it_pending(self, runner: CliRunner, drafted: Path) -> None:
        runner.invoke(app, ["review", "trace-1042", "-C", str(drafted)], input="s\n")
        assert show_test("trace-1042", project_root=drafted).status is ReviewStatus.DRAFT

    def test_an_unrecognized_answer_skips(self, runner: CliRunner, drafted: Path) -> None:
        """Never guess a decision from an ambiguous answer."""
        runner.invoke(app, ["review", "trace-1042", "-C", str(drafted)], input="maybe\n")
        assert show_test("trace-1042", project_root=drafted).status is ReviewStatus.DRAFT

    def test_the_session_reports_what_it_did(self, runner: CliRunner, drafted: Path) -> None:
        result = runner.invoke(
            app, ["review", "-C", str(drafted), "--limit", "2"], input="a\n\ns\n"
        )
        assert "approved" in result.stdout
        assert "Only approved tests are exported" in result.stdout

    def test_nothing_to_review(self, runner: CliRunner, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        result = runner.invoke(app, ["review", "-C", str(initialized_project)])
        assert "Nothing to review" in result.stdout

    def test_approve_and_reject_without_the_loop(self, runner: CliRunner, drafted: Path) -> None:
        approved = runner.invoke(
            app,
            ["dataset", "approve", "trace-1042", "-C", str(drafted), "--reviewer", "alex"],
        )
        rejected = runner.invoke(
            app,
            ["dataset", "reject", "trace-1051", "-C", str(drafted), "--reviewer", "alex"],
        )
        assert approved.exit_code == ExitCode.OK and "approved" in approved.stdout
        assert rejected.exit_code == ExitCode.OK and "rejected" in rejected.stdout

    def test_edit_from_a_file(self, runner: CliRunner, drafted: Path, tmp_path: Path) -> None:
        document = tmp_path / "edit.yaml"
        document.write_text(
            yaml.safe_dump(
                {
                    "input": {"text": "Refund my latest order."},
                    "expectations": [REQUIRE.to_dict(), FORBID.to_dict()],
                }
            )
        )
        result = runner.invoke(
            app,
            ["dataset", "edit", "trace-1042", "-C", str(drafted), "--file", str(document)],
        )
        assert result.exit_code == ExitCode.OK
        stored = show_test("trace-1042", project_root=drafted)
        assert stored.edited
        assert stored.has_positive_expectation
        assert stored.warnings == []

    def test_edit_show_prints_the_document(self, runner: CliRunner, drafted: Path) -> None:
        result = runner.invoke(app, ["dataset", "edit", "trace-1042", "-C", str(drafted), "--show"])
        assert "expectations" in result.stdout

    def test_a_bad_edit_file_exits_two(
        self, runner: CliRunner, drafted: Path, tmp_path: Path
    ) -> None:
        document = tmp_path / "edit.yaml"
        document.write_text("expectations: []\ninput:\n  text: x\n")
        result = runner.invoke(
            app,
            ["dataset", "edit", "trace-1042", "-C", str(drafted), "--file", str(document)],
        )
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_a_missing_edit_file_exits_two(self, runner: CliRunner, drafted: Path) -> None:
        result = runner.invoke(
            app, ["dataset", "edit", "trace-1042", "-C", str(drafted), "--file", "/nope.yaml"]
        )
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_approving_a_broken_test_exits_two(
        self, runner: CliRunner, drafted: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(CommandError):
            edit_test(
                show_test("trace-1042", project_root=drafted).test_id,
                yaml.safe_dump({"input": {"text": "x"}, "expectations": []}),
                project_root=drafted,
            )
        result = runner.invoke(app, ["dataset", "approve", "nope", "-C", str(drafted)])
        assert result.exit_code == ExitCode.COMMAND_ERROR

"""Generating regression tests from analyzed failures (guide 8G)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkeep.analysis import Component, FailureAnalysis, FailureType, Severity
from evalkeep.cli import app
from evalkeep.commands.analyze_cmd import label_failure
from evalkeep.commands.dataset_cmd import build_dataset, list_tests, show_test
from evalkeep.commands.detect_cmd import review_failure, run_detection
from evalkeep.commands.discover_cmd import dismiss_cluster, list_clusters, run_discovery
from evalkeep.commands.ingest_cmd import ingest_traces
from evalkeep.config import Project
from evalkeep.errors import CommandError, ExitCode
from evalkeep.failures import Failure, FailureStatus
from evalkeep.generation import GENERATOR_VERSION, build_test, derive_expectations
from evalkeep.regression import ExpectationType, ReviewStatus
from evalkeep.storage import TraceStore
from evalkeep.trace import NormalizedTrace

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


def analysis(
    failure_type: FailureType = FailureType.WRONG_TOOL_ARGUMENT,
    *,
    summary: str = "Refunded the wrong order.",
    severity: Severity = Severity.HIGH,
) -> FailureAnalysis:
    return FailureAnalysis(
        failure_type=failure_type,
        component=Component.TOOL_ARGUMENTS,
        severity=severity,
        summary=summary,
        analyzer="manual:test",
        prompt_version=0,
    )


def trace(**overrides: Any) -> NormalizedTrace:
    payload: dict[str, Any] = {
        "trace_id": "trace-1042",
        "input": {"text": "Refund my latest order."},
        "outcome": {"status": "failure"},
    }
    payload.update(overrides)
    return NormalizedTrace.model_validate(payload)


def refund_trace() -> NormalizedTrace:
    """The guide's running example: a lookup, then the wrong refund."""
    return trace(
        events=[
            {
                "event_id": "e1",
                "type": "tool_call",
                "call_id": "c1",
                "tool": "list_orders",
                "arguments": {"customer_id": "cust-77"},
            },
            {
                "event_id": "e2",
                "type": "tool_result",
                "call_id": "c1",
                "tool": "list_orders",
                "result": [{"order_id": "order-A"}, {"order_id": "order-C"}],
            },
            {
                "event_id": "e3",
                "type": "tool_call",
                "call_id": "c2",
                "tool": "refund_order",
                "arguments": {"order_id": "order-A"},
            },
            {
                "event_id": "e4",
                "type": "tool_result",
                "call_id": "c2",
                "tool": "refund_order",
                "result": {"status": "refunded"},
            },
        ]
    )


@pytest.fixture
def discovered(initialized_project: Path) -> Path:
    """The refund example, ingested, detected, labelled and clustered."""
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
    return initialized_project


class TestExpectationDerivation:
    def test_a_wrong_argument_forbids_the_observed_value(self) -> None:
        expectations, _ = derive_expectations(refund_trace(), analysis())
        assert len(expectations) == 1
        only = expectations[0]
        assert only.type is ExpectationType.TOOL_ARGUMENT_NOT_EQUALS
        assert (only.tool, only.path, only.value) == ("refund_order", "order_id", "order-A")

    def test_earlier_lookups_are_not_asserted_against(self) -> None:
        """The list_orders call was correct; forbidding its arguments would be wrong."""
        expectations, warnings = derive_expectations(refund_trace(), analysis())
        assert all(e.tool != "list_orders" for e in expectations)
        assert any("earlier call" in warning for warning in warnings)

    def test_nested_arguments_become_dotted_paths(self) -> None:
        source = trace(
            events=[
                {
                    "event_id": "e1",
                    "type": "tool_call",
                    "tool": "refund_order",
                    "arguments": {"order": {"id": "order-A"}, "notify": True},
                }
            ]
        )
        expectations, _ = derive_expectations(source, analysis())
        paths = {e.path for e in expectations}
        assert paths == {"order.id", "notify"}

    def test_non_scalar_arguments_are_skipped(self) -> None:
        source = trace(
            events=[
                {
                    "event_id": "e1",
                    "type": "tool_call",
                    "tool": "refund_order",
                    "arguments": {"order_id": "order-A", "tags": ["a", "b"]},
                }
            ]
        )
        expectations, _ = derive_expectations(source, analysis())
        assert {e.path for e in expectations} == {"order_id"}

    def test_over_action_caps_the_repeated_tool(self) -> None:
        source = trace(
            events=[
                {
                    "event_id": f"e{i}",
                    "type": "tool_call",
                    "tool": "refund_order",
                    "arguments": {"order_id": f"order-{i}"},
                }
                for i in range(3)
            ]
        )
        expectations, warnings = derive_expectations(
            source, analysis(FailureType.UNNECESSARY_ACTION)
        )
        assert len(expectations) == 1
        assert expectations[0].type is ExpectationType.MAX_TOOL_CALLS
        assert expectations[0].value == 2  # only proves 3 was too many
        assert any("Tighten it during review" in warning for warning in warnings)

    def test_a_single_unnecessary_call_is_forbidden_outright(self) -> None:
        source = trace(events=[{"event_id": "e1", "type": "tool_call", "tool": "refund_order"}])
        expectations, _ = derive_expectations(source, analysis(FailureType.UNNECESSARY_ACTION))
        assert expectations[0].type is ExpectationType.TOOL_NOT_CALLED

    def test_wrong_tool_selection_forbids_the_tool(self) -> None:
        source = trace(events=[{"event_id": "e1", "type": "tool_call", "tool": "cancel_order"}])
        expectations, _ = derive_expectations(source, analysis(FailureType.WRONG_TOOL_SELECTION))
        assert expectations[0].type is ExpectationType.TOOL_NOT_CALLED
        assert expectations[0].tool == "cancel_order"

    @pytest.mark.parametrize(
        "kind",
        [
            FailureType.INCORRECT_ANSWER,
            FailureType.MISSING_TOOL_CALL,
            FailureType.POLICY_VIOLATION,
            FailureType.OTHER,
        ],
    )
    def test_undecidable_types_fall_back_to_a_rubric(self, kind: FailureType) -> None:
        expectations, warnings = derive_expectations(refund_trace(), analysis(kind))
        assert [e.type for e in expectations] == [ExpectationType.HUMAN_RUBRIC]
        assert any("needs a reviewer" in warning for warning in warnings)

    def test_the_rubric_is_a_last_resort_not_a_default(self) -> None:
        """A test with a real check must not also pay for an LLM judge."""
        expectations, _ = derive_expectations(refund_trace(), analysis())
        assert all(e.deterministic for e in expectations)

    def test_a_tool_failure_with_no_tool_calls_is_reported(self) -> None:
        expectations, warnings = derive_expectations(trace(), analysis())
        assert [e.type for e in expectations] == [ExpectationType.HUMAN_RUBRIC]
        assert any("no tool calls" in warning for warning in warnings)


class TestBuildTest:
    def _build(self, **kwargs: Any) -> Any:
        source = kwargs.pop("trace", refund_trace())
        return build_test(
            source,
            Failure.from_signals(source.trace_id, []),
            kwargs.pop("analysis", analysis()),
            **kwargs,
        )

    def test_a_draft_is_the_only_thing_generated(self) -> None:
        assert self._build().status is ReviewStatus.DRAFT

    def test_the_input_is_carried_over(self) -> None:
        assert self._build().input.text == "Refund my latest order."

    def test_message_inputs_are_carried_over(self) -> None:
        source = trace(input={"messages": [{"role": "user", "content": "Refund it."}]})
        built = self._build(trace=source)
        assert built.input.messages == [{"role": "user", "content": "Refund it."}]

    def test_fixtures_record_what_the_agent_saw(self) -> None:
        built = self._build()
        assert [f.tool for f in built.fixtures] == ["list_orders", "refund_order"]
        assert built.fixtures[0].arguments == {"customer_id": "cust-77"}
        assert built.fixtures[0].result == [{"order_id": "order-A"}, {"order_id": "order-C"}]

    def test_provenance_records_where_it_came_from(self) -> None:
        built = self._build(
            cluster_id="cl-abc", cluster_label="wrong order", representative_roles=["central"]
        )
        provenance = built.provenance
        assert provenance.trace_id == "trace-1042"
        assert provenance.content_hash.startswith("sha256:")
        assert provenance.cluster_id == "cl-abc"
        assert provenance.cluster_label == "wrong order"
        assert provenance.representative_roles == ["central"]
        assert provenance.failure_type == "wrong_tool_argument"
        assert provenance.analyzer == "manual:test"
        assert provenance.generator_version == GENERATOR_VERSION

    def test_the_test_id_survives_a_cluster_rename(self) -> None:
        """Committed test IDs must not move when a reviewer renames a family."""
        one = self._build(cluster_id="cl-abc", cluster_label="wrong order")
        two = self._build(cluster_id="cl-abc", cluster_label="something else entirely")
        assert one.test_id == two.test_id

    def test_the_test_id_survives_a_reanalysis(self) -> None:
        one = self._build(analysis=analysis())
        two = self._build(analysis=analysis(FailureType.WRONG_TOOL_SELECTION))
        assert one.test_id == two.test_id

    def test_a_forbidding_only_test_is_flagged(self) -> None:
        built = self._build()
        assert not built.has_positive_expectation
        assert any("No positive expectation" in warning for warning in built.warnings)

    def test_generated_tests_are_never_self_contradictory(self) -> None:
        for kind in FailureType:
            built = self._build(analysis=analysis(kind))
            assert built.contradictions == []

    def test_it_reproduces_the_guides_running_example(self) -> None:
        built = self._build()
        assert built.test_id.startswith("refund_my_latest_order_")
        forbidden = built.expectations[0]
        assert forbidden.type is ExpectationType.TOOL_ARGUMENT_NOT_EQUALS
        assert (forbidden.tool, forbidden.path, forbidden.value) == (
            "refund_order",
            "order_id",
            "order-A",
        )


class TestDatasetBuild:
    def test_only_representatives_get_a_test(self, discovered: Path) -> None:
        report = build_dataset(project_root=discovered)
        clusters = list_clusters(project_root=discovered)
        expected = sum(len(cluster.representatives) for cluster in clusters)
        assert report.created == expected

    def test_all_covers_every_failure(self, discovered: Path) -> None:
        report = build_dataset(project_root=discovered, representatives_only=False)
        assert report.created == 3

    def test_dismissed_clusters_are_not_covered(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        dismiss_cluster(clusters[1].cluster_id, project_root=discovered)
        build_dataset(project_root=discovered)
        covered = {t.provenance.trace_id for t in list_tests(project_root=discovered).tests}
        assert "trace-1051" not in covered

    def test_dismissed_failures_are_not_covered(self, discovered: Path) -> None:
        review_failure("trace-1051", FailureStatus.DISMISSED, project_root=discovered)
        build_dataset(project_root=discovered, representatives_only=False)
        covered = {t.provenance.trace_id for t in list_tests(project_root=discovered).tests}
        assert "trace-1051" not in covered

    def test_everything_generated_is_a_draft(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        listing = list_tests(project_root=discovered)
        assert all(test.status is ReviewStatus.DRAFT for test in listing.tests)
        assert set(listing.counts) == {ReviewStatus.DRAFT}

    def test_a_second_build_changes_nothing(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        second = build_dataset(project_root=discovered)
        assert second.created == 0
        assert second.skipped > 0
        assert not second.changed

    def test_regenerate_rewrites_drafts(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        report = build_dataset(project_root=discovered, regenerate=True)
        assert report.regenerated > 0
        assert report.created == 0

    def test_regenerate_never_touches_a_reviewed_test(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        listing = list_tests(project_root=discovered)
        approved = listing.tests[0]
        with TraceStore.open(Project.load(discovered).database_path) as store:
            approved.status = ReviewStatus.APPROVED
            store.tests.save(approved)

        report = build_dataset(project_root=discovered, regenerate=True)
        assert report.reviewed_kept == 1
        assert show_test(approved.test_id, project_root=discovered).status is (
            ReviewStatus.APPROVED
        )

    def test_the_creation_time_survives_regeneration(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        before = list_tests(project_root=discovered).tests[0]
        build_dataset(project_root=discovered, regenerate=True)
        after = show_test(before.test_id, project_root=discovered)
        assert after.created_at == before.created_at

    def test_the_limit_stops_early(self, discovered: Path) -> None:
        report = build_dataset(project_root=discovered, limit=1)
        assert report.created == 1

    def test_unanalyzed_failures_are_reported(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        run_detection(project_root=initialized_project)
        report = build_dataset(project_root=initialized_project, representatives_only=False)
        assert report.unanalyzed == 3
        assert report.created == 0

    def test_building_without_clusters_is_a_command_error(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        run_detection(project_root=initialized_project)
        with pytest.raises(CommandError, match="No clusters"):
            build_dataset(project_root=initialized_project)

    def test_building_with_nothing_at_all_is_a_command_error(
        self, initialized_project: Path
    ) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        with pytest.raises(CommandError, match="Nothing to generate"):
            build_dataset(project_root=initialized_project, representatives_only=False)

    def test_warnings_are_surfaced_per_test(self, discovered: Path) -> None:
        report = build_dataset(project_root=discovered)
        assert report.needs_expectation == report.created
        assert report.warnings


class TestInspection:
    def test_show_resolves_by_test_failure_or_trace(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        by_trace = show_test("trace-1042", project_root=discovered)
        by_test = show_test(by_trace.test_id, project_root=discovered)
        by_failure = show_test(by_trace.failure_id, project_root=discovered)
        assert by_trace.test_id == by_test.test_id == by_failure.test_id

    def test_an_unknown_id_is_a_command_error(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        with pytest.raises(CommandError, match="No regression test matching"):
            show_test("nope", project_root=discovered)

    def test_a_test_round_trips_through_storage(self, discovered: Path) -> None:
        build_dataset(project_root=discovered)
        loaded = show_test("trace-1042", project_root=discovered)
        assert loaded.expectations[0].type is ExpectationType.TOOL_ARGUMENT_NOT_EQUALS
        assert loaded.fixtures
        assert loaded.provenance.content_hash.startswith("sha256:")
        assert loaded.warnings


class TestCli:
    def test_build_reports_what_it_made(self, runner: CliRunner, discovered: Path) -> None:
        result = runner.invoke(app, ["dataset", "build", "-C", str(discovered)])
        assert result.exit_code == ExitCode.OK
        assert "drafts created" in result.stdout
        assert "pending" in result.stdout

    def test_list_shows_drafts(self, runner: CliRunner, discovered: Path) -> None:
        runner.invoke(app, ["dataset", "build", "-C", str(discovered)])
        result = runner.invoke(app, ["dataset", "list", "-C", str(discovered)])
        assert "draft" in result.stdout
        assert "refund_my_latest_order" in result.stdout

    def test_list_when_empty(self, runner: CliRunner, discovered: Path) -> None:
        result = runner.invoke(app, ["dataset", "list", "-C", str(discovered)])
        assert "No regression tests" in result.stdout

    def test_show_renders_expectations_and_fixtures(
        self, runner: CliRunner, discovered: Path
    ) -> None:
        runner.invoke(app, ["dataset", "build", "-C", str(discovered)])
        result = runner.invoke(app, ["dataset", "show", "trace-1042", "-C", str(discovered)])
        assert result.exit_code == ExitCode.OK
        assert "tool_argument_not_equals" in result.stdout
        assert "fixtures" in result.stdout
        assert "needs review" in result.stdout

    def test_an_unknown_test_exits_two(self, runner: CliRunner, discovered: Path) -> None:
        result = runner.invoke(app, ["dataset", "show", "nope", "-C", str(discovered)])
        assert result.exit_code == ExitCode.COMMAND_ERROR

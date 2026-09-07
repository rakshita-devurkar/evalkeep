"""``from-traces``: the whole pipeline in one command, before anyone has
described anything.

The stages are individually coherent, but nine of them is a lot to understand
before finding out whether the tool is worth anything. This covers the path a
new user actually takes: a trace file, no analyzer, no labels.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from evalkeep.analysis import Component, FailureType, Severity
from evalkeep.cli import app
from evalkeep.clustering import (
    ClusterInput,
    average_linkage,
    build_clusters,
    derive_label,
    observation_text,
)
from evalkeep.commands.analyze_cmd import label_failure
from evalkeep.commands.dataset_cmd import list_tests
from evalkeep.commands.discover_cmd import list_clusters
from evalkeep.commands.pipeline_cmd import from_traces
from evalkeep.commands.review_cmd import approve_test
from evalkeep.config import ClusteringConfig
from evalkeep.embeddings import HashingEmbedder
from evalkeep.errors import CommandError, ExitCode
from evalkeep.generation import derive_expectations
from evalkeep.regression import ExpectationType, ReviewStatus
from evalkeep.trace import NormalizedTrace

EXAMPLE = Path(__file__).resolve().parents[1] / "src/evalkeep/examples/refund-agent/traces.jsonl"


def use_provider(project_root: Path, provider: str) -> None:
    config = project_root / "evalkeep.yaml"
    config.write_text(config.read_text().replace("provider: manual", f"provider: {provider}"))


class TestColdStart:
    """No analyzer, no labels — what a new user gets from one command."""

    def test_it_reaches_the_review_queue(self, initialized_project: Path) -> None:
        report = from_traces(EXAMPLE, project_root=initialized_project)
        assert report.traces == 5
        assert report.failures == 3
        assert report.families == 2
        assert report.pending_review == 3

    def test_the_families_match_what_describing_them_would_find(
        self, initialized_project: Path
    ) -> None:
        """Grouping on behaviour is weaker, but it should not be wrong."""
        from_traces(EXAMPLE, project_root=initialized_project)
        sizes = sorted(c.size for c in list_clusters(project_root=initialized_project))
        assert sizes == [1, 2]

    def test_drafts_are_generated_without_a_diagnosis(self, initialized_project: Path) -> None:
        report = from_traces(EXAMPLE, project_root=initialized_project)
        assert report.drafts == 3
        tests = list_tests(project_root=initialized_project).tests
        assert all(test.deterministic_expectations for test in tests)

    def test_the_second_count_qualifies_the_first(self, initialized_project: Path) -> None:
        """Reported as a remainder, three failures read as six findings."""
        report = from_traces(EXAMPLE, project_root=initialized_project)
        assert report.ready == 3
        assert report.needs_expectation <= report.ready

    def test_forbidding_the_observed_mistake_counts_as_coverage(
        self, initialized_project: Path
    ) -> None:
        """It is what makes the example suite fail on baseline and pass on the fix."""
        report = from_traces(EXAMPLE, project_root=initialized_project)
        assert report.ready == report.pending_review
        assert report.needs_expectation == 3

    def test_every_draft_says_it_is_undiagnosed(self, initialized_project: Path) -> None:
        from_traces(EXAMPLE, project_root=initialized_project)
        for test in list_tests(project_root=initialized_project).tests:
            assert any("has not been described" in w for w in test.warnings)

    def test_it_says_what_it_grouped_on(self, initialized_project: Path) -> None:
        report = from_traces(EXAMPLE, project_root=initialized_project)
        assert not report.described
        assert any("grouped by what was observed" in note for note in report.notes)

    def test_nothing_is_approved_on_your_behalf(self, initialized_project: Path) -> None:
        """It stops at the gate. Approving is a judgement, not a stage."""
        from_traces(EXAMPLE, project_root=initialized_project)
        listing = list_tests(project_root=initialized_project)
        assert listing.counts[ReviewStatus.DRAFT] == listing.total

    def test_running_twice_is_stable(self, initialized_project: Path) -> None:
        first = from_traces(EXAMPLE, project_root=initialized_project)
        second = from_traces(EXAMPLE, project_root=initialized_project)
        assert second.traces == 0
        assert second.already_known == 5
        assert second.families == first.families


class TestRerunning:
    """The README promises a re-run is safe. This is that promise."""

    def test_a_reviewed_test_is_never_rewritten(self, initialized_project: Path) -> None:
        from_traces(EXAMPLE, project_root=initialized_project)
        first = list_tests(project_root=initialized_project).tests[0]
        approve_test(first.test_id, project_root=initialized_project, reviewer="someone")

        from_traces(EXAMPLE, project_root=initialized_project)
        after = next(
            test
            for test in list_tests(project_root=initialized_project).tests
            if test.test_id == first.test_id
        )
        assert after.status is ReviewStatus.APPROVED
        assert after.reviewer == "someone"

    def test_unreviewed_drafts_pick_up_a_new_description(self, initialized_project: Path) -> None:
        """Labelling a failure and re-running used to leave the old draft in place."""
        from_traces(EXAMPLE, project_root=initialized_project)
        before = next(
            test
            for test in list_tests(project_root=initialized_project).tests
            if test.provenance.trace_id == "trace-1042"
        )
        assert before.provenance.failure_type is None

        label_failure(
            "trace-1042",
            failure_type=FailureType.WRONG_TOOL_ARGUMENT,
            component=Component.TOOL_ARGUMENTS,
            severity=Severity.HIGH,
            summary="Refunded the oldest order instead of the newest order.",
            project_root=initialized_project,
        )
        from_traces(EXAMPLE, project_root=initialized_project)
        after = next(
            test
            for test in list_tests(project_root=initialized_project).tests
            if test.provenance.trace_id == "trace-1042"
        )
        assert after.provenance.failure_type == "wrong_tool_argument"


class TestDegradation:
    def test_an_empty_trace_file_is_a_command_error(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        empty = tmp_path / "none.jsonl"
        empty.write_text("")
        with pytest.raises(CommandError, match="nothing to work with"):
            from_traces(empty, project_root=initialized_project)

    def test_traces_with_no_evidence_stop_cleanly(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        """Not an error: Evalkeep found nothing because there was nothing to find."""
        clean = tmp_path / "clean.jsonl"
        clean.write_text('{"trace_id":"t1","input":{"text":"hi"},"outcome":{"status":"success"}}\n')
        report = from_traces(clean, project_root=initialized_project)
        assert report.found_nothing
        assert report.families == 0

    def test_a_configured_but_broken_analyzer_degrades(
        self, initialized_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configured is not the same as working; it must not die on that."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        use_provider(initialized_project, "anthropic")
        report = from_traces(EXAMPLE, project_root=initialized_project)
        assert report.families == 2
        assert not report.described
        assert any("could not be described" in note for note in report.notes)

    def test_a_working_analyzer_describes_them(self, initialized_project: Path) -> None:
        use_provider(initialized_project, "stub")
        report = from_traces(EXAMPLE, project_root=initialized_project)
        assert report.described
        assert report.analyzed == 3
        assert not any("grouped by what was observed" in n for n in report.notes)


class TestObservationText:
    def test_behaviour_outweighs_wording(self) -> None:
        """Two reports of one bug are worded differently; the calls are not."""
        embedder = HashingEmbedder()
        same_bug = [
            observation_text(
                ["list_orders", "refund_order"],
                ["explicit_status"],
                ["Refunded the oldest order instead of the newest."],
            ),
            observation_text(
                ["list_orders", "refund_order"],
                ["failed_evaluator"],
                ["Expected order-F, got order-D."],
            ),
        ]
        other_bug = observation_text(
            ["refund_order"], ["explicit_status"], ["Refunded three orders; I asked for one."]
        )
        a, b, c = embedder.embed([*same_bug, other_bug])
        assert _distance(a, b) < _distance(a, c)

    def test_boilerplate_evidence_is_dropped(self) -> None:
        """It appears on every explicit failure, so it distinguishes none."""
        text = observation_text(
            ["refund_order"], ["explicit_status"], ["the trace is explicitly marked as failed"]
        )
        assert "explicitly marked" not in text

    def test_the_tools_are_weighted(self) -> None:
        text = observation_text(["refund_order"], [], ["something"])
        assert text.count("refund_order") > 1

    def test_it_survives_a_failure_with_no_tools(self) -> None:
        assert observation_text([], ["explicit_status"], ["went wrong"])


class TestUndescribedThreshold:
    def test_undescribed_input_uses_its_own_cut(self) -> None:
        """The text is a different shape, so its distances sit on a tighter scale."""
        config = ClusteringConfig()
        assert config.undescribed_threshold < config.threshold

    def test_the_documented_band_holds(self) -> None:
        """Measured stable from 0.35 to 0.50; a value in that band must group."""
        embedder = HashingEmbedder()
        texts = [
            observation_text(
                ["list_orders", "refund_order"],
                ["explicit_status"],
                ["Refunded the oldest order instead of the newest."],
            ),
            observation_text(
                ["list_orders", "refund_order"],
                ["failed_evaluator"],
                ["Expected order-F, got order-D."],
            ),
            observation_text(
                ["refund_order"], ["explicit_status"], ["Refunded three orders; I asked for one."]
            ),
        ]
        matrix = np.asarray(embedder.embed(texts))
        for threshold in (0.35, 0.40, 0.45, 0.50):
            labels = average_linkage(matrix, threshold)
            assert labels[0] == labels[1], threshold
            assert labels[2] != labels[0], threshold


class TestMixedPopulations:
    """One failure described by hand, the rest not -- the state the README's
    walkthrough creates, and the one that used to pick the wrong threshold."""

    def _mixed(self) -> tuple[list[ClusterInput], list[list[float]]]:
        described = [
            ClusterInput(f"d{i}", "refunded the oldest order instead of the newest")
            for i in range(2)
        ]
        undescribed = [
            ClusterInput.from_observation(
                f"u{i}",
                observation_text(["refund_order"], ["explicit_status"], ["it went wrong"]),
                behaviour="refund_order",
            )
            for i in range(2)
        ]
        inputs = [*described, *undescribed]
        return inputs, HashingEmbedder().embed([item.text for item in inputs])

    def test_the_two_kinds_are_never_grouped_together(self) -> None:
        """A distance between an account and a tool list does not mean anything."""
        inputs, vectors = self._mixed()
        clusters = build_clusters(inputs, vectors, ClusteringConfig())
        described_ids = {"d0", "d1"}
        for cluster in clusters:
            members = {member.failure_id for member in cluster.members}
            assert members <= described_ids or not (members & described_ids)

    def test_describing_one_failure_does_not_reshape_the_others(self) -> None:
        """It used to: one described input switched the whole run's threshold."""
        inputs, vectors = self._mixed()
        config = ClusteringConfig()
        mixed = build_clusters(inputs, vectors, config)
        alone = build_clusters(inputs[2:], vectors[2:], config)
        undescribed_sizes = sorted(
            cluster.size
            for cluster in mixed
            if not any(m.failure_id.startswith("d") for m in cluster.members)
        )
        assert undescribed_sizes == sorted(cluster.size for cluster in alone)


class TestLabelling:
    def test_an_undescribed_family_is_named_for_what_it_did(self) -> None:
        inputs = [
            ClusterInput.from_observation("f1", "x", behaviour="refund_order"),
            ClusterInput.from_observation("f2", "x", behaviour="refund_order"),
        ]
        assert derive_label(inputs) == "undescribed: refund_order"

    def test_it_never_reads_like_a_diagnosis(self) -> None:
        """A label that looks analysed when nothing was analysed would be a lie."""
        label = derive_label([ClusterInput.from_observation("f1", "x")])
        assert "undescribed" in label


class TestLabellingWithoutTools:
    """A ledger of outcomes rather than a trace of actions: real production
    exports carry a verdict and no tool calls, and every family then came out
    named "undescribed failures"."""

    def _family(self, evidence: list[str], count: int = 4) -> list[ClusterInput]:
        return [
            ClusterInput.from_observation(
                f"f{i}", observation_text([], ["explicit_status"], evidence)
            )
            for i in range(count)
        ]

    def test_a_family_is_named_for_what_its_members_share(self) -> None:
        label = derive_label(self._family(["output was too short", "output was too short"]))
        assert "too" in label and "short" in label

    def test_two_different_families_get_different_names(self) -> None:
        """The whole point: 239 families all reading the same thing name none."""
        short = derive_label(self._family(["the output was too short"]))
        forbidden = derive_label(self._family(["forbidden content appeared"]))
        assert short != forbidden

    def test_measurements_are_left_out_of_the_name(self) -> None:
        """`150` is what makes two instances differ, not what makes them a family."""
        label = derive_label(
            [
                ClusterInput.from_observation(
                    f"f{i}",
                    observation_text([], ["explicit_status"], [f"too short ({i}00 of 150)"]),
                )
                for i in range(4)
            ]
        )
        assert "150" not in label
        assert "short" in label

    def test_the_evidence_kind_never_becomes_the_name(self) -> None:
        """It is on every family, so it distinguishes none."""
        label = derive_label(self._family(["output was too short"]))
        assert "explicit_status" not in label

    def test_it_still_falls_back_when_there_is_nothing_to_share(self) -> None:
        assert derive_label([ClusterInput.from_observation("f1", "")]) == "undescribed failures"

    def test_a_tool_call_still_wins(self) -> None:
        """Behaviour names a family better than its wording does."""
        label = derive_label(
            [ClusterInput.from_observation("f1", "too short", behaviour="refund_order")]
        )
        assert label == "undescribed: refund_order"


class TestUndescribedGeneration:
    def test_the_observed_action_is_forbidden(self, initialized_project: Path) -> None:
        from_traces(EXAMPLE, project_root=initialized_project)
        tests = list_tests(project_root=initialized_project).tests
        forbidding = [
            expectation
            for test in tests
            for expectation in test.expectations
            if expectation.type is ExpectationType.TOOL_ARGUMENT_NOT_EQUALS
        ]
        assert forbidding

    def test_provenance_records_that_nothing_diagnosed_it(self, initialized_project: Path) -> None:
        from_traces(EXAMPLE, project_root=initialized_project)
        test = list_tests(project_root=initialized_project).tests[0]
        assert test.provenance.failure_type is None
        assert test.provenance.analyzer is None


class TestProseArguments:
    """Found on real tau-bench trajectories: an agent that gives up and calls
    `transfer_to_human_agents(summary="<300 words>")`. Forbidding that exact
    summary is a check no rewording can fail."""

    def _trace(self, arguments: dict[str, object]) -> NormalizedTrace:
        return NormalizedTrace.model_validate(
            {
                "trace_id": "t1",
                "input": {"text": "help me with my delayed flight"},
                "events": [
                    {
                        "event_id": "e1",
                        "type": "tool_call",
                        "tool": "escalate",
                        "arguments": arguments,
                    }
                ],
                "outcome": {"status": "failure"},
            }
        )

    def test_a_free_text_argument_is_not_asserted_on(self) -> None:
        prose = "The user is upset about a delayed flight and wants compensation " * 3
        expectations, _ = derive_expectations(self._trace({"summary": prose}), None)
        assert not any(e.type is ExpectationType.TOOL_ARGUMENT_NOT_EQUALS for e in expectations)

    def test_the_call_itself_is_forbidden_instead(self) -> None:
        """Making the call at all is the mistake, not how it was worded."""
        prose = "The user is upset about a delayed flight and wants compensation " * 3
        expectations, warnings = derive_expectations(self._trace({"summary": prose}), None)
        assert [e.type for e in expectations] == [ExpectationType.TOOL_NOT_CALLED]
        assert any("forbids the call itself" in w for w in warnings)

    def test_identifiers_are_still_asserted_on(self) -> None:
        """The fix must not throw away the arguments that do pin a mistake down."""
        expectations, _ = derive_expectations(
            self._trace({"order_id": "#W8528674", "reason": "no longer needed"}), None
        )
        paths = {e.path for e in expectations}
        assert "order_id" in paths

    def test_a_mixed_call_keeps_only_the_specific_argument(self) -> None:
        prose = "The customer explained at length that the item arrived damaged " * 3
        expectations, _ = derive_expectations(
            self._trace({"order_id": "#W1", "notes": prose}), None
        )
        assert {e.path for e in expectations} == {"order_id"}


class TestCli:
    def test_it_reports_the_shape_of_the_result(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        result = runner.invoke(app, ["from-traces", str(EXAMPLE), "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.OK
        assert "failure(s) found" in result.stdout
        assert "famil" in result.stdout
        assert "evalkeep review" in result.stdout

    def test_finding_nothing_explains_itself(
        self, runner: CliRunner, initialized_project: Path, tmp_path: Path
    ) -> None:
        clean = tmp_path / "clean.jsonl"
        clean.write_text('{"trace_id":"t1","input":{"text":"hi"},"outcome":{"status":"success"}}\n')
        result = runner.invoke(app, ["from-traces", str(clean), "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.OK
        assert "No failures found" in result.stdout
        assert "only reports evidence" in result.stdout

    def test_it_reads_other_formats_too(self, runner: CliRunner, initialized_project: Path) -> None:
        spans = EXAMPLE.parent.parent / "opentelemetry/spans.json"
        result = runner.invoke(
            app,
            ["from-traces", str(spans), "-C", str(initialized_project), "--format", "otlp"],
        )
        assert result.exit_code == ExitCode.OK
        assert "failure(s) found" in result.stdout


def _distance(one: list[float], two: list[float]) -> float:
    return 1 - sum(a * b for a, b in zip(one, two, strict=True))

"""Comparison, classification and paired statistics (guide 8J)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalsmith.cli import app
from evalsmith.commands.compare_cmd import (
    compare,
    current_baseline,
    list_runs,
    promote_baseline,
    show_run,
)
from evalsmith.comparison import (
    MIN_DISCORDANT_FOR_INTERVAL,
    CaseComparison,
    Classification,
    compare_results,
    paired_statistics,
)
from evalsmith.config import Project
from evalsmith.errors import CommandError, ExitCode
from evalsmith.runs import CaseResult, ErrorKind, EvaluationRun, Outcome, RunStatus
from evalsmith.storage import TraceStore
from evalsmith.storage.runs import AmbiguousRun

SUITE = "sha256:abc123"


def result(test_id: str, outcome: Outcome, **overrides: Any) -> CaseResult:
    return CaseResult(test_id=test_id, outcome=outcome, **overrides)


def run(run_id: str, target: str, *, suite: str = SUITE, **overrides: Any) -> EvaluationRun:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "target_id": target,
        "suite_hash": suite,
        "tests": 0,
        "status": RunStatus.COMPLETED,
    }
    payload.update(overrides)
    return EvaluationRun(**payload)


def comparison(kind: Classification) -> CaseComparison:
    return CaseComparison(test_id="t", classification=kind)


class TestTruthTable:
    """Guide 9.1, row by row."""

    def _classify(self, before: Outcome, after: Outcome, **kwargs: Any) -> Classification:
        report = compare_results(
            run("b", "baseline"),
            [result("t1", before, **kwargs)],
            run("c", "candidate"),
            [result("t1", after)],
        )
        return report.comparisons[0].classification

    def test_pass_pass_is_unchanged_pass(self) -> None:
        assert self._classify(Outcome.PASS, Outcome.PASS) is Classification.UNCHANGED_PASS

    def test_fail_pass_is_fixed(self) -> None:
        assert self._classify(Outcome.FAIL, Outcome.PASS) is Classification.FIXED

    def test_pass_fail_is_a_regression(self) -> None:
        assert self._classify(Outcome.PASS, Outcome.FAIL) is Classification.REGRESSION

    def test_fail_fail_is_unchanged_failure(self) -> None:
        assert self._classify(Outcome.FAIL, Outcome.FAIL) is Classification.UNCHANGED_FAILURE

    def test_an_error_before_is_not_comparable(self) -> None:
        assert self._classify(Outcome.ERROR, Outcome.PASS) is Classification.NOT_COMPARABLE

    def test_an_error_after_is_not_comparable(self) -> None:
        report = compare_results(
            run("b", "baseline"),
            [result("t1", Outcome.PASS)],
            run("c", "candidate"),
            [result("t1", Outcome.ERROR, error_kind=ErrorKind.TIMEOUT)],
        )
        assert report.comparisons[0].classification is Classification.NOT_COMPARABLE


class TestAlignment:
    def test_results_align_by_stable_test_id(self) -> None:
        """Order must not matter: alignment is by ID, never by position."""
        report = compare_results(
            run("b", "baseline"),
            [result("t2", Outcome.FAIL), result("t1", Outcome.PASS)],
            run("c", "candidate"),
            [result("t1", Outcome.PASS), result("t2", Outcome.PASS)],
        )
        by_id = {c.test_id: c.classification for c in report.comparisons}
        assert by_id == {
            "t1": Classification.UNCHANGED_PASS,
            "t2": Classification.FIXED,
        }

    def test_a_test_only_in_one_run_is_missing(self) -> None:
        report = compare_results(
            run("b", "baseline"),
            [result("t1", Outcome.PASS)],
            run("c", "candidate"),
            [result("t1", Outcome.PASS), result("t2", Outcome.PASS)],
        )
        missing = [c for c in report.comparisons if c.classification is Classification.MISSING]
        assert [c.test_id for c in missing] == ["t2"]
        assert "absent from the baseline run" in missing[0].reason

    def test_two_empty_runs_compare_to_nothing(self) -> None:
        report = compare_results(run("b", "baseline"), [], run("c", "candidate"), [])
        assert report.comparisons == []
        assert report.baseline_pass_rate is None


class TestErrorsAreExcluded:
    def _report(self) -> Any:
        return compare_results(
            run("b", "baseline"),
            [
                result("t1", Outcome.FAIL),
                result("t2", Outcome.ERROR, error_kind=ErrorKind.TIMEOUT),
                result("t3", Outcome.PASS),
            ],
            run("c", "candidate"),
            [
                result("t1", Outcome.PASS),
                result("t2", Outcome.PASS),
                result("t3", Outcome.PASS),
            ],
        )

    def test_errored_pairs_are_not_counted_as_failures(self) -> None:
        """An outage must never read as a regression."""
        report = self._report()
        assert report.counts.get(Classification.REGRESSION, 0) == 0
        assert report.counts[Classification.NOT_COMPARABLE] == 1

    def test_rates_are_computed_over_comparable_tests_only(self) -> None:
        report = self._report()
        assert len(report.comparable) == 2
        assert report.baseline_pass_rate == 0.5
        assert report.candidate_pass_rate == 1.0

    def test_the_excluded_pair_explains_itself(self) -> None:
        (excluded,) = self._report().excluded
        assert excluded.test_id == "t2"
        assert "timeout" in excluded.reason

    def test_all_errors_means_nothing_can_be_concluded(self) -> None:
        report = compare_results(
            run("b", "baseline"),
            [result("t1", Outcome.ERROR, error_kind=ErrorKind.EXECUTION_ERROR)],
            run("c", "candidate"),
            [result("t1", Outcome.PASS)],
        )
        assert report.comparable == []
        assert report.baseline_pass_rate is None
        assert report.statistics is None


class TestSuiteCompatibility:
    def test_matching_suites_are_compatible(self) -> None:
        report = compare_results(run("b", "baseline"), [], run("c", "candidate"), [])
        assert report.suite_compatible

    def test_different_suites_are_flagged(self) -> None:
        report = compare_results(
            run("b", "baseline"),
            [],
            run("c", "candidate", suite="sha256:different"),
            [],
        )
        assert not report.suite_compatible


class TestPairedStatistics:
    def test_no_comparable_tests_yields_nothing(self) -> None:
        assert paired_statistics([]) is None

    def test_no_change_is_not_evidence_of_no_change(self) -> None:
        statistics = paired_statistics([comparison(Classification.UNCHANGED_PASS)] * 5)
        assert statistics is not None
        assert statistics.p_value == 1.0
        assert statistics.discordant == 0
        assert statistics.note is not None and "nothing to test" in statistics.note

    def test_a_small_improvement_is_not_significant(self) -> None:
        """Three fixes out of three is 100%, and still not evidence."""
        statistics = paired_statistics([comparison(Classification.FIXED)] * 3)
        assert statistics is not None
        assert statistics.difference == 1.0
        assert statistics.p_value == pytest.approx(0.25)
        assert not statistics.significant

    def test_a_small_sample_gets_no_interval(self) -> None:
        statistics = paired_statistics([comparison(Classification.FIXED)] * 3)
        assert statistics is not None
        assert statistics.interval is None
        assert statistics.note is not None and "too few" in statistics.note

    def test_a_large_sample_gets_an_interval(self) -> None:
        comparisons = (
            [comparison(Classification.FIXED)] * 12
            + [comparison(Classification.REGRESSION)] * 2
            + [comparison(Classification.UNCHANGED_PASS)] * 36
        )
        statistics = paired_statistics(comparisons)
        assert statistics is not None
        assert statistics.interval is not None
        assert statistics.interval_method == "paired Wald, 95%"
        low, high = statistics.interval
        assert low < statistics.difference < high
        assert statistics.significant

    def test_the_interval_threshold_is_the_documented_one(self) -> None:
        just_under = [comparison(Classification.FIXED)] * (MIN_DISCORDANT_FOR_INTERVAL - 1)
        just_over = [comparison(Classification.FIXED)] * MIN_DISCORDANT_FOR_INTERVAL
        assert paired_statistics(just_under).interval is None  # type: ignore[union-attr]
        assert paired_statistics(just_over).interval is not None  # type: ignore[union-attr]

    def test_only_discordant_pairs_carry_information(self) -> None:
        """Adding tests both runs passed changes the rate, not the evidence."""
        few = paired_statistics([comparison(Classification.FIXED)] * 6)
        many = paired_statistics(
            [comparison(Classification.FIXED)] * 6
            + [comparison(Classification.UNCHANGED_PASS)] * 100
        )
        assert few is not None and many is not None
        assert few.p_value == many.p_value
        assert few.difference > many.difference

    def test_regressions_and_fixes_cancel(self) -> None:
        statistics = paired_statistics(
            [comparison(Classification.FIXED)] * 5 + [comparison(Classification.REGRESSION)] * 5
        )
        assert statistics is not None
        assert statistics.difference == 0.0
        assert statistics.p_value == 1.0

    def test_a_pure_regression_is_negative(self) -> None:
        statistics = paired_statistics([comparison(Classification.REGRESSION)] * 4)
        assert statistics is not None
        assert statistics.difference == -1.0


class TestCommands:
    @pytest.fixture
    def with_runs(self, initialized_project: Path) -> Path:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(
                run("aaaa1111bbbb2222", "baseline", tests=3),
                [
                    result("t1", Outcome.FAIL),
                    result("t2", Outcome.FAIL),
                    result("t3", Outcome.PASS),
                ],
            )
            store.runs.save(
                run(
                    "cccc3333dddd4444",
                    "candidate",
                    tests=3,
                    started_at=datetime.now(UTC) + timedelta(seconds=5),
                ),
                [
                    result("t1", Outcome.PASS),
                    result("t2", Outcome.PASS),
                    result("t3", Outcome.FAIL),
                ],
            )
        return initialized_project

    def test_comparing_by_target_name(self, with_runs: Path) -> None:
        report = compare(project_root=with_runs)
        assert report.counts[Classification.FIXED] == 2
        assert report.counts[Classification.REGRESSION] == 1

    def test_comparing_by_run_prefix(self, with_runs: Path) -> None:
        """A listing prints a prefix, so a prefix must be accepted back."""
        report = compare(project_root=with_runs, baseline="aaaa1111", candidate="cccc3333")
        assert report.baseline_run.run_id == "aaaa1111bbbb2222"

    def test_an_ambiguous_prefix_is_a_command_error(self, initialized_project: Path) -> None:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(run("abc111", "baseline"), [])
            store.runs.save(run("abc222", "baseline"), [])
            with pytest.raises(AmbiguousRun):
                store.runs.resolve("abc")
        with pytest.raises(CommandError, match="matches several runs"):
            compare(project_root=initialized_project, baseline="abc", candidate="abc111")

    def test_comparing_a_run_with_itself_is_refused(self, with_runs: Path) -> None:
        with pytest.raises(CommandError, match="same run"):
            compare(project_root=with_runs, baseline="aaaa1111", candidate="aaaa1111")

    def test_an_unknown_run_is_a_command_error(self, with_runs: Path) -> None:
        with pytest.raises(CommandError, match="No baseline run matching"):
            compare(project_root=with_runs, baseline="nope")

    def test_incompatible_suites_are_refused(self, initialized_project: Path) -> None:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(run("b1", "baseline"), [result("t1", Outcome.PASS)])
            store.runs.save(
                run("c1", "candidate", suite="sha256:other"), [result("t1", Outcome.PASS)]
            )
        with pytest.raises(CommandError, match="different test suites"):
            compare(project_root=initialized_project)

    def test_suite_drift_can_be_allowed_explicitly(self, initialized_project: Path) -> None:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(run("b1", "baseline"), [result("t1", Outcome.FAIL)])
            store.runs.save(
                run("c1", "candidate", suite="sha256:other"), [result("t1", Outcome.PASS)]
            )
        report = compare(project_root=initialized_project, allow_suite_drift=True)
        assert not report.suite_compatible
        assert report.counts[Classification.FIXED] == 1

    def test_listing_runs(self, with_runs: Path) -> None:
        summaries = list_runs(project_root=with_runs)
        assert {s.run.target_id for s in summaries} == {"baseline", "candidate"}
        assert not any(s.is_baseline for s in summaries)

    def test_showing_a_run(self, with_runs: Path) -> None:
        run_record, results = show_run("aaaa1111", project_root=with_runs)
        assert run_record.target_id == "baseline"
        assert len(results) == 3


class TestPromotion:
    @pytest.fixture
    def with_runs(self, initialized_project: Path) -> Path:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(run("aaaa1111", "baseline", tests=1), [result("t1", Outcome.PASS)])
            store.runs.save(run("bbbb2222", "candidate", tests=1), [result("t1", Outcome.PASS)])
            store.runs.save(
                run("cccc3333", "baseline", tests=1),
                [result("t1", Outcome.ERROR, error_kind=ErrorKind.TIMEOUT)],
            )
        return initialized_project

    def test_promotion_is_recorded_with_who_and_why(self, with_runs: Path) -> None:
        promotion = promote_baseline(
            "aaaa1111", project_root=with_runs, reviewer="alex", reason="shipped"
        )
        assert promotion.run_id == "aaaa1111"
        assert promotion.reviewer == "alex"
        assert promotion.reason == "shipped"

    def test_nothing_is_a_baseline_until_promoted(self, with_runs: Path) -> None:
        assert current_baseline(project_root=with_runs) is None

    def test_the_current_baseline_is_the_latest_promotion(self, with_runs: Path) -> None:
        promote_baseline("aaaa1111", project_root=with_runs, reviewer="alex")
        promote_baseline("bbbb2222", project_root=with_runs, reviewer="sam")
        current = current_baseline(project_root=with_runs)
        assert current is not None
        assert current[0].run_id == "bbbb2222"

    def test_history_is_kept(self, with_runs: Path) -> None:
        promote_baseline("aaaa1111", project_root=with_runs, reviewer="alex")
        promote_baseline("bbbb2222", project_root=with_runs, reviewer="sam")
        with TraceStore.open(Project.load(with_runs).database_path) as store:
            assert len(store.runs.promotions()) == 2

    def test_a_run_with_errors_cannot_be_promoted(self, with_runs: Path) -> None:
        """A reference point that half ran is not a reference point."""
        with pytest.raises(CommandError, match="never executed"):
            promote_baseline("cccc3333", project_root=with_runs, reviewer="alex")

    def test_an_unknown_run_cannot_be_promoted(self, with_runs: Path) -> None:
        with pytest.raises(CommandError, match="No run with ID"):
            promote_baseline("nope", project_root=with_runs, reviewer="alex")

    def test_a_promoted_run_becomes_the_comparison_baseline(self, with_runs: Path) -> None:
        promote_baseline("aaaa1111", project_root=with_runs, reviewer="alex")
        report = compare(project_root=with_runs, candidate="bbbb2222")
        assert report.baseline_run.run_id == "aaaa1111"

    def test_listing_marks_the_baseline(self, with_runs: Path) -> None:
        promote_baseline("aaaa1111", project_root=with_runs, reviewer="alex")
        summaries = list_runs(project_root=with_runs)
        assert [s.run.run_id for s in summaries if s.is_baseline] == ["aaaa1111"]


class TestCli:
    @pytest.fixture
    def with_runs(self, initialized_project: Path) -> Path:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(
                run("aaaa1111", "baseline", tests=2),
                [result("t1", Outcome.FAIL), result("t2", Outcome.PASS)],
            )
            store.runs.save(
                run(
                    "bbbb2222",
                    "candidate",
                    tests=2,
                    started_at=datetime.now(UTC) + timedelta(seconds=5),
                ),
                [result("t1", Outcome.PASS), result("t2", Outcome.FAIL)],
            )
        return initialized_project

    def test_compare_reports_the_truth_table(self, runner: CliRunner, with_runs: Path) -> None:
        result_ = runner.invoke(app, ["compare", "-C", str(with_runs)])
        assert result_.exit_code == ExitCode.OK
        assert "fixed" in result_.stdout
        assert "regression" in result_.stdout
        assert "p-value" in result_.stdout

    def test_fail_on_regression_exits_one(self, runner: CliRunner, with_runs: Path) -> None:
        result_ = runner.invoke(app, ["compare", "-C", str(with_runs), "--fail-on-regression"])
        assert result_.exit_code == ExitCode.RECORD_ERRORS

    def test_fail_on_regression_passes_when_clean(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(run("b1", "baseline"), [result("t1", Outcome.FAIL)])
            store.runs.save(
                run("c1", "candidate", started_at=datetime.now(UTC) + timedelta(seconds=5)),
                [result("t1", Outcome.PASS)],
            )
        result_ = runner.invoke(
            app, ["compare", "-C", str(initialized_project), "--fail-on-regression"]
        )
        assert result_.exit_code == ExitCode.OK

    def test_runs_list(self, runner: CliRunner, with_runs: Path) -> None:
        result_ = runner.invoke(app, ["runs", "list", "-C", str(with_runs)])
        assert result_.exit_code == ExitCode.OK
        assert "baseline" in result_.stdout

    def test_runs_list_when_empty(self, runner: CliRunner, initialized_project: Path) -> None:
        result_ = runner.invoke(app, ["runs", "list", "-C", str(initialized_project)])
        assert "No runs" in result_.stdout

    def test_runs_show(self, runner: CliRunner, with_runs: Path) -> None:
        result_ = runner.invoke(app, ["runs", "show", "aaaa1111", "-C", str(with_runs)])
        assert result_.exit_code == ExitCode.OK
        assert "t1" in result_.stdout

    def test_baseline_promote_and_show(self, runner: CliRunner, with_runs: Path) -> None:
        promoted = runner.invoke(
            app,
            ["baseline", "promote", "aaaa1111", "-C", str(with_runs), "--reviewer", "alex"],
        )
        shown = runner.invoke(app, ["baseline", "show", "-C", str(with_runs)])
        assert promoted.exit_code == ExitCode.OK and "promoted" in promoted.stdout
        assert "alex" in shown.stdout

    def test_baseline_show_without_a_promotion(self, runner: CliRunner, with_runs: Path) -> None:
        result_ = runner.invoke(app, ["baseline", "show", "-C", str(with_runs)])
        assert "No baseline has been promoted" in result_.stdout

    def test_comparing_without_runs_exits_two(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        result_ = runner.invoke(app, ["compare", "-C", str(initialized_project)])
        assert result_.exit_code == ExitCode.COMMAND_ERROR

    def test_an_all_error_comparison_says_so(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        with TraceStore.open(Project.load(initialized_project).database_path) as store:
            store.runs.save(
                run("b1", "baseline"),
                [result("t1", Outcome.ERROR, error_kind=ErrorKind.TIMEOUT)],
            )
            store.runs.save(
                run("c1", "candidate", started_at=datetime.now(UTC) + timedelta(seconds=5)),
                [result("t1", Outcome.PASS)],
            )
        result_ = runner.invoke(app, ["compare", "-C", str(initialized_project)])
        assert "Nothing can be concluded" in result_.stdout

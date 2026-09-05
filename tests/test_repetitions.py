"""Repeated execution and per-case confidence (roadmap gap 1)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from evalkeep.comparison import Classification, compare_results
from evalkeep.runs import (
    CaseResult,
    CaseSummary,
    ErrorKind,
    EvaluationRun,
    Outcome,
    Verdict,
    summarize,
    wilson_interval,
)
from evalkeep.storage import TraceStore
from evalkeep.storage.migrations import apply_migrations

SUITE = "sha256:abc"


def results(test_id: str, *outcomes: Outcome) -> list[CaseResult]:
    return [
        CaseResult(test_id=test_id, outcome=outcome, repetition=index)
        for index, outcome in enumerate(outcomes)
    ]


def summary(passed: int, failed: int = 0, errored: int = 0) -> CaseSummary:
    return CaseSummary(test_id="t1", passed=passed, failed=failed, errored=errored)


def run(run_id: str, target: str, repetitions: int = 1) -> EvaluationRun:
    return EvaluationRun(
        run_id=run_id,
        target_id=target,
        suite_hash=SUITE,
        tests=1,
        repetitions=repetitions,
    )


def classify(before: list[Outcome], after: list[Outcome]) -> Classification:
    report = compare_results(
        run("b", "baseline", len(before)),
        results("t1", *before),
        run("c", "candidate", len(after)),
        results("t1", *after),
    )
    return report.comparisons[0].classification


PASS, FAIL, ERROR = Outcome.PASS, Outcome.FAIL, Outcome.ERROR


class TestCaseSummary:
    def test_all_passes_is_a_pass(self) -> None:
        assert summary(passed=20).verdict is Verdict.PASS

    def test_all_failures_is_a_failure(self) -> None:
        assert summary(passed=0, failed=20).verdict is Verdict.FAIL

    def test_a_mix_is_flaky(self) -> None:
        """The finding a single execution cannot produce."""
        assert summary(passed=16, failed=4).verdict is Verdict.FLAKY
        assert summary(passed=16, failed=4).flaky

    def test_all_errors_is_an_error(self) -> None:
        assert summary(passed=0, errored=5).verdict is Verdict.ERROR

    def test_errors_are_not_evidence_either_way(self) -> None:
        one = summary(passed=8, failed=2, errored=10)
        assert one.evaluated == 10
        assert one.pass_rate == 0.8
        assert one.repetitions == 20

    def test_a_single_execution_still_gives_a_verdict(self) -> None:
        assert summary(passed=1).verdict is Verdict.PASS
        assert summary(passed=0, failed=1).verdict is Verdict.FAIL

    def test_summarize_groups_by_case(self) -> None:
        grouped = summarize(results("t1", PASS, FAIL) + results("t2", PASS, PASS))
        assert grouped["t1"].verdict is Verdict.FLAKY
        assert grouped["t2"].verdict is Verdict.PASS


class TestWilsonInterval:
    def test_nothing_measured_yields_nothing(self) -> None:
        assert wilson_interval(0, 0) is None

    def test_a_perfect_score_is_not_certainty(self) -> None:
        """20/20 is strong evidence and not proof; the interval must say so."""
        interval = wilson_interval(20, 20)
        assert interval is not None
        assert interval[0] < 1.0
        assert interval[1] == 1.0

    def test_more_trials_narrow_it(self) -> None:
        few = wilson_interval(8, 10)
        many = wilson_interval(80, 100)
        assert few is not None and many is not None
        assert (many[1] - many[0]) < (few[1] - few[0])

    def test_it_stays_within_zero_and_one(self) -> None:
        for successes, trials in ((0, 3), (3, 3), (1, 1)):
            low, high = wilson_interval(successes, trials)  # type: ignore[misc]
            assert 0.0 <= low <= high <= 1.0


class TestTruthTableWithOneRepetition:
    """With a single execution, the extended rules must reduce to the old ones."""

    def test_pass_pass(self) -> None:
        assert classify([PASS], [PASS]) is Classification.UNCHANGED_PASS

    def test_fail_pass(self) -> None:
        assert classify([FAIL], [PASS]) is Classification.FIXED

    def test_pass_fail(self) -> None:
        assert classify([PASS], [FAIL]) is Classification.REGRESSION

    def test_fail_fail(self) -> None:
        assert classify([FAIL], [FAIL]) is Classification.UNCHANGED_FAILURE

    def test_an_error_is_still_not_comparable(self) -> None:
        assert classify([ERROR], [PASS]) is Classification.NOT_COMPARABLE


class TestTruthTableWithRepetitions:
    def test_consistently_fixed(self) -> None:
        assert classify([FAIL] * 20, [PASS] * 20) is Classification.FIXED

    def test_mostly_fixed_is_only_likely(self) -> None:
        """The case the whole feature exists for: 16/20 is not a fix."""
        assert classify([FAIL] * 20, [PASS] * 16 + [FAIL] * 4) is Classification.LIKELY_FIXED

    def test_a_bare_majority_is_not_even_likely(self) -> None:
        """11/20 looks like a majority and is not evidence of one."""
        assert classify([FAIL] * 20, [PASS] * 11 + [FAIL] * 9) is Classification.UNCHANGED_FAILURE

    def test_becoming_unreliable_is_a_regression(self) -> None:
        """It used to always pass; now it sometimes does not."""
        assert classify([PASS] * 20, [PASS] * 18 + [FAIL] * 2) is Classification.REGRESSION

    def test_becoming_reliable_is_a_fix(self) -> None:
        assert classify([PASS] * 10 + [FAIL] * 10, [PASS] * 20) is Classification.FIXED

    def test_flaky_to_failing_is_a_regression(self) -> None:
        assert classify([PASS] * 10 + [FAIL] * 10, [FAIL] * 20) is Classification.REGRESSION

    def test_flaky_getting_better(self) -> None:
        assert (
            classify([PASS] * 4 + [FAIL] * 16, [PASS] * 17 + [FAIL] * 3)
            is Classification.LIKELY_FIXED
        )

    def test_flaky_getting_worse(self) -> None:
        assert (
            classify([PASS] * 17 + [FAIL] * 3, [PASS] * 4 + [FAIL] * 16)
            is Classification.REGRESSION
        )

    def test_flaky_and_unchanged(self) -> None:
        pattern = [PASS] * 10 + [FAIL] * 10
        assert classify(pattern, pattern) is Classification.UNCHANGED_FAILURE


class TestFlakinessIsSurfaced:
    def _report(self, before: list[Outcome], after: list[Outcome]) -> Any:
        return compare_results(
            run("b", "baseline", len(before)),
            results("t1", *before),
            run("c", "candidate", len(after)),
            results("t1", *after),
        )

    def test_a_flaky_case_is_flagged_as_well_as_classified(self) -> None:
        report = self._report([PASS] * 20, [PASS] * 18 + [FAIL] * 2)
        (comparison,) = report.comparisons
        assert comparison.classification is Classification.REGRESSION
        assert comparison.flaky
        assert report.flaky == [comparison]

    def test_a_stable_case_is_not_flagged(self) -> None:
        report = self._report([FAIL] * 20, [PASS] * 20)
        assert not report.comparisons[0].flaky
        assert report.flaky == []

    def test_the_rates_are_reported(self) -> None:
        report = self._report([FAIL] * 20, [PASS] * 16 + [FAIL] * 4)
        assert report.comparisons[0].rates == "before 0/20, after 16/20"

    def test_confidence_comes_from_the_candidate(self) -> None:
        report = self._report([FAIL] * 20, [PASS] * 16 + [FAIL] * 4)
        interval = report.comparisons[0].confidence
        assert interval is not None
        assert 0.5 < interval[0] < 0.8

    def test_a_single_execution_reports_no_rates(self) -> None:
        assert self._report([FAIL], [PASS]).comparisons[0].rates == ""


class TestRatesAndStatistics:
    def test_a_flaky_case_does_not_count_as_passing(self) -> None:
        """Counting it would reintroduce the overclaim repeating was meant to remove."""
        report = compare_results(
            run("b", "baseline", 20),
            results("t1", *([FAIL] * 20)),
            run("c", "candidate", 20),
            results("t1", *([PASS] * 16 + [FAIL] * 4)),
        )
        assert report.candidate_pass_rate == 0.0
        statistics = report.statistics
        assert statistics is not None
        assert statistics.fixed == 0
        assert statistics.p_value == 1.0

    def test_a_reliable_fix_does_count(self) -> None:
        report = compare_results(
            run("b", "baseline", 20),
            results("t1", *([FAIL] * 20)),
            run("c", "candidate", 20),
            results("t1", *([PASS] * 20)),
        )
        assert report.candidate_pass_rate == 1.0
        statistics = report.statistics
        assert statistics is not None
        assert statistics.fixed == 1


class TestRunnerInvocation:
    def test_repeat_is_only_passed_when_repeating(self, tmp_path: Path) -> None:
        from evalkeep.runner import execute

        captured: dict[str, Any] = {}

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake(monkeypatch: Any, repetitions: int) -> list[str]:
            import subprocess

            original = subprocess.run

            def spy(argv: Any, **kwargs: Any) -> Any:
                if not (isinstance(argv, list) and "eval" in argv):
                    return original(argv, **kwargs)
                captured["argv"] = argv
                directory = Path(str(argv[argv.index("--output") + 1])).parent
                (directory / "results.json").write_text(
                    json.dumps({"results": {"results": [], "version": 3}})
                )
                return Completed()

            monkeypatch.setattr(subprocess, "run", spy)
            execute(
                [],
                _script_target(),
                directory=tmp_path / f"run{repetitions}",
                command=["runner"],
                timeout_seconds=60,
                working_directory=tmp_path,
                repetitions=repetitions,
            )
            return list(captured["argv"])

        with pytest.MonkeyPatch.context() as monkeypatch:
            once = fake(monkeypatch, 1)
        with pytest.MonkeyPatch.context() as monkeypatch:
            many = fake(monkeypatch, 20)

        assert "--repeat" not in once
        assert many[many.index("--repeat") + 1] == "20"

    def test_imported_results_are_numbered(self, tmp_path: Path) -> None:
        from evalkeep.runner import import_results

        path = tmp_path / "results.json"
        records = [
            {
                "testCase": {"description": "t1", "metadata": {"test_id": "t1"}},
                "success": index % 2 == 0,
                "failureReason": 0 if index % 2 == 0 else 1,
            }
            for index in range(4)
        ]
        path.write_text(json.dumps({"results": {"results": records, "version": 3}}))

        imported = import_results(path)
        assert [r.repetition for r in imported] == [0, 1, 2, 3]
        assert summarize(imported)["t1"].verdict is Verdict.FLAKY


class TestStorage:
    def test_migration_ten_adds_the_repetition_key(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(test_results)")}
        assert "repetition" in columns
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
        assert "repetitions" in run_columns

    def test_repetitions_round_trip(self, tmp_path: Path) -> None:
        with TraceStore.open(tmp_path / "database.db") as store:
            store.runs.save(run("r1", "candidate", 3), results("t1", PASS, FAIL, PASS))
            loaded = store.runs.get("r1")
            assert loaded is not None and loaded.repetitions == 3
            assert [r.repetition for r in store.runs.results("r1")] == [0, 1, 2]
            assert store.runs.summaries("r1")["t1"].verdict is Verdict.FLAKY

    def test_saving_twice_replaces_every_repetition(self, tmp_path: Path) -> None:
        with TraceStore.open(tmp_path / "database.db") as store:
            store.runs.save(run("r1", "candidate", 3), results("t1", PASS, FAIL, PASS))
            store.runs.save(run("r1", "candidate", 1), results("t1", PASS))
            assert len(store.runs.results("r1")) == 1

    def test_errors_still_survive_the_round_trip(self, tmp_path: Path) -> None:
        with TraceStore.open(tmp_path / "database.db") as store:
            store.runs.save(
                run("r1", "candidate", 2),
                [
                    CaseResult(test_id="t1", outcome=ERROR, error_kind=ErrorKind.TIMEOUT),
                    CaseResult(test_id="t1", outcome=PASS, repetition=1),
                ],
            )
            stored = store.runs.summaries("r1")["t1"]
            assert (stored.errored, stored.passed) == (1, 1)
            assert stored.verdict is Verdict.PASS  # the one that ran, passed


def _script_target() -> Any:
    from evalkeep.targets import Target, TargetKind

    return Target(target_id="t", kind=TargetKind.PYTHON, path="agent.py")

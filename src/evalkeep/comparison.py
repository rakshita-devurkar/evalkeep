"""Comparing two runs, and saying only what the numbers support.

The whole pipeline exists to answer one question -- did this change make the
agent better or worse -- and this is where that answer is produced. Three rules
shape it, all of them about not overclaiming:

* **A test that errored is not a data point.** An error says the harness or the
  target broke, not that the agent got the answer wrong. Errored pairs are
  excluded from every count and reported separately, because letting an outage
  read as a regression is the single most damaging mistake this tool could make.
* **Two runs are only comparable if they answered the same questions.** Runs
  carry a suite hash; comparing across different suites is refused unless the
  caller explicitly asks for the intersection.
* **A confidence interval is only reported when it means something.** With a
  handful of discordant pairs the normal approximation is not trustworthy, so
  the interval is withheld and the reason is printed instead of a number that
  would look authoritative and be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import comb

from evalkeep.runs import (
    CaseResult,
    CaseSummary,
    EvaluationRun,
    Outcome,
    Verdict,
    summarize,
)

#: Below this many discordant pairs the normal approximation behind the interval
#: is not trustworthy, so no interval is reported. A common rule of thumb, and
#: chosen here because being silent is better than being confidently wrong.
MIN_DISCORDANT_FOR_INTERVAL = 10

#: 95% two-sided normal quantile.
_Z = 1.959963984540054


class Classification(StrEnum):
    """Guide 9.1's truth table, with its two error rows made explicit."""

    UNCHANGED_PASS = "unchanged_pass"
    FIXED = "fixed"
    #: Improved and now passes most of the time, but not every time. Only
    #: reachable with repetitions -- a single execution cannot tell the
    #: difference between this and `fixed`, which is the whole reason to repeat.
    LIKELY_FIXED = "likely_fixed"
    REGRESSION = "regression"
    UNCHANGED_FAILURE = "unchanged_failure"
    #: One side never ran. Excluded from the counts, reported on its own.
    NOT_COMPARABLE = "not_comparable"
    #: Present in one run and absent from the other.
    MISSING = "missing"


#: The four classifications that say something about the agent.
COMPARABLE = (
    Classification.UNCHANGED_PASS,
    Classification.FIXED,
    Classification.LIKELY_FIXED,
    Classification.REGRESSION,
    Classification.UNCHANGED_FAILURE,
)

#: Classifications that count as the candidate passing, for the paired test.
#: A case that only sometimes passes is not counted as passing: the point of
#: repeating was to stop calling that a fix.
_PASSING = (Classification.UNCHANGED_PASS, Classification.FIXED)


@dataclass(frozen=True)
class CaseComparison:
    test_id: str
    classification: Classification
    baseline: CaseResult | None = None
    candidate: CaseResult | None = None
    baseline_summary: CaseSummary | None = None
    candidate_summary: CaseSummary | None = None

    @property
    def flaky(self) -> bool:
        """Whether either side was inconsistent across its repetitions.

        Orthogonal to the classification: a case can be both a regression and
        flaky, and hiding one behind the other would lose a real finding.
        """
        return any(
            summary is not None and summary.flaky
            for summary in (self.baseline_summary, self.candidate_summary)
        )

    @property
    def confidence(self) -> tuple[float, float] | None:
        """How reliably the candidate passes this case, if it was repeated."""
        if self.candidate_summary is None:
            return None
        return self.candidate_summary.confidence

    @property
    def reason(self) -> str:
        """Why this pair is not comparable, when it is not."""
        if self.classification is Classification.MISSING:
            side = "candidate" if self.baseline_summary is not None else "baseline"
            return f"absent from the {side} run"
        for label, summary, result in (
            ("baseline", self.baseline_summary, self.baseline),
            ("candidate", self.candidate_summary, self.candidate),
        ):
            if summary is not None and summary.verdict is Verdict.ERROR:
                kind = (
                    result.error_kind.value if result is not None and result.error_kind else "error"
                )
                return f"{label} {kind}"
        return ""

    @property
    def rates(self) -> str:
        """How often each side passed, when either was repeated."""
        parts = []
        for label, summary in (
            ("before", self.baseline_summary),
            ("after", self.candidate_summary),
        ):
            if summary is not None and summary.repetitions > 1:
                parts.append(f"{label} {summary.passed}/{summary.evaluated}")
        return ", ".join(parts)


@dataclass
class PairedStatistics:
    """Paired analysis over the tests both runs actually evaluated."""

    pairs: int
    fixed: int
    regressions: int
    difference: float
    p_value: float
    interval: tuple[float, float] | None = None
    interval_method: str | None = None
    #: Why an interval was withheld, when it was.
    note: str | None = None

    @property
    def discordant(self) -> int:
        """Pairs where the two runs disagreed. All the information is here."""
        return self.fixed + self.regressions

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05


@dataclass
class ComparisonReport:
    baseline_run: EvaluationRun
    candidate_run: EvaluationRun
    comparisons: list[CaseComparison] = field(default_factory=list)
    suite_compatible: bool = True

    @property
    def counts(self) -> dict[Classification, int]:
        tally: dict[Classification, int] = {}
        for comparison in self.comparisons:
            tally[comparison.classification] = tally.get(comparison.classification, 0) + 1
        return tally

    @property
    def comparable(self) -> list[CaseComparison]:
        return [c for c in self.comparisons if c.classification in COMPARABLE]

    @property
    def excluded(self) -> list[CaseComparison]:
        return [c for c in self.comparisons if c.classification not in COMPARABLE]

    @property
    def regressions(self) -> list[CaseComparison]:
        return [c for c in self.comparisons if c.classification is Classification.REGRESSION]

    @property
    def fixes(self) -> list[CaseComparison]:
        return [c for c in self.comparisons if c.classification is Classification.FIXED]

    @property
    def baseline_pass_rate(self) -> float | None:
        return _rate(_passing(self.comparable, before=True), len(self.comparable))

    @property
    def candidate_pass_rate(self) -> float | None:
        """The share of cases that pass *reliably*.

        A case that passes nine times in ten is not counted as passing here.
        Counting it would reintroduce exactly the overclaim that repeating the
        run was meant to remove.
        """
        return _rate(_passing(self.comparable, before=False), len(self.comparable))

    @property
    def flaky(self) -> list[CaseComparison]:
        return [c for c in self.comparisons if c.flaky]

    @property
    def repeated(self) -> bool:
        return max(self.baseline_run.repetitions, self.candidate_run.repetitions) > 1

    @property
    def statistics(self) -> PairedStatistics | None:
        return paired_statistics(self.comparable)


def compare_results(
    baseline_run: EvaluationRun,
    baseline_results: list[CaseResult],
    candidate_run: EvaluationRun,
    candidate_results: list[CaseResult],
) -> ComparisonReport:
    """Align two runs by stable test ID and classify every case."""
    baseline_summaries = summarize(baseline_results)
    candidate_summaries = summarize(candidate_results)
    baseline_first = _first_by_case(baseline_results)
    candidate_first = _first_by_case(candidate_results)

    comparisons = [
        _classify(
            test_id,
            baseline_summaries.get(test_id),
            candidate_summaries.get(test_id),
            baseline_first.get(test_id),
            candidate_first.get(test_id),
        )
        for test_id in sorted(set(baseline_summaries) | set(candidate_summaries))
    ]
    return ComparisonReport(
        baseline_run=baseline_run,
        candidate_run=candidate_run,
        comparisons=comparisons,
        suite_compatible=baseline_run.suite_hash == candidate_run.suite_hash,
    )


def _first_by_case(results: list[CaseResult]) -> dict[str, CaseResult]:
    """One representative execution per case, for showing a failure reason."""
    first: dict[str, CaseResult] = {}
    for result in results:
        first.setdefault(result.test_id, result)
        if result.outcome is Outcome.FAIL:
            first[result.test_id] = result
    return first


def _classify(
    test_id: str,
    baseline: CaseSummary | None,
    candidate: CaseSummary | None,
    baseline_result: CaseResult | None,
    candidate_result: CaseResult | None,
) -> CaseComparison:
    """Extend the truth table from single outcomes to repeated ones.

    With one repetition each side is either PASS or FAIL and this reduces
    exactly to the original four rows. With more, a third verdict appears --
    FLAKY -- and it is what stops a single lucky pass being reported as a fix.
    """
    if baseline is None or candidate is None:
        classification = Classification.MISSING
    elif baseline.verdict is Verdict.ERROR or candidate.verdict is Verdict.ERROR:
        classification = Classification.NOT_COMPARABLE
    else:
        classification = _direction(baseline, candidate)

    return CaseComparison(
        test_id=test_id,
        classification=classification,
        baseline=baseline_result,
        candidate=candidate_result,
        baseline_summary=baseline,
        candidate_summary=candidate,
    )


def _direction(baseline: CaseSummary, candidate: CaseSummary) -> Classification:
    before, after = baseline.verdict, candidate.verdict

    if after is Verdict.PASS:
        # Reliably passing now. It is a fix unless it was already reliable.
        return Classification.UNCHANGED_PASS if before is Verdict.PASS else Classification.FIXED

    if after is Verdict.FAIL:
        # Never passes now. A regression only if it used to pass at all.
        return (
            Classification.UNCHANGED_FAILURE
            if before is Verdict.FAIL
            else Classification.REGRESSION
        )

    # The candidate is flaky. Whether that is progress depends on what it was.
    if before is Verdict.PASS:
        # It used to always pass and now sometimes does not. That is worse,
        # whatever the rate says.
        return Classification.REGRESSION
    if before is Verdict.FAIL:
        return (
            Classification.LIKELY_FIXED
            if _passes_more_often_than_not(candidate)
            else Classification.UNCHANGED_FAILURE
        )

    # Flaky before and flaky after: compare how often, not whether.
    before_rate = baseline.pass_rate or 0.0
    after_rate = candidate.pass_rate or 0.0
    if after_rate > before_rate:
        return (
            Classification.LIKELY_FIXED
            if _passes_more_often_than_not(candidate)
            else Classification.UNCHANGED_FAILURE
        )
    if after_rate < before_rate:
        return Classification.REGRESSION
    return Classification.UNCHANGED_FAILURE


def _passes_more_often_than_not(summary: CaseSummary) -> bool:
    """True only when the sample supports the claim, not merely suggests it.

    Two passes out of three looks like a majority and is not evidence of one;
    the lower bound of the interval is what decides.
    """
    interval = summary.confidence
    return interval is not None and interval[0] > 0.5


def paired_statistics(comparable: list[CaseComparison]) -> PairedStatistics | None:
    """McNemar's exact test over the pairs, with an interval only when earned.

    The test is exact rather than the chi-square approximation: suites here are
    often small, and the approximation is unreliable exactly where these suites
    live. Only discordant pairs carry information -- a test both runs passed
    says nothing about whether anything changed -- so the test is a two-sided
    binomial on fixes versus regressions.
    """
    pairs = len(comparable)
    if pairs == 0:
        return None

    fixed = sum(1 for c in comparable if c.classification is Classification.FIXED)
    regressions = sum(1 for c in comparable if c.classification is Classification.REGRESSION)
    difference = (fixed - regressions) / pairs

    discordant = fixed + regressions
    if discordant == 0:
        # Nothing changed on any test. There is no evidence of a difference,
        # which is not the same as evidence of no difference.
        return PairedStatistics(
            pairs=pairs,
            fixed=0,
            regressions=0,
            difference=0.0,
            p_value=1.0,
            note="No test changed outcome, so there is nothing to test.",
        )

    p_value = exact_binomial_two_sided(fixed, discordant)

    statistics = PairedStatistics(
        pairs=pairs,
        fixed=fixed,
        regressions=regressions,
        difference=difference,
        p_value=p_value,
    )

    if discordant < MIN_DISCORDANT_FOR_INTERVAL:
        statistics.note = (
            f"Only {discordant} test(s) changed outcome; that is too few for a "
            "trustworthy interval, so none is given."
        )
        return statistics

    # Wald interval for the paired difference in proportions.
    variance = (fixed + regressions - (fixed - regressions) ** 2 / pairs) / pairs**2
    margin = _Z * (variance**0.5)
    statistics.interval = (
        max(-1.0, difference - margin),
        min(1.0, difference + margin),
    )
    statistics.interval_method = "paired Wald, 95%"
    return statistics


def exact_binomial_two_sided(successes: int, trials: int) -> float:
    """The exact two-sided binomial p-value at p = 0.5.

    This is McNemar's exact test: under the null, each discordant pair is a coin
    flip, so the question is how surprising this split would be. At p = 0.5 the
    distribution is symmetric, which makes the two-sided value simply both tails
    of the more extreme side -- no approximation, and no reason to carry SciPy's
    82 MB for one call. Checked against `scipy.stats.binomtest` for every split
    up to sixty trials before that dependency was removed; the largest
    disagreement was 5.6e-16, which is floating-point noise.
    """
    if trials <= 0:
        return 1.0
    smaller = min(successes, trials - successes)
    tail = sum(comb(trials, k) for k in range(smaller + 1)) / 2**trials
    return float(min(1.0, 2 * tail))


def _reliably_passing(summary: CaseSummary | None) -> bool:
    return summary is not None and summary.verdict is Verdict.PASS


def _passing(comparisons: list[CaseComparison], *, before: bool) -> int:
    return sum(
        1
        for c in comparisons
        if _reliably_passing(c.baseline_summary if before else c.candidate_summary)
    )


def _became(comparison: CaseComparison, *, passing: bool) -> bool:
    """Whether this case crossed the pass/not-pass line in the given direction."""
    was = _reliably_passing(comparison.baseline_summary)
    now = _reliably_passing(comparison.candidate_summary)
    return (now and not was) if passing else (was and not now)


def _rate(passed: int, total: int) -> float | None:
    return passed / total if total else None

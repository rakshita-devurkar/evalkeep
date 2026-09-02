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

from scipy.stats import binomtest

from evalsmith.runs import CaseResult, EvaluationRun, Outcome

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
    Classification.REGRESSION,
    Classification.UNCHANGED_FAILURE,
)


@dataclass(frozen=True)
class CaseComparison:
    test_id: str
    classification: Classification
    baseline: CaseResult | None = None
    candidate: CaseResult | None = None

    @property
    def reason(self) -> str:
        """Why this pair is not comparable, when it is not."""
        if self.classification is Classification.MISSING:
            side = "candidate" if self.baseline is not None else "baseline"
            return f"absent from the {side} run"
        for label, result in (("baseline", self.baseline), ("candidate", self.candidate)):
            if result is not None and result.outcome is Outcome.ERROR:
                kind = result.error_kind.value if result.error_kind else "error"
                return f"{label} {kind}"
        return ""


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
        return _rate(
            sum(
                1
                for c in self.comparable
                if c.baseline is not None and c.baseline.outcome is Outcome.PASS
            ),
            len(self.comparable),
        )

    @property
    def candidate_pass_rate(self) -> float | None:
        return _rate(
            sum(
                1
                for c in self.comparable
                if c.candidate is not None and c.candidate.outcome is Outcome.PASS
            ),
            len(self.comparable),
        )

    @property
    def statistics(self) -> PairedStatistics | None:
        return paired_statistics(self.comparable)


def compare_results(
    baseline_run: EvaluationRun,
    baseline_results: list[CaseResult],
    candidate_run: EvaluationRun,
    candidate_results: list[CaseResult],
) -> ComparisonReport:
    """Align two runs by stable test ID and classify every pair."""
    baseline_by_id = {result.test_id: result for result in baseline_results}
    candidate_by_id = {result.test_id: result for result in candidate_results}

    comparisons = [
        _classify(test_id, baseline_by_id.get(test_id), candidate_by_id.get(test_id))
        for test_id in sorted(set(baseline_by_id) | set(candidate_by_id))
    ]
    return ComparisonReport(
        baseline_run=baseline_run,
        candidate_run=candidate_run,
        comparisons=comparisons,
        suite_compatible=baseline_run.suite_hash == candidate_run.suite_hash,
    )


def _classify(
    test_id: str, baseline: CaseResult | None, candidate: CaseResult | None
) -> CaseComparison:
    if baseline is None or candidate is None:
        classification = Classification.MISSING
    elif not baseline.comparable or not candidate.comparable:
        # An error on either side removes the pair from the analysis entirely.
        classification = Classification.NOT_COMPARABLE
    else:
        passed_before = baseline.outcome is Outcome.PASS
        passed_after = candidate.outcome is Outcome.PASS
        classification = {
            (True, True): Classification.UNCHANGED_PASS,
            (False, True): Classification.FIXED,
            (True, False): Classification.REGRESSION,
            (False, False): Classification.UNCHANGED_FAILURE,
        }[(passed_before, passed_after)]

    return CaseComparison(
        test_id=test_id,
        classification=classification,
        baseline=baseline,
        candidate=candidate,
    )


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

    p_value = float(binomtest(fixed, discordant, 0.5, alternative="two-sided").pvalue)

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


def _rate(passed: int, total: int) -> float | None:
    return passed / total if total else None

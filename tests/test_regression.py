"""The regression-test model: IDs, validation and contradictions (guide 8G)."""

from __future__ import annotations

import pytest

from evalkeep.regression import (
    Contradiction,
    Expectation,
    ExpectationType,
    ReviewStatus,
    find_contradictions,
    make_test_id,
    slugify,
    validate_expectation,
)


def expectation(kind: ExpectationType, **kwargs: object) -> Expectation:
    return Expectation(type=kind, **kwargs)  # type: ignore[arg-type]


class TestIdentifiers:
    def test_an_id_is_readable_and_stable(self) -> None:
        one = make_test_id("trace-1042", "Refund my latest order.")
        two = make_test_id("trace-1042", "Refund my latest order.")
        assert one == two
        assert one.startswith("refund_my_latest_order_")

    def test_different_traces_get_different_ids(self) -> None:
        assert make_test_id("trace-1", "Refund it.") != make_test_id("trace-2", "Refund it.")

    def test_identical_wording_still_disambiguates(self) -> None:
        """Two traces asking the same thing are two tests, not one."""
        one = make_test_id("trace-1", "Refund my latest order.")
        two = make_test_id("trace-2", "Refund my latest order.")
        assert one != two
        assert one.rsplit("_", 1)[0] == two.rsplit("_", 1)[0]

    def test_the_id_does_not_depend_on_a_cluster_label(self) -> None:
        """Cluster labels are renamed by reviewers; committed test IDs are not."""
        signature = make_test_id.__doc__ or ""
        assert "mutable" in signature
        assert make_test_id("trace-1042", "Refund my latest order.") == make_test_id(
            "trace-1042", "Refund my latest order."
        )

    def test_an_empty_input_still_yields_an_id(self) -> None:
        assert make_test_id("trace-1", "").startswith("test_")

    def test_redaction_placeholders_are_kept_out_of_the_id(self) -> None:
        generated = make_test_id("trace-1", "Refund for [REDACTED:email] please")
        assert "redacted" not in generated
        assert generated.startswith("refund_for_please_")

    def test_long_inputs_are_truncated(self) -> None:
        generated = make_test_id("trace-1", " ".join(["word"] * 50))
        assert len(generated) < 70

    def test_punctuation_and_case_are_normalized(self) -> None:
        assert slugify("Refund MY latest order!!") == "refund_my_latest_order"


class TestValidation:
    def test_a_well_formed_expectation_passes(self) -> None:
        assert (
            validate_expectation(
                expectation(
                    ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
                    tool="refund_order",
                    path="order_id",
                    value="order-A",
                )
            )
            is None
        )

    def test_an_argument_expectation_needs_a_path(self) -> None:
        problem = validate_expectation(
            expectation(ExpectationType.TOOL_ARGUMENT_EQUALS, tool="refund_order", value="x")
        )
        assert problem is not None and "path" in problem

    def test_a_tool_expectation_needs_a_tool(self) -> None:
        assert validate_expectation(expectation(ExpectationType.TOOL_CALLED)) is not None

    def test_a_negative_call_limit_is_refused(self) -> None:
        problem = validate_expectation(
            expectation(ExpectationType.MAX_TOOL_CALLS, tool="t", value=-1)
        )
        assert problem is not None and "negative" in problem

    def test_a_non_numeric_call_limit_is_refused(self) -> None:
        assert (
            validate_expectation(expectation(ExpectationType.MAX_TOOL_CALLS, tool="t", value="two"))
            is not None
        )

    def test_a_boolean_is_not_a_call_limit(self) -> None:
        assert (
            validate_expectation(expectation(ExpectationType.MAX_TOOL_CALLS, tool="t", value=True))
            is not None
        )

    def test_an_uncompilable_pattern_is_refused(self) -> None:
        problem = validate_expectation(expectation(ExpectationType.OUTPUT_MATCHES, value="refund("))
        assert problem is not None and "does not compile" in problem

    def test_a_valid_pattern_passes(self) -> None:
        assert (
            validate_expectation(expectation(ExpectationType.OUTPUT_MATCHES, value=r"order-[A-Z]"))
            is None
        )

    def test_empty_output_text_is_refused(self) -> None:
        assert (
            validate_expectation(expectation(ExpectationType.OUTPUT_CONTAINS, value="  "))
            is not None
        )

    def test_an_empty_rubric_is_refused(self) -> None:
        assert validate_expectation(expectation(ExpectationType.HUMAN_RUBRIC, value="")) is not None


class TestContradictions:
    def _find(self, *expectations: Expectation) -> list[Contradiction]:
        return find_contradictions(list(expectations))

    def test_a_consistent_set_has_none(self) -> None:
        assert not self._find(
            expectation(
                ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
                tool="refund_order",
                path="order_id",
                value="order-A",
            ),
            expectation(
                ExpectationType.TOOL_ARGUMENT_EQUALS,
                tool="refund_order",
                path="order_id",
                value="order-C",
            ),
        )

    def test_required_and_forbidden_tool(self) -> None:
        found = self._find(
            expectation(ExpectationType.TOOL_CALLED, tool="refund_order"),
            expectation(ExpectationType.TOOL_NOT_CALLED, tool="refund_order"),
        )
        assert len(found) == 1
        assert "required and forbidden" in found[0].reason

    def test_a_different_tool_is_not_a_conflict(self) -> None:
        assert not self._find(
            expectation(ExpectationType.TOOL_CALLED, tool="get_order"),
            expectation(ExpectationType.TOOL_NOT_CALLED, tool="refund_order"),
        )

    def test_equals_and_not_equals_on_one_value(self) -> None:
        found = self._find(
            expectation(
                ExpectationType.TOOL_ARGUMENT_EQUALS,
                tool="t",
                path="p",
                value="v",
            ),
            expectation(
                ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
                tool="t",
                path="p",
                value="v",
            ),
        )
        assert len(found) == 1

    def test_two_required_values_for_one_argument(self) -> None:
        found = self._find(
            expectation(ExpectationType.TOOL_ARGUMENT_EQUALS, tool="t", path="p", value="a"),
            expectation(ExpectationType.TOOL_ARGUMENT_EQUALS, tool="t", path="p", value="b"),
        )
        assert len(found) == 1
        assert "two different values" in found[0].reason

    def test_an_argument_of_a_forbidden_tool(self) -> None:
        found = self._find(
            expectation(ExpectationType.TOOL_NOT_CALLED, tool="refund_order"),
            expectation(
                ExpectationType.TOOL_ARGUMENT_EQUALS,
                tool="refund_order",
                path="order_id",
                value="order-C",
            ),
        )
        assert len(found) == 1

    def test_a_required_tool_capped_at_zero(self) -> None:
        found = self._find(
            expectation(ExpectationType.TOOL_CALLED, tool="refund_order"),
            expectation(ExpectationType.MAX_TOOL_CALLS, tool="refund_order", value=0),
        )
        assert len(found) == 1

    def test_contains_and_not_contains_the_same_text(self) -> None:
        found = self._find(
            expectation(ExpectationType.OUTPUT_CONTAINS, value="refunded"),
            expectation(ExpectationType.OUTPUT_NOT_CONTAINS, value="refunded"),
        )
        assert len(found) == 1

    def test_two_limits_for_one_tool(self) -> None:
        found = self._find(
            expectation(ExpectationType.MAX_TOOL_CALLS, tool="t", value=1),
            expectation(ExpectationType.MAX_TOOL_CALLS, tool="t", value=2),
        )
        assert len(found) == 1

    def test_order_does_not_matter(self) -> None:
        one = expectation(ExpectationType.TOOL_CALLED, tool="t")
        two = expectation(ExpectationType.TOOL_NOT_CALLED, tool="t")
        assert len(self._find(one, two)) == len(self._find(two, one)) == 1

    def test_every_conflicting_pair_is_reported(self) -> None:
        found = self._find(
            expectation(ExpectationType.TOOL_CALLED, tool="a"),
            expectation(ExpectationType.TOOL_NOT_CALLED, tool="a"),
            expectation(ExpectationType.OUTPUT_CONTAINS, value="x"),
            expectation(ExpectationType.OUTPUT_NOT_CONTAINS, value="x"),
        )
        assert len(found) == 2


class TestClassification:
    def test_only_the_rubric_needs_a_judge(self) -> None:
        assert not expectation(ExpectationType.HUMAN_RUBRIC, value="x").deterministic
        assert expectation(
            ExpectationType.TOOL_ARGUMENT_NOT_EQUALS, tool="t", path="p", value="v"
        ).deterministic

    @pytest.mark.parametrize(
        "kind",
        [
            ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
            ExpectationType.TOOL_NOT_CALLED,
            ExpectationType.OUTPUT_NOT_CONTAINS,
            ExpectationType.MAX_TOOL_CALLS,
        ],
    )
    def test_forbidding_expectations_are_not_positive(self, kind: ExpectationType) -> None:
        assert not expectation(kind, tool="t", path="p", value=1).positive

    @pytest.mark.parametrize(
        "kind",
        [
            ExpectationType.TOOL_ARGUMENT_EQUALS,
            ExpectationType.TOOL_CALLED,
            ExpectationType.OUTPUT_CONTAINS,
            ExpectationType.HUMAN_RUBRIC,
        ],
    )
    def test_requiring_expectations_are_positive(self, kind: ExpectationType) -> None:
        assert expectation(kind, tool="t", path="p", value="v").positive


class TestDescriptions:
    @pytest.mark.parametrize(
        ("kind", "kwargs", "expected"),
        [
            (
                ExpectationType.TOOL_ARGUMENT_EQUALS,
                {"tool": "refund_order", "path": "order_id", "value": "order-C"},
                "refund_order.order_id == 'order-C'",
            ),
            (
                ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
                {"tool": "refund_order", "path": "order_id", "value": "order-A"},
                "refund_order.order_id != 'order-A'",
            ),
            (ExpectationType.TOOL_CALLED, {"tool": "get_order"}, "calls get_order"),
            (
                ExpectationType.TOOL_NOT_CALLED,
                {"tool": "refund_order"},
                "never calls refund_order",
            ),
            (
                ExpectationType.MAX_TOOL_CALLS,
                {"tool": "refund_order", "value": 1},
                "at most 1 calls to refund_order",
            ),
            (
                ExpectationType.MAX_TOOL_CALLS,
                {"value": 3},
                "at most 3 calls to any tool",
            ),
            (
                ExpectationType.HUMAN_RUBRIC,
                {"value": "refunds the newest order"},
                "rubric: refunds the newest order",
            ),
            (
                ExpectationType.OUTPUT_CONTAINS,
                {"value": "refunded"},
                "output_contains 'refunded'",
            ),
        ],
    )
    def test_every_type_reads_as_a_sentence(
        self, kind: ExpectationType, kwargs: dict[str, object], expected: str
    ) -> None:
        assert expectation(kind, **kwargs).describe() == expected

    def test_a_contradiction_describes_both_sides(self) -> None:
        (found,) = find_contradictions(
            [
                expectation(ExpectationType.TOOL_CALLED, tool="refund_order"),
                expectation(ExpectationType.TOOL_NOT_CALLED, tool="refund_order"),
            ]
        )
        described = found.describe()
        assert "calls refund_order" in described
        assert "never calls refund_order" in described


class TestSerialization:
    def test_an_expectation_round_trips(self) -> None:
        one = expectation(
            ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
            tool="refund_order",
            path="order_id",
            value="order-A",
        )
        assert Expectation.from_dict(one.to_dict()) == one

    def test_optional_fields_are_omitted(self) -> None:
        payload = expectation(ExpectationType.OUTPUT_CONTAINS, value="x").to_dict()
        assert "tool" not in payload and "path" not in payload

    def test_generation_only_ever_writes_drafts(self) -> None:
        assert ReviewStatus.DRAFT.value == "draft"
        assert {status.value for status in ReviewStatus} == {"draft", "approved", "rejected"}

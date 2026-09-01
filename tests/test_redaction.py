"""Redaction: what must be scrubbed, and what must survive (guide 8C)."""

from __future__ import annotations

from typing import Any

import pytest

from evalsmith.config import RedactionConfig
from evalsmith.redaction import RedactionRule, RedactionSummary, Redactor, is_secret_field
from evalsmith.trace import NormalizedTrace


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


def redact(redactor: Redactor, text: str) -> str:
    return redactor.redact_text(text, RedactionSummary())


class TestEmails:
    @pytest.mark.parametrize(
        "text",
        [
            "shopper@example.com",
            "Contact first.last+tag@sub.example.co.uk please",
            "MIXED.Case@Example.COM",
        ],
    )
    def test_addresses_are_replaced(self, redactor: Redactor, text: str) -> None:
        result = redact(redactor, text)
        assert "@" not in result
        assert "[REDACTED:email]" in result

    def test_surrounding_text_is_preserved(self, redactor: Redactor) -> None:
        assert redact(redactor, "mail a@b.com now") == "mail [REDACTED:email] now"

    def test_ordinary_text_is_untouched(self, redactor: Redactor) -> None:
        assert redact(redactor, "Refund my latest order.") == "Refund my latest order."


class TestPhoneNumbers:
    @pytest.mark.parametrize(
        "text",
        ["(415) 555-2671", "415-555-2671", "415.555.2671", "+1 415-555-2671", "+14155552671"],
    )
    def test_recognized_formats_are_replaced(self, redactor: Redactor, text: str) -> None:
        assert redact(redactor, text) == "[REDACTED:phone]"

    @pytest.mark.parametrize("text", ["order-4155552671", "1234567", "$24.00", "2026-08-14"])
    def test_non_phone_digits_survive(self, redactor: Redactor, text: str) -> None:
        assert redact(redactor, text) == text


class TestPaymentCards:
    @pytest.mark.parametrize(
        "number", ["4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111"]
    )
    def test_luhn_valid_numbers_are_replaced(self, redactor: Redactor, number: str) -> None:
        assert redact(redactor, f"card {number} charged") == "card [REDACTED:payment_card] charged"

    def test_a_card_embedded_in_an_identifier_is_still_caught(self, redactor: Redactor) -> None:
        assert "4111" not in redact(redactor, "ref4111111111111111end")

    def test_a_card_next_to_a_phone_number_is_still_caught(self, redactor: Redactor) -> None:
        """One digit run, two secrets: neither may shield the other."""
        result = redact(redactor, "(415) 555-2671 4111 1111 1111 1111")
        assert result == "[REDACTED:phone] [REDACTED:payment_card]"

    def test_two_adjacent_cards_are_both_caught(self, redactor: Redactor) -> None:
        result = redact(redactor, "4111 1111 1111 1111 5555 5555 5555 4444")
        assert result == "[REDACTED:payment_card] [REDACTED:payment_card]"

    def test_digit_runs_that_fail_luhn_survive(self, redactor: Redactor) -> None:
        """An order number is not a card, and redacting it would lose signal."""
        assert redact(redactor, "order 1234567890123") == "order 1234567890123"

    def test_money_amounts_survive(self, redactor: Redactor) -> None:
        assert redact(redactor, "refunded $24.00 of $1,234.56") == "refunded $24.00 of $1,234.56"


class TestTokens:
    @pytest.mark.parametrize(
        "token",
        [
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "xoxb-1234567890-abcdefghij",
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        ],
    )
    def test_recognized_prefixes_are_replaced(self, redactor: Redactor, token: str) -> None:
        assert redact(redactor, f"using {token} now") == "using [REDACTED:token] now"

    def test_authorization_headers_are_replaced(self, redactor: Redactor) -> None:
        result = redact(redactor, "Bearer abcdefghijklmnopqrstuvwxyz0123")
        assert result == "[REDACTED:token]"

    def test_ordinary_words_are_not_tokens(self, redactor: Redactor) -> None:
        assert redact(redactor, "sk-1 is too short") == "sk-1 is too short"


class TestSecretFieldNames:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "api_key",
            "apiKey",
            "APIKey",
            "access_token",
            "token",
            "authorization",
            "client_secret",
            "private_key",
            "card_number",
            "cvv",
            "ssn",
        ],
    )
    def test_credential_field_names_are_recognized(self, key: str) -> None:
        assert is_secret_field(key)

    @pytest.mark.parametrize(
        "key", ["order_id", "tool", "trace_id", "customer_id", "content", "status", "name"]
    )
    def test_ordinary_field_names_are_not(self, key: str) -> None:
        assert not is_secret_field(key)

    def test_string_values_are_replaced(self, redactor: Redactor) -> None:
        cleaned, summary = _redact_arguments(redactor, {"api_key": "anything at all"})
        assert cleaned["api_key"] == "[REDACTED:secret_field]"
        assert summary.counts[RedactionRule.SECRET_FIELD] == 1

    def test_nested_containers_are_replaced_whole(self, redactor: Redactor) -> None:
        cleaned, _ = _redact_arguments(redactor, {"credentials": {"user": "a", "pass": "b"}})
        assert cleaned["credentials"] == "[REDACTED:secret_field]"

    def test_counts_and_flags_are_not_credentials(self, redactor: Redactor) -> None:
        """``token_count: 512`` is signal, not a secret."""
        cleaned, _ = _redact_arguments(redactor, {"token_count": 512, "auth": True})
        assert cleaned == {"token_count": 512, "auth": True}

    def test_secrets_nested_inside_arguments_are_found(self, redactor: Redactor) -> None:
        cleaned, _ = _redact_arguments(redactor, {"config": {"retries": 3, "password": "hunter2"}})
        assert cleaned["config"] == {"retries": 3, "password": "[REDACTED:secret_field]"}


class TestIdentityIsPreserved:
    def test_identifiers_are_never_rewritten(self, redactor: Redactor) -> None:
        trace = _trace(
            trace_id="a@b.com",
            events=[
                {
                    "event_id": "e@x.com",
                    "type": "tool_call",
                    "tool": "refund_order",
                    "call_id": "c@x.com",
                }
            ],
        )
        cleaned, _ = redactor.redact(trace)
        assert cleaned.trace_id == "a@b.com"
        assert cleaned.events[0].event_id == "e@x.com"
        assert cleaned.tool_calls[0].call_id == "c@x.com"
        assert cleaned.tool_calls[0].tool == "refund_order"

    def test_the_result_is_still_a_valid_trace(self, redactor: Redactor) -> None:
        cleaned, _ = redactor.redact(_trace(input={"text": "mail me: a@b.com"}))
        assert isinstance(cleaned, NormalizedTrace)
        assert cleaned.input.text == "mail me: [REDACTED:email]"

    def test_the_original_trace_is_not_mutated(self, redactor: Redactor) -> None:
        original = _trace(input={"text": "a@b.com"})
        redactor.redact(original)
        assert original.input.text == "a@b.com"


class TestDeterminism:
    def test_the_same_input_redacts_identically(self, redactor: Redactor) -> None:
        trace = _trace(input={"text": "a@b.com and 4111 1111 1111 1111"})
        first, _ = redactor.redact(trace)
        second, _ = redactor.redact(trace)
        assert first == second

    def test_different_secrets_collapse_to_the_same_placeholder(self, redactor: Redactor) -> None:
        """Clustering should see two customers' emails as the same shape."""
        one = redact(redactor, "contact a@x.com")
        two = redact(redactor, "contact b@y.com")
        assert one == two


class TestConfiguration:
    def test_a_disabled_rule_leaves_values_alone(self) -> None:
        redactor = Redactor(RedactionConfig(emails=False))
        assert redact(redactor, "a@b.com") == "a@b.com"

    def test_other_rules_still_run(self) -> None:
        redactor = Redactor(RedactionConfig(emails=False))
        assert redact(redactor, "a@b.com (415) 555-2671") == "a@b.com [REDACTED:phone]"

    def test_disabling_secret_fields_keeps_values(self) -> None:
        redactor = Redactor(RedactionConfig(secret_field_names=False))
        cleaned, _ = _redact_arguments(redactor, {"password": "hunter2"})
        assert cleaned["password"] == "hunter2"


class TestSummary:
    def test_counts_every_rule_that_fired(self, redactor: Redactor) -> None:
        trace = _trace(
            input={"text": "a@b.com (415) 555-2671 4111 1111 1111 1111 AKIAIOSFODNN7EXAMPLE"},
            events=[
                {
                    "event_id": "e1",
                    "type": "tool_call",
                    "tool": "refund_order",
                    "arguments": {"api_key": "x"},
                }
            ],
        )
        _, summary = redactor.redact(trace)
        assert summary.to_dict() == {
            "email": 1,
            "payment_card": 1,
            "phone": 1,
            "secret_field": 1,
            "token": 1,
        }
        assert summary.total == 5

    def test_a_clean_trace_reports_nothing(self, redactor: Redactor) -> None:
        _, summary = redactor.redact(_trace())
        assert summary.total == 0
        assert summary.to_dict() == {}

    def test_summaries_merge(self) -> None:
        first = RedactionSummary()
        first.record(RedactionRule.EMAIL, 2)
        second = RedactionSummary()
        second.record(RedactionRule.EMAIL)
        second.record(RedactionRule.TOKEN)
        first.merge(second)
        assert first.to_dict() == {"email": 3, "token": 1}


def _trace(**overrides: Any) -> NormalizedTrace:
    payload: dict[str, Any] = {
        "trace_id": "trace-1",
        "input": {"text": "Refund my latest order."},
        "outcome": {"status": "failure"},
    }
    payload.update(overrides)
    return NormalizedTrace.model_validate(payload)


def _redact_arguments(
    redactor: Redactor, arguments: dict[str, Any]
) -> tuple[dict[str, Any], RedactionSummary]:
    trace = _trace(
        events=[
            {"event_id": "e1", "type": "tool_call", "tool": "refund_order", "arguments": arguments}
        ]
    )
    cleaned, summary = redactor.redact(trace)
    return cleaned.tool_calls[0].arguments, summary

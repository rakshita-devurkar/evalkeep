"""Deterministic redaction, applied in memory before anything reaches storage.

Redaction runs on the parsed trace and returns a new one; the raw trace is never
written to the database, exported, or sent to an analyzer. The rules err toward
over-redaction -- a redacted order total is an inconvenience, a stored API key is
an incident -- with two exceptions that keep the data usable:

* Identity fields (``trace_id``, ``event_id``, ``call_id``, ``tool`` and the
  other structural keys in :data:`NEVER_REDACTED`) are never rewritten. Scrubbing
  an identifier would break the very links the pipeline is built on.
* Secret *field names* only redact string and container values. ``token_count:
  512`` is a number, not a credential, and redacting it would lose real signal.

Everything is deterministic: the same input always produces the same output, with
fixed placeholders rather than hashes of the original. Two customers' email
addresses collapse to the same placeholder, which is what clustering wants.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from evalkeep.config import RedactionConfig
from evalkeep.trace import NormalizedTrace


class RedactionRule(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    PAYMENT_CARD = "payment_card"
    TOKEN = "token"
    SECRET_FIELD = "secret_field"


def placeholder(rule: RedactionRule) -> str:
    return f"[REDACTED:{rule.value}]"


#: Structural and identity keys whose values are never rewritten.
NEVER_REDACTED: frozenset[str] = frozenset(
    {
        "trace_id",
        "event_id",
        "call_id",
        "schema_version",
        "type",
        "role",
        "status",
        "tool",
        "rating",
        "passed",
        "score",
        "name",
    }
)

#: A key segment equal to one of these marks a credential.
SECRET_SEGMENTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "apikey",
        "cvv",
        "cvc",
        "ssn",
        "authorization",
        "pin",
    }
)

#: A normalized key containing one of these marks a credential.
SECRET_PHRASES: tuple[str, ...] = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "bearertoken",
    "privatekey",
    "secretkey",
    "clientsecret",
    "sessionkey",
    "creditcard",
    "cardnumber",
    "socialsecurity",
)

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Requires separators, so order numbers and totals are not mistaken for phones.
PHONE_PATTERN = re.compile(
    r"""(?<![\w-])
        (?:\+\d{1,3}[\s.\-]?)?
        (?:\(\d{3}\)\s?|\d{3}[\s.\-])
        \d{3}[\s.\-]\d{4}
        (?![\w-])""",
    re.VERBOSE,
)
E164_PATTERN = re.compile(r"(?<![\w-])\+\d{10,15}(?![\w-])")

#: A candidate only; :func:`_luhn_ok` decides whether it is really a card. The
#: boundaries exclude only adjacent digits, so a card embedded in an identifier
#: is still caught -- over-redacting an ID beats storing a card number.
CARD_CANDIDATE_PATTERN = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")

TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bpk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
)


@dataclass
class RedactionSummary:
    """How many values each rule replaced. Stored alongside the trace."""

    counts: dict[RedactionRule, int] = field(default_factory=dict)

    def record(self, rule: RedactionRule, times: int = 1) -> None:
        if times:
            self.counts[rule] = self.counts.get(rule, 0) + times

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def to_dict(self) -> dict[str, int]:
        return {rule.value: count for rule, count in sorted(self.counts.items())}

    def merge(self, other: RedactionSummary) -> None:
        for rule, count in other.counts.items():
            self.record(rule, count)


class Redactor:
    """Applies the configured rules to a trace, in memory, before storage."""

    def __init__(self, config: RedactionConfig | None = None) -> None:
        self.config = config or RedactionConfig()

    def redact(self, trace: NormalizedTrace) -> tuple[NormalizedTrace, RedactionSummary]:
        """Return a redacted copy of ``trace`` and what was replaced."""
        summary = RedactionSummary()
        payload = trace.model_dump(mode="json")
        cleaned = self._walk(payload, summary, key=None)
        # Re-validating proves redaction produced a trace the rest of the
        # pipeline can still read, rather than something merely dict-shaped.
        return NormalizedTrace.model_validate(cleaned), summary

    def redact_text(self, text: str, summary: RedactionSummary) -> str:
        """Apply every enabled pattern rule to one string."""
        if self.config.token_prefixes:
            for pattern in TOKEN_PATTERNS:
                text, hits = pattern.subn(placeholder(RedactionRule.TOKEN), text)
                summary.record(RedactionRule.TOKEN, hits)
        # Phones before cards, deliberately. A phone number sitting next to a
        # card forms one digit run, and Luhn alone cannot say which digits
        # belong to which; replacing the phone first removes the ambiguity
        # rather than guessing. Phone patterns require separators in positions
        # a card's groups never produce, so they cannot match inside a card.
        if self.config.phone_numbers:
            for pattern in (PHONE_PATTERN, E164_PATTERN):
                text, hits = pattern.subn(placeholder(RedactionRule.PHONE), text)
                summary.record(RedactionRule.PHONE, hits)
        if self.config.payment_cards:
            text = self._redact_cards(text, summary)
        if self.config.emails:
            text, hits = EMAIL_PATTERN.subn(placeholder(RedactionRule.EMAIL), text)
            summary.record(RedactionRule.EMAIL, hits)
        return text

    def _redact_cards(self, text: str, summary: RedactionSummary) -> str:
        """Replace only digit runs that pass Luhn, so totals and IDs survive.

        Candidates are matched as whole separator-delimited groups, and a
        candidate that fails Luhn is retried over its sub-runs of groups. Both
        matter: a phone number sitting next to a card forms one long digit run
        that fails Luhn as a whole, and searching groups rather than arbitrary
        offsets finds the card inside it without inventing Luhn-valid windows
        that were never a card.
        """

        def replace(match: re.Match[str]) -> str:
            span = match.group(0)
            groups = [(m.start(), m.end()) for m in re.finditer(r"\d+", span)]
            for start in range(len(groups)):
                for end in range(len(groups), start, -1):
                    digits = "".join(span[a:b] for a, b in groups[start:end])
                    if not 13 <= len(digits) <= 19 or not _luhn_ok(digits):
                        continue
                    summary.record(RedactionRule.PAYMENT_CARD)
                    low, high = groups[start][0], groups[end - 1][1]
                    tail = self._redact_cards(span[high:], summary)
                    return span[:low] + placeholder(RedactionRule.PAYMENT_CARD) + tail
            return span

        return CARD_CANDIDATE_PATTERN.sub(replace, text)

    def _walk(self, value: Any, summary: RedactionSummary, *, key: str | None) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for child_key, child in value.items():
                if self._is_secret_value(child_key, child):
                    summary.record(RedactionRule.SECRET_FIELD)
                    cleaned[child_key] = placeholder(RedactionRule.SECRET_FIELD)
                else:
                    cleaned[child_key] = self._walk(child, summary, key=child_key)
            return cleaned
        if isinstance(value, list):
            return [self._walk(item, summary, key=key) for item in value]
        if isinstance(value, str):
            if key is not None and key in NEVER_REDACTED:
                return value
            return self.redact_text(value, summary)
        return value

    def _is_secret_value(self, key: str, value: Any) -> bool:
        """A credential is a string or a container, never a count or a flag."""
        if not self.config.secret_field_names:
            return False
        if not isinstance(value, str | dict | list):
            return False
        return is_secret_field(key)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, character in enumerate(reversed(digits)):
        digit = int(character)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def is_secret_field(key: str) -> bool:
    """True when a field name marks its value as a credential."""
    if key in NEVER_REDACTED:
        return False
    segments = list(_ordered_segments(key))
    if set(segments) & SECRET_SEGMENTS:
        return True
    joined = "".join(segments)
    return any(phrase in joined for phrase in SECRET_PHRASES)


def _ordered_segments(key: str) -> Iterable[str]:
    parts = re.split(r"[^A-Za-z0-9]+", key)
    for part in parts:
        for piece in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part):
            yield piece.lower()

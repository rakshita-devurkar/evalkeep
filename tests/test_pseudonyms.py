"""Deterministic pseudonymization of identifiers (roadmap gap 5)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from evalkeep.cli import app
from evalkeep.commands.detect_cmd import add_failure, run_detection
from evalkeep.commands.ingest_cmd import ingest_traces
from evalkeep.commands.trace_cmd import show_trace
from evalkeep.config import Project
from evalkeep.errors import CommandError, ExitCode
from evalkeep.hashing import content_hash
from evalkeep.pseudonyms import Pseudonymizer
from evalkeep.redaction import RedactionRule, Redactor, risky_identifiers
from evalkeep.trace import NormalizedTrace, ToolCallEvent, ToolResultEvent

RISKY_ID = "order-jane@example.com-2026-06-01"


def trace(trace_id: str = RISKY_ID, **overrides: Any) -> NormalizedTrace:
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "input": {"text": "Refund my latest order."},
        "events": [
            {
                "event_id": "e1",
                "type": "tool_call",
                "tool": "refund_order",
                "call_id": "c1",
                "arguments": {"order_id": "order-A"},
            }
        ],
        "outcome": {"status": "failure"},
    }
    payload.update(overrides)
    return NormalizedTrace.model_validate(payload)


def enable(project_root: Path) -> None:
    config = project_root / "evalkeep.yaml"
    config.write_text(
        config.read_text().replace(
            "pseudonymize_identifiers: false", "pseudonymize_identifiers: true"
        )
    )


def write_trace(path: Path, **overrides: Any) -> Path:
    payload = trace(**overrides).model_dump(mode="json", exclude_none=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


class TestPseudonymizer:
    def test_the_same_value_always_yields_the_same_token(self) -> None:
        one = Pseudonymizer(b"s" * 32)
        assert one.token("trace-1", field="trace_id") == one.token("trace-1", field="trace_id")

    def test_different_values_yield_different_tokens(self) -> None:
        one = Pseudonymizer(b"s" * 32)
        assert one.token("trace-1", field="trace_id") != one.token("trace-2", field="trace_id")

    def test_a_different_salt_is_a_different_namespace(self) -> None:
        """A shared export must not leak anything about another project's data."""
        assert Pseudonymizer(b"a" * 32).token("trace-1", field="trace_id") != Pseudonymizer(
            b"b" * 32
        ).token("trace-1", field="trace_id")

    def test_the_token_says_what_kind_of_id_it_replaced(self) -> None:
        one = Pseudonymizer(b"s" * 32)
        assert one.token("x", field="trace_id").startswith("trace-")
        assert one.token("x", field="event_id").startswith("event-")
        assert one.token("x", field="call_id").startswith("call-")

    def test_the_original_is_not_in_the_token(self) -> None:
        token = Pseudonymizer(b"s" * 32).token(RISKY_ID, field="trace_id")
        assert "jane" not in token and "example.com" not in token

    def test_a_short_salt_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 16 bytes"):
            Pseudonymizer(b"tiny")


class TestSaltFile:
    def test_it_is_created_on_first_use(self, tmp_path: Path) -> None:
        path = tmp_path / "salt"
        Pseudonymizer.load(path)
        assert path.is_file()

    def test_it_is_not_world_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "salt"
        Pseudonymizer.load(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_it_is_reused_so_tokens_stay_stable(self, tmp_path: Path) -> None:
        path = tmp_path / "salt"
        first = Pseudonymizer.load(path).token("trace-1", field="trace_id")
        second = Pseudonymizer.load(path).token("trace-1", field="trace_id")
        assert first == second

    def test_a_corrupt_salt_is_a_command_error(self, tmp_path: Path) -> None:
        path = tmp_path / "salt"
        path.write_text("not hexadecimal at all")
        with pytest.raises(CommandError, match="Could not read the pseudonymization salt"):
            Pseudonymizer.load(path)

    def test_init_excludes_it_from_git(self, initialized_project: Path) -> None:
        assert ".evalkeep/salt" in (initialized_project / ".gitignore").read_text()


class TestRedaction:
    def _redacted(self, **kwargs: Any) -> Any:
        return Redactor(pseudonymizer=Pseudonymizer(b"s" * 32), **kwargs).redact(trace())

    def test_identifiers_become_tokens(self) -> None:
        cleaned, summary = self._redacted()
        assert cleaned.trace_id.startswith("trace-")
        assert cleaned.events[0].event_id.startswith("event-")
        assert cleaned.tool_calls[0].call_id.startswith("call-")
        assert summary.counts[RedactionRule.PSEUDONYM] == 3

    def test_tool_names_are_left_alone(self) -> None:
        """Expectations reference tool names; rewriting them would break tests."""
        cleaned, _ = self._redacted()
        assert cleaned.tool_calls[0].tool == "refund_order"

    def test_without_a_pseudonymizer_identifiers_survive(self) -> None:
        cleaned, summary = Redactor().redact(trace())
        assert cleaned.trace_id == RISKY_ID
        assert RedactionRule.PSEUDONYM not in summary.counts

    def test_the_content_hash_is_unaffected(self) -> None:
        """Identity of the interaction must not depend on how IDs are spelled."""
        plain, _ = Redactor().redact(trace())
        pseudonymized, _ = self._redacted()
        assert content_hash(plain) == content_hash(pseudonymized)

    def test_two_traces_do_not_collide(self) -> None:
        """A placeholder would collapse them; a token must not."""
        one, _ = Redactor(pseudonymizer=Pseudonymizer(b"s" * 32)).redact(
            trace("order-a@example.com")
        )
        two, _ = Redactor(pseudonymizer=Pseudonymizer(b"s" * 32)).redact(
            trace("order-b@example.com")
        )
        assert one.trace_id != two.trace_id

    def test_call_ids_stay_consistent_within_a_trace(self) -> None:
        """A tool_result must still match the tool_call that opened it."""
        source = trace(
            events=[
                {"event_id": "e1", "type": "tool_call", "tool": "refund_order", "call_id": "c1"},
                {"event_id": "e2", "type": "tool_result", "tool": "refund_order", "call_id": "c1"},
            ]
        )
        cleaned, _ = Redactor(pseudonymizer=Pseudonymizer(b"s" * 32)).redact(source)
        call, result = cleaned.events
        assert isinstance(call, ToolCallEvent)
        assert isinstance(result, ToolResultEvent)
        assert call.call_id == result.call_id
        assert call.call_id is not None and call.call_id.startswith("call-")


class TestRiskDetection:
    def test_an_email_in_a_trace_id_is_reported(self) -> None:
        (risk,) = risky_identifiers(trace())
        assert "trace_id" in risk and "email" in risk

    def test_a_phone_number_is_reported(self) -> None:
        (risk,) = risky_identifiers(trace("customer-(415) 555-2671"))
        assert "phone" in risk

    def test_a_credential_is_reported(self) -> None:
        (risk,) = risky_identifiers(trace("run-sk-abcdefghijklmnopqrstuvwxyz012345"))
        assert "credential" in risk

    def test_an_event_id_is_checked_too(self) -> None:
        source = trace(
            "trace-1",
            events=[
                {
                    "event_id": "step-jane@example.com",
                    "type": "tool_call",
                    "tool": "refund_order",
                }
            ],
        )
        (risk,) = risky_identifiers(source)
        assert "event_id" in risk

    def test_opaque_identifiers_are_not_reported(self) -> None:
        assert risky_identifiers(trace("3f2b9c14-1e6a-4b2f-9c77-8a1d2e3f4a5b")) == []


class TestIngest:
    def test_off_by_default_the_identifier_is_stored_as_is(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        write_trace(tmp_path / "t.jsonl")
        report = ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        assert report.identifier_risks == 1
        assert report.notices
        database = Project.load(initialized_project).database_path
        assert b"jane@example.com" in database.read_bytes()

    def test_on_the_identifier_never_reaches_the_database(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        enable(initialized_project)
        write_trace(tmp_path / "t.jsonl")
        report = ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        assert report.identifier_risks == 0
        database = Project.load(initialized_project).database_path
        assert b"jane@example.com" not in database.read_bytes()

    def test_the_stored_id_is_a_token(self, initialized_project: Path, tmp_path: Path) -> None:
        enable(initialized_project)
        write_trace(tmp_path / "t.jsonl")
        ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        stored = show_trace(RISKY_ID, project_root=initialized_project)
        assert stored.trace.trace_id.startswith("trace-")
        assert "jane" not in stored.trace.trace_id

    def test_lookup_works_by_the_original_and_by_the_token(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        enable(initialized_project)
        write_trace(tmp_path / "t.jsonl")
        ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        by_original = show_trace(RISKY_ID, project_root=initialized_project)
        by_token = show_trace(by_original.trace.trace_id, project_root=initialized_project)
        assert by_original.trace.trace_id == by_token.trace.trace_id

    def test_re_ingesting_is_still_idempotent(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        """Stable tokens mean the same trace is recognised on the second pass."""
        enable(initialized_project)
        write_trace(tmp_path / "t.jsonl")
        ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        second = ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        assert second.stored == 0
        assert second.already_stored == 1

    def test_failures_resolve_by_the_original_id(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        enable(initialized_project)
        write_trace(tmp_path / "t.jsonl")
        ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        run_detection(project_root=initialized_project)
        from evalkeep.commands.detect_cmd import show_failure

        detail = show_failure(RISKY_ID, project_root=initialized_project)
        assert detail.failure.trace_id.startswith("trace-")

    def test_adding_a_failure_by_the_original_id(
        self, initialized_project: Path, tmp_path: Path
    ) -> None:
        enable(initialized_project)
        write_trace(tmp_path / "t.jsonl", outcome={"status": "success"})
        ingest_traces(tmp_path / "t.jsonl", project_root=initialized_project)
        failure = add_failure(RISKY_ID, project_root=initialized_project, reviewer="alex")
        assert failure.trace_id.startswith("trace-")


class TestCli:
    def test_ingest_warns_when_identifiers_look_risky(
        self, runner: CliRunner, initialized_project: Path, tmp_path: Path
    ) -> None:
        write_trace(tmp_path / "t.jsonl")
        result = runner.invoke(
            app, ["ingest", str(tmp_path / "t.jsonl"), "-C", str(initialized_project)]
        )
        assert result.exit_code == ExitCode.OK
        # Warnings go to stderr so a piped stdout stays machine-readable.
        assert "personal data" in result.stderr
        assert "pseudonymize_identifiers" in result.stderr

    def test_no_warning_once_it_is_enabled(
        self, runner: CliRunner, initialized_project: Path, tmp_path: Path
    ) -> None:
        enable(initialized_project)
        write_trace(tmp_path / "t.jsonl")
        result = runner.invoke(
            app, ["ingest", str(tmp_path / "t.jsonl"), "-C", str(initialized_project)]
        )
        assert "personal data" not in result.stderr

    def test_the_setting_survives_a_config_round_trip(self, initialized_project: Path) -> None:
        enable(initialized_project)
        config = yaml.safe_load((initialized_project / "evalkeep.yaml").read_text())
        assert config["redaction"]["pseudonymize_identifiers"] is True
        assert Project.load(initialized_project).pseudonymizer() is not None

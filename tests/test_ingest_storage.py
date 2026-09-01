"""Ingest with storage: redaction before writing, dedup, dry-run, inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalsmith.cli import app
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.commands.trace_cmd import list_traces, show_trace
from evalsmith.config import Project
from evalsmith.errors import CommandError, ExitCode
from evalsmith.ingest import IngestMode

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/refund-agent/traces.jsonl"

SECRET_EMAIL = "shopper@example.com"
SECRET_KEY = "sk-livekey0123456789abcdefghij"
SECRET_CARD = "4111 1111 1111 1111"


def trace_payload(trace_id: str = "trace-1", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "input": {"text": "Refund my latest order."},
        "outcome": {"status": "failure"},
    }
    payload.update(overrides)
    return payload


def write_traces(path: Path, *payloads: dict[str, Any]) -> Path:
    path.write_text("".join(json.dumps(p) + "\n" for p in payloads), encoding="utf-8")
    return path


@pytest.fixture
def traces_file(tmp_path: Path) -> Path:
    return tmp_path / "traces.jsonl"


@pytest.fixture
def secret_traces(traces_file: Path) -> Path:
    return write_traces(
        traces_file,
        trace_payload(
            "trace-secret",
            input={"text": f"Refund for {SECRET_EMAIL}, card {SECRET_CARD}"},
            events=[
                {
                    "event_id": "e1",
                    "type": "tool_call",
                    "tool": "refund_order",
                    "arguments": {"order_id": "order-A", "api_key": SECRET_KEY},
                }
            ],
            metadata={"extra": {"authorization": SECRET_KEY}},
        ),
    )


class TestStoresRedactedOnly:
    def test_no_secret_reaches_the_database_file(
        self, initialized_project: Path, secret_traces: Path
    ) -> None:
        """The strongest form of the check: grep the raw bytes on disk."""
        ingest_traces(secret_traces, project_root=initialized_project)

        database = Project.load(initialized_project).database_path
        raw = database.read_bytes()
        for secret in (SECRET_EMAIL, SECRET_KEY, "4111"):
            assert secret.encode() not in raw, f"{secret!r} leaked into the database"

    def test_placeholders_are_what_got_stored(
        self, initialized_project: Path, secret_traces: Path
    ) -> None:
        ingest_traces(secret_traces, project_root=initialized_project)
        stored = show_trace("trace-secret", project_root=initialized_project)
        assert "[REDACTED:email]" in (stored.trace.input.text or "")
        assert "[REDACTED:payment_card]" in (stored.trace.input.text or "")
        assert stored.trace.tool_calls[0].arguments["api_key"] == "[REDACTED:secret_field]"
        assert stored.trace.metadata.extra["authorization"] == "[REDACTED:secret_field]"

    def test_the_redaction_count_is_reported_and_kept(
        self, initialized_project: Path, secret_traces: Path
    ) -> None:
        report = ingest_traces(secret_traces, project_root=initialized_project)
        assert report.redactions == 4
        assert report.redacted_traces == 1
        stored = show_trace("trace-secret", project_root=initialized_project)
        assert stored.redaction_summary == {"email": 1, "payment_card": 1, "secret_field": 2}

    def test_the_source_file_is_left_untouched(
        self, initialized_project: Path, secret_traces: Path
    ) -> None:
        before = secret_traces.read_bytes()
        ingest_traces(secret_traces, project_root=initialized_project)
        assert secret_traces.read_bytes() == before


class TestDryRun:
    def test_writes_nothing(self, initialized_project: Path, traces_file: Path) -> None:
        write_traces(traces_file, trace_payload())
        report = ingest_traces(traces_file, project_root=initialized_project, dry_run=True)
        assert report.mode is IngestMode.DRY_RUN
        assert report.stored == 1
        assert list_traces(project_root=initialized_project).total == 0

    def test_reports_what_would_be_skipped(
        self, initialized_project: Path, traces_file: Path
    ) -> None:
        write_traces(traces_file, trace_payload())
        ingest_traces(traces_file, project_root=initialized_project)
        report = ingest_traces(traces_file, project_root=initialized_project, dry_run=True)
        assert (report.stored, report.already_stored) == (0, 1)

    def test_still_reports_invalid_records(
        self, initialized_project: Path, traces_file: Path
    ) -> None:
        traces_file.write_text(json.dumps(trace_payload()) + "\n{not json\n")
        report = ingest_traces(traces_file, project_root=initialized_project, dry_run=True)
        assert report.invalid == 1
        assert report.exit_code is ExitCode.RECORD_ERRORS


class TestIdempotence:
    def test_ingesting_the_same_file_twice_stores_it_once(self, initialized_project: Path) -> None:
        first = ingest_traces(EXAMPLE, project_root=initialized_project)
        second = ingest_traces(EXAMPLE, project_root=initialized_project)
        assert first.stored == 5
        assert (second.stored, second.already_stored) == (0, 5)
        assert second.ok
        assert list_traces(project_root=initialized_project).total == 5

    def test_an_id_conflict_is_a_record_error(
        self, initialized_project: Path, traces_file: Path
    ) -> None:
        write_traces(traces_file, trace_payload())
        ingest_traces(traces_file, project_root=initialized_project)

        write_traces(traces_file, trace_payload(input={"text": "A different question."}))
        report = ingest_traces(traces_file, project_root=initialized_project)

        assert report.id_conflicts == 1
        assert report.stored == 0
        assert not report.ok
        assert report.sample[0].kind.value == "id_conflict"

    def test_the_same_content_under_a_new_id_is_skipped(
        self, initialized_project: Path, traces_file: Path
    ) -> None:
        write_traces(traces_file, trace_payload("trace-1"))
        ingest_traces(traces_file, project_root=initialized_project)

        write_traces(traces_file, trace_payload("trace-2"))
        report = ingest_traces(traces_file, project_root=initialized_project)

        assert report.content_duplicates == 1
        assert report.ok
        assert list_traces(project_root=initialized_project).total == 1

    def test_redaction_does_not_change_identity(
        self, initialized_project: Path, traces_file: Path
    ) -> None:
        """Two exports of one interaction match even with different customers."""
        write_traces(
            traces_file, trace_payload("trace-1", input={"text": f"Refund for {SECRET_EMAIL}"})
        )
        ingest_traces(traces_file, project_root=initialized_project)

        write_traces(
            traces_file, trace_payload("trace-2", input={"text": "Refund for other@example.com"})
        )
        report = ingest_traces(traces_file, project_root=initialized_project)
        assert report.content_duplicates == 1


class TestTraceInspection:
    def test_show_returns_the_stored_trace(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        stored = show_trace("trace-1042", project_root=initialized_project)
        assert stored.trace.trace_id == "trace-1042"
        assert stored.content_hash.startswith("sha256:")

    def test_show_tolerates_surrounding_whitespace(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        assert show_trace("  trace-1042 ", project_root=initialized_project) is not None

    def test_an_unknown_id_is_a_command_error(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        with pytest.raises(CommandError, match="No stored trace"):
            show_trace("trace-nope", project_root=initialized_project)

    def test_inspection_needs_an_initialized_project(self, tmp_path: Path) -> None:
        with pytest.raises(CommandError, match=r"evalsmith\.yaml"):
            show_trace("trace-1", project_root=tmp_path)

    def test_list_reports_totals(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        listing = list_traces(project_root=initialized_project, limit=2)
        assert listing.total == 5
        assert len(listing.summaries) == 2

    def test_list_filters_by_status(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        listing = list_traces(project_root=initialized_project, status="failure")
        assert {s.trace_id for s in listing.summaries} == {
            "trace-1042",
            "trace-1043",
            "trace-1051",
        }


class TestCli:
    def test_ingest_stores_and_exits_zero(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        result = runner.invoke(app, ["ingest", str(EXAMPLE), "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.OK
        assert "Ingested 5 traces" in result.stdout

    def test_dry_run_says_so(self, runner: CliRunner, initialized_project: Path) -> None:
        result = runner.invoke(
            app, ["ingest", str(EXAMPLE), "-C", str(initialized_project), "--dry-run"]
        )
        assert result.exit_code == ExitCode.OK
        assert "Dry run" in result.stdout
        assert "nothing was written" in result.stdout

    def test_an_id_conflict_exits_one(
        self, runner: CliRunner, initialized_project: Path, traces_file: Path
    ) -> None:
        write_traces(traces_file, trace_payload())
        runner.invoke(app, ["ingest", str(traces_file), "-C", str(initialized_project)])
        write_traces(traces_file, trace_payload(input={"text": "different"}))
        result = runner.invoke(app, ["ingest", str(traces_file), "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.RECORD_ERRORS
        assert "id conflicts" in result.stdout

    def test_trace_list_shows_stored_traces(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        runner.invoke(app, ["ingest", str(EXAMPLE), "-C", str(initialized_project)])
        result = runner.invoke(app, ["trace", "list", "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.OK
        assert "trace-1042" in result.stdout
        assert "5 of 5 traces" in result.stdout

    def test_trace_list_on_an_empty_store(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        result = runner.invoke(app, ["trace", "list", "-C", str(initialized_project)])
        assert "No stored traces" in result.stdout

    def test_trace_show_renders_the_interaction(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        runner.invoke(app, ["ingest", str(EXAMPLE), "-C", str(initialized_project)])
        result = runner.invoke(app, ["trace", "show", "trace-1042", "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.OK
        assert "refund_order" in result.stdout
        assert "Refunded the oldest order" in result.stdout

    def test_trace_show_json_is_machine_readable(
        self, runner: CliRunner, initialized_project: Path
    ) -> None:
        runner.invoke(app, ["ingest", str(EXAMPLE), "-C", str(initialized_project)])
        result = runner.invoke(
            app, ["trace", "show", "trace-1042", "-C", str(initialized_project), "--json"]
        )
        assert json.loads(result.stdout)["trace_id"] == "trace-1042"

    def test_trace_show_redacts_what_it_prints(
        self, runner: CliRunner, initialized_project: Path, secret_traces: Path
    ) -> None:
        runner.invoke(app, ["ingest", str(secret_traces), "-C", str(initialized_project)])
        result = runner.invoke(
            app, ["trace", "show", "trace-secret", "-C", str(initialized_project)]
        )
        assert SECRET_EMAIL not in result.stdout
        assert SECRET_KEY not in result.stdout

    def test_an_unknown_trace_exits_two(self, runner: CliRunner, initialized_project: Path) -> None:
        result = runner.invoke(app, ["trace", "show", "nope", "-C", str(initialized_project)])
        assert result.exit_code == ExitCode.COMMAND_ERROR

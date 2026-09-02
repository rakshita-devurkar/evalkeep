"""Streaming validation: duplicate detection, error JSONL and exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkeep.adapters import IssueKind, JsonlAdapter
from evalkeep.cli import app
from evalkeep.commands.ingest_cmd import ingest_traces
from evalkeep.errors import CommandError, ExitCode
from evalkeep.ingest import ingest_file

MINIMAL: dict[str, Any] = {
    "trace_id": "trace-1",
    "input": {"text": "Refund my latest order."},
    "outcome": {"status": "failure"},
}

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/refund-agent/traces.jsonl"


def write_traces(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def trace_line(**overrides: Any) -> str:
    return json.dumps({**MINIMAL, **overrides})


@pytest.fixture
def traces_file(tmp_path: Path) -> Path:
    return tmp_path / "traces.jsonl"


class TestValidFile:
    def test_counts_every_record(self, traces_file: Path) -> None:
        write_traces(traces_file, trace_line(trace_id="t1"), trace_line(trace_id="t2"))
        report = ingest_file(traces_file, JsonlAdapter())
        assert (report.records, report.valid, report.invalid) == (2, 2, 0)
        assert report.ok
        assert report.exit_code is ExitCode.OK

    def test_the_bundled_example_validates_cleanly(self) -> None:
        report = ingest_file(EXAMPLE, JsonlAdapter())
        assert report.records == 5
        assert report.invalid == 0

    def test_an_empty_file_is_valid_and_empty(self, traces_file: Path) -> None:
        traces_file.write_text("")
        report = ingest_file(traces_file, JsonlAdapter())
        assert (report.records, report.valid) == (0, 0)
        assert report.ok


class TestDuplicateIds:
    def test_the_second_occurrence_is_rejected(self, traces_file: Path) -> None:
        write_traces(traces_file, trace_line(trace_id="t1"), trace_line(trace_id="t1"))
        report = ingest_file(traces_file, JsonlAdapter())
        assert (report.valid, report.invalid, report.duplicate_ids) == (1, 1, 1)
        assert report.sample[0].kind is IssueKind.DUPLICATE_ID
        assert report.sample[0].line == 2

    def test_a_third_copy_is_also_rejected(self, traces_file: Path) -> None:
        write_traces(traces_file, *[trace_line(trace_id="t1")] * 3)
        report = ingest_file(traces_file, JsonlAdapter())
        assert (report.valid, report.duplicate_ids) == (1, 2)

    def test_trimming_makes_ids_collide(self, traces_file: Path) -> None:
        write_traces(traces_file, trace_line(trace_id="t1"), trace_line(trace_id="  t1  "))
        assert ingest_file(traces_file, JsonlAdapter()).duplicate_ids == 1

    def test_distinct_ids_do_not_collide(self, traces_file: Path) -> None:
        write_traces(traces_file, trace_line(trace_id="t1"), trace_line(trace_id="t2"))
        assert ingest_file(traces_file, JsonlAdapter()).duplicate_ids == 0

    def test_an_invalid_record_does_not_reserve_its_id(self, traces_file: Path) -> None:
        write_traces(
            traces_file,
            json.dumps({"trace_id": "t1", "input": {}}),
            trace_line(trace_id="t1"),
        )
        report = ingest_file(traces_file, JsonlAdapter())
        assert (report.valid, report.duplicate_ids) == (1, 0)


class TestErrorFile:
    def test_writes_one_json_object_per_issue(self, traces_file: Path, tmp_path: Path) -> None:
        write_traces(
            traces_file,
            trace_line(trace_id="t1"),
            "{not json",
            json.dumps({"trace_id": "t3", "input": {}}),
        )
        errors = tmp_path / "errors.jsonl"
        report = ingest_file(traces_file, JsonlAdapter(), error_path=errors)

        written = [json.loads(line) for line in errors.read_text().splitlines()]
        assert len(written) == report.issue_count
        assert {record["line"] for record in written} == {2, 3}
        assert {record["kind"] for record in written} == {"json", "schema"}

    def test_creates_the_parent_directory(self, traces_file: Path, tmp_path: Path) -> None:
        write_traces(traces_file, "{not json")
        errors = tmp_path / "nested" / "dir" / "errors.jsonl"
        ingest_file(traces_file, JsonlAdapter(), error_path=errors)
        assert errors.is_file()

    def test_a_clean_file_writes_an_empty_error_file(
        self, traces_file: Path, tmp_path: Path
    ) -> None:
        write_traces(traces_file, trace_line())
        errors = tmp_path / "errors.jsonl"
        ingest_file(traces_file, JsonlAdapter(), error_path=errors)
        assert errors.read_text() == ""

    def test_an_unwritable_error_path_is_a_command_error(
        self, traces_file: Path, tmp_path: Path
    ) -> None:
        write_traces(traces_file, trace_line())
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        with pytest.raises(CommandError):
            ingest_file(traces_file, JsonlAdapter(), error_path=blocker / "errors.jsonl")


class TestSampling:
    def test_the_sample_is_bounded_and_the_count_is_not(self, traces_file: Path) -> None:
        write_traces(traces_file, *["{not json"] * 50)
        report = ingest_file(traces_file, JsonlAdapter(), sample_limit=5)
        assert len(report.sample) == 5
        assert report.issue_count == 50
        assert report.truncated == 45

    def test_nothing_is_truncated_when_it_all_fits(self, traces_file: Path) -> None:
        write_traces(traces_file, "{not json")
        assert ingest_file(traces_file, JsonlAdapter()).truncated == 0


class TestFileErrors:
    def test_a_missing_file_is_a_command_error(self, tmp_path: Path) -> None:
        with pytest.raises(CommandError, match="does not exist"):
            ingest_file(tmp_path / "nope.jsonl", JsonlAdapter())

    def test_a_directory_is_a_command_error(self, tmp_path: Path) -> None:
        with pytest.raises(CommandError, match="is a directory"):
            ingest_file(tmp_path, JsonlAdapter())


class TestIngestCommand:
    def test_storing_needs_an_initialized_project(self, traces_file: Path, tmp_path: Path) -> None:
        write_traces(traces_file, trace_line())
        with pytest.raises(CommandError, match=r"evalkeep\.yaml"):
            ingest_traces(traces_file, project_root=tmp_path)

    def test_validate_only_and_dry_run_cannot_be_combined(self, traces_file: Path) -> None:
        write_traces(traces_file, trace_line())
        with pytest.raises(CommandError, match="cannot be combined"):
            ingest_traces(traces_file, validate_only=True, dry_run=True)

    def test_an_unknown_format_is_a_command_error(self, traces_file: Path) -> None:
        write_traces(traces_file, trace_line())
        with pytest.raises(CommandError, match="Unknown trace format"):
            ingest_traces(traces_file, adapter_name="parquet", validate_only=True)


class TestIngestCli:
    def test_a_valid_file_exits_zero(self, runner: CliRunner, traces_file: Path) -> None:
        write_traces(traces_file, trace_line())
        result = runner.invoke(app, ["ingest", str(traces_file), "--validate-only"])
        assert result.exit_code == ExitCode.OK
        assert "Valid" in result.stdout

    def test_record_errors_exit_one(self, runner: CliRunner, traces_file: Path) -> None:
        write_traces(traces_file, trace_line(), "{not json")
        result = runner.invoke(app, ["ingest", str(traces_file), "--validate-only"])
        assert result.exit_code == ExitCode.RECORD_ERRORS
        assert "Invalid" in result.stdout

    def test_a_missing_file_exits_two(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["ingest", str(tmp_path / "nope.jsonl"), "--validate-only"])
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_without_validate_only_it_exits_two(self, runner: CliRunner, traces_file: Path) -> None:
        write_traces(traces_file, trace_line())
        result = runner.invoke(app, ["ingest", str(traces_file)])
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_the_errors_option_writes_the_file(
        self, runner: CliRunner, traces_file: Path, tmp_path: Path
    ) -> None:
        write_traces(traces_file, "{not json")
        errors = tmp_path / "errors.jsonl"
        result = runner.invoke(
            app,
            ["ingest", str(traces_file), "--validate-only", "--errors", str(errors)],
        )
        assert result.exit_code == ExitCode.RECORD_ERRORS
        assert json.loads(errors.read_text().splitlines()[0])["line"] == 1

    def test_extra_issues_point_at_the_errors_option(
        self, runner: CliRunner, traces_file: Path
    ) -> None:
        write_traces(traces_file, *["{not json"] * 12)
        result = runner.invoke(app, ["ingest", str(traces_file), "--validate-only", "--show", "3"])
        assert "and 9 more" in result.stdout
        assert "--errors" in result.stdout

    def test_extra_issues_point_at_the_error_file_when_there_is_one(
        self, runner: CliRunner, traces_file: Path, tmp_path: Path
    ) -> None:
        write_traces(traces_file, *["{not json"] * 12)
        errors = tmp_path / "errors.jsonl"
        result = runner.invoke(
            app,
            ["ingest", str(traces_file), "--validate-only", "--show", "3", "--errors", str(errors)],
        )
        assert "and 9 more" in result.stdout
        assert len(errors.read_text().splitlines()) == 12

    def test_duplicate_ids_are_called_out_in_the_summary(
        self, runner: CliRunner, traces_file: Path
    ) -> None:
        write_traces(traces_file, trace_line(trace_id="t1"), trace_line(trace_id="t1"))
        result = runner.invoke(app, ["ingest", str(traces_file), "--validate-only"])
        assert "duplicate ids" in result.stdout

    def test_the_bundled_example_passes_through_the_cli(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["ingest", str(EXAMPLE), "--validate-only"])
        assert result.exit_code == ExitCode.OK

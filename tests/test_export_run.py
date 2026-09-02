"""Export and delegated execution (guide 8I)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from typer.testing import CliRunner

from evalsmith.cli import app
from evalsmith.commands.analyze_cmd import label_failure
from evalsmith.commands.dataset_cmd import build_dataset, list_tests
from evalsmith.commands.detect_cmd import run_detection
from evalsmith.commands.discover_cmd import run_discovery
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.commands.review_cmd import approve_test, edit_test
from evalsmith.commands.run_cmd import approved_tests, export_suite, run_suite
from evalsmith.commands.target_cmd import add_target
from evalsmith.errors import CommandError, ExitCode
from evalsmith.exporters import ExportFormat, parse_format
from evalsmith.exporters.generic import to_jsonl, to_record
from evalsmith.exporters.promptfoo import assertion, provider_for
from evalsmith.regression import Expectation, ExpectationType
from evalsmith.runner import import_results, write_suite
from evalsmith.runs import ErrorKind, Outcome, suite_hash
from evalsmith.targets import Target, TargetKind

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/refund-agent/traces.jsonl"
AGENTS = ROOT / "examples/refund-agent/agents"

NODE = shutil.which("node")
E2E = os.environ.get("EVALSMITH_E2E") == "1"

LABELS: dict[str, tuple[str, str, str, str]] = {
    "trace-1042": (
        "wrong_tool_argument",
        "tool_arguments",
        "high",
        "Refunded the oldest order instead of the newest order.",
    ),
    "trace-1043": (
        "wrong_tool_argument",
        "tool_arguments",
        "critical",
        "Refunded an older order instead of the newest order.",
    ),
    "trace-1051": (
        "unnecessary_action",
        "planning",
        "critical",
        "Refunded every order on the account when the user asked for one.",
    ),
}

COMPLETED = yaml.safe_dump(
    {
        "input": {"text": "Refund my latest order."},
        "expectations": [
            {
                "type": "tool_argument_equals",
                "tool": "refund_order",
                "path": "order_id",
                "value": "order-C",
            },
            {
                "type": "tool_argument_not_equals",
                "tool": "refund_order",
                "path": "order_id",
                "value": "order-A",
            },
            {"type": "max_tool_calls", "tool": "refund_order", "value": 1},
        ],
    }
)


def expectation(kind: ExpectationType, **kwargs: Any) -> Expectation:
    return Expectation(type=kind, **kwargs)


def script_target(name: str = "baseline") -> Target:
    return Target(
        target_id=name,
        kind=TargetKind.PYTHON,
        path=f"agents/{name}.py",
        function="call_api",
    )


@pytest.fixture
def approved(initialized_project: Path) -> Path:
    """A project with three approved, complete regression tests."""
    from evalsmith.analysis import Component, FailureType, Severity

    ingest_traces(EXAMPLE, project_root=initialized_project)
    run_detection(project_root=initialized_project)
    for trace_id, (kind, component, severity, summary) in LABELS.items():
        label_failure(
            trace_id,
            failure_type=FailureType(kind),
            component=Component(component),
            severity=Severity(severity),
            summary=summary,
            project_root=initialized_project,
            labeler="alex",
        )
    run_discovery(project_root=initialized_project)
    build_dataset(project_root=initialized_project, representatives_only=False)
    for test in list_tests(project_root=initialized_project).tests:
        edit_test(test.test_id, COMPLETED, project_root=initialized_project, editor="alex")
        approve_test(test.test_id, project_root=initialized_project, reviewer="alex")

    agents = initialized_project / "agents"
    agents.mkdir(exist_ok=True)
    for name in ("baseline.py", "candidate.py"):
        shutil.copy(AGENTS / name, agents / name)
    for name in ("baseline", "candidate"):
        add_target(
            name,
            TargetKind.PYTHON,
            project_root=initialized_project,
            path=f"agents/{name}.py",
            function="call_api",
        )
    return initialized_project


def run_js(expression: str, output: Any) -> Any:
    """Evaluate a generated assertion the way the runner will."""
    script = f"const output = {json.dumps(output)};\nconsole.log(JSON.stringify(({expression})));"
    completed = subprocess.run(
        [str(NODE), "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout.strip())


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestGeneratedJavaScript:
    """The assertions are JavaScript, so they are tested by running JavaScript.

    Reading them is not enough: the bug these tests exist for was an operator
    precedence mistake that made every tool assertion silently pass.
    """

    REFUNDED_A: ClassVar[dict[str, Any]] = {
        "text": "I've refunded order order-A.",
        "toolCalls": [
            {"tool": "list_orders", "arguments": {"customer_id": "cust-77"}},
            {"tool": "refund_order", "arguments": {"order_id": "order-A"}},
        ],
    }
    REFUNDED_C: ClassVar[dict[str, Any]] = {
        "text": "I've refunded order order-C.",
        "toolCalls": [
            {"tool": "list_orders", "arguments": {"customer_id": "cust-77"}},
            {"tool": "refund_order", "arguments": {"order_id": "order-C"}},
        ],
    }

    def _js(self, kind: ExpectationType, **kwargs: Any) -> str:
        value = assertion(expectation(kind, **kwargs))["value"]
        return str(value)

    def test_tool_argument_not_equals_rejects_the_observed_value(self) -> None:
        js = self._js(
            ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
            tool="refund_order",
            path="order_id",
            value="order-A",
        )
        assert run_js(js, self.REFUNDED_A) is False
        assert run_js(js, self.REFUNDED_C) is True

    def test_tool_argument_equals_requires_the_right_value(self) -> None:
        js = self._js(
            ExpectationType.TOOL_ARGUMENT_EQUALS,
            tool="refund_order",
            path="order_id",
            value="order-C",
        )
        assert run_js(js, self.REFUNDED_A) is False
        assert run_js(js, self.REFUNDED_C) is True

    def test_a_second_wrong_call_is_not_excused_by_a_right_one(self) -> None:
        """not_equals means no call passed that value, not 'some call differed'."""
        js = self._js(
            ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
            tool="refund_order",
            path="order_id",
            value="order-A",
        )
        both = {
            "text": "",
            "toolCalls": [
                {"tool": "refund_order", "arguments": {"order_id": "order-C"}},
                {"tool": "refund_order", "arguments": {"order_id": "order-A"}},
            ],
        }
        assert run_js(js, both) is False

    def test_tool_called_and_not_called(self) -> None:
        called = self._js(ExpectationType.TOOL_CALLED, tool="refund_order")
        not_called = self._js(ExpectationType.TOOL_NOT_CALLED, tool="refund_order")
        assert run_js(called, self.REFUNDED_A) is True
        assert run_js(not_called, self.REFUNDED_A) is False
        empty = {"text": "", "toolCalls": []}
        assert run_js(called, empty) is False
        assert run_js(not_called, empty) is True

    def test_max_tool_calls_counts_the_named_tool(self) -> None:
        js = self._js(ExpectationType.MAX_TOOL_CALLS, tool="refund_order", value=1)
        assert run_js(js, self.REFUNDED_A) is True
        three = {
            "text": "",
            "toolCalls": [
                {"tool": "refund_order", "arguments": {"order_id": f"order-{i}"}} for i in "ABC"
            ],
        }
        assert run_js(js, three) is False

    def test_max_tool_calls_without_a_tool_counts_everything(self) -> None:
        js = self._js(ExpectationType.MAX_TOOL_CALLS, value=1)
        assert run_js(js, self.REFUNDED_A) is False

    def test_output_assertions(self) -> None:
        contains = self._js(ExpectationType.OUTPUT_CONTAINS, value="order-A")
        not_contains = self._js(ExpectationType.OUTPUT_NOT_CONTAINS, value="order-A")
        matches = self._js(ExpectationType.OUTPUT_MATCHES, value="order-[A-Z]")
        assert run_js(contains, self.REFUNDED_A) is True
        assert run_js(not_contains, self.REFUNDED_A) is False
        assert run_js(matches, self.REFUNDED_A) is True

    def test_a_missing_output_does_not_throw(self) -> None:
        js = self._js(ExpectationType.TOOL_CALLED, tool="refund_order")
        assert run_js(js, None) is False
        assert run_js(js, {"text": "hi"}) is False

    def test_values_with_quotes_do_not_break_out(self) -> None:
        """Every literal is embedded as JSON, so trace text cannot inject code."""
        js = self._js(
            ExpectationType.TOOL_ARGUMENT_EQUALS,
            tool="refund_order",
            path="order_id",
            value='order-"); process.exit(1); //',
        )
        assert run_js(js, self.REFUNDED_A) is False

    def test_nested_argument_paths_resolve(self) -> None:
        js = self._js(
            ExpectationType.TOOL_ARGUMENT_EQUALS,
            tool="refund_order",
            path="order.id",
            value="order-C",
        )
        nested = {
            "text": "",
            "toolCalls": [{"tool": "refund_order", "arguments": {"order": {"id": "order-C"}}}],
        }
        assert run_js(js, nested) is True

    def test_a_number_and_its_string_are_different(self) -> None:
        js = self._js(ExpectationType.TOOL_ARGUMENT_EQUALS, tool="t", path="n", value=1)
        assert run_js(js, {"toolCalls": [{"tool": "t", "arguments": {"n": "1"}}]}) is False
        assert run_js(js, {"toolCalls": [{"tool": "t", "arguments": {"n": 1}}]}) is True


class TestProviderTranslation:
    def test_a_python_target_becomes_a_file_provider(self) -> None:
        provider = provider_for(script_target())
        assert provider["id"] == "file://agents/baseline.py:call_api"

    def test_a_script_path_is_rewritten_for_the_config_location(self, tmp_path: Path) -> None:
        """The config is a build artifact and can land anywhere; the path must follow."""
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents/baseline.py").write_text("")
        config_dir = tmp_path / ".evalsmith" / "runs" / "abc"
        config_dir.mkdir(parents=True)
        provider = provider_for(script_target(), project_root=tmp_path, config_dir=config_dir)
        resolved = (config_dir / provider["id"].removeprefix("file://").split(":")[0]).resolve()
        assert resolved == (tmp_path / "agents/baseline.py").resolve()

    def test_an_http_target_carries_its_request_and_extraction(self) -> None:
        target = Target(
            target_id="candidate",
            kind=TargetKind.HTTP,
            url="https://agent.example.com/chat",
            method="POST",
            headers={"Authorization": "${AGENT_TOKEN}"},
            body={"message": "{{input}}"},
        )
        target.extract.output = "json.reply"
        target.extract.tool_calls = "json.tool_calls"
        provider = provider_for(target)
        assert provider["id"] == "https://agent.example.com/chat"
        assert provider["config"]["headers"] == {"Authorization": "${AGENT_TOKEN}"}
        assert "json.reply" in provider["config"]["transformResponse"]
        assert "json.tool_calls" in provider["config"]["transformResponse"]

    def test_a_model_target_passes_its_provider_id(self) -> None:
        target = Target(
            target_id="m", kind=TargetKind.MODEL, provider="anthropic:messages:claude-opus-5"
        )
        assert provider_for(target)["id"] == "anthropic:messages:claude-opus-5"

    def test_a_rubric_becomes_an_llm_rubric(self) -> None:
        built = assertion(
            expectation(ExpectationType.HUMAN_RUBRIC, value="refunds the newest order")
        )
        assert built == {"type": "llm-rubric", "value": "refunds the newest order"}


class TestExport:
    def test_only_approved_tests_are_exported(self, approved: Path) -> None:
        assert len(approved_tests(project_root=approved)) == 3
        result = export_suite(project_root=approved, target_id="baseline")
        config = yaml.safe_load(result.path.read_text())
        assert len(config["tests"]) == 3

    def test_a_draft_is_never_exported(self, approved: Path) -> None:
        from evalsmith.config import Project
        from evalsmith.regression import ReviewStatus
        from evalsmith.storage import TraceStore

        with TraceStore.open(Project.load(approved).database_path) as store:
            test = store.tests.list(status=ReviewStatus.APPROVED)[0]
            test.status = ReviewStatus.DRAFT
            store.tests.save(test)
        assert len(approved_tests(project_root=approved)) == 2

    def test_exporting_nothing_is_a_command_error(self, initialized_project: Path) -> None:
        with pytest.raises(CommandError, match="No approved tests"):
            export_suite(project_root=initialized_project)

    def test_the_config_names_tests_by_their_stable_id(self, approved: Path) -> None:
        result = export_suite(project_root=approved, target_id="baseline")
        config = yaml.safe_load(result.path.read_text())
        for case in config["tests"]:
            assert case["description"] == case["metadata"]["test_id"]

    def test_jsonl_export_carries_provenance(self, approved: Path) -> None:
        result = export_suite(project_root=approved, export_format=ExportFormat.JSONL)
        records = [json.loads(line) for line in result.path.read_text().splitlines()]
        assert len(records) == 3
        assert records[0]["provenance"]["trace_id"]
        assert records[0]["status"] == "approved"

    def test_an_unknown_format_is_a_command_error(self) -> None:
        with pytest.raises(CommandError, match="Unknown export format"):
            parse_format("parquet")

    def test_an_ambiguous_target_is_a_command_error(self, approved: Path) -> None:
        with pytest.raises(CommandError, match="Which target"):
            export_suite(project_root=approved)

    def test_the_written_config_is_valid_yaml(self, approved: Path, tmp_path: Path) -> None:
        result = export_suite(project_root=approved, target_id="baseline", out=tmp_path)
        assert yaml.safe_load(result.path.read_text())["providers"]


class TestResultImport:
    def _results(self, tmp_path: Path, records: list[dict[str, Any]]) -> Any:
        path = tmp_path / "results.json"
        path.write_text(json.dumps({"results": {"results": records, "version": 3}}))
        return import_results(path)

    def _record(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "testCase": {"description": "t1", "metadata": {"test_id": "t1"}},
            "success": True,
            "failureReason": 0,
            "latencyMs": 12,
            "response": {"output": {"text": "ok", "toolCalls": []}},
        }
        record.update(overrides)
        return record

    def test_a_pass_is_a_pass(self, tmp_path: Path) -> None:
        (result,) = self._results(tmp_path, [self._record()])
        assert result.outcome is Outcome.PASS
        assert result.error_kind is None
        assert result.latency_ms == 12

    def test_an_assertion_failure_is_a_failure(self, tmp_path: Path) -> None:
        (result,) = self._results(
            tmp_path,
            [
                self._record(
                    success=False,
                    failureReason=1,
                    error="Custom function returned false",
                    gradingResult={
                        "pass": False,
                        "componentResults": [
                            {"pass": False, "reason": "refunded order-A"},
                            {"pass": True, "reason": "fine"},
                        ],
                    },
                )
            ],
        )
        assert result.outcome is Outcome.FAIL
        assert result.error_kind is None
        assert result.failed_assertions == ["refunded order-A"]

    def test_an_execution_error_is_not_a_failure(self, tmp_path: Path) -> None:
        """A test that never ran must not look like a regression."""
        (result,) = self._results(
            tmp_path,
            [self._record(success=False, failureReason=2, error="Python error: boom")],
        )
        assert result.outcome is Outcome.ERROR
        assert result.error_kind is ErrorKind.EXECUTION_ERROR
        assert not result.comparable

    def test_a_timeout_is_distinguished_from_a_crash(self, tmp_path: Path) -> None:
        (result,) = self._results(
            tmp_path,
            [
                self._record(
                    success=False,
                    failureReason=2,
                    error="Error: Worker failed to become ready within timeout",
                )
            ],
        )
        assert result.outcome is Outcome.ERROR
        assert result.error_kind is ErrorKind.TIMEOUT

    def test_an_unclassified_failure_is_treated_as_an_error(self, tmp_path: Path) -> None:
        (result,) = self._results(
            tmp_path, [self._record(success=False, failureReason=99, error="who knows")]
        )
        assert result.outcome is Outcome.ERROR

    def test_the_observation_is_redacted(self, tmp_path: Path) -> None:
        (result,) = self._results(
            tmp_path,
            [
                self._record(
                    response={"output": {"text": "mailed shopper@example.com", "toolCalls": []}}
                )
            ],
        )
        assert result.observation is not None
        assert "shopper@example.com" not in result.observation
        assert "[REDACTED:email]" in result.observation

    def test_errors_are_redacted_too(self, tmp_path: Path) -> None:
        (result,) = self._results(
            tmp_path,
            [
                self._record(
                    success=False,
                    failureReason=2,
                    error="auth failed for sk-live0123456789abcdefghij",
                )
            ],
        )
        assert result.error is not None and "sk-live" not in result.error

    def test_records_without_a_test_id_are_skipped(self, tmp_path: Path) -> None:
        assert self._results(tmp_path, [{"success": True, "testCase": {}}]) == []

    def test_an_unreadable_results_file_is_a_command_error(self, tmp_path: Path) -> None:
        with pytest.raises(CommandError, match="Could not read"):
            import_results(tmp_path / "missing.json")


class TestSuiteHash:
    def test_the_same_tests_hash_the_same(self) -> None:
        assert suite_hash(["a", "b"]) == suite_hash(["b", "a"])

    def test_a_different_suite_hashes_differently(self) -> None:
        assert suite_hash(["a", "b"]) != suite_hash(["a", "b", "c"])


class TestRunInvocation:
    def test_the_runner_is_never_given_a_shell(
        self, approved: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trace text reaches this command; a shell string would be an RCE."""
        captured: dict[str, Any] = {}
        original = subprocess.run

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv: Any, **kwargs: Any) -> Any:
            # Only intercept the runner itself: other code (platform probing,
            # for one) legitimately uses subprocess too.
            if not (isinstance(argv, list) and "eval" in argv):
                return original(argv, **kwargs)
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            directory = Path(str(argv[argv.index("--output") + 1])).parent
            (directory / "results.json").write_text(
                json.dumps({"results": {"results": [], "version": 3}})
            )
            return Completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        run_suite(project_root=approved, target_id="baseline")

        assert isinstance(captured["argv"], list)
        assert captured["kwargs"]["shell"] is False
        assert "eval" in captured["argv"]
        assert not any(";" in str(part) or "|" in str(part) for part in captured["argv"])

    def test_a_missing_runner_is_a_command_error(self, approved: Path) -> None:
        from evalsmith.config import Project

        project = Project.load(approved)
        config_path = approved / "evalsmith.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["runner"] = {"command": ["definitely-not-installed-xyz"]}
        config_path.write_text(yaml.safe_dump(config))
        assert project is not None
        with pytest.raises(CommandError, match="Could not run"):
            run_suite(project_root=approved, target_id="baseline")

    def test_running_without_approved_tests_is_a_command_error(
        self, initialized_project: Path
    ) -> None:
        add_target(
            "baseline",
            TargetKind.PYTHON,
            project_root=initialized_project,
            path="agents/baseline.py",
        )
        with pytest.raises(CommandError, match="No approved tests"):
            run_suite(project_root=initialized_project, target_id="baseline")

    def test_a_missing_environment_variable_stops_the_run(self, approved: Path) -> None:
        add_target(
            "remote",
            TargetKind.HTTP,
            project_root=approved,
            url="https://agent.example.com/chat",
            body={"message": "{{input}}"},
            headers={"Authorization": "${DEFINITELY_UNSET_TOKEN}"},
        )
        with pytest.raises(CommandError, match="DEFINITELY_UNSET_TOKEN"):
            run_suite(project_root=approved, target_id="remote")


class TestWriteSuite:
    def test_it_writes_a_loadable_config(self, approved: Path, tmp_path: Path) -> None:
        tests = approved_tests(project_root=approved)
        path = write_suite(tests, script_target(), tmp_path, project_root=approved)
        config = yaml.safe_load(path.read_text())
        assert config["providers"][0]["id"].startswith("file://")
        assert len(config["tests"]) == 3


class TestGenericExport:
    def test_a_record_carries_everything_a_runner_needs(self, approved: Path) -> None:
        test = approved_tests(project_root=approved)[0]
        record = to_record(test)
        assert set(record) >= {"test_id", "input", "expectations", "fixtures", "provenance"}

    def test_jsonl_is_one_object_per_line(self, approved: Path) -> None:
        text = to_jsonl(approved_tests(project_root=approved))
        assert all(json.loads(line) for line in text.splitlines())


class TestCli:
    def test_targets_list(self, runner: CliRunner, approved: Path) -> None:
        result = runner.invoke(app, ["targets", "list", "-C", str(approved)])
        assert result.exit_code == ExitCode.OK
        assert "baseline" in result.stdout

    def test_targets_add_rejects_a_secret(self, runner: CliRunner, approved: Path) -> None:
        result = runner.invoke(
            app,
            [
                "targets",
                "add",
                "leaky",
                "--type",
                "http",
                "-C",
                str(approved),
                "--url",
                "https://x.example.com",
                "--body",
                '{"m": "{{input}}"}',
                "--header",
                "Authorization=Bearer sk-live0123456789abcdefghijkl",
            ],
        )
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_targets_show_reports_environment(self, runner: CliRunner, approved: Path) -> None:
        result = runner.invoke(app, ["targets", "show", "baseline", "-C", str(approved)])
        assert result.exit_code == ExitCode.OK
        assert "baseline" in result.stdout

    def test_export_writes_a_config(self, runner: CliRunner, approved: Path) -> None:
        result = runner.invoke(app, ["export", "-C", str(approved), "--target", "baseline"])
        assert result.exit_code == ExitCode.OK
        assert "promptfoo" in result.stdout
        assert "Approved tests only" in result.stdout

    def test_export_jsonl(self, runner: CliRunner, approved: Path) -> None:
        result = runner.invoke(app, ["export", "-C", str(approved), "--format", "jsonl"])
        assert result.exit_code == ExitCode.OK

    def test_a_bad_header_is_a_command_error(self, runner: CliRunner, approved: Path) -> None:
        result = runner.invoke(
            app,
            [
                "targets",
                "add",
                "x",
                "--type",
                "http",
                "-C",
                str(approved),
                "--url",
                "https://x",
                "--body",
                "{}",
                "--header",
                "no-equals-sign",
            ],
        )
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_a_bad_body_is_a_command_error(self, runner: CliRunner, approved: Path) -> None:
        result = runner.invoke(
            app,
            [
                "targets",
                "add",
                "x",
                "--type",
                "http",
                "-C",
                str(approved),
                "--url",
                "https://x",
                "--body",
                "not json",
            ],
        )
        assert result.exit_code == ExitCode.COMMAND_ERROR


@pytest.mark.skipif(not E2E, reason="set EVALSMITH_E2E=1 to run the real runner")
class TestAgainstTheRealRunner:
    """Guide 9.2: the buggy agent fails the suite and the fixed one passes it."""

    def test_the_baseline_fails_and_the_candidate_passes(self, approved: Path) -> None:
        baseline = run_suite(project_root=approved, target_id="baseline")
        candidate = run_suite(project_root=approved, target_id="candidate")
        assert baseline.counts.get(Outcome.FAIL) == 3
        assert candidate.counts.get(Outcome.PASS) == 3
        assert baseline.run.suite_hash == candidate.run.suite_hash

"""Analysis: provider interface, caching, manual labelling (guide 8E)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from evalkeep.analysis import (
    AnalyzerError,
    AnalyzerProvider,
    Component,
    FailureAnalysis,
    FailureType,
    ProviderAnalysis,
    Severity,
)
from evalkeep.analysis_run import analyze_failures
from evalkeep.analyzers import MANUAL_PROVIDER, StubAnalyzer, get_analyzer
from evalkeep.cache import AnalysisCache, cache_key
from evalkeep.cli import app
from evalkeep.commands.analyze_cmd import label_failure, run_analysis
from evalkeep.commands.detect_cmd import run_detection, show_failure
from evalkeep.commands.ingest_cmd import ingest_traces
from evalkeep.config import AnalyzerConfig, Project
from evalkeep.detectors import Signal
from evalkeep.errors import CommandError, ExitCode
from evalkeep.failures import FailureStatus
from evalkeep.prompts import (
    FAILURE_ANALYSIS_PROMPT_VERSION,
    FAILURE_ANALYSIS_SCHEMA,
    failure_analysis_prompt,
)
from evalkeep.storage import TraceStore
from evalkeep.trace import NormalizedTrace

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/refund-agent/traces.jsonl"


class RecordingAnalyzer:
    """A provider that answers predictably and counts how often it was asked."""

    name: ClassVar[str] = "recording"
    description: ClassVar[str] = "test double"

    def __init__(
        self,
        *,
        model: str = "test-model",
        summary: str = "refunded the wrong order",
        error: str | None = None,
    ) -> None:
        self.model = model
        self.summary = summary
        self.error = error
        self.calls: list[str] = []

    @property
    def identity(self) -> str:
        return f"{self.name}:{self.model}"

    def analyze_failure(self, trace: NormalizedTrace, signals: list[Signal]) -> ProviderAnalysis:
        self.calls.append(trace.trace_id)
        if self.error is not None:
            raise AnalyzerError(self.error)
        return ProviderAnalysis(
            failure_type=FailureType.WRONG_TOOL_ARGUMENT,
            component=Component.TOOL_ARGUMENTS,
            severity=Severity.HIGH,
            summary=self.summary,
            raw_response=json.dumps({"summary": self.summary}),
        )


@pytest.fixture
def detected(initialized_project: Path) -> Path:
    """A project with the refund example ingested and detected."""
    ingest_traces(EXAMPLE, project_root=initialized_project)
    run_detection(project_root=initialized_project)
    return initialized_project


@pytest.fixture
def store(detected: Path) -> Iterator[TraceStore]:
    with TraceStore.open(Project.load(detected).database_path) as opened:
        yield opened


@pytest.fixture
def cache(tmp_path: Path) -> AnalysisCache:
    return AnalysisCache(tmp_path / "cache")


class TestProviderRegistry:
    def test_manual_resolves_to_no_provider(self) -> None:
        """Manual is the absence of a provider, not a provider that guesses."""
        assert get_analyzer(AnalyzerConfig(provider=MANUAL_PROVIDER)) is None

    def test_manual_is_the_default(self) -> None:
        assert AnalyzerConfig().provider == MANUAL_PROVIDER

    def test_the_stub_resolves(self) -> None:
        provider = get_analyzer(AnalyzerConfig(provider="stub"))
        assert isinstance(provider, AnalyzerProvider)
        assert provider is not None and provider.identity == "stub"

    def test_an_unknown_provider_is_a_command_error(self) -> None:
        with pytest.raises(CommandError, match="Unknown analyzer provider"):
            get_analyzer(AnalyzerConfig(provider="oracle"))

    def test_the_model_is_part_of_the_analyst_identity(self) -> None:
        one = RecordingAnalyzer(model="model-a")
        two = RecordingAnalyzer(model="model-b")
        assert one.identity != two.identity


class TestAnalysisPass:
    def test_analyzes_every_unlabelled_failure(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        provider = RecordingAnalyzer()
        report = analyze_failures(store, provider, cache)
        assert report.considered == 3
        assert report.analyzed == 3
        assert len(provider.calls) == 3

    def test_stores_the_analysis_with_its_provenance(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        analyze_failures(store, RecordingAnalyzer(), cache)
        failure = store.failures.get_by_trace("trace-1042")
        assert failure is not None
        analysis = store.failures.get_analysis(failure.failure_id)
        assert analysis is not None
        assert analysis.failure_type is FailureType.WRONG_TOOL_ARGUMENT
        assert analysis.component is Component.TOOL_ARGUMENTS
        assert analysis.severity is Severity.HIGH
        assert analysis.analyzer == "recording:test-model"
        assert analysis.prompt_version == FAILURE_ANALYSIS_PROMPT_VERSION

    def test_keeps_the_raw_response_for_audit(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        analyze_failures(store, RecordingAnalyzer(), cache)
        failure = store.failures.get_by_trace("trace-1042")
        assert failure is not None
        analysis = store.failures.get_analysis(failure.failure_id)
        assert analysis is not None and analysis.raw_response is not None
        assert "refunded the wrong order" in analysis.raw_response

    def test_dismissed_failures_are_not_analyzed(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        failure = store.failures.get_by_trace("trace-1051")
        assert failure is not None
        failure.review(FailureStatus.DISMISSED, reviewer="alex", reason="synthetic")
        store.failures.save(failure)

        report = analyze_failures(store, RecordingAnalyzer(), cache)
        assert report.considered == 2

    def test_the_limit_stops_early(self, store: TraceStore, cache: AnalysisCache) -> None:
        provider = RecordingAnalyzer()
        report = analyze_failures(store, provider, cache, limit=1)
        assert report.analyzed == 1
        assert len(provider.calls) == 1

    def test_counts_types(self, store: TraceStore, cache: AnalysisCache) -> None:
        report = analyze_failures(store, RecordingAnalyzer(), cache)
        assert report.by_type == {"wrong_tool_argument": 3}


class TestProviderFailures:
    def test_one_bad_analysis_does_not_abandon_the_run(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        class Flaky(RecordingAnalyzer):
            def analyze_failure(
                self, trace: NormalizedTrace, signals: list[Signal]
            ) -> ProviderAnalysis:
                if trace.trace_id == "trace-1043":
                    raise AnalyzerError("model refused")
                return super().analyze_failure(trace, signals)

        report = analyze_failures(store, Flaky(), cache)
        assert (report.analyzed, report.failed) == (2, 1)
        assert report.errors[0][1] == "model refused"

    def test_a_failed_analysis_stores_nothing(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        report = analyze_failures(store, RecordingAnalyzer(error="down"), cache)
        assert report.failed == 3
        assert store.failures.counts_by_type() == {}


class TestCaching:
    def test_the_key_covers_content_analyst_and_prompt_version(self) -> None:
        base = cache_key("sha256:abc", "anthropic:m1", 1)
        assert base != cache_key("sha256:def", "anthropic:m1", 1)
        assert base != cache_key("sha256:abc", "anthropic:m2", 1)
        assert base != cache_key("sha256:abc", "anthropic:m1", 2)
        assert base == cache_key("sha256:abc", "anthropic:m1", 1)

    def test_a_second_pass_reuses_cached_answers(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        provider = RecordingAnalyzer()
        analyze_failures(store, provider, cache)

        # Wipe the stored analyses; the cache alone must answer.
        store._connection.execute("DELETE FROM failure_analyses")
        store._connection.commit()

        report = analyze_failures(store, provider, cache)
        assert report.from_cache == 3
        assert len(provider.calls) == 3  # no new calls

    def test_a_different_model_does_not_reuse_the_cache(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        analyze_failures(store, RecordingAnalyzer(model="model-a"), cache)
        other = RecordingAnalyzer(model="model-b")
        report = analyze_failures(store, other, cache)
        assert report.analyzed == 3
        assert len(other.calls) == 3

    def test_a_disabled_cache_writes_nothing(self, store: TraceStore, tmp_path: Path) -> None:
        cache = AnalysisCache(tmp_path / "cache", enabled=False)
        analyze_failures(store, RecordingAnalyzer(), cache)
        assert not (tmp_path / "cache").exists()

    def test_a_corrupt_entry_is_a_miss_not_a_crash(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        provider = RecordingAnalyzer()
        analyze_failures(store, provider, cache)
        for path in cache.root.rglob("*.json"):
            path.write_text("{ this is not json")

        store._connection.execute("DELETE FROM failure_analyses")
        store._connection.commit()
        report = analyze_failures(store, provider, cache)
        assert report.analyzed == 3

    def test_the_cache_survives_a_missing_directory(self, tmp_path: Path) -> None:
        cache = AnalysisCache(tmp_path / "does-not-exist-yet")
        assert cache.get("abc") is None
        cache.put("abc", {"hello": "world"})
        assert cache.get("abc") == {"hello": "world"}


class TestReanalysis:
    def test_an_existing_analysis_is_left_alone(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        provider = RecordingAnalyzer()
        analyze_failures(store, provider, cache)
        report = analyze_failures(store, provider, cache)
        assert (report.analyzed, report.skipped) == (0, 3)
        assert len(provider.calls) == 3

    def test_a_new_prompt_version_makes_analyses_stale(
        self, store: TraceStore, cache: AnalysisCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = RecordingAnalyzer()
        analyze_failures(store, provider, cache)
        monkeypatch.setattr("evalkeep.analysis_run.FAILURE_ANALYSIS_PROMPT_VERSION", 2)
        report = analyze_failures(store, provider, cache)
        assert report.analyzed == 3

    def test_reanalyze_refreshes_machine_analyses(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        provider = RecordingAnalyzer()
        analyze_failures(store, provider, cache)
        report = analyze_failures(store, provider, cache, reanalyze=True)
        assert report.analyzed == 3


class TestManualLabelsAreProtected:
    def _label(self, detected: Path, trace_id: str = "trace-1042") -> FailureAnalysis:
        return label_failure(
            trace_id,
            failure_type=FailureType.WRONG_TOOL_ARGUMENT,
            component=Component.TOOL_ARGUMENTS,
            severity=Severity.CRITICAL,
            summary="Refunded the oldest order instead of the newest.",
            project_root=detected,
            labeler="alex",
        )

    def test_a_hand_label_records_its_author(self, detected: Path) -> None:
        analysis = self._label(detected)
        assert analysis.analyzer == "manual:alex"
        assert analysis.labeler == "alex"
        assert analysis.manual

    def test_analysis_skips_hand_labelled_failures(
        self, detected: Path, store: TraceStore, cache: AnalysisCache
    ) -> None:
        self._label(detected)
        with TraceStore.open(Project.load(detected).database_path) as opened:
            report = analyze_failures(opened, RecordingAnalyzer(), cache)
        assert report.manual_kept == 1
        assert report.analyzed == 2

    def test_reanalyze_alone_does_not_replace_a_hand_label(
        self, detected: Path, cache: AnalysisCache
    ) -> None:
        self._label(detected)
        with TraceStore.open(Project.load(detected).database_path) as opened:
            analyze_failures(opened, RecordingAnalyzer(), cache, reanalyze=True)
        detail = show_failure("trace-1042", project_root=detected)
        assert detail.analysis is not None
        assert detail.analysis.manual

    def test_overwrite_manual_replaces_it_explicitly(
        self, detected: Path, cache: AnalysisCache
    ) -> None:
        self._label(detected)
        with TraceStore.open(Project.load(detected).database_path) as opened:
            analyze_failures(
                opened, RecordingAnalyzer(), cache, reanalyze=True, overwrite_manual=True
            )
        detail = show_failure("trace-1042", project_root=detected)
        assert detail.analysis is not None
        assert not detail.analysis.manual

    def test_a_label_replaces_a_previous_label(self, detected: Path) -> None:
        self._label(detected)
        second = label_failure(
            "trace-1042",
            failure_type=FailureType.UNNECESSARY_ACTION,
            component=Component.PLANNING,
            severity=Severity.LOW,
            summary="Different opinion.",
            project_root=detected,
            labeler="sam",
        )
        detail = show_failure("trace-1042", project_root=detected)
        assert detail.analysis is not None
        assert detail.analysis.failure_type is second.failure_type
        assert detail.analysis.labeler == "sam"

    def test_an_empty_summary_is_refused(self, detected: Path) -> None:
        with pytest.raises(CommandError, match="non-empty --summary"):
            label_failure(
                "trace-1042",
                failure_type=FailureType.OTHER,
                component=Component.UNKNOWN,
                severity=Severity.LOW,
                summary="   ",
                project_root=detected,
            )

    def test_an_unknown_failure_is_a_command_error(self, detected: Path) -> None:
        with pytest.raises(CommandError, match="No failure matching"):
            label_failure(
                "nope",
                failure_type=FailureType.OTHER,
                component=Component.UNKNOWN,
                severity=Severity.LOW,
                summary="x",
                project_root=detected,
            )


class TestRedaction:
    def test_a_provider_summary_is_redacted_before_storage(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        """The model only saw redacted text, but that is an argument, not a guarantee."""
        provider = RecordingAnalyzer(summary="the agent emailed shopper@example.com")
        report = analyze_failures(store, provider, cache)
        failure = store.failures.get_by_trace("trace-1042")
        assert failure is not None
        analysis = store.failures.get_analysis(failure.failure_id)
        assert analysis is not None
        assert "shopper@example.com" not in analysis.summary
        assert "[REDACTED:email]" in analysis.summary
        assert report.redactions >= 1

    def test_a_hand_written_summary_is_redacted_too(self, detected: Path) -> None:
        analysis = label_failure(
            "trace-1042",
            failure_type=FailureType.OTHER,
            component=Component.UNKNOWN,
            severity=Severity.LOW,
            summary="reported by shopper@example.com",
            project_root=detected,
        )
        assert "shopper@example.com" not in analysis.summary

    def test_secrets_do_not_reach_the_database(
        self, store: TraceStore, cache: AnalysisCache
    ) -> None:
        provider = RecordingAnalyzer(summary="key sk-abcdefghijklmnopqrstuvwxyz012345")
        analyze_failures(store, provider, cache)
        raw = Path(store._connection.execute("PRAGMA database_list").fetchone()[2])
        assert b"sk-abcdefghijklmnop" not in raw.read_bytes()


class TestPrompt:
    def test_the_schema_is_closed(self) -> None:
        assert FAILURE_ANALYSIS_SCHEMA["additionalProperties"] is False
        assert set(FAILURE_ANALYSIS_SCHEMA["required"]) == {
            "failure_type",
            "component",
            "severity",
            "summary",
        }

    def test_the_enums_match_the_vocabularies(self) -> None:
        properties = FAILURE_ANALYSIS_SCHEMA["properties"]
        assert properties["failure_type"]["enum"] == [m.value for m in FailureType]
        assert properties["severity"]["enum"] == [m.value for m in Severity]

    def test_the_prompt_includes_evidence_and_the_interaction(self, store: TraceStore) -> None:
        failure = store.failures.get_by_trace("trace-1042")
        stored = store.get("trace-1042")
        assert failure is not None and stored is not None
        prompt = failure_analysis_prompt(stored.trace, failure.signals)
        assert "Refund my latest order." in prompt
        assert "refund_order" in prompt
        assert "explicit_status" in prompt

    def test_the_prompt_is_stable_for_the_same_trace(self, store: TraceStore) -> None:
        """It feeds the cache key by way of the content hash; drift would be silent."""
        failure = store.failures.get_by_trace("trace-1042")
        stored = store.get("trace-1042")
        assert failure is not None and stored is not None
        first = failure_analysis_prompt(stored.trace, failure.signals)
        second = failure_analysis_prompt(stored.trace, failure.signals)
        assert first == second


class TestStubAnalyzer:
    def test_it_labels_its_own_output_as_a_stub(self) -> None:
        trace = NormalizedTrace.model_validate(
            {"trace_id": "t1", "input": {"text": "hi"}, "outcome": {"status": "failure"}}
        )
        produced = StubAnalyzer().analyze_failure(trace, [])
        assert produced.summary.startswith("[stub]")
        assert produced.failure_type is FailureType.OTHER


class TestCommands:
    def test_analyze_without_a_provider_is_a_command_error(self, detected: Path) -> None:
        with pytest.raises(CommandError, match="No analyzer provider"):
            run_analysis(project_root=detected)

    def test_the_error_points_at_manual_labelling(self, detected: Path) -> None:
        try:
            run_analysis(project_root=detected)
        except CommandError as exc:
            assert exc.hint is not None and "failures label" in exc.hint
        else:  # pragma: no cover
            raise AssertionError("expected a CommandError")

    def test_analyze_needs_detected_failures(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        _use_stub(initialized_project)
        with pytest.raises(CommandError, match="No failure candidates"):
            run_analysis(project_root=initialized_project)

    def test_analyze_runs_with_the_stub_provider(self, detected: Path) -> None:
        _use_stub(detected)
        report = run_analysis(project_root=detected)
        assert report.analyzed == 3
        assert report.analyzer == "stub"


class TestCli:
    def test_analyze_reports_what_it_did(self, runner: CliRunner, detected: Path) -> None:
        _use_stub(detected)
        result = runner.invoke(app, ["analyze", "-C", str(detected)])
        assert result.exit_code == ExitCode.OK
        assert "analyzer" in result.stdout
        assert "stub" in result.stdout

    def test_analyze_without_a_provider_exits_two(self, runner: CliRunner, detected: Path) -> None:
        result = runner.invoke(app, ["analyze", "-C", str(detected)])
        assert result.exit_code == ExitCode.COMMAND_ERROR

    def test_label_from_the_cli(self, runner: CliRunner, detected: Path) -> None:
        result = runner.invoke(
            app,
            [
                "failures",
                "label",
                "trace-1042",
                "-C",
                str(detected),
                "--type",
                "wrong_tool_argument",
                "--component",
                "tool_arguments",
                "--severity",
                "high",
                "--summary",
                "Refunded the oldest order.",
                "--labeler",
                "alex",
            ],
        )
        assert result.exit_code == ExitCode.OK
        assert "wrong_tool_argument" in result.stdout

    def test_an_invalid_label_value_is_rejected(self, runner: CliRunner, detected: Path) -> None:
        result = runner.invoke(
            app,
            [
                "failures",
                "label",
                "trace-1042",
                "-C",
                str(detected),
                "--type",
                "made_up_type",
                "--component",
                "tool_arguments",
                "--severity",
                "high",
                "--summary",
                "x",
            ],
        )
        assert result.exit_code != ExitCode.OK

    def test_failures_show_renders_the_analysis(self, runner: CliRunner, detected: Path) -> None:
        _use_stub(detected)
        run_analysis(project_root=detected)
        result = runner.invoke(app, ["failures", "show", "trace-1042", "-C", str(detected)])
        assert "analysis" in result.stdout
        assert "stub" in result.stdout

    def test_failures_show_says_when_nothing_is_analyzed(
        self, runner: CliRunner, detected: Path
    ) -> None:
        result = runner.invoke(app, ["failures", "show", "trace-1042", "-C", str(detected)])
        assert "Not analyzed yet" in result.stdout

    def test_failures_list_shows_type_and_severity(self, runner: CliRunner, detected: Path) -> None:
        runner.invoke(
            app,
            [
                "failures",
                "label",
                "trace-1042",
                "-C",
                str(detected),
                "--type",
                "wrong_tool_argument",
                "--component",
                "tool_arguments",
                "--severity",
                "critical",
                "--summary",
                "Refunded the oldest order.",
            ],
        )
        result = runner.invoke(app, ["failures", "list", "-C", str(detected)])
        assert "critical" in result.stdout


def _use_stub(project_root: Path) -> None:
    config_path = project_root / "evalkeep.yaml"
    config_path.write_text(config_path.read_text().replace("provider: manual", "provider: stub"))


class _Block:
    def __init__(self, type: str, text: str = "") -> None:
        self.type = type
        self.text = text


class _Response:
    def __init__(self, content: list[Any], stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


def _valid_payload(**overrides: Any) -> str:
    payload = {
        "failure_type": "wrong_tool_argument",
        "component": "tool_arguments",
        "severity": "high",
        "summary": "Refunded the oldest order instead of the newest.",
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestAnthropicResponseParsing:
    """The provider cannot be exercised against a live API here, so the response
    handling it depends on is tested directly."""

    def test_a_schema_valid_response_parses(self) -> None:
        from evalkeep.analyzers.anthropic import _parse

        produced = _parse(_Response([_Block("text", _valid_payload())]), "anthropic:m")
        assert produced.failure_type is FailureType.WRONG_TOOL_ARGUMENT
        assert produced.component is Component.TOOL_ARGUMENTS
        assert produced.severity is Severity.HIGH
        assert produced.raw_response is not None

    def test_thinking_blocks_are_skipped(self) -> None:
        from evalkeep.analyzers.anthropic import _parse

        response = _Response([_Block("thinking"), _Block("text", _valid_payload())])
        assert _parse(response, "anthropic:m").summary.startswith("Refunded")

    def test_a_refusal_is_an_analyzer_error(self) -> None:
        from evalkeep.analyzers.anthropic import _parse

        with pytest.raises(AnalyzerError, match="declined"):
            _parse(_Response([], stop_reason="refusal"), "anthropic:m")

    def test_a_truncated_response_is_an_analyzer_error(self) -> None:
        from evalkeep.analyzers.anthropic import _parse

        with pytest.raises(AnalyzerError, match="max_tokens"):
            _parse(_Response([_Block("text", "{")], stop_reason="max_tokens"), "anthropic:m")

    def test_no_text_block_is_an_analyzer_error(self) -> None:
        from evalkeep.analyzers.anthropic import _parse

        with pytest.raises(AnalyzerError, match="no text content"):
            _parse(_Response([_Block("thinking")]), "anthropic:m")

    def test_non_json_text_is_an_analyzer_error(self) -> None:
        from evalkeep.analyzers.anthropic import _parse

        with pytest.raises(AnalyzerError, match="not JSON"):
            _parse(_Response([_Block("text", "Sure! Here you go:")]), "anthropic:m")

    def test_an_off_vocabulary_value_is_an_analyzer_error(self) -> None:
        """A label outside the closed vocabulary must not reach the database."""
        from evalkeep.analyzers.anthropic import _parse

        with pytest.raises(AnalyzerError, match="does not match the schema"):
            _parse(
                _Response([_Block("text", _valid_payload(failure_type="vibes"))]),
                "anthropic:m",
            )

    def test_a_missing_field_is_an_analyzer_error(self) -> None:
        from evalkeep.analyzers.anthropic import _parse

        text = json.dumps({"failure_type": "other", "component": "unknown"})
        with pytest.raises(AnalyzerError, match="does not match the schema"):
            _parse(_Response([_Block("text", text)]), "anthropic:m")

    def test_the_identity_names_the_model(self) -> None:
        from evalkeep.analyzers.anthropic import AnthropicAnalyzer

        assert AnthropicAnalyzer(model="claude-opus-5").identity == "anthropic:claude-opus-5"

    def test_it_satisfies_the_provider_protocol(self) -> None:
        from evalkeep.analyzers.anthropic import AnthropicAnalyzer

        assert isinstance(AnthropicAnalyzer(), AnalyzerProvider)

    def test_a_client_error_becomes_an_analyzer_error(self) -> None:
        from evalkeep.analyzers.anthropic import AnthropicAnalyzer

        class Boom:
            class messages:
                @staticmethod
                def create(**kwargs: Any) -> Any:
                    raise RuntimeError("connection reset")

        trace = NormalizedTrace.model_validate(
            {"trace_id": "t1", "input": {"text": "hi"}, "outcome": {"status": "failure"}}
        )
        with pytest.raises(AnalyzerError, match="connection reset"):
            AnthropicAnalyzer(client=Boom()).analyze_failure(trace, [])

    def test_the_request_is_shaped_for_structured_output(self) -> None:
        from evalkeep.analyzers.anthropic import AnthropicAnalyzer

        captured: dict[str, Any] = {}

        class Recorder:
            class messages:
                @staticmethod
                def create(**kwargs: Any) -> Any:
                    captured.update(kwargs)
                    return _Response([_Block("text", _valid_payload())])

        trace = NormalizedTrace.model_validate(
            {"trace_id": "t1", "input": {"text": "hi"}, "outcome": {"status": "failure"}}
        )
        AnthropicAnalyzer(model="claude-opus-5", client=Recorder()).analyze_failure(trace, [])

        assert captured["model"] == "claude-opus-5"
        assert captured["output_config"]["format"]["type"] == "json_schema"
        assert captured["output_config"]["format"]["schema"] is FAILURE_ANALYSIS_SCHEMA
        assert captured["messages"][0]["role"] == "user"


class TestPromptRendering:
    def test_every_event_kind_is_rendered(self) -> None:
        trace = NormalizedTrace.model_validate(
            {
                "trace_id": "t1",
                "input": {"messages": [{"role": "user", "content": "Refund it."}]},
                "output": {"text": "Done."},
                "events": [
                    {
                        "event_id": "e1",
                        "type": "message",
                        "role": "assistant",
                        "content": "Looking that up.",
                    },
                    {
                        "event_id": "e2",
                        "type": "tool_call",
                        "tool": "refund_order",
                        "call_id": "c1",
                        "arguments": {"order_id": "order-A"},
                    },
                    {
                        "event_id": "e3",
                        "type": "tool_result",
                        "tool": "refund_order",
                        "call_id": "c1",
                        "result": {"status": "refunded"},
                    },
                    {
                        "event_id": "e4",
                        "type": "evaluation",
                        "name": "grader",
                        "passed": False,
                        "reason": "wrong order",
                    },
                ],
                "outcome": {
                    "status": "failure",
                    "feedback": {"rating": "negative", "comment": "wrong one"},
                    "evaluations": [{"name": "checker", "passed": False, "reason": "nope"}],
                },
            }
        )
        prompt = failure_analysis_prompt(trace, [])
        for expected in [
            "user: Refund it.",
            "assistant: Looking that up.",
            'tool_call: refund_order({"order_id": "order-A"})',
            "tool_result: refund_order",
            "evaluation: grader fail",
            "assistant: Done.",
            "feedback: wrong one",
            "failed evaluation: checker: nope",
        ]:
            assert expected in prompt

    def test_a_tool_error_is_shown(self) -> None:
        trace = NormalizedTrace.model_validate(
            {
                "trace_id": "t1",
                "input": {"text": "Refund it."},
                "events": [
                    {
                        "event_id": "e1",
                        "type": "tool_result",
                        "tool": "refund_order",
                        "error": "timeout",
                    }
                ],
                "outcome": {"status": "error"},
            }
        )
        assert "error=timeout" in failure_analysis_prompt(trace, [])

"""Delegating execution to Promptfoo, and reading the results back.

Two things this module is careful about:

* **The runner is invoked as an argument list, never through a shell.** Test
  inputs, tool names and file paths all come from recorded traces. Building a
  command string out of them would make a trace containing ``; rm -rf`` a
  remote-code-execution bug, so ``subprocess.run`` is called with a list and
  ``shell=False``, and nothing is ever passed through a shell.
* **A test that never ran is not a test that failed.** Promptfoo distinguishes
  an assertion failure from a provider error, and so does the import: an error
  is recorded as :class:`~evalsmith.runs.Outcome.ERROR`, never as a failure.
  Letting a crashed provider look like a regression is precisely the wrong
  answer for a tool whose job is deciding whether a release got worse.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from evalsmith.errors import CommandError
from evalsmith.exporters.promptfoo import build_config
from evalsmith.redaction import RedactionSummary, Redactor
from evalsmith.regression import RegressionTest
from evalsmith.runs import CaseResult, ErrorKind, EvaluationRun, Outcome, RunStatus, suite_hash
from evalsmith.targets import Target, referenced_environment

CONFIG_FILENAME = "promptfooconfig.yaml"
RESULTS_FILENAME = "results.json"

#: Promptfoo's own failure taxonomy, which the import preserves rather than
#: flattening: 0 none, 1 assertion, 2 provider/execution error.
_ASSERTION_FAILURE = 1
_EXECUTION_ERROR = 2

_TIMEOUT_MARKERS = ("timeout", "timed out", "etimedout", "esockettimedout")


@dataclass
class RunOutcome:
    run: EvaluationRun
    results: list[CaseResult] = field(default_factory=list)
    #: Anything the runner said that a person should see.
    messages: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[Outcome, int]:
        tally: dict[Outcome, int] = {}
        for result in self.results:
            tally[result.outcome] = tally.get(result.outcome, 0) + 1
        return tally


def write_suite(
    tests: list[RegressionTest],
    target: Target,
    directory: Path,
    *,
    project_root: Path | None = None,
) -> Path:
    """Write a Promptfoo configuration for ``tests`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    config = build_config(tests, target, project_root=project_root, config_dir=directory)
    path = directory / CONFIG_FILENAME
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def execute(
    tests: list[RegressionTest],
    target: Target,
    *,
    directory: Path,
    command: list[str],
    timeout_seconds: int,
    working_directory: Path,
    redactor: Redactor | None = None,
) -> RunOutcome:
    """Run the suite against ``target`` and import what came back."""
    missing = [name for name, present in referenced_environment(target).items() if not present]
    if missing:
        raise CommandError(
            f"Target {target.target_id!r} needs environment variables that are not "
            f"set: {', '.join(sorted(missing))}.",
            hint="Export them, or put them in a .env file that is not committed.",
        )

    config_path = write_suite(tests, target, directory, project_root=working_directory)
    results_path = directory / RESULTS_FILENAME

    run = EvaluationRun(
        run_id=uuid.uuid4().hex,
        target_id=target.target_id,
        suite_hash=suite_hash([test.test_id for test in tests]),
        tests=len(tests),
        environment=_environment(),
        output_dir=str(directory),
    )

    argv = [
        *command,
        "eval",
        "--config",
        str(config_path),
        "--output",
        str(results_path),
        "--no-cache",
    ]
    try:
        # shell=False is the default and is relied upon: every element here can
        # contain text that came out of a recorded trace.
        completed = subprocess.run(
            argv,
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            f"Could not run {command[0]!r}: {exc}.",
            hint="Promptfoo runs on Node. Install Node.js, or set runner.command.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        raise CommandError(
            f"The runner did not finish within {timeout_seconds}s.",
            hint="Raise runner.timeout_seconds, or run fewer tests with --limit.",
        ) from exc

    messages: list[str] = []
    if not results_path.is_file():
        raise CommandError(
            "The runner produced no results file.",
            hint=_tail(completed.stderr or completed.stdout),
        )

    results = import_results(results_path, redactor=redactor or Redactor())
    run.finished_at = datetime.now(UTC)
    run.runner = _runner_version(results_path)
    # A non-zero exit is expected when tests fail; it only matters when nothing
    # came back at all, which the missing-file check above already covers.
    if completed.returncode != 0 and not results:
        run.status = RunStatus.FAILED
        messages.append(_tail(completed.stderr or completed.stdout))

    return RunOutcome(run=run, results=results, messages=messages)


def import_results(path: Path, *, redactor: Redactor | None = None) -> list[CaseResult]:
    """Read a Promptfoo results file into per-test outcomes."""
    redactor = redactor or Redactor()
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Could not read the runner's results at {path}: {exc}") from exc

    records = (payload.get("results") or {}).get("results") or []
    return [_result(record, redactor) for record in records if _test_id(record)]


def _result(record: dict[str, Any], redactor: Redactor) -> CaseResult:
    test_id = _test_id(record)
    failure_reason = record.get("failureReason") or 0
    error = record.get("error") or None

    if record.get("success"):
        outcome, error_kind = Outcome.PASS, None
    elif failure_reason == _EXECUTION_ERROR:
        outcome = Outcome.ERROR
        error_kind = (
            ErrorKind.TIMEOUT
            if any(marker in (error or "").lower() for marker in _TIMEOUT_MARKERS)
            else ErrorKind.EXECUTION_ERROR
        )
    elif failure_reason == _ASSERTION_FAILURE:
        outcome, error_kind = Outcome.FAIL, None
    else:
        # A failure Promptfoo did not classify is not assumed to be the agent's
        # fault; it is reported as an error so it cannot masquerade as one.
        outcome = Outcome.ERROR
        error_kind = ErrorKind.EXECUTION_ERROR

    summary = RedactionSummary()
    return CaseResult(
        test_id=test_id,
        outcome=outcome,
        error_kind=error_kind,
        error=redactor.redact_text(error, summary) if error else None,
        latency_ms=record.get("latencyMs"),
        observation=redactor.redact_text(_observation(record), summary) or None,
        failed_assertions=[
            redactor.redact_text(reason, summary) for reason in _failed_assertions(record)
        ],
    )


def _test_id(record: dict[str, Any]) -> str:
    case = record.get("testCase") or {}
    metadata = case.get("metadata") or {}
    return str(metadata.get("test_id") or case.get("description") or "")


def _observation(record: dict[str, Any]) -> str:
    response = record.get("response") or {}
    output = response.get("output")
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return json.dumps(output, sort_keys=True, default=str)


def _failed_assertions(record: dict[str, Any]) -> list[str]:
    grading = record.get("gradingResult") or {}
    components = grading.get("componentResults") or []
    reasons = [
        str(component.get("reason", "")).strip()
        for component in components
        if not component.get("pass", True)
    ]
    if reasons:
        return reasons
    reason = str(grading.get("reason", "")).strip()
    return [reason] if reason and not grading.get("pass", True) else []


def _runner_version(results_path: Path) -> str | None:
    try:
        payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - already parsed once
        return None
    version = (payload.get("results") or {}).get("version")
    return f"promptfoo:{version}" if version else None


def _environment() -> dict[str, Any]:
    """What the run happened on, so a surprising result can be placed."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])

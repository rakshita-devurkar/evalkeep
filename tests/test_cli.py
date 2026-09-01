"""The CLI surface: version reporting and error/exit-code handling."""

from __future__ import annotations

from typer.testing import CliRunner

from evalsmith import __version__
from evalsmith.cli import app
from evalsmith.errors import ExitCode


def test_version_command(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == ExitCode.OK
    assert __version__ in result.stdout


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.OK
    assert __version__ in result.stdout


def test_no_args_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(app, [])
    assert "init" in result.stdout


def test_unknown_command_is_a_command_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["definitely-not-a-command"])
    assert result.exit_code == ExitCode.COMMAND_ERROR

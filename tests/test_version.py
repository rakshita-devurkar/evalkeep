"""The version a user sees must be the version that was packaged.

`__version__` was once written out by hand beside the one in pyproject.toml, and
the release workflow compared the tag against pyproject only. Nothing compared
the two to each other, so 0.1.1 came within a commit of shipping a binary that
reported 0.1.0.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

import evalkeep
from evalkeep.cli import app
from evalkeep.errors import ExitCode

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def packaged_version() -> str:
    version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert isinstance(version, str)
    return version


class TestVersion:
    def test_it_matches_the_packaged_version(self) -> None:
        assert evalkeep.__version__ == packaged_version()

    def test_it_is_not_written_out_by_hand(self) -> None:
        """A literal here is a second source of truth, and it will drift."""
        source = (Path(evalkeep.__file__)).read_text()
        assert f'"{packaged_version()}"' not in source

    def test_the_cli_reports_it(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == ExitCode.OK
        assert packaged_version() in result.stdout

    def test_the_version_command_agrees_with_the_flag(self, runner: CliRunner) -> None:
        flag = runner.invoke(app, ["--version"]).stdout.strip()
        command = runner.invoke(app, ["version"]).stdout.strip()
        assert flag == command

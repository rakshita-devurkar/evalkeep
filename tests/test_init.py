"""``evalkeep init`` must be safe to run repeatedly and never destroy work."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalkeep.cli import app
from evalkeep.commands.init_cmd import Action, initialize_project
from evalkeep.config import (
    CONFIG_FILENAME,
    GITIGNORE_ENTRIES,
    STATE_SUBDIRS,
    Project,
    ProjectConfig,
)
from evalkeep.errors import CommandError, ExitCode


def test_creates_config_state_dirs_and_gitignore(project_dir: Path) -> None:
    report = initialize_project(project_dir)

    assert (project_dir / CONFIG_FILENAME).is_file()
    state = project_dir / ".evalkeep"
    assert state.is_dir()
    for name in STATE_SUBDIRS:
        assert (state / name).is_dir()

    ignored = set((project_dir / ".gitignore").read_text().splitlines())
    assert set(GITIGNORE_ENTRIES) <= ignored
    assert report.changed


def test_names_project_after_the_directory_by_default(project_dir: Path) -> None:
    report = initialize_project(project_dir)
    assert report.project.config.project_name == "demo-project"


def test_second_run_changes_nothing(project_dir: Path) -> None:
    initialize_project(project_dir)
    config_before = (project_dir / CONFIG_FILENAME).read_text()
    gitignore_before = (project_dir / ".gitignore").read_text()

    report = initialize_project(project_dir)

    assert not report.changed
    assert all(step.action is Action.EXISTS for step in report.steps)
    assert (project_dir / CONFIG_FILENAME).read_text() == config_before
    assert (project_dir / ".gitignore").read_text() == gitignore_before


def test_preserves_a_hand_edited_config(project_dir: Path) -> None:
    initialize_project(project_dir)
    config_path = project_dir / CONFIG_FILENAME
    edited = ProjectConfig(project_name="demo-project")
    edited.redaction.phone_numbers = False
    config_path.write_text(edited.to_yaml())

    report = initialize_project(project_dir)

    assert report.project.config.redaction.phone_numbers is False


def test_force_rewrites_the_config(project_dir: Path) -> None:
    initialize_project(project_dir)
    config_path = project_dir / CONFIG_FILENAME
    config_path.write_text(
        ProjectConfig(project_name="demo-project", state_dir=".elsewhere").to_yaml()
    )

    report = initialize_project(project_dir, force=True)

    assert report.project.config.state_dir == ".evalkeep"
    assert any(step.action is Action.OVERWRITTEN for step in report.steps)


def test_keeps_existing_gitignore_lines_and_adds_only_what_is_missing(
    project_dir: Path,
) -> None:
    gitignore = project_dir / ".gitignore"
    gitignore.write_text("__pycache__/\n.env\n")

    initialize_project(project_dir)

    lines = gitignore.read_text().splitlines()
    assert lines[0] == "__pycache__/"
    assert lines.count(".env") == 1
    assert ".evalkeep/runs/" in lines


def test_does_not_delete_existing_state(project_dir: Path) -> None:
    initialize_project(project_dir)
    kept = project_dir / ".evalkeep" / "data" / "traces.jsonl"
    kept.write_text('{"trace_id": "trace-1"}\n')

    initialize_project(project_dir)

    assert kept.read_text() == '{"trace_id": "trace-1"}\n'


def test_renaming_an_initialized_project_requires_force(project_dir: Path) -> None:
    initialize_project(project_dir)
    with pytest.raises(CommandError):
        initialize_project(project_dir, project_name="something-else")


def test_missing_directory_is_a_command_error(tmp_path: Path) -> None:
    with pytest.raises(CommandError):
        initialize_project(tmp_path / "nope")


def test_file_instead_of_directory_is_a_command_error(tmp_path: Path) -> None:
    target = tmp_path / "a-file"
    target.write_text("")
    with pytest.raises(CommandError):
        initialize_project(target)


def test_unwritable_directory_is_a_command_error(project_dir: Path) -> None:
    project_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(CommandError):
            initialize_project(project_dir)
    finally:
        project_dir.chmod(stat.S_IRWXU)


def test_state_path_blocked_by_a_file_is_a_command_error(project_dir: Path) -> None:
    (project_dir / ".evalkeep").write_text("not a directory")
    with pytest.raises(CommandError):
        initialize_project(project_dir)


class TestProjectLoad:
    def test_round_trips_an_initialized_project(self, project_dir: Path) -> None:
        initialize_project(project_dir, project_name="demo-project")
        project = Project.load(project_dir)
        assert project.config.project_name == "demo-project"
        assert project.database_path == project_dir / ".evalkeep" / "database.db"

    def test_uninitialized_directory_is_a_command_error(self, project_dir: Path) -> None:
        with pytest.raises(CommandError):
            Project.load(project_dir)

    def test_malformed_yaml_is_a_command_error(self, project_dir: Path) -> None:
        (project_dir / CONFIG_FILENAME).write_text("version: [unclosed\n")
        with pytest.raises(CommandError):
            Project.load(project_dir)

    def test_non_mapping_yaml_is_a_command_error(self, project_dir: Path) -> None:
        (project_dir / CONFIG_FILENAME).write_text("- just\n- a list\n")
        with pytest.raises(CommandError):
            Project.load(project_dir)

    def test_a_newer_config_version_is_refused(self, project_dir: Path) -> None:
        (project_dir / CONFIG_FILENAME).write_text("version: 999\n")
        with pytest.raises(CommandError):
            Project.load(project_dir)


class TestInitCommand:
    def test_exits_zero_and_reports_the_root(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["init", str(project_dir)])
        assert result.exit_code == ExitCode.OK
        assert "demo-project" in result.stdout
        assert (project_dir / CONFIG_FILENAME).is_file()

    def test_reports_an_already_initialized_project(
        self, runner: CliRunner, project_dir: Path
    ) -> None:
        runner.invoke(app, ["init", str(project_dir)])
        result = runner.invoke(app, ["init", str(project_dir)])
        assert result.exit_code == ExitCode.OK
        assert "Already initialized" in result.stdout

    def test_missing_directory_exits_two(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init", str(tmp_path / "nope")])
        assert result.exit_code == ExitCode.COMMAND_ERROR

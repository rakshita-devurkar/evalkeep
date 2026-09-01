from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    d = tmp_path / "demo-project"
    d.mkdir()
    return d


@pytest.fixture
def initialized_project(project_dir: Path) -> Path:
    """A project directory that ``evalsmith init`` has already prepared."""
    from evalsmith.commands.init_cmd import initialize_project

    initialize_project(project_dir)
    return project_dir

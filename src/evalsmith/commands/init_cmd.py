"""``evalsmith init`` -- create a safe, idempotent local project structure.

Initialization never destroys existing work. Re-running it fills in whatever is
missing and leaves everything else untouched, so it is safe to run in a
half-configured project or as part of a setup script. ``--force`` is the only
way to rewrite an existing configuration file, and even then nothing under the
state directory is deleted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from evalsmith.config import (
    CONFIG_FILENAME,
    GITIGNORE_ENTRIES,
    GITIGNORE_HEADER,
    STATE_SUBDIRS,
    Project,
    ProjectConfig,
)
from evalsmith.errors import CommandError


class Action(StrEnum):
    """What initialization did to one path."""

    CREATED = "created"
    EXISTS = "exists"
    UPDATED = "updated"
    OVERWRITTEN = "overwritten"


@dataclass(frozen=True)
class Step:
    action: Action
    path: Path
    detail: str = ""


@dataclass
class InitReport:
    project: Project
    steps: list[Step] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(step.action is not Action.EXISTS for step in self.steps)


def initialize_project(
    root: Path,
    *,
    project_name: str | None = None,
    force: bool = False,
) -> InitReport:
    """Create or complete an Evalsmith project rooted at ``root``."""
    root = root.expanduser().resolve()
    _check_writable_directory(root)

    config_path = root / CONFIG_FILENAME
    steps: list[Step] = []

    if config_path.is_file() and not force:
        config = ProjectConfig.from_yaml(
            config_path.read_text(encoding="utf-8"), source=config_path
        )
        if project_name is not None and project_name != config.project_name:
            raise CommandError(
                f"{config_path} already names this project "
                f"{config.project_name!r}, not {project_name!r}.",
                hint="Edit the file directly, or re-run with --force.",
            )
        steps.append(Step(Action.EXISTS, config_path, "left unchanged"))
    else:
        overwriting = config_path.is_file()
        config = ProjectConfig(project_name=project_name or root.name)
        _write_text(config_path, config.to_yaml())
        steps.append(Step(Action.OVERWRITTEN if overwriting else Action.CREATED, config_path))

    project = Project(root, config)
    steps.extend(_ensure_state_dirs(project))
    steps.append(_ensure_gitignore(root))
    return InitReport(project=project, steps=steps)


def _ensure_state_dirs(project: Project) -> list[Step]:
    steps: list[Step] = []
    state_dir = project.state_dir
    steps.append(_ensure_dir(state_dir))
    for name, purpose in STATE_SUBDIRS.items():
        steps.append(_ensure_dir(project.subdir(name), detail=purpose))
    # Git does not track empty directories; a placeholder keeps the layout
    # intact for anyone who clones the repository.
    keep = project.subdir("exports") / ".gitkeep"
    if not keep.exists():
        _write_text(keep, "")
    return steps


def _ensure_dir(path: Path, *, detail: str = "") -> Step:
    if path.is_dir():
        return Step(Action.EXISTS, path, detail)
    if path.exists():
        raise CommandError(f"{path} exists but is not a directory.")
    try:
        path.mkdir(parents=True)
    except OSError as exc:
        raise CommandError(f"Could not create {path}: {exc}") from exc
    return Step(Action.CREATED, path, detail)


def _ensure_gitignore(root: Path) -> Step:
    """Add only the entries that are missing, preserving the existing file."""
    path = root / ".gitignore"
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    present = {line.strip() for line in existing_lines}
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in present]

    if not missing:
        return Step(Action.EXISTS, path, "all entries present")

    block = [GITIGNORE_HEADER, *missing]
    if existing_lines:
        prefix = existing_lines + ([""] if existing_lines[-1].strip() else [])
        action = Action.UPDATED
    else:
        prefix = []
        action = Action.CREATED
    _write_text(path, "\n".join([*prefix, *block]) + "\n")
    return Step(action, path, f"{len(missing)} entr{'y' if len(missing) == 1 else 'ies'} added")


def _check_writable_directory(root: Path) -> None:
    if not root.exists():
        raise CommandError(f"{root} does not exist.")
    if not root.is_dir():
        raise CommandError(f"{root} is not a directory.")
    if not os.access(root, os.W_OK):
        raise CommandError(f"{root} is not writable.")


def _write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"Could not write {path}: {exc}") from exc

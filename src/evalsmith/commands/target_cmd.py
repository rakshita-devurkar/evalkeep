"""``evalsmith targets`` -- configure the agents under test."""

from __future__ import annotations

from pathlib import Path

from evalsmith.config import Project
from evalsmith.errors import CommandError
from evalsmith.targets import (
    Extraction,
    Target,
    TargetKind,
    find_secrets,
    load_targets,
    referenced_environment,
    save_targets,
)

SECRET_HINT = (
    "Reference secrets as ${ENV_VAR}; targets.yaml is committed, so a literal "
    "credential here would be a leak."
)


def add_target(
    target_id: str,
    kind: TargetKind,
    *,
    project_root: Path = Path(),
    description: str | None = None,
    url: str | None = None,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
    path: str | None = None,
    function: str | None = None,
    provider: str | None = None,
    output_path: str | None = None,
    tool_calls_path: str | None = None,
    replace: bool = False,
) -> Target:
    """Record a target, refusing anything that carries a literal credential."""
    project = Project.load(project_root.expanduser().resolve())
    file = load_targets(project.root)

    cleaned = target_id.strip()
    if not cleaned:
        raise CommandError("A target needs a name.")
    if cleaned in file.targets and not replace:
        raise CommandError(
            f"A target named {cleaned!r} already exists.",
            hint="Pass --replace to overwrite it.",
        )

    extract = Extraction()
    if output_path is not None:
        extract.output = output_path
    if tool_calls_path is not None:
        extract.tool_calls = tool_calls_path

    target = Target(
        target_id=cleaned,
        kind=kind,
        description=description,
        url=url,
        method=method,
        headers=headers or {},
        body=body or {},
        extract=extract,
        path=path,
        function=function,
        provider=provider,
    )
    target.validate_shape()

    # The check that makes targets.yaml safe to commit.
    problems = find_secrets(target)
    if problems:
        raise CommandError(
            "This target contains what looks like a credential:\n  - " + "\n  - ".join(problems),
            hint=SECRET_HINT,
        )

    file.targets[cleaned] = target
    save_targets(project.root, file)
    return target


def list_targets(*, project_root: Path = Path()) -> list[Target]:
    project = Project.load(project_root.expanduser().resolve())
    return sorted(load_targets(project.root).targets.values(), key=lambda t: t.target_id)


def show_target(target_id: str, *, project_root: Path = Path()) -> tuple[Target, dict[str, bool]]:
    """A target and the environment variables it depends on."""
    from evalsmith.targets import get_target

    project = Project.load(project_root.expanduser().resolve())
    target = get_target(project.root, target_id)
    return target, referenced_environment(target)


def remove_target(target_id: str, *, project_root: Path = Path()) -> None:
    project = Project.load(project_root.expanduser().resolve())
    file = load_targets(project.root)
    if target_id.strip() not in file.targets:
        raise CommandError(f"No target named {target_id.strip()!r}.")
    del file.targets[target_id.strip()]
    save_targets(project.root, file)

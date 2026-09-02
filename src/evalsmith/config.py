"""Project configuration and the on-disk layout created by ``evalsmith init``.

Everything Evalsmith writes lives under a single state directory
(``.evalsmith/`` by default) so that a project can be inspected, backed up or
deleted in one step. Only ``evalsmith.yaml`` is meant to be committed; the
state directory holds raw traces, the database, caches and run outputs, all of
which stay out of Git (see the generated ``.gitignore`` entries).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from evalsmith.errors import CommandError

CONFIG_FILENAME = "evalsmith.yaml"
STATE_DIRNAME = ".evalsmith"

#: Bumped when the on-disk layout changes in a way that needs a migration.
CONFIG_VERSION = 1

#: Subdirectories of the state directory, with the purpose of each.
STATE_SUBDIRS: dict[str, str] = {
    "data": "Redacted trace payloads and intermediate artifacts.",
    "cache": "Analyzer and embedding caches, keyed by content hash.",
    "runs": "Raw Promptfoo run outputs, one directory per run.",
    "exports": "Generated export files (generic JSONL, Promptfoo YAML).",
}

#: Paths excluded from Git. Approved tests and config are committed; traces,
#: the database, caches and run outputs are not.
GITIGNORE_ENTRIES: tuple[str, ...] = (
    ".env",
    f"{STATE_DIRNAME}/database.db",
    f"{STATE_DIRNAME}/data/",
    f"{STATE_DIRNAME}/cache/",
    f"{STATE_DIRNAME}/runs/",
)

GITIGNORE_HEADER = "# evalsmith"


class RedactionConfig(BaseModel):
    """Which built-in redactors run before anything is written to storage."""

    emails: bool = True
    phone_numbers: bool = True
    payment_cards: bool = True
    token_prefixes: bool = True
    secret_field_names: bool = True


class AnalyzerConfig(BaseModel):
    """Which provider describes failures, if any.

    ``manual`` is the default: Evalsmith runs fully offline and failures are
    labelled by hand until a provider is configured.
    """

    provider: str = "manual"
    model: str = "claude-opus-5"
    effort: str = "medium"
    max_tokens: int = 16000


class ProjectConfig(BaseModel):
    """The contents of ``evalsmith.yaml``."""

    version: int = CONFIG_VERSION
    project_name: str = "evalsmith-project"
    state_dir: str = STATE_DIRNAME
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    analyzer: AnalyzerConfig = Field(default_factory=AnalyzerConfig)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json"), sort_keys=False, default_flow_style=False
        )

    @classmethod
    def from_yaml(cls, text: str, *, source: Path | None = None) -> ProjectConfig:
        where = f" in {source}" if source is not None else ""
        try:
            raw: Any = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise CommandError(f"Could not parse YAML{where}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise CommandError(f"Expected a YAML mapping{where}, got {type(raw).__name__}.")
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise CommandError(f"Invalid configuration{where}:\n{exc}") from exc


class Project:
    """Resolved paths for one Evalsmith project rooted at ``root``."""

    def __init__(self, root: Path, config: ProjectConfig) -> None:
        self.root = root
        self.config = config

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def state_dir(self) -> Path:
        return self.root / self.config.state_dir

    @property
    def database_path(self) -> Path:
        return self.state_dir / "database.db"

    def subdir(self, name: str) -> Path:
        return self.state_dir / name

    @classmethod
    def load(cls, root: Path) -> Project:
        """Load an initialized project, or explain how to create one."""
        config_path = root / CONFIG_FILENAME
        if not config_path.is_file():
            raise CommandError(
                f"No {CONFIG_FILENAME} found in {root}.",
                hint="Run 'evalsmith init' first.",
            )
        config = ProjectConfig.from_yaml(
            config_path.read_text(encoding="utf-8"), source=config_path
        )
        if config.version > CONFIG_VERSION:
            raise CommandError(
                f"{config_path} was written by a newer Evalsmith "
                f"(config version {config.version}, this build understands {CONFIG_VERSION}).",
                hint="Upgrade evalsmith.",
            )
        return cls(root, config)

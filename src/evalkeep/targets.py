"""Agent targets: how to reach the thing under test, without its credentials.

A target says where an agent lives and how to read its answer. It is committed
to Git, so it must never contain a secret -- and that is enforced, not merely
requested: saving a target whose configuration contains something that looks
like a credential is refused, and the same detectors that redact traces do the
looking. Secrets are referenced as ``${ENV_VAR}`` and resolved by the runner
from the environment at run time.

Every provider kind is normalized to one response shape::

    {"text": "...", "toolCalls": [{"tool": "...", "arguments": {...}}, ...]}

so an expectation means the same thing whether it runs against an HTTP endpoint,
a local script or a model provider.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evalkeep.errors import CommandError
from evalkeep.redaction import TOKEN_PATTERNS, is_secret_field

TARGETS_FILENAME = "targets.yaml"
TARGETS_VERSION = 1

BASELINE = "baseline"
CANDIDATE = "candidate"

#: The only way a secret may appear in a committed target.
ENV_REFERENCE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


class TargetKind(StrEnum):
    HTTP = "http"
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    #: A model provider called directly, for testing tool selection rather than
    #: a whole application.
    MODEL = "model"


class Extraction(BaseModel):
    """Where the answer lives in the target's response.

    Expressions are evaluated over the parsed response body, so ``json.reply``
    reads ``{"reply": ...}``. Only HTTP targets need these; a script or model
    provider is adapted by the generated configuration.
    """

    model_config = ConfigDict(extra="forbid")

    output: str = "json.output"
    tool_calls: str | None = "json.toolCalls"


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    kind: TargetKind
    description: str | None = None

    # HTTP
    url: str | None = None
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    extract: Extraction = Field(default_factory=Extraction)

    # Script targets
    path: str | None = None
    function: str | None = None

    # Direct model provider
    provider: str | None = None

    def validate_shape(self) -> None:
        """Check the fields this kind of target actually needs."""
        match self.kind:
            case TargetKind.HTTP:
                if not self.url:
                    raise CommandError("An http target needs a url.")
                if not self.body:
                    raise CommandError(
                        "An http target needs a body.",
                        hint="Use {{input}} where the test input should go.",
                    )
            case TargetKind.PYTHON | TargetKind.JAVASCRIPT:
                if not self.path:
                    raise CommandError(f"A {self.kind.value} target needs a path.")
            case TargetKind.MODEL:
                if not self.provider:
                    raise CommandError(
                        "A model target needs a provider.",
                        hint="For example: anthropic:messages:claude-opus-5",
                    )


class TargetFile(BaseModel):
    """The contents of ``targets.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = TARGETS_VERSION
    targets: dict[str, Target] = Field(default_factory=dict)

    def to_yaml(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        for target in payload["targets"].values():
            target.pop("target_id", None)
        return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)


def find_secrets(target: Target) -> list[str]:
    """Every place this target holds a literal credential.

    Two rules, both borrowed from redaction so the definitions cannot drift: a
    value that *looks* like a known token, and any value under a
    credential-shaped key that is not an ``${ENV_VAR}`` reference.
    """
    problems: list[str] = []
    for path, value in _walk(target.model_dump(mode="json")):
        if not isinstance(value, str) or ENV_REFERENCE.match(value.strip()):
            continue
        key = path.rsplit(".", 1)[-1]
        if any(pattern.search(value) for pattern in TOKEN_PATTERNS):
            problems.append(f"{path} looks like a credential")
        elif is_secret_field(key) and value.strip():
            problems.append(
                f"{path} is a credential field with a literal value; use ${{ENV_VAR}} instead"
            )
    return problems


def referenced_environment(target: Target) -> dict[str, bool]:
    """Which environment variables this target needs, and whether each is set."""
    referenced: dict[str, bool] = {}
    for _, value in _walk(target.model_dump(mode="json")):
        if isinstance(value, str) and (match := ENV_REFERENCE.match(value.strip())):
            name = match.group(1)
            referenced[name] = bool(os.environ.get(name))
    return referenced


def load_targets(root: Path) -> TargetFile:
    path = root / TARGETS_FILENAME
    if not path.is_file():
        return TargetFile()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CommandError(f"Could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CommandError(f"Expected a mapping in {path}.")

    for target_id, entry in (raw.get("targets") or {}).items():
        if isinstance(entry, dict):
            entry.setdefault("target_id", target_id)
    try:
        return TargetFile.model_validate(raw)
    except ValidationError as exc:
        raise CommandError(f"Invalid target configuration in {path}:\n{exc}") from exc


def save_targets(root: Path, targets: TargetFile) -> Path:
    path = root / TARGETS_FILENAME
    try:
        path.write_text(targets.to_yaml(), encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"Could not write {path}: {exc}") from exc
    return path


def get_target(root: Path, target_id: str) -> Target:
    targets = load_targets(root)
    target = targets.targets.get(target_id.strip())
    if target is None:
        known = ", ".join(sorted(targets.targets)) or "none configured"
        raise CommandError(
            f"No target named {target_id.strip()!r}.",
            hint=f"Known targets: {known}. Add one with 'evalkeep targets add'.",
        )
    return target


def _walk(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_walk(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk(child, f"{prefix}{index}."))
    else:
        found.append((prefix.rstrip("."), value))
    return found

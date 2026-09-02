"""Stable exit codes and the error type that carries them.

The codes are part of the CLI contract and are relied on by scripts and CI:

* ``0`` -- the command succeeded.
* ``1`` -- the command ran, but some records were invalid or rejected.
* ``2`` -- the command itself could not run (bad usage, missing file,
  unwritable directory, uninitialized project).
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    RECORD_ERRORS = 1
    COMMAND_ERROR = 2


class EvalkeepError(Exception):
    """An error that maps to a stable exit code and a readable message."""

    exit_code: ExitCode = ExitCode.COMMAND_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class CommandError(EvalkeepError):
    """The command could not run at all."""

    exit_code = ExitCode.COMMAND_ERROR


class RecordError(EvalkeepError):
    """The command ran but some input records were rejected."""

    exit_code = ExitCode.RECORD_ERRORS

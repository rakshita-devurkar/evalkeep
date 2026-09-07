"""Bundled example data, shipped with the package.

These live inside the package rather than beside it so that installing from
PyPI gets them too. A quick start whose every command points at files only a
git clone has is not a quick start.

``evalkeep demo`` copies them out; :func:`root` locates them in place.
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: Files that are not example data and should not be copied out.
_SKIP = {"__init__.py", "__pycache__"}


def root() -> Path:
    """Where the bundled examples live inside the installed package."""
    return Path(__file__).resolve().parent


def available() -> list[str]:
    return sorted(
        entry.name for entry in root().iterdir() if entry.is_dir() and entry.name not in _SKIP
    )


def copy_to(destination: Path) -> list[Path]:
    """Copy every bundled example into ``destination``, returning what was written."""
    written: list[Path] = []
    for name in available():
        target = destination / name
        shutil.copytree(root() / name, target, dirs_exist_ok=True)
        written.extend(sorted(p for p in target.rglob("*") if p.is_file()))
    return written

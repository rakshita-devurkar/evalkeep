"""Evalkeep: turn production agent failures into a small, reviewed regression suite."""

from importlib.metadata import PackageNotFoundError, version as _installed_version

try:
    #: Read from installed metadata rather than written here, so it cannot drift
    #: from pyproject.toml. It did: 0.1.1 was one commit away from shipping a
    #: binary that reported 0.1.0, because the release check compared the tag
    #: against pyproject and nothing compared this.
    __version__ = _installed_version("evalkeep")
except PackageNotFoundError:  # pragma: no cover - only when running uninstalled
    __version__ = "0+unknown"

__all__ = ["__version__", "main"]


def main() -> None:
    """Console-script entry point (delegates to the Typer app)."""
    from evalkeep.cli import main as _main

    _main()

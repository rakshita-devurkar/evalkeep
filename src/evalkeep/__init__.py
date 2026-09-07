"""Evalkeep: turn production agent failures into a small, reviewed regression suite."""

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:
    """Console-script entry point (delegates to the Typer app)."""
    from evalkeep.cli import main as _main

    _main()

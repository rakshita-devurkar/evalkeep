"""Evalsmith: turn real AI-agent failures into reviewed regression tests."""

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:
    """Console-script entry point (delegates to the Typer app)."""
    from evalsmith.cli import main as _main

    _main()

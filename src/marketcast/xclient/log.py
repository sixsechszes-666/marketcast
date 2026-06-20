"""Shared logging setup via ``rich``."""

from __future__ import annotations

import logging
import sys

from rich.console import Console
from rich.logging import RichHandler

# On Windows the console may be cp1251 — force stdout/stderr to utf-8.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

console = Console(force_terminal=True, legacy_windows=False)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging with a single rich handler."""
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                show_path=False,
                rich_tracebacks=True,
                markup=True,
            )
        ],
    )


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)

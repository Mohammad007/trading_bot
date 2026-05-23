"""
Structured logger with rich console output and rotating file logs.

Usage:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("hello")
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler

from config import settings

_console = Console()
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_configured: bool = False


def _configure_root() -> None:
    """Configure root logger exactly once."""
    global _configured
    if _configured:
        return

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Clear stale handlers (e.g., from re-imports under reload).
    for h in list(root.handlers):
        root.removeHandler(h)

    # Pretty console handler
    rich_handler = RichHandler(
        console=_console,
        show_time=True,
        show_level=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(level)
    rich_handler.setFormatter(logging.Formatter("%(message)s", datefmt=_DATE_FORMAT))
    root.addHandler(rich_handler)

    # Rotating file handler
    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(file_handler)

    # Calm noisy libs
    for noisy in ("urllib3", "asyncio", "websockets", "httpx", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name if name else "bot")


def console() -> Console:
    """Shared rich console (used by the dashboard)."""
    return _console


def fatal(msg: str) -> None:
    """Log a fatal error and exit."""
    get_logger("fatal").critical(msg)
    sys.exit(1)

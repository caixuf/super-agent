"""Centralized structured logging infrastructure for super-agent runtime and core.

Features:
1. Safe log initialization with RotatingFileHandler to avoid disk explosion.
2. Context-aware LogRecord enrichment (task_id, session_id, node_id).
3. Zero-crash design: logging failures never abort business execution.
4. Clean separation of stdout/stderr console streaming and file persistence.
"""
from __future__ import annotations

from contextvars import ContextVar
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Any, Optional

_CURRENT_TASK_ID: ContextVar[str] = ContextVar("current_task_id", default="")
_CURRENT_SESSION_ID: ContextVar[str] = ContextVar("current_session_id", default="")
_CURRENT_NODE_ID: ContextVar[str] = ContextVar("current_node_id", default="")

DEFAULT_LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] [%(name)s] "
    "[task=%(task_id)s session=%(session_id)s node=%(node_id)s] %(message)s"
)
DEFAULT_CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ContextFilter(logging.Filter):
    """Inject contextual metadata from ContextVars into LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "task_id"):
            record.task_id = _CURRENT_TASK_ID.get() or "-"
        if not hasattr(record, "session_id"):
            record.session_id = _CURRENT_SESSION_ID.get() or "-"
        if not hasattr(record, "node_id"):
            record.node_id = _CURRENT_NODE_ID.get() or "-"
        return True


def set_log_context(*, task_id: Optional[str] = None, session_id: Optional[str] = None, node_id: Optional[str] = None) -> None:
    """Set the contextual IDs for the current task/async context."""
    if task_id is not None:
        _CURRENT_TASK_ID.set(str(task_id))
    if session_id is not None:
        _CURRENT_SESSION_ID.set(str(session_id))
    if node_id is not None:
        _CURRENT_NODE_ID.set(str(node_id))


def get_logger(name: str) -> logging.Logger:
    """Return a logger with ContextFilter attached."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, ContextFilter) for f in logger.filters):
        logger.addFilter(ContextFilter())
    return logger


def configure_logging(
    log_dir: str | Path | None = None,
    *,
    level: str | int = "INFO",
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 5,
    enable_console: bool = True,
) -> None:
    """Configure root logger with file rotation and context filtering."""
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers if called multiple times
    for handler in list(root.handlers):
        if getattr(handler, "_superagent_configured", False):
            root.removeHandler(handler)

    ctx_filter = ContextFilter()

    if enable_console:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(logging.Formatter(DEFAULT_CONSOLE_FORMAT, datefmt=DEFAULT_DATE_FORMAT))
        console.addFilter(ctx_filter)
        setattr(console, "_superagent_configured", True)
        root.addHandler(console)

    if log_dir is not None:
        path = Path(log_dir)
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            file_path = path / "superagent.log"
            file_handler = RotatingFileHandler(
                str(file_path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=DEFAULT_DATE_FORMAT))
            file_handler.addFilter(ctx_filter)
            setattr(file_handler, "_superagent_configured", True)
            root.addHandler(file_handler)

            err_file_path = path / "superagent.error.log"
            err_handler = RotatingFileHandler(
                str(err_file_path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            err_handler.setLevel(logging.ERROR)
            err_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=DEFAULT_DATE_FORMAT))
            err_handler.addFilter(ctx_filter)
            setattr(err_handler, "_superagent_configured", True)
            root.addHandler(err_handler)
        except OSError:
            # File logger failure must never crash the runtime
            pass

import logging
from pathlib import Path
import tempfile
import pytest

from agentcore.structured_logging import (
    configure_logging,
    get_logger,
    set_log_context,
    ContextFilter,
)


def test_context_filter_injects_metadata():
    filter_ = ContextFilter()
    record = logging.LogRecord("test", logging.INFO, "test.py", 10, "hello", (), None)
    filter_.filter(record)
    assert record.task_id == "-"
    assert record.session_id == "-"
    assert record.node_id == "-"

    set_log_context(task_id="task-123", session_id="sess-456", node_id="node-789")
    record2 = logging.LogRecord("test", logging.INFO, "test.py", 11, "world", (), None)
    filter_.filter(record2)
    assert record2.task_id == "task-123"
    assert record2.session_id == "sess-456"
    assert record2.node_id == "node-789"


def test_configure_logging_creates_files(tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging(log_dir, level="DEBUG", enable_console=False)

    logger = get_logger("test_comp")
    logger.info("info message")
    logger.error("error message")

    main_log = log_dir / "superagent.log"
    err_log = log_dir / "superagent.error.log"

    assert main_log.is_file()
    assert err_log.is_file()

    main_text = main_log.read_text(encoding="utf-8")
    err_text = err_log.read_text(encoding="utf-8")

    assert "info message" in main_text
    assert "error message" in main_text
    assert "error message" in err_text
    assert "info message" not in err_text

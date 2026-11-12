import subprocess
from unittest.mock import MagicMock
import pytest

from install.worker.dev_exec_runner import (
    SANITISED_EMPTY_RESPONSES,
    run_hermes,
)


@pytest.fixture(autouse=True)
def mock_hermes_environment(monkeypatch):
    monkeypatch.setattr("install.worker.dev_exec_runner._hermes_bin", lambda: "/bin/true")


def test_sanitised_empty_responses_set():
    assert "(empty)" in SANITISED_EMPTY_RESPONSES
    assert "[System: Empty message content sanitised to satisfy protocol]" in SANITISED_EMPTY_RESPONSES


def test_run_hermes_sanitised_empty_response_fails(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HERMES_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_MODEL", "mock-model")

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "[System: Empty message content sanitised to satisfy protocol]\n"
    mock_run.stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_run)
    monkeypatch.setattr("install.worker.dev_exec_runner._find_and_load_checkpoint", lambda: None)

    res = run_hermes("test goal")
    assert res["ok"] is False
    assert res.get("error") == "empty_model_response"
    assert res["returncode"] == 0


def test_run_hermes_whitespace_only_response_fails(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HERMES_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_MODEL", "mock-model")

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "   \n  \t "
    mock_run.stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_run)
    monkeypatch.setattr("install.worker.dev_exec_runner._find_and_load_checkpoint", lambda: None)

    res = run_hermes("test goal")
    assert res["ok"] is False
    assert res.get("error") == "empty_model_response"


def test_run_hermes_valid_response_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HERMES_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_MODEL", "mock-model")

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "Job completed successfully with verified results."
    mock_run.stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_run)
    monkeypatch.setattr("install.worker.dev_exec_runner._find_and_load_checkpoint", lambda: None)

    res = run_hermes("test goal")
    assert res["ok"] is True
    assert res.get("error") is None
    assert "Job completed successfully" in res["summary"]


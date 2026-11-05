"""Receipt contract for the two-engine execution runner (install/worker)."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "install" / "worker" / "dev_exec_runner.py"


def _module():
    spec = importlib.util.spec_from_file_location("dev_exec_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, capsys, payload):
    module = _module()
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert module.main() in (0, 1)
    return json.loads(capsys.readouterr().out)


def test_primary_success_never_calls_fallback(monkeypatch, capsys):
    module = _module()
    calls = []
    monkeypatch.setattr(module, "run_hermes", lambda goal: {"ok": True, "output": "done"})
    monkeypatch.setattr(module, "run_pi", lambda goal: calls.append(goal) or {"ok": True})
    monkeypatch.setattr(module.sys, "stdin", io.StringIO('{"payload": {"goal": "inspect"}}'))
    assert module.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["engine"] == "hermes"
    assert receipt["result"]["output"] == "done"
    assert calls == []


def test_primary_failure_falls_back_to_pi(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(module, "run_hermes", lambda goal: {"ok": False, "error": "turn_failed"})
    monkeypatch.setattr(module, "run_pi", lambda goal: {"ok": True, "output": "from pi"})
    monkeypatch.setattr(module.sys, "stdin", io.StringIO('{"payload": {"goal": "build"}}'))
    assert module.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True
    assert receipt["engine"] == "pi"
    assert receipt["primary_error"] == "turn_failed"


def test_both_engines_failing_is_reported_not_raised(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(module, "run_hermes", lambda goal: {"ok": False, "error": "runner_timeout"})
    monkeypatch.setattr(module, "run_pi", lambda goal: {"ok": False, "error": "pi_run_failed"})
    monkeypatch.setattr(module.sys, "stdin", io.StringIO('{"payload": {"goal": "x"}}'))
    assert module.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is False
    assert receipt["engine"] == "none"
    assert receipt["error"] == "both_engines_failed"
    assert receipt["primary_error"] == "runner_timeout"


@pytest.mark.parametrize("payload", [
    '{"payload": {}}',
    '{"payload": {"goal": "   "}}',
    '{"payload": {"goal": 7}}',
    'not json',
])
def test_invalid_jobs_are_rejected(monkeypatch, capsys, payload):
    module = _module()
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(payload))
    assert module.main() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is False
    assert receipt["error"].startswith("invalid_job")


def test_oversized_goal_is_rejected(monkeypatch, capsys):
    module = _module()
    big = "x" * (module.MAX_GOAL + 1)
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps({"payload": {"goal": big}})))
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["error"].startswith("invalid_job")


def test_hermes_timeout_is_a_soft_failure(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_hermes_bin", lambda: "/bin/true")

    def boom(*args, **kwargs):
        raise module.subprocess.TimeoutExpired(cmd="hermes", timeout=1)

    monkeypatch.setattr(module.subprocess, "run", boom)
    assert module.run_hermes("goal") == {"ok": False, "error": "runner_timeout"}


def test_hermes_empty_output_is_not_success(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_hermes_bin", lambda: "/bin/true")
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "  ", "stderr": ""})())
    result = module.run_hermes("goal")
    assert result["ok"] is False and result["error"] == "turn_failed"


def test_missing_hermes_binary_marks_engine_unavailable(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_hermes_bin", lambda: None)
    assert module.run_hermes("goal") == {"ok": False, "error": "hermes_unavailable"}


def test_default_workdir_is_an_existing_directory(monkeypatch):
    module = _module()
    monkeypatch.delenv("DEV_WORKDIR", raising=False)
    assert module._workdir().is_dir()

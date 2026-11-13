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
    assert result["ok"] is False and result["error"] == "empty_model_response"


def test_missing_hermes_binary_marks_engine_unavailable(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_hermes_bin", lambda: None)
    assert module.run_hermes("goal") == {"ok": False, "error": "hermes_unavailable"}


def test_default_workdir_is_an_existing_directory(monkeypatch):
    module = _module()
    monkeypatch.delenv("DEV_WORKDIR", raising=False)
    assert module._workdir().is_dir()


def test_backend_environment_is_unattended(monkeypatch):
    """Durable jobs run without a human, so approvals must never block one."""
    module = _module()
    monkeypatch.setenv("HERMES_HOME", "/tmp/h")
    env = module._clean_env()
    assert env["HERMES_YOLO_MODE"] == "1"
    assert env["HERMES_SESSION_PLATFORM"] == "api_server"


def test_clean_hermes_output_strips_cli_noise_and_resume_footer():
    module = _module()
    raw = (
        "查询： 真实开发任务\n"
        "────────────────────────────────────────\n"
        "∴ 思考了 1 秒 · Internal monologue\n"
        "────────────────────────────────────────\n"
        "☤ Hermes\n"
        "任务已执行完毕。\n"
        "1. 创建了 fib.py\n"
        "2. 3 passed in 0.01s\n"
        "────────────────────────────────────────\n\n"
        "恢复此会话：\n"
        "  hermes --resume local-123456\n"
    )
    assert module._clean_hermes_output(raw) == "任务已执行完毕。\n1. 创建了 fib.py\n2. 3 passed in 0.01s"


def test_decorate_goal_injects_expert_duty_and_target_host(monkeypatch):
    module = _module()
    monkeypatch.setenv("DEV_EXPERT_DUTY", "Run hardware bench SIT suite")
    monkeypatch.setenv("DEV_TARGET_HOST", "bench-host-win")
    decorated = module._decorate_goal("Run smoke test")
    assert "[Expert Context]" in decorated
    assert "Output Format (STRICT)" in decorated
    assert "Duty: Run hardware bench SIT suite" in decorated
    assert "Target Host: bench-host-win" in decorated
    assert decorated.endswith("Run smoke test")


def test_remote_ssh_host_executes_hermes_over_ssh(monkeypatch):
    module = _module()
    monkeypatch.setenv("DEV_SSH_HOST", "worker-node-01")
    monkeypatch.setenv("DEV_REMOTE_PROFILE_HOME", "/srv/hermes/profiles/domain")
    monkeypatch.setenv("DEV_REMOTE_WORKDIR", "/srv/code/domain")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return type("R", (), {"returncode": 0, "stdout": "☤ Hermes\nRemote execution ok", "stderr": ""})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    res = module.run_hermes("check remote build")
    assert res["ok"] is True
    assert res["summary"] == "Remote execution ok"
    assert captured["cmd"][:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "worker-node-01"]
    assert "HERMES_HOME=/srv/hermes/profiles/domain" in captured["cmd"][6]
    assert "--query-file -" in captured["cmd"][6]
    assert captured["kwargs"]["input"] == "check remote build"


def test_clean_env_routes_to_colleague_profile_when_present(monkeypatch, tmp_path):
    module = _module()
    colleague_dir = tmp_path / "profiles" / "architect"
    colleague_dir.mkdir(parents=True)
    monkeypatch.setattr(module, "_profile_home", lambda: tmp_path)

    env = module._clean_env(route={"role": "architect"})
    assert env["HERMES_HOME"] == str(colleague_dir)
    assert env["HERMES_SESSION_CHAT_ID"].startswith("colleague-architect-")


def test_remote_ssh_host_routes_to_colleague_profile(monkeypatch):
    module = _module()
    monkeypatch.setenv("DEV_SSH_HOST", "worker-node-01")
    monkeypatch.setenv("DEV_REMOTE_PROFILE_HOME", "/srv/hermes")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "☤ Hermes\nRemote execution ok", "stderr": ""})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    res = module.run_hermes("review code", route={"role": "reviewer"})
    assert res["ok"] is True
    assert "HERMES_HOME=/srv/hermes/profiles/reviewer" in captured["cmd"][6]
    assert "HERMES_SESSION_CHAT_ID=colleague-reviewer-" in captured["cmd"][6]


def test_hermes_bin_discovers_single_root_super_agent_release(monkeypatch, tmp_path):
    module = _module()
    monkeypatch.delenv("DEV_HERMES_BIN", raising=False)
    profile_home = tmp_path / ".hermes" / "profiles" / "caixuf"
    profile_home.mkdir(parents=True)
    managed_bin = tmp_path / "super_agent" / "caixuf" / "hermes-current" / ".hermes" / "bin" / "hermes"
    managed_bin.parent.mkdir(parents=True)
    managed_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(module, "_profile_home", lambda: profile_home)
    assert module._hermes_bin() == str(managed_bin)


def test_progress_monitor_streams_steps_and_writes_progress_json(tmp_path):
    import time
    from agentcore.liveness import LivenessLedger
    module = _module()
    attempt_dir = tmp_path / "attempts" / "att1"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "agent.log"
    log_file.write_text("initial logs\n", encoding="utf-8")

    liveness_db = tmp_path / "liveness.db"
    ledger = LivenessLedger(liveness_db)
    ledger.register("task-test-pm", kind="dispatch", state="queued")

    monitor = module.ProgressMonitor(
        job_id="task-test-pm",
        attempt_dir=attempt_dir,
        log_file=log_file,
        liveness_path=liveness_db,
    )
    monitor.start()
    try:
        # Check initial progress.json
        prog_file = attempt_dir / "progress.json"
        assert prog_file.is_file()
        pdata = json.loads(prog_file.read_text(encoding="utf-8"))
        assert pdata["status"] == "running"
        assert pdata["progress"] == 0.05

        # Append simulated tool completion to agent.log
        with log_file.open("a", encoding="utf-8") as f:
            f.write("2026-11-08 12:00:00,000 INFO agent.tool_executor: tool read_file completed (0.1s)\n")
            f.flush()

        # Allow background monitor thread to read
        time.sleep(3.0)

        # Inspect liveness.db
        live = ledger.inspect("task-test-pm")
        assert live["ok"] is True
        assert "read_file" in live["phase"]
        assert live["progress"]["percent"] > 5
    finally:
        monitor.stop(ok=True)

    # After stop, progress is 1.0 and completed
    pdata_final = json.loads(prog_file.read_text(encoding="utf-8"))
    assert pdata_final["status"] == "completed"
    assert pdata_final["progress"] == 1.0


def test_harvest_attempt_artifacts_stages_report_files_into_attempt_dir(monkeypatch, tmp_path):
    module = _module()
    fake_home = tmp_path / "home" / "worker"
    reports_dir = fake_home / "reports" / "2026-11-09_arch"
    reports_dir.mkdir(parents=True, exist_ok=True)
    html_file = reports_dir / "agent_arch.html"
    html_file.write_text("<html><body>arch</body></html>", encoding="utf-8")
    png_file = reports_dir / "agent_arch.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n")
    page2_png = reports_dir / "agent_arch.page-02.png"
    page2_png.write_bytes(b"\x89PNG\r\n\x1a\n")

    attempt_dir = tmp_path / "attempts" / ("a" * 32)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "progress.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(attempt_dir))

    summary = (
        f"产出文件：\n"
        f"- HTML：`{html_file}`\n"
        f"- PNG：`{png_file}`、`{page2_png}`\n"
    )
    harvested = module._harvest_attempt_artifacts(summary)
    assert harvested == [
        str(attempt_dir.resolve() / "agent_arch.html"),
        str(attempt_dir.resolve() / "agent_arch.png"),
    ]
    assert (attempt_dir / "agent_arch.html").read_text(encoding="utf-8") == "<html><body>arch</body></html>"
    assert (attempt_dir / "agent_arch.png").read_bytes() == b"\x89PNG\r\n\x1a\n"


def test_harvest_attempt_artifacts_includes_markdown_and_auto_materializes_long_text(monkeypatch, tmp_path):
    module = _module()
    attempt_dir = tmp_path / "attempts" / ("b" * 32)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(attempt_dir))

    # 1. Existing .md file directly in attempt_dir is harvested
    md_file = attempt_dir / "audit_report.md"
    md_file.write_text("# Audit\nDetailed text", encoding="utf-8")
    harvested = module._harvest_attempt_artifacts("Short summary")
    assert str(md_file.resolve()) in harvested

    # 2. Long summary auto-materializes if no markdown artifact exists
    attempt_dir_2 = tmp_path / "attempts" / ("c" * 32)
    attempt_dir_2.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(attempt_dir_2))
    long_summary = "A" * 2000
    harvested_2 = module._harvest_attempt_artifacts(long_summary)
    assert len(harvested_2) == 1
    assert Path(harvested_2[0]).name == "任务执行报告.md"
    assert Path(harvested_2[0]).read_text(encoding="utf-8") == long_summary


def test_wrap_with_bwrap_sandbox_binds_meegle_and_markdown_outbox_rw(monkeypatch, tmp_path):
    module = _module()
    from agentcore.colleague_containment import AttemptBoundSandboxAdapter

    if not AttemptBoundSandboxAdapter.is_bwrap_functional():
        import pytest
        pytest.skip("bwrap not functional in this environment")

    fake_home = tmp_path / "home"
    meegle_dir = fake_home / ".meegle"
    meegle_dir.mkdir(parents=True)
    state_root = fake_home / "super_agent" / "demo" / "state"
    outbox_dir = state_root / "markdown-outbox"
    outbox_dir.mkdir(parents=True)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    attempt_dir = state_root / "colleague-queues" / "triage" / "attempts" / ("d" * 32)
    attempt_dir.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("SUPERAGENT_ENABLE_BWRAP", "1")
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(attempt_dir))
    monkeypatch.setattr(module, "_workdir", lambda: workdir)

    wrapped, meta = module._wrap_with_bwrap_sandbox(["echo", "ok"], route={"role": "triage"})
    assert meta is not None and meta["backend"] == "bwrap"
    binds = set(meta["audit_log"][-1]["writable_binds"])
    assert str(meegle_dir.resolve()) in binds
    assert str(outbox_dir.resolve()) in binds
    assert "--bind" in wrapped


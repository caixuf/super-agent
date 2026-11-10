"""End-to-end reliability seam regression tests across all 5 architectural laws.

Covers:
1. Law 1 & 2 (P0): bwrap sandbox Overlay Read-Only Armor — Hermes runtime home
   SQLite WAL & PM lease/lock writes succeed inside bwrap while config.yaml,
   SOUL.md, .env, auth.json, plugins, skills, and binding.json stay read-only;
   single-root super_agent attempt_dir and state subdirs work; primary_stderr is
   exposed on fallback; unknown toolset banner lines are stripped cleanly.
2. Law 3 (P3): Terminal card_patch_unknown bounded 3-strike reconciliation &
   retirement, plus progress_unknown retirement on terminal jobs.
3. Law 4 (P2): SIGTERM (143/-15) translates to EX_TEMPFAIL (75) without spawning
   fallback Engine 2 (pi), preserves cross-attempt StateGraph SqliteCheckpointer
   so the next attempt resumes from prepare_context, and superagent.py pre-stop
   drain waits boundedly only when running jobs exist.
4. Law 5 (P1): Explicit payload _model_tier overrides static DEV_MODEL_TIER=flash;
   durable_status({}) lists owned/route tasks without task_id; recursive
   meta-status durable_submit inquiries are rejected cleanly.
5. Law 2 (P4): superagent.py _worker_queue_health_observation detects stuck
   card_outbox_unknown or post-install primary engine degradation without
   leaking task prompts or outputs.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "dev_exec_runner_seams", REPO / "install" / "worker" / "dev_exec_runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "superagent_installer_seams", REPO / "tools" / "superagent.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _load_dispatch_plugin():
    spec = importlib.util.spec_from_file_location(
        "durable_dispatch_seams", REPO / "plugins" / "durable_dispatch" / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _load_transport():
    spec = importlib.util.spec_from_file_location(
        "hermes_transport_seams", REPO / "runtime" / "hermes_completion_transport.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_bwrap_overlay_ro_armor_allows_hermes_wal_and_leases_while_protecting_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agentcore.colleague_containment import AttemptBoundSandboxAdapter, is_forbidden_host_path

    if not AttemptBoundSandboxAdapter.is_bwrap_functional():
        pytest.skip("bwrap user namespaces unavailable in this kernel/container")

    fake_home = tmp_path / "fake_home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    # Ensure is_forbidden_host_path was NOT weakened!
    assert is_forbidden_host_path(fake_home / ".hermes" / "auth.json")[0] is True
    assert is_forbidden_host_path(fake_home / "super_agent" / "alpha" / "install-manifest.json")[0] is True

    workdir = fake_home / "workspace"
    workdir.mkdir(parents=True)

    # Simulate single-root super_agent layout + .hermes runtime profile & PM installs
    attempt_dir = fake_home / "super_agent" / "alpha" / "state" / "dev-queue" / "attempts" / ("a" * 32)
    attempt_dir.mkdir(parents=True)
    spool_dir = fake_home / "super_agent" / "alpha" / "state" / "memory-spool"
    spool_dir.mkdir(parents=True)

    runtime_home = fake_home / ".hermes" / "profiles" / "alpha"
    (runtime_home / "sessions").mkdir(parents=True)
    (runtime_home / "plugins").mkdir(parents=True)
    (runtime_home / "config.yaml").write_text("model:\n  default_model: main\n", encoding="utf-8")
    (runtime_home / "SOUL.md").write_text("Immutable Soul\n", encoding="utf-8")
    (runtime_home / ".env").write_text("API_KEY=present\n", encoding="utf-8")
    (runtime_home / "auth.json").write_text('{"ok": true}\n', encoding="utf-8")

    inst_dir = fake_home / ".hermes" / "installs" / "rel1"
    leases_dir = inst_dir / "environments" / "env1" / ".leases"
    leases_dir.mkdir(parents=True)
    install_lock = inst_dir / ".install.lock"
    install_lock.touch()

    adapter = AttemptBoundSandboxAdapter.create_host_bound(
        job="job_armor_1",
        attempt="a" * 32,
        allowed_workdir=workdir,
        role="cases",
        network_policy="allowed",
        attempt_dir=attempt_dir,
    )

    probe_script = workdir / "probe_armor.py"
    probe_script.write_text(
        "import json, pathlib, sqlite3, sys\n"
        f"hr = pathlib.Path({str(runtime_home)!r})\n"
        f"att = pathlib.Path({str(attempt_dir)!r})\n"
        f"leases = pathlib.Path({str(leases_dir)!r})\n"
        f"lockf = pathlib.Path({str(install_lock)!r})\n"
        f"spool = pathlib.Path({str(spool_dir)!r})\n"
        "# 1. SQLite WAL inside runtime_home must succeed\n"
        "with sqlite3.connect(hr / 'state.db') as conn:\n"
        "    conn.execute('PRAGMA journal_mode=WAL')\n"
        "    conn.execute('CREATE TABLE t (x INT)')\n"
        "    conn.execute('INSERT INTO t VALUES (1)')\n"
        "# 2. PM lock & lease creation + memory-spool + attempt_dir deliverable write must succeed\n"
        "lockf.open('a').close()\n"
        "(leases / 'lease-001').write_text('active')\n"
        "(spool / 'entry.json').write_text('{}')\n"
        "(att / 'report.md').write_text('# Deliverable')\n"
        "# 3. Protected files + binding.json must remain readable yet strictly read-only\n"
        "assert 'default_model' in (hr / 'config.yaml').read_text()\n"
        "blocked = {}\n"
        "for label, target in [\n"
        "    ('config.yaml', hr / 'config.yaml'),\n"
        "    ('SOUL.md', hr / 'SOUL.md'),\n"
        "    ('.env', hr / '.env'),\n"
        "    ('auth.json', hr / 'auth.json'),\n"
        "    ('plugins', hr / 'plugins' / 'evil.py'),\n"
        "    ('binding.json', att / 'binding.json'),\n"
        "]:\n"
        "    try:\n"
        "        target.write_text('tampered')\n"
        "        blocked[label] = False\n"
        "    except OSError:\n"
        "        blocked[label] = True\n"
        "print(json.dumps({'ok': True, 'blocked': blocked}))\n",
        encoding="utf-8",
    )

    bwrap_cmd = adapter.build_worker_bwrap_argv(
        [sys.executable, str(probe_script)],
        workdir,
        attempt_dir=attempt_dir,
        hermes_runtime_home=runtime_home,
        hermes_pm_writable_paths=[install_lock, leases_dir],
        managed_state_paths=[spool_dir],
    )
    proc = subprocess.run(bwrap_cmd, capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["ok"] is True
    assert all(out["blocked"].values()), out["blocked"]


def test_clean_hermes_output_strips_unknown_toolset_warning_and_exposes_primary_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    runner = _load_runner()
    raw = "警告：未知的工具集：colleague_team\n☤ Hermes\n警告：未知的工具集：foo\n美国东部时间 11:45\n"
    assert runner._clean_hermes_output(raw) == "美国东部时间 11:45"

    monkeypatch.setattr(
        runner,
        "run_hermes",
        lambda goal, route=None: {
            "ok": False,
            "error": "turn_failed",
            "returncode": 1,
            "stderr": "OSError: [Errno 30] Read-only file system",
        },
    )
    monkeypatch.setattr(
        runner,
        "run_pi",
        lambda goal, route=None, on_event=None: {"ok": True, "summary": "fallback answer", "output": "fallback answer"},
    )
    monkeypatch.setattr(runner.sys, "stdin", io.StringIO('{"payload": {"goal": "check time"}}'))
    assert runner.main() == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["engine"] == "pi"
    assert envelope["engine_degraded"] is True
    assert "Read-only file system" in envelope["primary_stderr"]
    assert envelope["primary_returncode"] == 1


def test_sigterm_exits_tempfail_without_spawning_pi_and_resumes_stategraph_across_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    runner = _load_runner()
    pool_root = tmp_path / "dev-queue"
    att1 = pool_root / "attempts" / ("1" * 32)
    att2 = pool_root / "attempts" / ("2" * 32)
    att1.mkdir(parents=True)
    att2.mkdir(parents=True)

    pi_calls: list[str] = []
    monkeypatch.setattr(runner, "_workdir", lambda: tmp_path)
    monkeypatch.setattr(
        runner,
        "run_pi",
        lambda goal, route=None, on_event=None: pi_calls.append(goal) or {"ok": True, "output": "should not run"},
    )

    # Attempt 1: Hermes is interrupted by SIGTERM (returncode -15)
    monkeypatch.setenv("SUPERAGENT_JOB_ID", "job_sigterm_resume_01")
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(att1))
    monkeypatch.setattr(
        runner,
        "run_hermes",
        lambda goal, route=None: {
            "ok": False,
            "interrupted": True,
            "error": "interrupted_by_shutdown",
            "returncode": -15,
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        runner.sys,
        "stdin",
        io.StringIO('{"job": "job_sigterm_resume_01", "attempt": "1111", "payload": {"goal": "run long task"}}'),
    )
    rc1 = runner.main()
    out1 = json.loads(capsys.readouterr().out)
    assert rc1 == 75  # EX_TEMPFAIL: Pool.finish requeues without burning tries!
    assert out1["interrupted"] is True
    assert pi_calls == []  # Engine 2 (pi) MUST NEVER spawn during shutdown!
    assert (att1 / "state_checkpoints.db").is_file()

    # Attempt 2: Worker restarts and runs the requeued job under a brand-new attempt UUID dir
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(att2))
    monkeypatch.setattr(
        runner,
        "run_hermes",
        lambda goal, route=None: {"ok": True, "summary": "completed cleanly", "output": "completed cleanly"},
    )
    monkeypatch.setattr(
        runner.sys,
        "stdin",
        io.StringIO('{"job": "job_sigterm_resume_01", "attempt": "2222", "payload": {"goal": "run long task"}}'),
    )
    rc2 = runner.main()
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert out2["ok"] is True
    assert out2["engine"] == "hermes"
    assert out2["resumed_from_node"] == "prepare_context"
    assert (att2 / "state_checkpoints.db").is_file()


def test_explicit_payload_model_tier_overrides_worker_env_default(monkeypatch: pytest.MonkeyPatch):
    runner = _load_runner()
    monkeypatch.setenv("DEV_MODEL_TIER", "flash")
    monkeypatch.setenv("DEV_MODEL_ROLE", "dev")

    # Explicit task-level _model_tier='pro' + _colleague_role='architect' overrides worker unit defaults
    route = runner._resolve_job_model_route({"_colleague_role": "architect", "_model_tier": "pro"})
    assert route["role"] == "architect"
    assert route["selected_model"] == "pro"

    # Without explicit task tier, worker unit default DEV_MODEL_TIER='flash' applies
    default_route = runner._resolve_job_model_route({})
    assert default_route["selected_model"] == "flash"


def test_terminal_unknown_card_converges_to_degraded_after_3_strikes_and_progress_retires(tmp_path: Path):
    from agentcore.task_card_bridge import TaskCardBridge

    plugin = _load_dispatch_plugin()
    transport_mod = _load_transport()

    origin = {"profile": "p", "route": "feishu:chat-1", "channel": "feishu", "chat": "oc_1"}
    service = plugin.DispatchService(
        str(tmp_path / "queue"),
        "p",
        [str(tmp_path)],
        role="dev",
        route_provider=lambda _: origin,
        task_cards_enabled=True,
    )

    patch_attempts = 0

    def failing_patch(**kwargs):
        nonlocal patch_attempts
        patch_attempts += 1
        return {"ok": False, "error": "feishu_502"}

    ctx = SimpleNamespace(
        get_config=lambda k, default=None: (
            True
            if k in ("direct_delivery_enabled", "completion_card_enabled")
            else (str(REPO) if k == "engine_root" else (str(tmp_path / "cards") if k == "cards_root" else default))
        ),
        plugin_id="durable-dispatch",
        profile_name="p",
    )
    transport = transport_mod.create_transport(ctx, service)
    transport.card_bridge = TaskCardBridge(tmp_path / "cards", profile="p", sender=failing_patch)
    transport.card_creator = lambda **kw: {"ok": True, "message_id": "om_card_1"}
    transport.sender = lambda *args, **kwargs: {"ok": True, "message_id": "om_txt_1"}

    job = service.submit("sess-1", {"goal": "verify convergence", "request_key": "rk-1"})["task_id"]
    assert service.drain_cards(transport)[0]["state"] == "sent"

    # Simulate progress_unknown while running
    with service.pool.db() as db:
        db.execute("UPDATE card_outbox SET progress_state='unknown' WHERE job=?", (job,))

    claim, fd = service.pool.claim()
    try:
        assert service.pool.finish(job, claim["attempt"], True, {"summary": "done"})
    finally:
        os.close(fd)

    # Strike 1 during completion delivery: fallback text succeeds (acknowledged=1), card is terminal_unknown
    assert service.drain_delivery(transport)["delivered"] == 1
    assert service.inspect_card_health(bridge=transport.card_bridge)["terminal_unknown"] == 1
    assert patch_attempts == 1

    # Sync running cards retires progress_unknown -> terminal on completed job
    service.sync_running_cards(transport)
    assert service.inspect_card_health(bridge=transport.card_bridge)["progress_unknown"] == 0

    # Strikes 2 & 3 via reconcile_terminal_cards (aged past min_age) -> degrades & clears all warnings!
    for _ in range(2):
        with service.pool.db() as db:
            db.execute("UPDATE card_outbox SET updated=? WHERE job=?", (time.time() - 60, job))
        service.reconcile_terminal_cards(transport, min_age=30.0)

    assert patch_attempts == 3
    health = service.inspect_card_health(bridge=transport.card_bridge)
    assert health["terminal_unknown"] == 0
    assert health["bridge_terminal_unknown"] == 0
    with service.pool.db() as db:
        row = db.execute("SELECT state, error FROM card_outbox WHERE job=?", (job,)).fetchone()
    assert row["state"] == "degraded"
    assert row["error"] == "card_patch_degraded"


def test_durable_status_without_task_id_lists_chat_tasks_and_blocks_meta_status_submit(tmp_path: Path):
    plugin = _load_dispatch_plugin()
    shared_route = {"route": "feishu:oc_team", "channel": "feishu", "chat": "oc_team"}
    dev_svc = plugin.DispatchService(
        str(tmp_path / "dev"),
        "p",
        [str(tmp_path)],
        role="dev",
        route_provider=lambda sid: shared_route if sid.startswith("chat1") else {"route": "feishu:other", "channel": "feishu", "chat": "other"},
    )
    router = plugin.DispatchRouter({"dev": dev_svc})

    sub = router.submit("chat1-turn1", {"goal": "【用户原话】：跑冒烟回归\n执行冒烟测试", "request_key": "k1"})
    task_id = sub["task_id"]

    # Calling durable_status({}) with no task_id lists the session's active/recent tasks!
    listed = router.status("chat1-turn1", {})
    assert listed["ok"] is True
    assert listed["count"] == 1
    assert listed["tasks"][0]["task_id"] == task_id
    assert listed["tasks"][0]["status"] == "queued"

    # Strict session isolation is preserved when cross_session_status=False
    other_listed = router.status("other-session", {})
    assert other_listed["count"] == 0

    # Pure meta-status inquiries submitted via durable_submit are rejected with guidance to call durable_status
    with pytest.raises(ValueError, match="status_inquiry_use_durable_status"):
        router.submit(
            "chat1-turn2",
            {"goal": "【用户原话】：重跑这么久了，还没完事吗？\n查看刚才的任务跑完没有", "request_key": "meta1"},
        )


def test_doctor_worker_queue_health_observation_detects_degradation_without_leaking_secrets(tmp_path: Path):
    installer = _load_installer()
    root = tmp_path / "managed_root"
    qdir = root / "state" / "dev-queue"
    qdir.mkdir(parents=True)
    db_path = qdir / "pool.db"

    installed_at = 1000.0
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, created REAL, payload TEXT, result TEXT)")
        db.execute("CREATE TABLE card_outbox (job TEXT PRIMARY KEY, state TEXT)")
        db.execute(
            "INSERT INTO jobs VALUES (?, 'completed', ?, ?, ?)",
            (
                "j1",
                installed_at + 10.0,
                json.dumps({"goal": "TOP_SECRET_PROMPT"}),
                json.dumps({"ok": True, "engine": "pi", "engine_degraded": True, "summary": "SECRET_OUTPUT"}),
            ),
        )
        db.execute("INSERT INTO card_outbox VALUES ('j1', 'unknown')")

    obs = installer._worker_queue_health_observation(root, installed_at=installed_at)
    assert obs["state"] == "observed"
    assert obs["healthy"] is False
    assert obs["card_outbox_unknown"] == 1
    assert obs["recent_completed_post_install"] == 1
    assert obs["recent_engine_degraded_post_install"] == 1
    assert "SECRET" not in repr(obs)


def test_managed_hermes_launcher_rewrites_to_inproc_oneshot_inside_bwrap(tmp_path: Path, monkeypatch):
    der = _load_runner()
    att_dir = tmp_path / "attempt_1"
    att_dir.mkdir(parents=True)
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(att_dir))

    managed_bin = tmp_path / "hermes"
    managed_bin.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:2] == ['--print-runtime-command']:\n"
        f"    print(json.dumps([{sys.executable!r}, '-I', '-c', "
        "\"import os, sys, runpy; runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)\"]))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    managed_bin.chmod(0o755)

    cmd = der._build_local_hermes_cmd(
        str(managed_bin),
        budget=120,
        max_turns=25,
        route={"selected_model": "flash", "selected_reasoning_effort": "low"},
        env=dict(os.environ),
    )
    assert cmd[0] == sys.executable
    assert cmd[2] == "-c"
    assert "hermes_inproc_oneshot.py" in cmd[3]
    assert "hermes_cli.main" not in cmd[3]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "flash"
    assert "--reasoning" in cmd and cmd[cmd.index("--reasoning") + 1] == "low"
    assert "--usage-file" in cmd and cmd[cmd.index("--usage-file") + 1].endswith("hermes_usage.json")


def test_backend_runner_plugins_load_cleanly_against_readonly_state(tmp_path: Path, monkeypatch):
    """Inside bwrap, state/ queues and colleague-team/registry.sqlite are read-only."""
    from agentcore.colleague_registry import Registry

    dd_plugin = _load_dispatch_plugin()
    ct_spec = importlib.util.spec_from_file_location(
        "ct_plugin_test", REPO / "plugins" / "colleague_team" / "__init__.py"
    )
    ct_plugin = importlib.util.module_from_spec(ct_spec)
    ct_spec.loader.exec_module(ct_plugin)

    reg_dir = tmp_path / "colleague-team"
    reg_dir.mkdir(mode=0o700)
    reg_file = reg_dir / "registry.sqlite"
    # Host initializes registry first
    Registry(reg_file, profile_id="team-alpha", primary_id="lead", primary_name="Lead")

    # Make both queue parent and registry directory read-only (simulating bwrap --ro-bind)
    ro_parent = tmp_path / "readonly_queues"
    ro_parent.mkdir(mode=0o500)
    reg_file.chmod(0o400)
    reg_dir.chmod(0o500)

    try:
        monkeypatch.setenv("SUPERAGENT_BACKEND_RUNNER", "1")
        tools_registered: dict[str, tuple[str, object]] = {}
        prompts_registered: list[str] = []

        dd_ctx = SimpleNamespace(
            profile_name="lead",
            get_config=lambda k, default=None: {
                "enabled": True,
                "queue_root": str(ro_parent / "analysis-queue"),
                "allowed_roots": [str(tmp_path)],
                "deliver_completions": True,
            }.get(k, default),
            register_tool=lambda **kw: tools_registered.__setitem__(kw["name"], (kw["toolset"], kw["handler"])),
            register_system_prompt_section=lambda name, *a, **kw: prompts_registered.append(name),
        )
        # Must NOT open ro_parent/analysis-queue or start backfill threads!
        dd_plugin.register(dd_ctx)
        assert not tools_registered
        assert not prompts_registered

        ct_ctx = SimpleNamespace(
            profile_name="lead",
            get_config=lambda k, default=None: {
                "enabled": True,
                "engine_root": str(REPO),
                "team_id": "team-alpha",
                "primary_id": "lead",
                "primary_name": "Lead",
                "registry_path": str(reg_file),
                "gateway_dispatch.enabled": True,
            }.get(k, default),
            register_tool=lambda **kw: tools_registered.__setitem__(kw["name"], (kw["toolset"], kw["handler"])),
            register_system_prompt_section=lambda name, *a, **kw: prompts_registered.append(name),
        )
        ct_plugin.register(ct_ctx)
        assert list(tools_registered) == ["colleague_list"]
        toolset, list_handler = tools_registered["colleague_list"]
        assert toolset == "colleague_team"
        out = json.loads(list_handler({}, session_id="worker-sess-1"))
        assert out["ok"] is True
        assert any(m["member_id"] == "lead" for m in out["members"])
    finally:
        ro_parent.chmod(0o700)
        reg_dir.chmod(0o700)
        reg_file.chmod(0o600)


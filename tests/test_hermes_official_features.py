"""Unit tests for Hermes official features migration:
1. Built-in Feishu doc/drive tool thread-local client auto-injection (pre_tool_call hook).
2. Cron pre-run gate script (tools/nightly_review_gate.py) wakeAgent contract.
3. Pool exit-code 75 (EX_TEMPFAIL) and 78 (EX_CONFIG) semantic triage.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import importlib.util
from pathlib import Path
import sqlite3
import sys
import types

from agentcore.pool import Pool
from plugins.markdown_delivery import _TenantToken, _make_pre_tool_call

_GATE_PATH = Path(__file__).resolve().parents[1] / "tools" / "nightly_review_gate.py"
_gate_spec = importlib.util.spec_from_file_location("nightly_review_gate_test", _GATE_PATH)
_gate_mod = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(_gate_mod)
evaluate_gate = _gate_mod.evaluate_gate


class _DummyCtx:
    def __init__(self, config: dict):
        self._config = config

    def get_config(self, key: str, default=None):
        return self._config.get(key, default)


def test_pre_tool_call_injects_lark_client_from_sibling_env(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    colleague_dir = profiles_dir / "cases"
    main_dir = profiles_dir / "main_bot"
    colleague_dir.mkdir(parents=True)
    main_dir.mkdir(parents=True)

    # Colleague .env lacks credentials, sibling main_bot/.env has them
    (colleague_dir / ".env").write_text("OTHER_VAR=1\n", encoding="utf-8")
    (main_dir / ".env").write_text(
        "FEISHU_APP_ID=cli_test_app_123\nFEISHU_APP_SECRET=sec_test_456\n",
        encoding="utf-8",
    )

    # Mock lark_oapi and tools.feishu_lark
    built_clients = []

    class _FakeBuilder:
        def __init__(self):
            self.info = {}

        def app_id(self, val):
            self.info["app_id"] = val
            return self

        def app_secret(self, val):
            self.info["app_secret"] = val
            return self

        def domain(self, val):
            self.info["domain"] = val
            return self

        def log_level(self, val):
            self.info["log_level"] = val
            return self

        def build(self):
            client = {"built": dict(self.info)}
            built_clients.append(client)
            return client

    fake_lark = types.ModuleType("lark_oapi")
    fake_lark.FEISHU_DOMAIN = "https://open.feishu.cn"
    fake_lark.LARK_DOMAIN = "https://open.larksuite.com"
    fake_lark.LogLevel = types.SimpleNamespace(WARNING="WARN")
    fake_lark.Client = types.SimpleNamespace(builder=lambda: _FakeBuilder())
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)

    holder = {"client": None}
    fake_feishu_lark = types.ModuleType("tools.feishu_lark")
    fake_feishu_lark.get_client = lambda: holder["client"]
    fake_feishu_lark.set_client = lambda c: holder.__setitem__("client", c)
    fake_tools_pkg = types.ModuleType("tools")
    fake_tools_pkg.feishu_lark = fake_feishu_lark
    monkeypatch.setitem(sys.modules, "tools.feishu_lark", fake_feishu_lark)

    ctx = _DummyCtx({"env_file": str(colleague_dir / ".env")})
    token = _TenantToken(ctx)
    hook = _make_pre_tool_call(token)

    # Non-feishu tool should be ignored
    hook(tool_name="read_file")
    assert holder["client"] is None

    # feishu_doc_read should lazily build and inject the client
    hook(tool_name="feishu_doc_read")
    assert holder["client"] is not None
    assert holder["client"]["built"]["app_id"] == "cli_test_app_123"
    assert holder["client"]["built"]["app_secret"] == "sec_test_456"
    assert len(built_clients) == 1


def _init_state_db(db_path: Path, rows: list[tuple]):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL,
                last_activity_at REAL,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                title TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count, tool_call_count, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_nightly_review_gate_conditions(tmp_path):
    hermes_home = tmp_path / "profile"
    hermes_home.mkdir()
    state_dir = tmp_path / "state"
    (state_dir / "dev-queue").mkdir(parents=True)

    now_daytime = datetime(2026, 11, 10, 10, 30, tzinfo=timezone.utc)
    now_nightly = datetime(2026, 11, 10, 2, 15, tzinfo=timezone.utc)

    # 1. Outside nightly window -> wakeAgent: False
    summary, payload = evaluate_gate(
        hermes_home=hermes_home,
        state_dir=str(state_dir),
        now_dt=now_daytime,
        start_hour=1,
        end_hour=4,
    )
    assert summary == ""
    assert payload["wakeAgent"] is False
    assert payload["reason"] == "outside_nightly_window"

    # 2. Inside window, but no sessions -> wakeAgent: False
    _init_state_db(
        hermes_home / "state.db",
        [("cron_123", "cron", now_nightly.timestamp() - 60, now_nightly.timestamp() - 30, 4, 1, "cron job")],
    )
    summary, payload = evaluate_gate(
        hermes_home=hermes_home,
        state_dir=str(state_dir),
        now_dt=now_nightly,
        start_hour=1,
        end_hour=4,
    )
    assert payload["wakeAgent"] is False
    assert payload["reason"] == "no_recent_sessions"

    # 3. Add a real user session -> wakeAgent: True
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess_user_1", "gateway", now_nightly.timestamp() - 3600, now_nightly.timestamp() - 1800, 6, 3, "Debug issue"),
        )
    summary, payload = evaluate_gate(
        hermes_home=hermes_home,
        state_dir=str(state_dir),
        now_dt=now_nightly,
        start_hour=1,
        end_hour=4,
    )
    assert payload["wakeAgent"] is True
    assert payload["session_count"] == 1
    assert "sess_user_1" in summary

    # 4. Active running job in pool.db blocks wakeAgent unless forced
    pool_db = state_dir / "dev-queue" / "pool.db"
    with sqlite3.connect(pool_db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.execute("INSERT INTO jobs VALUES ('j1', 'running')")
    summary, payload = evaluate_gate(
        hermes_home=hermes_home,
        state_dir=str(state_dir),
        now_dt=now_nightly,
        start_hour=1,
        end_hour=4,
    )
    assert payload["wakeAgent"] is False
    assert payload["reason"] == "active_tasks_running"


def test_pool_exit_code_75_and_78_triage(tmp_path):
    pool = Pool(tmp_path / "pool")

    # Case 1: exit_code 75 (EX_TEMPFAIL) on replay_safe job requeues without burning tries
    job_tempfail = pool.submit("k-75", {"task": "t1"}, replay_safe=True, max_tries=2)
    claimed = pool.claim(lease=60)
    assert claimed is not None
    row, fd = claimed
    import os
    os.close(fd)
    assert row["id"] == job_tempfail
    assert row["tries"] == 1
    pool.finish(job_tempfail, row["attempt"], False, {"reason": "rate limit", "exit_code": 75})
    st = pool.status(job_tempfail)
    assert st["status"] == "queued"
    assert st["tries"] == 0

    # Case 2: exit_code 78 (EX_CONFIG) fails immediately even when replay_safe and tries < max_tries
    pool2 = Pool(tmp_path / "pool2")
    job_config = pool2.submit("k-78", {"task": "t2"}, replay_safe=True, max_tries=3)
    claimed2 = pool2.claim(lease=60)
    assert claimed2 is not None
    row2, fd2 = claimed2
    os.close(fd2)
    assert row2["id"] == job_config
    pool2.finish(job_config, row2["attempt"], False, {"reason": "invalid api key", "exit_code": 78})
    st2 = pool2.status(job_config)
    assert st2["status"] == "failed"

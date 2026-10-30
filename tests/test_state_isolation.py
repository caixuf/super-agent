"""测试不得写进运行中 profile 的真实状态目录。

现场实证（2026-10-29）：本机运行 profile 的 `~/.local/state/agentcore/liveness.db` 里
**561 行中有 457 行带测试 owner**（`test-profile:session`、`test-profile:session-one`、
`session`、`session-one`）—— 用例构造的 ledger / 插件服务没给显式路径，于是落到
`agentcore.liveness.DEFAULT_DB_PATH`（用户真实目录）。

本文件把这条约束钉成门禁：会话级夹具必须把隐式状态路径整体挪进 tmp。
它检查的是**生效值**，不是夹具写法，所以夹具被删掉或改名都会让它红。
"""
from __future__ import annotations

import os
from pathlib import Path


def _real_state_dir() -> Path:
    return Path.home() / ".local" / "state" / "agentcore"


def test_implicit_liveness_path_is_not_the_live_ledger():
    from agentcore.liveness import DEFAULT_DB_PATH, default_db_path

    effective = default_db_path()
    assert effective != DEFAULT_DB_PATH, (
        "agentcore.liveness.default_db_path() 仍指向用户真实账本；"
        "tests/conftest.py 的会话级隔离夹具没生效")
    real_dir = _real_state_dir()
    assert real_dir not in effective.parents, f"隐式账本落在真实状态目录下：{effective}"


def test_state_dir_env_is_honoured_and_isolated():
    from agentcore.liveness import default_db_path

    state = os.environ.get("AGENTCORE_STATE_DIR", "")
    ledger = os.environ.get("AGENTCORE_LIVENESS_DB", "")
    assert state and ledger, "夹具必须同时设置 AGENTCORE_STATE_DIR 与 AGENTCORE_LIVENESS_DB"
    assert str(default_db_path()).startswith(state)


def test_ledger_construction_without_a_path_stays_in_the_sandbox():
    """A ledger built the way an un-configured plugin builds one must not touch the live file."""
    from agentcore.liveness import LivenessLedger, default_db_path

    before = default_db_path()
    live = Path.home() / ".local" / "state" / "agentcore" / "liveness.db"
    # Snapshot the *live* file, not the sandbox one: a machine that never had a live ledger
    # (CI, a fresh checkout) reported `existed=True` off the sandbox path while `live` was
    # absent, so the guard below compared two different files and failed on a clean host.
    live_existed = live.exists()
    live_stamp = live.stat().st_mtime_ns if live_existed else None
    ledger = LivenessLedger(None)
    ledger.ensure_schema()
    assert Path(ledger.path).resolve() == before.resolve()
    assert Path(ledger.path).resolve() != live.resolve()
    # 真账本不该因为这条用例被创建或改动
    assert live.exists() == live_existed
    if live_existed:
        assert live.stat().st_mtime_ns == live_stamp, "用例改写了真实账本"
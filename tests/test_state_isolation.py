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
    # 比的是「线上账本」自己的前后状态。原版把 sandbox 路径的 exists() 拿去和
    # live 路径比（`existed = before.exists()` 却断言在 `live` 上），两个不同的文件，
    # 于是只要线上账本原本已存在就必然假红 —— CI 上就是这么红的。
    live = Path.home() / ".local" / "state" / "agentcore" / "liveness.db"
    live_existed = live.exists()
    ledger = LivenessLedger(None)
    ledger.ensure_schema()
    assert Path(ledger.path).resolve() == before.resolve()
    assert Path(ledger.path).resolve() != live.resolve()
    assert live.exists() == live_existed, "未配置路径的账本不得创建或删除线上账本"
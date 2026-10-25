#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆使用痕迹（usage trace）：记录「哪条记忆在哪次任务里被给出过」。

为什么需要（2026-10-23）：
    memory_synapses.json 里只有 1 条记录、reinforce_count=1 —— 突触账本基本是死的。
    根因不是强化函数不好用，而是**没有任何地方记录「这条记忆被用上了」**。
    没有 trace，reinforce_memory 只能拿「任务成功」给所有记忆加分，
    包括那些压根没起作用的 —— 那不是学习，是噪声累积。

    所以顺序必须是：先有「谁被给出过」的确定痕迹，才谈得上「谁真的有用」。

设计：
    - 只记录 fact_id，不复制内容（内容在 working_fact 里，别造第二份真相）
    - 写入是 O(1) 追加，SQLite WAL，不阻塞对话与诊断
    - 读不到就返回空，任何异常都不得影响诊断主结果
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("trace"))


def _db_path() -> Path:
    """实时追踪记录库（召回→使用→结果）。与事实库同目录。"""
    return get_config().trace_db_path()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_use (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id     TEXT NOT NULL,
            task_id     TEXT NOT NULL DEFAULT '',
            channel     TEXT NOT NULL DEFAULT '',   -- diagnose / chat / recall
            query       TEXT NOT NULL DEFAULT '',
            signal      TEXT NOT NULL DEFAULT '',   -- 诊断侧触发的信号词
            ebm         TEXT NOT NULL DEFAULT '',
            used_at     TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_use_fact ON memory_use(fact_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_use_task ON memory_use(task_id);")
    return conn


def record(fact_ids: List[str], *, task_id: str = "", channel: str = "",
           query: str = "", signal: str = "", ebm: str = "") -> Dict[str, Any]:
    """记录一次召回把哪些事实交给了调用方。失败只记日志，绝不向外抛。"""
    ids = [str(f).strip() for f in (fact_ids or []) if str(f).strip()]
    if not ids:
        return {"ok": True, "recorded": 0}
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn = _connect()
        try:
            conn.executemany(
                "INSERT INTO memory_use (fact_id, task_id, channel, query, signal, ebm, used_at)"
                " VALUES (?,?,?,?,?,?,?)",
                [(fid, task_id, channel, query[:500], signal[:200], ebm, now) for fid in ids])
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "recorded": len(ids)}
    except Exception as exc:  # noqa: BLE001 —— 记账失败不能影响诊断主结果
        logger.warning("记忆使用痕迹写入失败（不影响主流程）: %s", exc)
        return {"ok": False, "recorded": 0, "error": str(exc)}


def uses_of_task(task_id: str) -> List[Dict[str, Any]]:
    """某次任务用到了哪些记忆 —— 任务终态时据此决定强化谁。"""
    if not (task_id or "").strip():
        return []
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT fact_id, channel, signal, ebm FROM memory_use"
                " WHERE task_id = ?", (task_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取任务记忆使用记录失败: %s", exc)
        return []


def stats(fact_id: Optional[str] = None, days: int = 30) -> Dict[str, Any]:
    """使用统计：被给出多少次、被多少个不同任务用过。供巡检与调试看。"""
    try:
        conn = _connect()
        try:
            if fact_id:
                row = conn.execute(
                    "SELECT COUNT(*) c, COUNT(DISTINCT task_id) t FROM memory_use WHERE fact_id=?",
                    (fact_id,)).fetchone()
                return {"ok": True, "fact_id": fact_id,
                        "uses": row["c"], "tasks": row["t"]}
            rows = conn.execute(
                "SELECT fact_id, COUNT(*) c, COUNT(DISTINCT task_id) t FROM memory_use"
                " GROUP BY fact_id ORDER BY c DESC LIMIT 20").fetchall()
            return {"ok": True, "top": [dict(r) for r in rows]}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取记忆使用统计失败: %s", exc)
        return {"ok": False, "error": str(exc)}

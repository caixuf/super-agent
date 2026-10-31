"""agentcore.trajectory_ledger — 会话轨迹对账审计账本与防退化监控。

核心机制：
1. SQLite WAL 账本持久化记录每次会话交互轨迹：
   - 轮次总耗时、模型 API 延迟（首包与全包耗时）；
   - 调用的工具集及执行耗时；
   - 显式规划生成标志、重试自愈次数；
   - 终态机器验证结论 (PASS / FAIL / SKIPPED / USER_CORRECTED) 与证据摘要。
2. 暴露只读健康诊断与会话行为对账接口：
   - audit_session: 审计指定会话的多轮因果轨迹、工具命中率与异常重试率；
   - get_system_health: 全局健康度统计，防退化指标监控。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_FILENAME = "trajectory_ledger.db"


def get_default_trajectory_db_path() -> Path:
    env_path = os.getenv("SUPER_AGENT_TRAJECTORY_DB")
    if env_path:
        return Path(env_path).expanduser().resolve()
    agentcore_data = os.getenv("AGENTCORE_DATA_DIR")
    if agentcore_data:
        base = Path(agentcore_data).expanduser()
    else:
        base = Path.home() / ".agentcore" / "data"
    return (base / DEFAULT_LEDGER_FILENAME).resolve()


@dataclass
class TrajectoryTurnRecord:
    session_id: str
    turn_id: str
    user_query: str
    timestamp: float = field(default_factory=time.time)
    model_name: str = "main"
    total_latency_ms: float = 0.0
    first_token_latency_ms: float = 0.0
    tools_invoked: List[Dict[str, Any]] = field(default_factory=list)
    plan_generated: bool = False
    retry_count: int = 0
    verification_status: str = "PASS"  # PASS, FAIL, SKIPPED, USER_CORRECTED
    error_summary: str = ""
    evidence_summary: str = ""


class TrajectoryLedger:
    """会话行为轨迹审计账本"""

    def __init__(self, db_path: Optional[Union[Path, str]] = None) -> None:
        if db_path:
            self.db_path = Path(db_path).resolve()
        else:
            self.db_path = get_default_trajectory_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trajectory_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    model_name TEXT NOT NULL,
                    total_latency_ms REAL NOT NULL,
                    first_token_latency_ms REAL NOT NULL,
                    tools_invoked_json TEXT NOT NULL,
                    plan_generated INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL,
                    verification_status TEXT NOT NULL,
                    error_summary TEXT NOT NULL DEFAULT '',
                    evidence_summary TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traj_session ON trajectory_turns(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traj_timestamp ON trajectory_turns(timestamp)")
            conn.commit()

    def record_turn(
        self,
        session_id: str,
        turn_id: str,
        user_query: str,
        total_latency_ms: float,
        model_name: str = "main",
        first_token_latency_ms: float = 0.0,
        tools_invoked: Optional[List[Dict[str, Any]]] = None,
        plan_generated: bool = False,
        retry_count: int = 0,
        verification_status: str = "PASS",
        error_summary: str = "",
        evidence_summary: str = "",
        timestamp: Optional[float] = None,
    ) -> int:
        """记录单轮交互执行轨迹。"""
        tools_list = tools_invoked or []
        tools_json = json.dumps(tools_list, ensure_ascii=False)
        ts = timestamp if timestamp is not None else time.time()

        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO trajectory_turns (
                    session_id, turn_id, user_query, timestamp, model_name,
                    total_latency_ms, first_token_latency_ms, tools_invoked_json,
                    plan_generated, retry_count, verification_status,
                    error_summary, evidence_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_id,
                    user_query,
                    ts,
                    model_name,
                    total_latency_ms,
                    first_token_latency_ms,
                    tools_json,
                    1 if plan_generated else 0,
                    retry_count,
                    verification_status,
                    error_summary,
                    evidence_summary,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0

    def get_session_trajectory(self, session_id: str) -> List[Dict[str, Any]]:
        """获取指定会话的时间序列轨迹列表。"""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM trajectory_turns
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (session_id,),
            )
            records = []
            for r in cur.fetchall():
                d = dict(r)
                d["tools_invoked"] = json.loads(d.pop("tools_invoked_json") or "[]")
                d["plan_generated"] = bool(d["plan_generated"])
                records.append(d)
            return records

    def audit_session(self, session_id: str) -> Dict[str, Any]:
        """对指定会话进行综合行为审计，统计异常率、重试开销与模型延迟。"""
        turns = self.get_session_trajectory(session_id)
        if not turns:
            return {
                "session_id": session_id,
                "turn_count": 0,
                "status": "NOT_FOUND",
            }

        total_latency = sum(t["total_latency_ms"] for t in turns)
        all_tools = []
        for t in turns:
            all_tools.extend(t.get("tools_invoked", []))

        pass_count = sum(1 for t in turns if t["verification_status"] == "PASS")
        fail_count = sum(1 for t in turns if t["verification_status"] == "FAIL")
        retry_sum = sum(t.get("retry_count", 0) for t in turns)
        plan_count = sum(1 for t in turns if t.get("plan_generated"))

        return {
            "session_id": session_id,
            "turn_count": len(turns),
            "total_latency_ms": round(total_latency, 2),
            "avg_latency_ms": round(total_latency / len(turns), 2),
            "tool_call_count": len(all_tools),
            "unique_tools": list({t.get("name") for t in all_tools if t.get("name")}),
            "plan_generated_turns": plan_count,
            "total_retries": retry_sum,
            "pass_rate": round(pass_count / len(turns), 4),
            "failed_turns": fail_count,
            "health_verdict": "HEALTHY" if fail_count == 0 else "ANOMALY_DETECTED",
        }

    def get_system_health(self) -> Dict[str, Any]:
        """获取全系统防退化健康指标。"""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as total_turns FROM trajectory_turns")
            row = cur.fetchone()
            total_turns = row["total_turns"] if row else 0

            if total_turns == 0:
                return {
                    "total_turns": 0,
                    "overall_pass_rate": 1.0,
                    "avg_latency_ms": 0.0,
                    "anomaly_rate": 0.0,
                    "status": "IDLE",
                }

            cur = conn.execute(
                """
                SELECT
                    AVG(total_latency_ms) as avg_lat,
                    SUM(CASE WHEN verification_status = 'PASS' THEN 1 ELSE 0 END) as pass_cnt,
                    SUM(CASE WHEN verification_status = 'FAIL' THEN 1 ELSE 0 END) as fail_cnt,
                    SUM(retry_count) as total_retries
                FROM trajectory_turns
                """
            )
            agg = cur.fetchone()
            pass_cnt = agg["pass_cnt"] or 0
            fail_cnt = agg["fail_cnt"] or 0
            avg_lat = agg["avg_lat"] or 0.0

            return {
                "total_turns": total_turns,
                "overall_pass_rate": round(pass_cnt / total_turns, 4),
                "avg_latency_ms": round(avg_lat, 2),
                "failed_turns": fail_cnt,
                "total_retries": agg["total_retries"] or 0,
                "status": "HEALTHY" if fail_cnt == 0 else "DEGRADED",
            }


_GLOBAL_TRAJECTORY_LEDGER: Optional[TrajectoryLedger] = None


def get_default_trajectory_ledger() -> TrajectoryLedger:
    global _GLOBAL_TRAJECTORY_LEDGER
    if _GLOBAL_TRAJECTORY_LEDGER is None:
        _GLOBAL_TRAJECTORY_LEDGER = TrajectoryLedger()
    return _GLOBAL_TRAJECTORY_LEDGER

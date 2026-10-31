"""Unit tests for agentcore.trajectory_ledger."""
from __future__ import annotations

import time
import pytest

from agentcore.trajectory_ledger import TrajectoryLedger, get_default_trajectory_ledger


def test_trajectory_ledger_record_and_query(tmp_path):
    db_file = tmp_path / "test_traj.db"
    ledger = TrajectoryLedger(db_path=db_file)

    rowid = ledger.record_turn(
        session_id="sess-001",
        turn_id="turn-1",
        user_query="优化记忆检索",
        total_latency_ms=125.5,
        first_token_latency_ms=45.0,
        tools_invoked=[{"name": "recall_entity", "duration_ms": 2.5}],
        plan_generated=True,
        retry_count=0,
        verification_status="PASS",
        evidence_summary="7 passed in 0.26s",
    )
    assert rowid > 0

    turns = ledger.get_session_trajectory("sess-001")
    assert len(turns) == 1
    t = turns[0]
    assert t["turn_id"] == "turn-1"
    assert t["total_latency_ms"] == 125.5
    assert t["plan_generated"] is True
    assert t["verification_status"] == "PASS"
    assert len(t["tools_invoked"]) == 1
    assert t["tools_invoked"][0]["name"] == "recall_entity"


def test_trajectory_audit_session(tmp_path):
    db_file = tmp_path / "test_traj_audit.db"
    ledger = TrajectoryLedger(db_path=db_file)

    # Record 3 turns: 2 passes, 1 fail
    ledger.record_turn(
        session_id="sess-audit-1",
        turn_id="t1",
        user_query="查询硬件状态",
        total_latency_ms=100.0,
        verification_status="PASS",
    )
    ledger.record_turn(
        session_id="sess-audit-1",
        turn_id="t2",
        user_query="修改内核参数",
        total_latency_ms=200.0,
        retry_count=2,
        verification_status="FAIL",
        error_summary="kernel panic",
    )
    ledger.record_turn(
        session_id="sess-audit-1",
        turn_id="t3",
        user_query="修复配置",
        total_latency_ms=150.0,
        verification_status="PASS",
    )

    audit = ledger.audit_session("sess-audit-1")
    assert audit["turn_count"] == 3
    assert audit["total_latency_ms"] == 450.0
    assert audit["avg_latency_ms"] == 150.0
    assert audit["total_retries"] == 2
    assert audit["failed_turns"] == 1
    assert audit["pass_rate"] == 0.6667
    assert audit["health_verdict"] == "ANOMALY_DETECTED"


def test_trajectory_system_health(tmp_path):
    db_file = tmp_path / "test_traj_sys.db"
    ledger = TrajectoryLedger(db_path=db_file)

    # Empty health
    empty_health = ledger.get_system_health()
    assert empty_health["total_turns"] == 0
    assert empty_health["status"] == "IDLE"

    # Add healthy turns
    ledger.record_turn(
        session_id="sess-h1",
        turn_id="t1",
        user_query="query 1",
        total_latency_ms=80.0,
        verification_status="PASS",
    )
    ledger.record_turn(
        session_id="sess-h2",
        turn_id="t2",
        user_query="query 2",
        total_latency_ms=120.0,
        verification_status="PASS",
    )

    health = ledger.get_system_health()
    assert health["total_turns"] == 2
    assert health["overall_pass_rate"] == 1.0
    assert health["avg_latency_ms"] == 100.0
    assert health["status"] == "HEALTHY"

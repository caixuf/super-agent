"""Tests for Hermes Kanban Worker Contract and Pool Bridge (C05, C06, C07, C09, C10, C11)."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import pytest

from agentcore.kanban_contract import (
    KanbanWorkerEnvironment,
    KanbanWorkerContract,
    KanbanTaskBridge,
    init_kanban_schema,
    ENV_KANBAN_TASK,
    ENV_KANBAN_DB,
    ENV_KANBAN_WORKSPACE,
    ENV_KANBAN_RUN_ID,
    ENV_KANBAN_CLAIM_LOCK,
    ENV_HERMES_PROFILE,
)


def test_kanban_worker_environment_detection_and_validation(tmp_path: Path):
    db_file = tmp_path / "kanban.db"
    env_vars = {
        ENV_KANBAN_TASK: "t_12345678",
        ENV_KANBAN_DB: str(db_file),
        ENV_KANBAN_WORKSPACE: str(tmp_path / "workspace"),
        ENV_KANBAN_RUN_ID: "2",
        ENV_KANBAN_CLAIM_LOCK: "host:pid:uuid",
        ENV_HERMES_PROFILE: "architect",
    }
    worker_env = KanbanWorkerEnvironment(env_vars)
    assert worker_env.is_kanban_worker is True
    assert worker_env.task_id == "t_12345678"
    assert worker_env.db_path == db_file
    assert worker_env.run_id == 2
    assert worker_env.profile == "architect"
    worker_env.validate()

    empty_env = KanbanWorkerEnvironment({})
    assert empty_env.is_kanban_worker is False
    with pytest.raises(ValueError, match="Missing required environment"):
        empty_env.validate()


def test_kanban_worker_single_terminal_action_contract(tmp_path: Path):
    db_file = tmp_path / "kanban.db"
    bridge = KanbanTaskBridge(db_file)
    task_id = bridge.create_task("Architecture review", "Review interface specs", assignee="architect")

    env_vars = {
        ENV_KANBAN_TASK: task_id,
        ENV_KANBAN_DB: str(db_file),
        ENV_KANBAN_RUN_ID: "1",
    }
    worker = KanbanWorkerContract(KanbanWorkerEnvironment(env_vars))
    assert worker.terminated is False

    # First terminal call: complete succeeds
    res = worker.complete("Interface design finished", artifacts=["/tmp/spec.pdf"])
    assert res["status"] == "done"
    assert worker.terminated is True
    assert worker.terminal_action == "complete"

    # Second terminal call must fail immediately
    with pytest.raises(RuntimeError, match="Terminal action already recorded"):
        worker.complete("Duplicate finish")

    with pytest.raises(RuntimeError, match="Terminal action already recorded"):
        worker.block("Cannot block after complete")

    # Check state in db
    task = bridge.get_task(task_id)
    assert task["status"] == "done"


def test_kanban_task_creation_idempotency_c05(tmp_path: Path):
    db_file = tmp_path / "kanban.db"
    bridge = KanbanTaskBridge(db_file)

    t1 = bridge.create_task("Test task", "Task details", idempotency_key="req_unique_001")
    t2 = bridge.create_task("Test task", "Task details", idempotency_key="req_unique_001")
    assert t1 == t2

    task = bridge.get_task(t1)
    assert task["title"] == "Test task"
    assert task["status"] == "ready"

    # Empty title rejected
    with pytest.raises(ValueError, match="Task title must be non-empty"):
        bridge.create_task("")


def test_kanban_claim_and_heartbeat_c09(tmp_path: Path):
    db_file = tmp_path / "kanban.db"
    bridge = KanbanTaskBridge(db_file)
    task_id = bridge.create_task("Async coding", assignee="coder")

    # Claim ready task
    claimed = bridge.claim_task(task_id, "coder", claim_lock="lock:abc:123", run_id=1)
    assert claimed is True

    # Duplicate claim on running task must be rejected
    claimed_again = bridge.claim_task(task_id, "another_worker", claim_lock="lock:xyz:456")
    assert claimed_again is False

    task = bridge.get_task(task_id)
    assert task["status"] == "running"
    assert task["claim_lock"] == "lock:abc:123"

    # Emit heartbeat
    hb_ok = bridge.emit_heartbeat(task_id, run_id=1, progress="50% completed")
    assert hb_ok is True

    events = bridge.get_task_events(task_id)
    event_types = [e["event_type"] for e in events]
    assert "created" in event_types
    assert "claimed" in event_types
    assert "heartbeat" in event_types

    # Heartbeat on completed task must return False and not tamper status
    env_vars = {
        ENV_KANBAN_TASK: task_id,
        ENV_KANBAN_DB: str(db_file),
        ENV_KANBAN_RUN_ID: "1",
    }
    worker = KanbanWorkerContract(KanbanWorkerEnvironment(env_vars))
    worker.complete("Finished work")
    assert bridge.get_task(task_id)["status"] == "done"

    hb_after_done = bridge.emit_heartbeat(task_id, run_id=1)
    assert hb_after_done is False
    assert bridge.get_task(task_id)["status"] == "done"


def test_kanban_cancellation_with_stop_proof_c10(tmp_path: Path):
    db_file = tmp_path / "kanban.db"
    bridge = KanbanTaskBridge(db_file)
    task_id = bridge.create_task("Long running job")
    bridge.claim_task(task_id, "worker1")

    # Cancel without stop proof -> stop_unconfirmed, not fake cancelled
    res_unproven = bridge.cancel_task(task_id, reason="User abort", stop_proven=False)
    assert res_unproven["cancelled"] is False
    assert res_unproven["status"] == "stop_unconfirmed"
    assert bridge.get_task(task_id)["status"] == "stop_unconfirmed"

    # Cancel with confirmed stop proof -> cancelled
    res_proven = bridge.cancel_task(task_id, reason="SIGKILL confirmed", stop_proven=True)
    assert res_proven["cancelled"] is True
    assert res_proven["status"] == "cancelled"
    assert bridge.get_task(task_id)["status"] == "cancelled"


def test_kanban_stale_task_recovery_c11(tmp_path: Path):
    db_file = tmp_path / "kanban.db"
    bridge = KanbanTaskBridge(db_file)
    t1 = bridge.create_task("Crashed worker job")
    bridge.claim_task(t1, "worker_dead")

    t2 = bridge.create_task("Completed job")
    bridge.claim_task(t2, "worker_ok")
    env_vars = {ENV_KANBAN_TASK: t2, ENV_KANBAN_DB: str(db_file), ENV_KANBAN_RUN_ID: "1"}
    KanbanWorkerContract(KanbanWorkerEnvironment(env_vars)).complete("Completed properly")

    # Force t1 timestamp to be old
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (time.time() - 200, t1))
        conn.commit()

    recovered = bridge.recover_stale_tasks(heartbeat_timeout_s=30.0)
    assert t1 in recovered
    assert t2 not in recovered

    # t1 should be returned to ready for reclaim
    assert bridge.get_task(t1)["status"] == "ready"
    assert bridge.get_task(t2)["status"] == "done"

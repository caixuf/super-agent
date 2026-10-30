"""Liveness ledger: state transitions must be monotonic, evidence-based, and bounded.

The tests pin the properties a status tool is allowed to claim:
  * a heartbeat is the only thing that keeps a pid-less task alive;
  * a recorded pid that disappears is ``lost``, never ``done``;
  * a terminal state is never reopened by a late beat;
  * a sweep returns transitions once, so a daemon cannot spam;
  * an absent record is ``unknown``, not a fabricated success.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentcore import liveness


class Clock:
    def __init__(self, start: float = 1000.0):
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCORE_LIVENESS_DB", raising=False)
    clock = Clock()
    return liveness.LivenessLedger(tmp_path / "liveness.db", stall_seconds=60, clock=clock), clock


def test_register_beat_and_inspect_progress(ledger):
    store, clock = ledger
    store.register("t1", kind="terminal", owner="p:s", label="build", state="running")
    clock.advance(5)
    row = store.beat("t1", phase="compile", progress=(3, 12), detail="case 3/12")
    assert row is not None and row["state"] == "running"
    clock.advance(4)
    got = store.inspect("t1")
    assert got["ok"] is True
    assert got["verdict"] == "running"
    assert got["elapsed_s"] == 9.0
    assert got["heartbeat_age_s"] == 4.0
    assert got["phase"] == "compile"
    assert got["progress"] == {"done": 3, "total": 12, "percent": 25.0}


def test_terminal_state_is_write_once(ledger):
    store, _ = ledger
    store.register("t1", kind="terminal", state="running")
    first = store.finish("t1", "done", exit_code=0, tail="ok")
    assert first["changed"] is True
    again = store.finish("t1", "failed", exit_code=1, tail="late")
    assert again["changed"] is False
    assert again["task"]["state"] == "done"
    assert store.beat("t1", phase="late") is None



def test_owner_bound_finish_and_record_exit_do_not_modify_reassigned_task(ledger):
    store, _ = ledger
    store.register("owned", kind="terminal", owner="profile:session-a",
                   state="running", label="a")
    rejected_exit = store.record_exit("owned", 1, tail="foreign output",
                                     require_owner="profile:session-b")
    assert rejected_exit == {"ok": False, "changed": False, "error": "unknown_task",
                             "task_id": "owned"}
    row = store.get("owned")
    assert row["owner"] == "profile:session-a"
    assert row["state"] == "running"
    assert row["tail"] == ""

    store.register("owned", kind="terminal", owner="profile:session-b",
                   state="running", label="b")
    rejected_finish = store.finish("owned", "done", require_owner="profile:session-a")
    assert rejected_finish == {"ok": False, "changed": False, "error": "unknown_task",
                               "task_id": "owned"}
    row = store.get("owned")
    assert row["owner"] == "profile:session-b"
    assert row["state"] == "running"


def test_sweep_marks_pidless_silence_stalled_once(ledger):
    store, clock = ledger
    store.register("t1", kind="durable", state="running")
    clock.advance(61)
    changed = store.sweep()
    assert [row["task_id"] for row in changed] == ["t1"]
    assert changed[0]["previous_state"] == "running"
    assert changed[0]["state"] == "stalled"
    assert "no heartbeat" in changed[0]["detail"]
    assert "任务结果尚未确认" in store.inspect("t1")["liveness_note"]
    assert store.sweep() == []


def test_owner_scoped_sweep_never_changes_another_session(ledger):
    store, clock = ledger
    store.register('one', kind='durable', owner='profile:one', state='running')
    store.register('two', kind='durable', owner='profile:two', state='running')
    clock.advance(61)
    changed = store.sweep(owner='profile:one')
    assert [row['task_id'] for row in changed] == ['one']
    assert store.get('one')['state'] == 'stalled'
    assert store.get('two')['state'] == 'running'
    assert [row['task_id'] for row in store.sweep()] == ['two']


def test_live_pid_is_not_marked_stalled(ledger):
    store, clock = ledger
    store.register("t1", kind="terminal", state="running", pid=1234, pid_start="boot:1")
    clock.advance(600)
    assert store.sweep(pid_alive=lambda pid, start: True) == []
    got = store.inspect("t1", pid_alive=lambda pid, start: True)
    assert got["verdict"] == "running"
    assert got["pid_alive"] is True
    assert got["quiet"] is True
    assert "进程仍可见" in got["liveness_note"] and "不能确认任务仍有进展" in got["liveness_note"]


def test_dead_pid_becomes_lost_not_done(ledger):
    store, clock = ledger
    store.register("t1", kind="terminal", state="running", pid=1234, pid_start="boot:1")
    clock.advance(5)
    changed = store.sweep(pid_alive=lambda pid, start: False)
    assert changed and changed[0]["state"] == "lost"
    result = store.inspect("t1", pid_alive=lambda pid, start: False)
    assert result["verdict"] == "lost"
    assert "不能判断任务最终结果" in result["liveness_note"]


def test_heartbeat_clears_stalled_observation(ledger):
    store, clock = ledger
    store.register("t1", kind="durable", state="running")
    clock.advance(61)
    store.sweep()
    clock.advance(1)
    row = store.beat("t1", detail="recovered")
    assert row is not None and row["state"] == "running"
    assert store.inspect("t1")["verdict"] == "running"


def test_unknown_record_is_not_a_fake_success(ledger):
    store, _ = ledger
    got = store.inspect("missing")
    assert got == {"ok": False, "error": "unknown_task", "task_id": "missing"}
    assert store.beat("missing") is None
    assert store.finish("missing")["ok"] is False


def test_owner_scope_blocks_cross_owner_reads(ledger):
    store, _ = ledger
    store.register("t1", kind="terminal", owner="profile:a", state="running")
    assert store.get("t1", require_owner="profile:a") is not None
    assert store.get("t1", require_owner="profile:b") is None
    assert store.inspect("t1", require_owner="profile:b")["ok"] is False


def test_list_tasks_filters_owner_and_terminal(ledger):
    store, _ = ledger
    store.register("a", kind="terminal", owner="p:s", state="running")
    store.register("b", kind="terminal", owner="p:s", state="running")
    store.finish("b", "done")
    store.register("c", kind="terminal", owner="other", state="running")
    active = store.list_tasks(owner="p:s", active_only=True)
    assert [row["task_id"] for row in active] == ["a"]
    all_owned = store.list_tasks(owner="p:s", active_only=False)
    assert {row["task_id"] for row in all_owned} == {"a", "b"}


def test_tail_is_bounded_and_does_not_change_state(ledger):
    store, _ = ledger
    store.register("t1", kind="terminal", state="running")
    assert store.record_tail("t1", "x" * 5000, limit=100) is True
    got = store.inspect("t1")
    assert len(got["tail"]) == 100
    assert got["state"] == "running"


def test_register_keeps_started_at_and_does_not_reopen_terminal(ledger):
    store, clock = ledger
    store.register("t1", kind="terminal", state="running")
    clock.advance(10)
    store.finish("t1", "done")
    clock.advance(10)
    reopened = store.register("t1", kind="terminal", state="running", label="again")
    assert reopened["state"] == "done"
    assert reopened["started_at"] == 1000.0


def test_real_child_disappearance_becomes_lost(tmp_path):
    """End-to-end pid evidence: alive → running, killed → lost, never done."""
    import subprocess
    import sys

    store = liveness.LivenessLedger(tmp_path / "liveness.db", stall_seconds=1)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.register("p1", kind="terminal", state="running", pid=child.pid)
        assert store.inspect("p1")["verdict"] == "running"
        child.terminate()
        child.wait(timeout=10)
        changed = store.sweep()
        assert [row["task_id"] for row in changed] == ["p1"]
        assert changed[0]["state"] == "lost"
        assert store.inspect("p1")["verdict"] == "lost"
        assert store.get("p1")["state"] != "done"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_observe_uses_external_heartbeat_without_faking_now(ledger):
    store, clock = ledger
    store.register("t1", kind="durable", state="queued")
    clock.advance(50)
    row = store.observe("t1", heartbeat_at=1000.0 + 25, state="running", detail="worker beat")
    assert row is not None and row["state"] == "running"
    got = store.inspect("t1")
    assert got["heartbeat_age_s"] == 25.0  # now=1050, observed beat=1025
    assert got["verdict"] == "running"

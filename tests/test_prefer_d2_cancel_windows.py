"""Prefer-D2 cancel/pause hard windows (STARTING / CANCELING).

AC: D2-1 starting_timeout · D2-2 cancel_timeout · D2-3 soft cancel within window ·
D2-4 finalize_cancel immediate · D2-5 pause waiting_user · D2-6 pool/liveness sweep ·
card stage never healthy Running for pid-null / cancel=1.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from agentcore.card_emit import CardEmitter
from agentcore.card_store import CardStore
from agentcore.liveness import LivenessLedger
from agentcore.liveness_daemon import LivenessDaemon
from agentcore.pool import (
    CANCEL_WINDOW_S,
    PAUSE_QUIESCE_S,
    START_WINDOW_S,
    Pool,
    display_stage,
)
from agentcore.task_card import CardState


ORIGIN = {
    "profile": "prof-d2",
    "session": "sess-d2",
    "route": "route-d2",
    "chat": "oc_chat_d2",
    "thread": "om_thread_d2",
    "channel": "feishu",
}


class _FakeCardTransport:
    def __init__(self):
        self.emits = []

    def emit(self, handle, view, *, final=False):
        self.emits.append((handle.task_id, handle.message_id, handle.card_id, view, final))


def test_d2_9_config_constants_documented():
    assert 15.0 <= START_WINDOW_S <= 60.0
    assert 30.0 <= CANCEL_WINDOW_S <= 120.0
    assert PAUSE_QUIESCE_S > 0


def test_display_stage_never_healthy_running_for_pid_null_or_cancel():
    assert display_stage({"status": "running", "pid": None, "cancel": 0}) == "starting"
    assert display_stage({"status": "running", "pid": 42, "cancel": 1}) == "canceling"
    assert display_stage({"status": "waiting_user"}) == "paused"
    assert display_stage({"status": "running", "pid": 42, "cancel": 0}) == "running"


def test_d2_1_starting_timeout_claim_without_pid(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-start", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    assert claimed["id"] == job
    # Simulate claim heartbeat in the past (STARTING window elapsed, pid still NULL).
    past = time.time() - (START_WINDOW_S + 5)
    with pool.db() as db:
        db.execute(
            "UPDATE jobs SET heartbeat=?, pid=NULL, pid_start=NULL WHERE id=?",
            (past, job),
        )
    try:
        forced = pool.enforce_windows(now=time.time())
        assert any(f["id"] == job and f["reason"] == "starting_timeout" for f in forced)
        st = pool.status(job)
        assert st["status"] == "failed"
        result = json.loads(st["result"])
        assert result["reason"] == "starting_timeout"
        assert result.get("event") == "claim_without_pid"
        assert st["stage"] == "failed"
        receipts = pool.receipts()
        assert any(r.get("job") == job and r.get("status") == "failed" for r in receipts)
    finally:
        os.close(fd)


def test_d2_1_within_start_window_stays_running(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-start-ok", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    try:
        forced = pool.enforce_windows(now=time.time())
        assert not any(f["id"] == job for f in forced)
        st = pool.status(job)
        assert st["status"] == "running"
        assert st["pid"] is None
        assert st["stage"] == "starting"
    finally:
        os.close(fd)


def test_d2_2_canceling_hard_timeout_force_cancelled(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-cancel-to", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    # Soft cancel stamps CANCELING clock; leave non-terminal.
    pool.cancel(job)
    st0 = pool.status(job)
    assert st0["cancel"] == 1
    assert st0["cancel_requested_at"] is not None
    assert st0["stage"] == "canceling"
    past = time.time() - (CANCEL_WINDOW_S + 5)
    with pool.db() as db:
        db.execute("UPDATE jobs SET cancel_requested_at=? WHERE id=?", (past, job))
    try:
        forced = pool.enforce_windows(now=time.time())
        assert any(f["id"] == job and f["reason"] == "cancel_timeout" for f in forced)
        st = pool.status(job)
        assert st["status"] == "cancelled"
        result = json.loads(st["result"])
        assert result["reason"] == "cancel_timeout"
        assert any(r.get("job") == job and r.get("status") == "cancelled" for r in pool.receipts())
    finally:
        os.close(fd)


def test_d2_3_soft_cancel_within_window_finish_observes(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-soft", {"goal": "x"}, origin=ORIGIN, replay_safe=True)
    claimed, fd = pool.claim()
    attempt = claimed["attempt"]
    try:
        pool.cancel(job)
        st = pool.status(job)
        assert st["status"] == "running"
        assert st["cancel"] == 1
        assert st["stage"] == "canceling"
        # Within window: enforce does not force-terminal yet.
        forced = pool.enforce_windows(now=time.time())
        assert not any(f["id"] == job for f in forced)
        # finish observes cancel → terminal cancelled (soft happy path).
        assert pool.finish(job, attempt, True, {"summary": "late"}) is True
        st2 = pool.status(job)
        assert st2["status"] == "cancelled"
        result = json.loads(st2["result"])
        assert result["reason"] == "cancelled"
    finally:
        os.close(fd)


def test_d2_4_finalize_cancel_immediate(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-fin", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    try:
        assert pool.finalize_cancel(job) is True
        st = pool.status(job)
        assert st["status"] == "cancelled"
        result = json.loads(st["result"])
        assert result["reason"] == "cancelled"
        # Second call is no-op.
        assert pool.finalize_cancel(job) is False
    finally:
        os.close(fd)


def test_d2_5_pause_waiting_user_card_not_running(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-pause", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    store = CardStore(tmp_path / "cards.db")
    store.bind(job, "c1", "m1")
    transport = _FakeCardTransport()
    emitter = CardEmitter(store, job, transport)
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="暂停")
    emitter.open(phase="work")
    try:
        assert pool.pause(job, reason="paused") is True
        st = pool.status(job)
        assert st["status"] == "waiting_user"
        assert st["stage"] == "paused"
        view = emitter.pause(detail="paused")
        assert view is not None
        assert view.state == "waiting_user"
        assert view.phase == "paused"
        assert "运行中" not in view.note
        assert "暂停" in view.note or "paused" in view.phase
        os.close(fd)
        fd = None
        assert pool.claim() is None
    finally:
        if fd is not None:
            os.close(fd)


def test_d2_6_liveness_tick_invokes_enforce_windows(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-live", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    past = time.time() - (START_WINDOW_S + 10)
    with pool.db() as db:
        db.execute("UPDATE jobs SET heartbeat=?, pid=NULL WHERE id=?", (past, job))
    ledger = LivenessLedger(tmp_path / "live.db")
    events = []
    daemon = LivenessDaemon(ledger, events.append, pool=pool, clock=time.time)
    try:
        daemon.run_once()
        st = pool.status(job)
        assert st["status"] == "failed"
        assert json.loads(st["result"])["reason"] == "starting_timeout"
    finally:
        os.close(fd)


def test_card_stage_starting_and_canceling_not_healthy_running(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t-stage", "c1", "m1")
    transport = _FakeCardTransport()
    emitter = CardEmitter(store, "t-stage", transport)
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="阶段")
    emitter.open(phase="work")
    v1 = emitter.mark_starting()
    assert v1.phase == "starting"
    assert "运行中" not in v1.note
    assert "启动中" in v1.note
    v2 = emitter.mark_canceling()
    assert v2.phase == "canceling"
    assert "运行中" not in v2.note
    assert "取消中" in v2.note
    # apply_pool_stage maps display_stage values.
    v3 = emitter.apply_pool_stage("paused")
    assert v3.state == CardState.WAITING_USER.value
    assert v3.phase == "paused"


def test_cancel_stamp_coalesce_does_not_refresh_deadline(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-coal", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    try:
        pool.cancel(job)
        first = pool.status(job)["cancel_requested_at"]
        time.sleep(0.05)
        pool.cancel(job)
        second = pool.status(job)["cancel_requested_at"]
        assert first == second
    finally:
        os.close(fd)


def test_claim_sweeps_starting_timeout_before_select(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d2-claim-sweep", {"goal": "x"}, origin=ORIGIN, replay_safe=True, max_tries=2)
    claimed, fd = pool.claim()
    past = time.time() - (START_WINDOW_S + 5)
    with pool.db() as db:
        db.execute("UPDATE jobs SET heartbeat=?, pid=NULL WHERE id=?", (past, job))
    os.close(fd)
    # Next claim should enforce windows (failed) rather than leave fake Running.
    nxt = pool.claim(lease=0)
    # Job is terminal failed; may or may not claim something else (nothing else).
    assert nxt is None
    assert pool.status(job)["status"] == "failed"

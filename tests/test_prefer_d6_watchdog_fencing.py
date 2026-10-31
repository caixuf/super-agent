"""Prefer-D6 watchdog reconcile + attempt fencing harden (SQLite pool only).

AC: D6-1 finish-callback-loss → conditional terminal/requeue · D6-2 stale attempt
finish no-op · D6-3 silence alone ≠ terminal · D6-4 conditional terminal no overwrite ·
D6-5 outbox lease fence · D6-6 D2/D4 smoke.
BAN: Temporal / silence→done / D2 windows rewrite / D4 land rewrite.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agentcore.liveness_daemon import LivenessDaemon
from agentcore.liveness import LivenessLedger
from agentcore.pool import Pool, START_WINDOW_S, CANCEL_WINDOW_S


ORIGIN = {
    "profile": "prof-d6",
    "session": "sess-d6",
    "route": "route-d6",
    "chat": "oc_chat_d6",
    "thread": "om_thread_d6",
    "channel": "feishu",
}


def _plant_dead_runner(pool, job, *, attempt, lease_age=120.0):
    """Simulate finish-callback loss: running + fake dead PID + expired heartbeat."""
    past = time.time() - lease_age
    with pool.db() as db:
        db.execute(
            "UPDATE jobs SET status='running', attempt=?, heartbeat=?, "
            "pid=?, pid_start=?, containment_token=NULL WHERE id=?",
            (attempt, past, 1, "dead-boot:999999999", job),
        )


def test_d6_1_finish_callback_loss_watchdog_conditional_terminal(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d6-callback-loss", {"goal": "x"}, origin=ORIGIN,
                      replay_safe=False, max_tries=1)
    claimed, fd = pool.claim()
    attempt = claimed["attempt"]
    os.close(fd)  # release lock — worker crashed before finish
    _plant_dead_runner(pool, job, attempt=attempt, lease_age=120.0)

    report = pool.reconcile_watchdog(now=time.time(), lease=60)
    assert any(
        a["id"] == job and a["status"] == "failed" and a["reason"] == "finish_callback_lost"
        for a in report["actions"]
    )
    st = pool.status(job)
    assert st["status"] == "failed"
    result = json.loads(st["result"])
    assert result["reason"] == "finish_callback_lost"
    assert result.get("event") == "watchdog_finish_callback_lost"
    assert sum(1 for r in pool.receipts() if r.get("job") == job) == 1
    report2 = pool.reconcile_watchdog(now=time.time(), lease=60)
    assert not any(a["id"] == job for a in report2["actions"])
    assert sum(1 for r in pool.receipts() if r.get("job") == job) == 1


def test_d6_1_finish_callback_loss_replay_safe_requeue(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit(
        "d6-callback-requeue", {"goal": "x"}, origin=ORIGIN, replay_safe=True, max_tries=3,
    )
    claimed, fd = pool.claim()
    attempt = claimed["attempt"]
    os.close(fd)
    _plant_dead_runner(pool, job, attempt=attempt, lease_age=120.0)
    report = pool.reconcile_watchdog(now=time.time(), lease=60)
    assert any(
        a["id"] == job and a["status"] == "queued"
        and a["reason"] == "finish_callback_lost_requeue"
        for a in report["actions"]
    )
    st = pool.status(job)
    assert st["status"] == "queued"
    assert st["attempt"] is None
    assert len(pool.receipts()) == 0


def test_d6_2_stale_attempt_finish_noop_live_intact(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d6-stale-finish", {"goal": "x"}, origin=ORIGIN,
                      replay_safe=True, max_tries=5)
    first, fd1 = pool.claim()
    old_attempt = first["attempt"]
    os.close(fd1)
    # Dead PID identity (not pid NULL) so Prefer-D2 STARTING window does not fire;
    # expired heartbeat + lock free → claim can mint a new attempt.
    past = time.time() - 120
    with pool.db() as db:
        db.execute(
            "UPDATE jobs SET heartbeat=?, pid=?, pid_start=?, "
            "containment_token=NULL WHERE id=?",
            (past, 1, "dead-boot:999999999", job),
        )
    second, fd2 = pool.claim()
    try:
        new_attempt = second["attempt"]
        assert new_attempt != old_attempt
        assert pool.finish(job, old_attempt, True, {"stolen": True}) is False
        st = pool.status(job)
        assert st["status"] == "running"
        assert st["attempt"] == new_attempt
        assert pool.finish(job, new_attempt, True, {"ok": True}) is True
        assert pool.status(job)["status"] == "completed"
        result = json.loads(pool.status(job)["result"])
        assert result == {"ok": True}
    finally:
        os.close(fd2)


def test_d6_3_stall_silence_alone_not_terminal(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d6-silence", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    attempt = claimed["attempt"]
    past = time.time() - 120
    start = pool._process_start(os.getpid())
    with pool.db() as db:
        db.execute(
            "UPDATE jobs SET heartbeat=?, pid=?, pid_start=? WHERE id=?",
            (past, os.getpid(), start, job),
        )
    try:
        report = pool.reconcile_watchdog(now=time.time(), lease=60)
        assert not any(a["id"] == job for a in report["actions"])
        st = pool.status(job)
        assert st["status"] == "running"
        assert st["attempt"] == attempt
    finally:
        os.close(fd)

    # Lock free + live PID identity still matching → still no terminal (silence ≠ done).
    past = time.time() - 120
    start = pool._process_start(os.getpid())
    with pool.db() as db:
        db.execute(
            "UPDATE jobs SET status='running', attempt=?, heartbeat=?, pid=?, pid_start=? WHERE id=?",
            (attempt, past, os.getpid(), start, job),
        )
    report2 = pool.reconcile_watchdog(now=time.time(), lease=60)
    assert not any(a["id"] == job for a in report2["actions"])
    assert pool.status(job)["status"] == "running"


def test_d6_4_conditional_terminal_no_overwrite(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d6-cond-term", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    attempt = claimed["attempt"]
    assert pool.finish(job, attempt, True, {"first": True}) is True
    os.close(fd)
    st1 = pool.status(job)
    assert st1["status"] == "completed"
    first_result = json.loads(st1["result"])
    with pool.db() as db:
        ok = pool._terminal(db, job, "failed", {"second": True}, attempt=attempt)
        assert ok is False
    st2 = pool.status(job)
    assert st2["status"] == "completed"
    assert json.loads(st2["result"]) == first_result
    with pool.db() as db:
        row = db.execute("SELECT receipt FROM outbox WHERE job=?", (job,)).fetchone()
    assert json.loads(row[0])["status"] == "completed"


def test_d6_5_outbox_wrong_lease_cannot_ack_expired_reclaimable(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d6-outbox", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    assert pool.finish(job, claimed["attempt"], True, {"done": True})
    os.close(fd)
    claim = pool.claim_receipt(lease=30)
    assert claim is not None
    assert claim["receipt"]["job"] == job
    assert pool.acknowledge(job, "wrong-token") is False
    assert pool.acknowledge(job, claim["token"]) is True

    job2 = pool.submit("d6-outbox-reclaim", {"goal": "y"}, origin=ORIGIN)
    claimed2, fd2 = pool.claim()
    assert pool.finish(job2, claimed2["attempt"], True, {"done": True})
    os.close(fd2)
    claim2 = pool.claim_receipt(lease=30)
    token2 = claim2["token"]
    with pool.db() as db:
        db.execute(
            "UPDATE outbox SET lease_until=?, delivered=? WHERE job=?",
            (time.time() - 5, time.time() - 4, job2),
        )
    obs = pool.observe_outbox_reclaims(now=time.time())
    assert obs["reclaimable"] >= 1
    assert obs["delivered_missing_ack"] >= 1
    assert pool.acknowledge(job2, token2) is False
    again = pool.claim_receipt(lease=30)
    assert again is not None and again["receipt"]["job"] == job2
    assert again["token"] != token2
    assert pool.acknowledge(job2, again["token"]) is True


def test_d6_6_d2_enforce_windows_smoke(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d6-d2-smoke", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    past = time.time() - (START_WINDOW_S + 5)
    with pool.db() as db:
        db.execute("UPDATE jobs SET heartbeat=?, pid=NULL WHERE id=?", (past, job))
    try:
        report = pool.reconcile_watchdog(now=time.time(), lease=60)
        assert any(w["id"] == job and w["reason"] == "starting_timeout" for w in report["windows"])
        assert pool.status(job)["status"] == "failed"
    finally:
        os.close(fd)
    assert CANCEL_WINDOW_S > 0


def test_d6_6_d4_stalled_path_import_smoke():
    from agentcore.liveness_daemon import stalled_card_sink
    from agentcore.stalled_card_bridge import StalledCardBridge
    assert callable(stalled_card_sink)
    assert StalledCardBridge is not None


def test_d6_daemon_calls_reconcile_watchdog(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d6-daemon", {"goal": "x"}, origin=ORIGIN,
                      replay_safe=False, max_tries=1)
    claimed, fd = pool.claim()
    attempt = claimed["attempt"]
    os.close(fd)
    _plant_dead_runner(pool, job, attempt=attempt, lease_age=120.0)
    ledger = LivenessLedger(tmp_path / "live")
    events = []
    daemon = LivenessDaemon(ledger, events.append, interval=1.0, pool=pool)
    daemon.run_once()
    assert pool.status(job)["status"] == "failed"


def test_d6_7_no_temporal_import():
    import agentcore.pool as pool_mod
    import agentcore.liveness_daemon as daemon_mod
    for mod in (pool_mod, daemon_mod):
        src = Path(mod.__file__).read_text()
        assert "import temporal" not in src.lower()
        assert "from temporal" not in src.lower()
        assert "import hatchet" not in src.lower()
        assert "from hatchet" not in src.lower()


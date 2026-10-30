from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from agentcore.pool import Pool


def test_replay_safe_blocked_when_attempt_not_quiescent(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("task-unquiesced", {"work": "step"}, replay_safe=True, max_tries=3)
    claimed, fd = pool.claim()
    assert claimed["id"] == job
    os.close(fd)

    # Simulate execution failure where descendant processes survived (unquiesced)
    res = {
        "ok": False,
        "error": "attempt_not_quiescent",
        "attempt_quiescent": False,
        "attempt_token_scan_empty": False,
        "survivors": [99999]
    }
    finished = pool.finish(job, claimed["attempt"], False, res)
    assert finished is True

    # Must NOT return to queued; must be permanently failed with unquiesced reason
    st = pool.status(job)
    assert st["status"] == "failed"
    assert "replay_aborted_unquiesced" in st["result"]

    # Subsequent claims must find nothing to execute
    assert pool.claim() is None


def test_replay_safe_allowed_when_cleanly_quiesced(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("task-clean-retry", {"work": "step"}, replay_safe=True, max_tries=3)
    claimed, fd = pool.claim()
    assert claimed["id"] == job
    os.close(fd)

    # Simulate execution failure where attempt cleanly stopped without surviving processes
    res = {
        "ok": False,
        "error": "application_error",
        "attempt_quiescent": True,
        "attempt_token_scan_empty": True,
        "survivors": []
    }
    finished = pool.finish(job, claimed["attempt"], False, res)
    assert finished is True

    # Safely returned to queued
    st = pool.status(job)
    assert st["status"] == "queued"

    # Next claim can successfully re-claim it as attempt 2
    claimed2, fd2 = pool.claim()
    assert claimed2["id"] == job
    assert claimed2["tries"] == 2
    os.close(fd2)


def test_claim_expired_running_blocks_unquiesced_replay(tmp_path, monkeypatch):
    pool = Pool(tmp_path)
    job = pool.submit("task-expired-unquiesced", {"work": "step"}, replay_safe=True, max_tries=3)
    claimed, fd = pool.claim()
    assert claimed["id"] == job
    os.close(fd)

    # Simulate lease expiry by backdating heartbeat
    with pool.db() as db:
        db.execute("UPDATE jobs SET heartbeat=? WHERE id=?", (time.time() - 100, job))

    token = claimed["containment_token"]
    import agentcore.colleague_containment as containment
    monkeypatch.setattr(containment, "tagged", lambda tok: [99999] if tok == token else [])
    monkeypatch.setattr(containment, "sweep", lambda tok, **kw: {
        "token": tok, "signalled": [99999], "survivors": [99999],
        "quiescent": False, "token_scan_empty": False,
        "proof_scope": "token_scan", "observed_at": time.time()
    })

    # Attempting to claim must NOT spawn a concurrent attempt; it must terminate the job
    next_claim = pool.claim(lease=10)
    assert next_claim is None
    st = pool.status(job)
    assert st["status"] == "failed"
    assert "interrupted; replay unavailable" in st["result"]

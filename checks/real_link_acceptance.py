#!/usr/bin/env python3
"""Real pipeline acceptance script for local environment.

Verifies:
1. Plain Turn: Real Hermes CLI call against local model provider.
2. Queue Interleaving: Message insertion during long-running tasks.
3. Restart Recovery: Clean recovery without unquiesced duplicate replay.
4. Origin Attribution: Completed receipts strictly routed to submitting session.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from agentcore.pool import Pool


#: The profile this acceptance drives, read from the environment so the check carries no
#: personal profile name: a hardcoded one is exactly the "clone it and it still won't run
#: for you" shape the repo-level neutrality ratchet is about.
PROFILE = os.environ.get("HERMES_ACCEPTANCE_PROFILE", "default")


def test_plain_turn() -> dict:
    cmd = ["hermes", "-p", PROFILE, "chat", "-Q", "-q", "请回复 ACCEPTANCE_OK"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = proc.stdout.strip()
        ok = proc.returncode == 0 and "ACCEPTANCE_OK" in output
        return {"ok": ok, "output": output[:200], "exit_code": proc.returncode}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def test_queue_interleaving(pool_dir: Path) -> dict:
    pool = Pool(pool_dir)
    # Task 1: Long running simulation
    job1 = pool.submit("long-task-1", {"action": "sleep_step", "duration": 3}, replay_safe=True)
    claimed1, fd1 = pool.claim()
    assert claimed1["id"] == job1

    # Interleaving: Submit high-priority task 2 while task 1 is running
    job2 = pool.submit("interleave-task-2", {"action": "urgent_message"}, replay_safe=False)

    # Finish task 1 cleanly
    pool.finish(job1, claimed1["attempt"], True, {"status": "done"})
    os.close(fd1)

    # Next claim must pick up the interleaved job2
    claimed2, fd2 = pool.claim()
    ok = (claimed2 is not None and claimed2["id"] == job2)
    if claimed2:
        pool.finish(job2, claimed2["attempt"], True, {"status": "interleaved_done"})
        os.close(fd2)

    return {"ok": ok, "job1": job1, "job2": job2}


def test_restart_recovery(pool_dir: Path) -> dict:
    pool = Pool(pool_dir)
    # Submit job that runs then experiences sudden worker crash / restart
    job = pool.submit("crash-recovery-job", {"work": "resilient"}, replay_safe=True, max_tries=3)
    claimed, fd = pool.claim()
    token = claimed["containment_token"]
    os.close(fd)

    # Simulate restart by backdating heartbeat
    with pool.db() as db:
        db.execute("UPDATE jobs SET heartbeat=? WHERE id=?", (time.time() - 200, job))

    # Clean crash (no surviving processes)
    claimed_again, new_fd = pool.claim(lease=10)
    recovered_ok = (claimed_again is not None and claimed_again["id"] == job and claimed_again["tries"] == 2)
    if new_fd is not None:
        pool.finish(job, claimed_again["attempt"], True, {"recovered": True})
        os.close(new_fd)

    return {"ok": recovered_ok, "job": job}


def test_origin_attribution(pool_dir: Path) -> dict:
    pool = Pool(pool_dir)
    origin_a = {"profile": PROFILE, "session": "session-user-alpha",
                "route": "agent:%s:dm:user-a" % PROFILE}
    origin_b = {"profile": PROFILE, "session": "session-user-beta",
                "route": "agent:%s:dm:user-b" % PROFILE}

    job_a = pool.submit("job-user-a", {"msg": "hello from alpha"}, origin=origin_a)
    job_b = pool.submit("job-user-b", {"msg": "hello from beta"}, origin=origin_b)

    # Execute both jobs
    while True:
        c = pool.claim()
        if not c:
            break
        row, fd = c
        pool.finish(row["id"], row["attempt"], True, {"delivered_text": "response to " + row["id"]})
        os.close(fd)

    # Verify origin isolation on receipts
    receipts_a = pool.claim_receipt(origin_filter=lambda o: o == origin_a)
    assert receipts_a is not None
    assert receipts_a["receipt"]["job"] == job_a
    pool.acknowledge(job_a, token=receipts_a["token"])

    # Session alpha cannot claim session beta's receipt
    assert pool.claim_receipt(origin_filter=lambda o: o == origin_a) is None

    # Session beta receives its own receipt
    receipts_b = pool.claim_receipt(origin_filter=lambda o: o == origin_b)
    assert receipts_b is not None
    assert receipts_b["receipt"]["job"] == job_b
    pool.acknowledge(job_b, token=receipts_b["token"])

    return {"ok": True, "job_a": job_a, "job_b": job_b}


def main():
    print("=== Antigravity Real Pipeline Acceptance ===")
    results = {}

    print("1. Running Plain Turn Acceptance...")
    res_plain = test_plain_turn()
    print("   Plain Turn:", "PASS" if res_plain["ok"] else "FAIL", res_plain.get("output", ""))
    results["plain_turn"] = res_plain

    with tempfile.TemporaryDirectory(prefix="agentcore-acceptance-") as tmp:
        pool_dir = Path(tmp)

        print("2. Running Queue Interleaving Acceptance...")
        res_interleaving = test_queue_interleaving(pool_dir)
        print("   Queue Interleaving:", "PASS" if res_interleaving["ok"] else "FAIL")
        results["queue_interleaving"] = res_interleaving

        print("3. Running Restart Recovery Acceptance...")
        res_restart = test_restart_recovery(pool_dir)
        print("   Restart Recovery:", "PASS" if res_restart["ok"] else "FAIL")
        results["restart_recovery"] = res_restart

        print("4. Running Origin Attribution Acceptance...")
        res_origin = test_origin_attribution(pool_dir)
        print("   Origin Attribution:", "PASS" if res_origin["ok"] else "FAIL")
        results["origin_attribution"] = res_origin

    all_passed = all(r["ok"] for r in results.values())
    print("\nSummary:", "ALL PASSED" if all_passed else "SOME FAILED")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

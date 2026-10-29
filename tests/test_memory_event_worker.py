"""T2 worker tests: lease, retry budget, provider outage, dedupe.

All cases use a temp queue and an injected fake extractor; no production DB and
no network/model call is touched.
"""

import pytest

from agentcore.memory import event_worker, runtime_bridge
from agentcore.memory.event_worker import MemoryEventWorker


def _queue(tmp_path):
    return runtime_bridge.TurnEventQueue(tmp_path / "memory_turn_events.db")


def _event(turn_id="turn-1", **overrides):
    turn = {
        "profile": "profile-a",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "session_id": "session-1",
        "turn_id": turn_id,
        "user_message": "决定把阈值门限改成 3.0% 后退出自动模式",
        "assistant_reply": "已记录",
    }
    turn.update(overrides)
    return runtime_bridge.build_turn_event(**turn)


def _enqueue(queue, event):
    result = queue.enqueue(event)
    assert result["enqueued"] is True, result
    return event["idempotency_key"]


def _row(queue, key):
    conn = queue._connect()
    try:
        return conn.execute(
            "SELECT * FROM turn_events WHERE idempotency_key = ?", (key,)).fetchone()
    finally:
        conn.close()


class _Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def _worker(queue, clock, runner=None, **overrides):
    options = {
        "queue": queue,
        "clock": clock,
        "owner": "worker-1",
        "lease_seconds": 30,
        "max_attempts": 3,
        "backoff_base": 10,
        "backoff_max": 60,
    }
    options.update(overrides)
    return MemoryEventWorker(runner=runner or (lambda *args, **kwargs: {"ok": True}), **options)


def _seen_runner(seen):
    def runner(user_message, assistant_reply, turn_status="completed"):
        seen.append((user_message, assistant_reply, turn_status))
        return {"ok": True, "written": 1}
    return runner


def test_claim_leases_event_once(tmp_path):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)
    worker = _worker(queue, clock)

    claims = worker.claim(1)

    assert len(claims) == 1
    assert claims[0]["idempotency_key"] == key
    assert claims[0]["attempts"] == 1
    assert claims[0]["owner"] == "worker-1"
    assert queue.pending() == []
    assert queue.count("processing") == 1

    row = _row(queue, key)
    assert row["state"] == "processing"
    assert row["lease_owner"] == "worker-1"
    assert row["lease_until"] == 1030.0
    assert row["attempts"] == 1

    # 同一事件不能被第二个 worker / 第二次调用再领走。
    assert worker.claim(1) == []


def test_success_marks_done_and_calls_runner_with_completed_status(tmp_path):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)
    seen = []
    worker = _worker(queue, clock, runner=_seen_runner(seen))

    outcome = worker.process_one()

    assert outcome["processed"] is True
    assert outcome["state"] == "done"
    assert outcome["attempts"] == 1
    assert seen == [("决定把阈值门限改成 3.0% 后退出自动模式", "已记录", "completed")]

    row = _row(queue, key)
    assert row["state"] == "done"
    assert row["processed_at"] == 1000.0
    assert row["lease_owner"] is None
    assert row["lease_until"] is None
    assert row["last_error"] is None
    assert queue.pending() == []
    assert queue.count("done") == 1


def test_expired_lease_is_reclaimed_and_processed(tmp_path):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)
    seen = []
    worker = _worker(queue, clock, runner=_seen_runner(seen), lease_seconds=10)

    assert worker.claim(1)[0]["attempts"] == 1
    clock.advance(11)  # 租约到期：进程可能已经崩溃

    reclaimed = worker.reclaim_stale()
    assert reclaimed == {"reclaimed": 1, "failed": 0}
    assert _row(queue, key)["state"] == "pending"

    outcome = worker.process_one()
    assert outcome["processed"] is True and outcome["state"] == "done"
    assert len(seen) == 1
    assert _row(queue, key)["attempts"] == 2


def test_provider_unavailable_retries_after_backoff(tmp_path):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)
    outcomes = [
        {"ok": False, "written": 0, "skipped": True, "retryable": True,
         "reason": "provider_unavailable", "facts": []},
        {"ok": True, "written": 1},
    ]

    def runner(*args, **kwargs):
        return outcomes.pop(0)

    worker = _worker(queue, clock, runner=runner, backoff_base=30)

    first = worker.process_one()
    assert first["processed"] is True
    assert first["state"] == "pending"
    assert first["retryable"] is True
    assert first["next_attempt_at"] == 1030.0

    row = _row(queue, key)
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] == 1030.0
    assert row["last_error"] == "provider_unavailable"
    assert row["lease_owner"] is None

    # 退避窗口内不重新领取，事件仍在队列里，不是被丢掉。
    assert worker.process_one() == {"processed": False, "reason": "no_claimable_event"}
    assert queue.pending()  # pending() 本身不知道 next_attempt_at，但 worker 会挡

    clock.advance(30)
    second = worker.process_one()
    assert second["state"] == "done"
    assert _row(queue, key)["attempts"] == 2


def test_retry_budget_exhaustion_marks_failed(tmp_path):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)

    def runner(*args, **kwargs):
        return {"ok": False, "retryable": True, "reason": "provider_unavailable"}

    worker = _worker(queue, clock, runner=runner, max_attempts=2, backoff_base=10)

    assert worker.process_one()["state"] == "pending"
    clock.advance(10)
    second = worker.process_one()
    assert second["state"] == "failed"
    assert second["retryable"] is True
    assert second["error"] == "provider_unavailable"

    row = _row(queue, key)
    assert row["state"] == "failed"
    assert row["attempts"] == 2
    assert row["processed_at"] == 1010.0
    assert row["lease_owner"] is None
    assert queue.pending() == []

    # 预算耗尽后不再尝试。
    assert worker.process_one() == {"processed": False, "reason": "no_claimable_event"}


def test_expired_lease_with_exhausted_budget_fails_instead_of_looping(tmp_path):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)
    worker = _worker(queue, clock, lease_seconds=5, max_attempts=1)

    assert worker.claim(1)[0]["attempts"] == 1
    clock.advance(6)

    assert worker.reclaim_stale() == {"reclaimed": 0, "failed": 1}
    row = _row(queue, key)
    assert row["state"] == "failed"
    assert row["last_error"] == "lease_expired"
    assert worker.claim(1) == []


def test_duplicate_turn_is_not_processed_twice(tmp_path):
    queue = _queue(tmp_path)
    event = _event()
    key = _enqueue(queue, event)
    duplicate = queue.enqueue(event)
    assert duplicate["enqueued"] is False and duplicate["deduped"] is True

    clock = _Clock(1000.0)
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return {"ok": True, "written": 0, "skipped": True,
                "reason": "no_decision_signal"}

    worker = _worker(queue, clock, runner=runner)

    assert worker.process_one()["state"] == "done"
    assert worker.process_one() == {"processed": False, "reason": "no_claimable_event"}
    assert len(calls) == 1
    assert queue.count() == 1
    assert queue.count("done") == 1
    assert _row(queue, key)["attempts"] == 1


def test_runner_exception_is_retryable_not_a_crash(tmp_path):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)

    def runner(*args, **kwargs):
        raise RuntimeError("model backend exploded")

    worker = _worker(queue, clock, runner=runner)

    outcome = worker.process_one()

    assert outcome["processed"] is True
    assert outcome["state"] == "pending"
    assert outcome["retryable"] is True
    assert "RuntimeError: model backend exploded" in outcome["error"]
    row = _row(queue, key)
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert "RuntimeError" in row["last_error"]


def test_run_pending_drains_until_no_claimable_event(tmp_path):
    queue = _queue(tmp_path)
    for index in range(3):
        _enqueue(queue, _event(turn_id="turn-%d" % index))
    clock = _Clock(1000.0)
    worker = _worker(queue, clock)

    summary = worker.run_pending()

    assert summary == {"processed": 3, "done": 3, "retried": 0, "failed": 0}
    assert queue.count("done") == 3
    assert queue.pending() == []


# ---------------------------------------------------------------------------
# 与真实 memory_trigger.run 的集成（注入假 LLM，不联网）
# ---------------------------------------------------------------------------

def test_default_runner_provider_unavailable_is_retryable(tmp_path, configured):
    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)

    with configured(tmp_path, llm_fn=None):
        worker = MemoryEventWorker(queue=queue, clock=clock, owner="worker-1",
                                   lease_seconds=30, max_attempts=3, backoff_base=10)
        outcome = worker.process_one()

    assert outcome["state"] == "pending"
    assert outcome["retryable"] is True
    assert "provider_unavailable" in outcome["error"]
    row = _row(queue, key)
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] == 1010.0


def test_default_runner_with_fake_llm_marks_done(tmp_path, configured):
    def fake_llm(messages, **kwargs):
        return {"content": '{"facts": []}'}

    queue = _queue(tmp_path)
    key = _enqueue(queue, _event())
    clock = _Clock(1000.0)

    with configured(tmp_path, llm_fn=fake_llm):
        worker = MemoryEventWorker(queue=queue, clock=clock, owner="worker-1",
                                   lease_seconds=30, backoff_base=10)
        outcome = worker.process_one()

    assert outcome["processed"] is True
    assert outcome["state"] == "done"
    assert outcome["result"]["ok"] is True
    assert outcome["result"]["reason"] == "no_fact_extracted"
    assert _row(queue, key)["state"] == "done"

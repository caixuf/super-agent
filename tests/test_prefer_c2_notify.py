"""Prefer-C2 notify → completion_delivery / Feishu side-channel (thin).

AC: C2-1 terminal ≤1 notify no LLM · C2-2 payload · C2-3 delivery+unowned ·
C2-4 same card_id · C2-5 failure statuses · C2-7 C1 unchanged (separate suite).
"""
from __future__ import annotations

import os

import pytest

from agentcore.card_emit import CardEmitter
from agentcore.card_store import CardStore
from agentcore.completion_delivery import (
    AGENT_TURN_COMPLETE,
    MAX_SUMMARY_CHARS,
    build_message,
    deliver_once,
    is_terminal_status,
    kick_deliver_once,
    notify_on_terminal,
    origin_envelope_fields,
)
from agentcore.feishu_transport import CardNotifyBridge
from agentcore.hitl_bridge import HitlBridge, notify_approval_requested
from agentcore.liveness_daemon import terminal_delivery_sink
from agentcore.ops_alerts import DeliveryReceipt
from agentcore.pool import Pool
from agentcore.task_card import CardState

ORIGIN = {
    "profile": "prof-1",
    "session": "sess-1",
    "route": "route-1",
    "chat": "oc_chat_1",
    "thread": "om_thread_9",
    "turn": "42",
    "channel": "feishu",
}


class Transport:
    def __init__(self, *, accepted=True, receipt_id="rc-1", event_id="echo",
                 raise_exc=None):
        self.calls = []
        self.accepted = accepted
        self.receipt_id = receipt_id
        self.event_id = event_id
        self.raise_exc = raise_exc
        self.llm_calls = 0  # Prefer-C2: must stay 0

    def deliver(self, address, message, *, idempotency_key):
        self.calls.append({
            "address": dict(address),
            "message": dict(message),
            "idempotency_key": idempotency_key,
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        event = idempotency_key if self.event_id == "echo" else self.event_id
        return DeliveryReceipt(accepted=self.accepted, event_id=event,
                               receipt_id=self.receipt_id)

    def spawn_llm(self, *a, **k):  # never called by Prefer-C2 path
        self.llm_calls += 1
        raise AssertionError("Prefer-C2 must not spawn an LLM turn")


class _FakeCardTransport:
    def __init__(self):
        self.emits = []

    def emit(self, handle, view, *, final=False):
        self.emits.append((handle.task_id, handle.message_id, handle.card_id,
                           view, final))


def finished_job(pool, key="task-1", *, result=None, origin=ORIGIN, ok=True):
    job = pool.submit(key, {"goal": "analyse"}, origin=origin)
    claimed, fd = pool.claim()
    try:
        assert pool.finish(
            claimed["id"], claimed["attempt"], ok,
            {"summary": "the work is done"} if result is None else result,
        )
    finally:
        os.close(fd)
    return job


# --------------------------------------------------------------------------- #
# C2-1 / C2-8 — terminal ≤1 notify, no LLM, no second agent
# --------------------------------------------------------------------------- #

def test_terminal_finish_triggers_one_side_channel_notify_no_llm(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool)
    outcome = notify_on_terminal(pool, transport, status="completed")
    assert outcome["status"] == "delivered"
    assert outcome["task_id"] == job
    assert len(transport.calls) == 1
    assert transport.llm_calls == 0
    # Second kick after ack → empty (≤1 successful notify)
    assert notify_on_terminal(pool, transport, status="completed")["status"] == "empty"
    assert len(transport.calls) == 1


def test_non_terminal_status_skips_without_delivery(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    finished_job(pool)
    outcome = notify_on_terminal(pool, transport, status="running")
    assert outcome["status"] == "skipped"
    assert transport.calls == []


def test_kick_deliver_once_is_thin_alias(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool)
    assert kick_deliver_once(pool, transport)["status"] == "delivered"
    assert transport.calls[0]["idempotency_key"] == job


def test_is_terminal_status_maps_done_and_pool():
    assert is_terminal_status("completed")
    assert is_terminal_status("failed")
    assert is_terminal_status("cancelled")
    assert is_terminal_status("done")
    assert is_terminal_status(CardState.DONE)
    assert is_terminal_status(CardState.FAILED)
    assert not is_terminal_status("running")
    assert not is_terminal_status("waiting_user")


# --------------------------------------------------------------------------- #
# C2-2 — payload shape (Codex agent-turn-complete subset)
# --------------------------------------------------------------------------- #

def test_payload_includes_type_origin_and_bounded_summary(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool, result={"summary": "x" * 5000})
    deliver_once(pool, transport)
    msg = transport.calls[0]["message"]
    assert msg["type"] == AGENT_TURN_COMPLETE
    assert msg["task_id"] == job
    assert msg["status"] == "completed"
    assert msg["session"] == "sess-1"
    assert msg["chat"] == "oc_chat_1"
    assert msg["thread"] == "om_thread_9"
    assert msg["thread_id"] == "om_thread_9"
    assert msg["turn_id"] == "42"
    assert msg["truncated"] is True
    assert len(msg["summary"]) <= MAX_SUMMARY_CHARS + 64


def test_origin_envelope_fields_never_invent():
    assert origin_envelope_fields(None) == {}
    assert origin_envelope_fields({"ambient": "nope"}) == {}
    fields = origin_envelope_fields({"chat": " c ", "thread": "t"})
    assert fields == {"chat": "c", "thread": "t"}


def test_build_message_envelope_with_origin():
    msg = build_message(
        {"job": "a" * 32, "status": "failed", "result": {"reason": "boom"}},
        origin=ORIGIN,
    )
    rendered = msg.render()
    assert rendered["type"] == AGENT_TURN_COMPLETE
    assert rendered["status"] == "failed"
    assert rendered["summary"] == "boom"
    assert rendered["chat"] == "oc_chat_1"


# --------------------------------------------------------------------------- #
# C2-3 — completion_delivery + unowned refuse-guess + ack-on-acceptance
# --------------------------------------------------------------------------- #

def test_unowned_refuses_guess_and_stays_pending(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool, origin=None)
    outcome = notify_on_terminal(pool, transport, status="completed")
    assert outcome["status"] == "unowned"
    assert transport.calls == []
    assert pool.delivery_record(job)["acknowledged"] == 0


def test_no_transport_unavailable_not_fake_delivered(tmp_path):
    pool = Pool(tmp_path)
    finished_job(pool)
    outcome = notify_on_terminal(pool, None, status="failed")
    assert outcome == {"status": "unavailable", "reason": "no transport configured"}


# --------------------------------------------------------------------------- #
# C2-4 — same card_id terminal patch
# --------------------------------------------------------------------------- #

def test_same_card_terminal_notify_via_bridge(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t-done", "c1", "m1", card_id="card-kit-9")
    fake = _FakeCardTransport()
    emitter = CardEmitter(store, "t-done", fake)
    emitter.open(phase="work")
    pool, transport = Pool(tmp_path / "q"), Transport()
    finished_job(pool, key="t-done")
    bridge = CardNotifyBridge(emitter, pool=pool, transport=transport)
    result = bridge.notify_terminal(CardState.DONE, detail="完成")
    assert result["same_card"] is True
    assert result["message_id"] == "m1"
    assert result["card_id"] == "card-kit-9"
    assert result["delivery"]["status"] == "delivered"
    # All emits stayed on the same message_id.
    assert all(e[1] == "m1" for e in fake.emits)
    assert any(e[4] is True for e in fake.emits)  # final frame


# --------------------------------------------------------------------------- #
# C2-5 — failure statuses observable
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("factory,expected", [
    (lambda: Transport(accepted=False), "delivery_rejected"),
    (lambda: Transport(raise_exc=ConnectionResetError("x")), "delivery_unknown"),
    (lambda: None, "unavailable"),
])
def test_delivery_failure_statuses_observable(tmp_path, factory, expected):
    pool = Pool(tmp_path)
    finished_job(pool)
    transport = factory()
    outcome = notify_on_terminal(pool, transport, status="failed")
    assert outcome["status"] == expected


# --------------------------------------------------------------------------- #
# Idempotency / at-least-once
# --------------------------------------------------------------------------- #

def test_lost_ack_redelivers_at_least_once(tmp_path, monkeypatch):
    pool, transport = Pool(tmp_path), Transport()
    finished_job(pool)

    def boom(*a, **k):
        raise OSError("db")

    monkeypatch.setattr(pool, "acknowledge", boom)
    assert notify_on_terminal(pool, transport, status="completed",
                              lease=0.0001)["status"] == "ack_unknown"
    monkeypatch.undo()
    from agentcore.pool import Pool as PoolCls
    monkeypatch.setattr(pool, "acknowledge", PoolCls.acknowledge.__get__(pool, PoolCls))
    assert notify_on_terminal(pool, Transport(), status="completed")["status"] == "delivered"


# --------------------------------------------------------------------------- #
# C2-7 thin shared helper — approval-requested without C1 UX rework
# --------------------------------------------------------------------------- #

def test_notify_approval_requested_reuses_wait_user(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t-hitl", "c1", "m1")
    fake = _FakeCardTransport()
    emitter = CardEmitter(store, "t-hitl", fake)
    emitter.open(phase="work")
    view = notify_approval_requested(emitter, detail="请确认", kind="waiting_user")
    assert view is not None
    assert view.state == "waiting_user"
    assert fake.emits[-1][1] == "m1"


def test_hitl_bridge_approve_deny_still_works(tmp_path):
    """C1 regression smoke: Approve/Deny path untouched by C2 helper."""
    pool = Pool(tmp_path)
    store = CardStore(tmp_path / "cards.db")
    job = pool.submit("hitl-1", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    os.close(fd)
    # Park as waiting_user without finish.
    assert pool.wait_for_user(job)
    bridge = HitlBridge(pool, store)
    store.bind(job, "c1", "m1")
    out = bridge.handle_action(task_id=job, action="approve")
    assert out["ok"] is True
    assert out["action"] == "approve"


# --------------------------------------------------------------------------- #
# Liveness terminal sink kick
# --------------------------------------------------------------------------- #

def test_liveness_terminal_sink_kicks_delivery(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool)
    sink = terminal_delivery_sink(pool, transport)
    assert sink({"state": "running", "task_id": job}) is None
    outcome = sink({"state": "done", "task_id": job})
    assert outcome["status"] == "delivered"
    assert transport.llm_calls == 0

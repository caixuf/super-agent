"""Prefer-D3 durable Tab follow-up inbox + fast-ack SLO (thin).

AC: D3-0 tip after D2 · D3-1 enqueue→restart→peek · D3-2 no second exec ·
D3-3 fake-clock ≤2000 or slo_met=false · D3-4 IRREVERSIBLE · D3-5 durable
before ok-ack · D3-6 card queued count · D3-7 C1–D2 light regression ·
schema D3-S0–S5 held.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentcore.barge_in_control import (
    CONTROL_FAST_BUDGET_MS,
    BargeInController,
    ControlAck,
    FollowUpInbox,
    classify_control,
)
from agentcore.card_emit import CardEmitter
from agentcore.card_store import CardStore
from agentcore.follow_up_store import FollowUpStore, KINDS, STATES
from agentcore.pool import Pool
from agentcore.task_progress import IRREVERSIBLE_PHASES


ORIGIN = {
    "profile": "prof-d3",
    "session": "sess-d3",
    "route": "route-d3",
    "chat": "oc_chat_d3",
    "thread": "om_thread_d3",
    "channel": "feishu",
}


class _FakeCardTransport:
    def __init__(self):
        self.emits = []

    def emit(self, handle, view, *, final=False):
        self.emits.append((handle.task_id, view, final))


class _FakeMono:
    """Deterministic monotonic clock (seconds)."""

    def __init__(self, start: float = 100.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance_ms(self, ms: float) -> None:
        self.t += ms / 1000.0


def test_d3_s_schema_kinds_states_locked():
    assert KINDS == frozenset({"follow_up", "urgent_follow_up", "replan"})
    assert STATES == frozenset({
        "queued", "draining", "applied", "dropped", "blocked_irreversible",
    })
    assert CONTROL_FAST_BUDGET_MS == 2000.0


def test_d3_1_enqueue_restart_peek(tmp_path):
    store = FollowUpStore(tmp_path / "fu")
    item = store.enqueue("t-d3", "please add a footer", kind="follow_up", owner="o1")
    assert item["state"] == "queued"
    assert item["text"] == "please add a footer"
    assert "T" in item["created_at"] and item["created_at"].endswith("Z")
    # Restart: new store instance, same db.
    store2 = FollowUpStore(tmp_path / "fu")
    peek = store2.peek("t-d3")
    assert len(peek) == 1
    assert peek[0]["id"] == item["id"]
    assert peek[0]["state"] == "queued"
    # Mid-drain crash recovers to queued.
    drained = store2.drain("t-d3", current_phase="build")
    assert len(drained) == 1 and drained[0]["state"] == "draining"
    store3 = FollowUpStore(tmp_path / "fu")
    peek3 = store3.peek("t-d3")
    assert len(peek3) == 1 and peek3[0]["state"] == "queued"


def test_d3_1_idempotent_on_id(tmp_path):
    store = FollowUpStore(tmp_path / "fu")
    a = store.enqueue("t1", "one", id="fixed-id-1")
    b = store.enqueue("t1", "two-ignored", id="fixed-id-1")
    assert a["id"] == b["id"] == "fixed-id-1"
    assert b["text"] == "one"
    assert store.queued_count("t1") == 1


def test_d3_2_no_second_exec(tmp_path):
    store = FollowUpStore(tmp_path / "fu")
    inbox = FollowUpInbox(store=store)
    pool = Pool(tmp_path / "pool")
    ctrl = BargeInController(pool=pool, inbox=inbox, follow_up_store=store)
    out = ctrl.handle(task_id="job-1", text="补充标题", owner="u")
    assert out["ok"] is True
    assert out["second_exec"] is False
    assert out["durable_dispatch_submit"] is False
    assert ctrl._llm_calls == 0
    # enqueue/status/pause/cancel stay submit-free
    st = ctrl.handle(task_id="job-1", action="/status")
    assert st["second_exec"] is False and st["durable_dispatch_submit"] is False


def test_d3_3_fake_clock_slo(tmp_path):
    store = FollowUpStore(tmp_path / "fu")
    mono = _FakeMono(0.0)
    ctrl = BargeInController(
        inbox=FollowUpInbox(store=store),
        follow_up_store=store,
        mono_clock=mono,
    )

    def sink_advance(ack: ControlAck):
        # Simulate host work after enqueue, before ack return.
        pass

    ctrl.ack_sink = sink_advance
    # Fast path: advance 100ms between handle start and emit.
    # We advance inside a custom mono that ticks on each call after t0.
    calls = {"n": 0}

    def ticking():
        calls["n"] += 1
        # first call = t0; subsequent = +100ms
        return 0.0 if calls["n"] == 1 else 0.1

    ctrl2 = BargeInController(
        inbox=FollowUpInbox(store=store),
        follow_up_store=store,
        mono_clock=ticking,
    )
    out = ctrl2.handle(task_id="t-fast", text="补充")
    assert out["ack_latency_ms"] is not None
    assert out["ack_latency_ms"] <= CONTROL_FAST_BUDGET_MS
    assert out["slo_met"] is True
    assert out["slo_class"] == "control_fast"
    assert out["ack_id"]
    assert out["ack_emitted_at"].endswith("Z")

    # Slow path: latency > 2000ms → slo_met=false, still acks.
    calls2 = {"n": 0}

    def slow():
        calls2["n"] += 1
        return 0.0 if calls2["n"] == 1 else 2.5  # 2500ms

    ctrl3 = BargeInController(
        inbox=FollowUpInbox(store=store),
        follow_up_store=store,
        mono_clock=slow,
    )
    out3 = ctrl3.handle(task_id="t-slow", text="补充慢")
    assert out3["ok"] is True  # still ack
    assert out3["ack_latency_ms"] > CONTROL_FAST_BUDGET_MS
    assert out3["slo_met"] is False


def test_d3_4_irreversible_drain(tmp_path):
    store = FollowUpStore(tmp_path / "fu")
    store.enqueue("t-irr", "do not preempt", kind="follow_up")
    phase = next(iter(IRREVERSIBLE_PHASES))
    drained = store.drain("t-irr", current_phase=phase)
    assert drained == []
    peek = store.peek("t-irr")
    assert len(peek) == 1
    assert peek[0]["state"] == "blocked_irreversible"
    assert peek[0]["blocked_phase"] == phase
    # stay queued option when mark_blocked=False
    store.enqueue("t-irr2", "keep queued", kind="follow_up")
    drained2 = store.drain("t-irr2", current_phase=phase, mark_blocked=False)
    assert drained2 == []
    peek2 = store.peek("t-irr2")
    assert peek2[0]["state"] == "queued"


def test_d3_5_durable_before_ok_ack(tmp_path):
    """Enqueue hits the ledger before ack_sink sees the ControlAck."""
    store = FollowUpStore(tmp_path / "fu")
    order = []

    class OrderStore(FollowUpStore):
        def enqueue(self, *a, **k):
            order.append("durable")
            return super().enqueue(*a, **k)

    ost = OrderStore(tmp_path / "fu2")

    def sink(ack):
        order.append("ack")
        # Ledger must already hold the item.
        assert ost.queued_count(ack.task_id) >= 1

    ctrl = BargeInController(
        inbox=FollowUpInbox(store=ost),
        follow_up_store=ost,
        ack_sink=sink,
    )
    out = ctrl.handle(task_id="t-order", text="补充先落盘")
    assert order == ["durable", "ack"]
    assert out["ok"] is True
    assert out["detail"]["durable"] is True


def test_d3_6_card_queued_count_from_durable_peek(tmp_path):
    store = FollowUpStore(tmp_path / "fu")
    store.enqueue("card-task", "one")
    store.enqueue("card-task", "two")
    cards = CardStore(tmp_path / "cards")
    cards.bind("card-task", "c1", "m1")
    transport = _FakeCardTransport()
    emitter = CardEmitter(
        cards, "card-task", transport, follow_up_store=store, clock=lambda: 1.0,
    )
    emitter.open(phase="running")
    view = emitter.mark_queued_follow_up(detail="queued x2", count=99, task_id="card-task")
    assert view is not None
    assert emitter.durable_queued_follow_up_count() == 2
    assert emitter.durable_queued_follow_up_count("card-task") == 2


def test_d3_7_light_regression_classify_and_control_ack_shape(tmp_path):
    # C1/C2/D1/D2 orthognal: classify + ControlAck D1 flags still hold.
    d = classify_control(action="/status")
    assert d.verb.value == "status"
    d2 = classify_control("补充一下")
    assert d2.verb.value == "follow_up"
    store = FollowUpStore(tmp_path / "fu")
    ctrl = BargeInController(inbox=FollowUpInbox(store=store), follow_up_store=store)
    out = ctrl.handle(task_id="reg", text="补充")
    assert out["second_exec"] is False
    assert out["durable_dispatch_submit"] is False
    assert "ack_id" in out and "slo_met" in out
    # Drain yields items for ONE subsequent turn (not a second submit).
    items = ctrl.inbox.drain("reg", current_phase="build")
    assert len(items) == 1
    assert items[0]["state"] == "draining"


def test_d3_text_clip_and_owner_clip(tmp_path):
    store = FollowUpStore(tmp_path / "fu")
    big = "x" * 5000
    item = store.enqueue("t", big, owner="o" * 1000)
    assert len(item["text"]) == 2000
    assert len(item["owner"]) == 512

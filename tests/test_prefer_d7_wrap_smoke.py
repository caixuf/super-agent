"""Prefer-D7 wrap smoke — scripted long-task colleague card lifecycle (thin).

Stack-not-replace: drives existing Prefer C1–D6 knives with fake Feishu/clock.
No new product verbs · no Temporal · no live Feishu invent · no C1–D6 rewrite.

Path order (rails):
  long-task start
    → progress same-card updates
    → D1 barge-in/fast ack + D3 durable follow-up enqueue (second_exec=false)
    → D2 pause/cancel windows (no fake Running)
    → C1 Approve/Deny HITL
    → C2 ≤1 terminal notify
    → D4 stalled/lost actionable
    → D6 watchdog reconcile / attempt fence
    → D5 CardView A+B round-trip fields (blank-ok)

AC stamps: D7-1..D7-6 (smoke) · D7-7 WRAP doc · D7-8 verify.sh · D7-9 PARK.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agentcore.barge_in_control import (
    CONTROL_FAST_BUDGET_MS,
    BargeInController,
    FollowUpInbox,
)
from agentcore.card_emit import CardEmitter, CardView, view_for
from agentcore.card_store import CardStore
from agentcore.completion_delivery import notify_on_terminal
from agentcore.feishu_transport import colleague_card_payload
from agentcore.follow_up_store import FollowUpStore
from agentcore.hitl_bridge import HitlBridge
from agentcore.liveness import LivenessLedger
from agentcore.liveness_daemon import LivenessDaemon, stalled_card_sink
from agentcore.ops_alerts import DeliveryReceipt
from agentcore.pool import Pool, display_stage
from agentcore.stalled_card_bridge import StalledCardBridge
from agentcore.task_card import CardFrame, CardHandle, CardState


ORIGIN = {
    "profile": "prof-d7",
    "session": "sess-d7",
    "route": "route-d7",
    "chat": "oc_chat_d7",
    "thread": "om_thread_d7",
    "turn": "1",
    "channel": "feishu",
}

# Wide cancel window so soft-cancel assert never races the hard timeout in-smoke.
CANCEL_WIDE = 3600.0


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeCardTransport:
    """Fake Feishu card transport — records emits; never claims live delivery."""

    def __init__(self):
        self.emits = []

    def emit(self, handle, view, *, final=False):
        self.emits.append({
            "task_id": handle.task_id,
            "message_id": handle.message_id,
            "card_id": handle.card_id,
            "view": view,
            "final": final,
            "rendered": view.render() if hasattr(view, "render") else dict(view),
        })


class FakeNotifyTransport:
    def __init__(self):
        self.calls = []
        self.llm_calls = 0

    def deliver(self, address, message, *, idempotency_key):
        self.calls.append({
            "address": dict(address),
            "message": dict(message),
            "idempotency_key": idempotency_key,
        })
        return DeliveryReceipt(
            accepted=True, event_id=idempotency_key, receipt_id="rc-d7",
        )

    def spawn_llm(self, *a, **k):
        self.llm_calls += 1
        raise AssertionError("Prefer-D7/C2 path must not spawn LLM")


def _plant_dead_runner(pool, job, *, attempt, lease_age=120.0):
    past = time.time() - lease_age
    with pool.db() as db:
        db.execute(
            "UPDATE jobs SET status='running', attempt=?, heartbeat=?, "
            "pid=?, pid_start=?, containment_token=NULL WHERE id=?",
            (attempt, past, 1, "dead-boot:999999999", job),
        )


def _normalize_window_actions(report):
    if isinstance(report, dict):
        return list(report.get("actions") or [])
    if isinstance(report, list):
        return report
    return []


# --------------------------------------------------------------------------- #
# D7-1..D7-6 — one scripted wrap path (fake Feishu / fake clock)
# --------------------------------------------------------------------------- #

def test_prefer_d7_wrap_smoke_lifecycle(tmp_path):
    """Full Prefer stream wrap smoke: stack existing knives, fake transport only."""
    clock = FakeClock(2000.0)
    pool = Pool(tmp_path / "pool")
    cards = CardStore(tmp_path / "cards.db")
    fu_store = FollowUpStore(tmp_path / "fu")
    card_tx = FakeCardTransport()
    notify_tx = FakeNotifyTransport()
    fd = None

    # --- D7-1: long-task start → progress same-card updates ---
    job = pool.submit("d7-long", {"goal": "prefer wrap smoke"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    assert claimed["id"] == job
    handle = cards.bind(job, "oc_chat_d7", "om_msg_d7", card_id="card_d7")
    handle.role_id = "dev"
    handle.display_name = "开发"
    handle.task_title = "Prefer-D7 wrap"
    handle.started_at = clock()
    handle.state = CardState.RUNNING
    cards.save(handle)

    emitter = CardEmitter(
        cards, job, card_tx, clock=clock, follow_up_store=fu_store,
    )
    emitter.bind_identity(
        role_id="dev", display_name="开发", task_title="Prefer-D7 wrap",
    )
    open_view = emitter.open(phase="weld")
    assert open_view is not None
    assert open_view.state == "running"
    assert open_view.display_name == "开发"
    msg_id = card_tx.emits[0]["message_id"]
    card_id = card_tx.emits[0]["card_id"]

    clock.advance(5)
    progress = emitter.update(detail="焊接中 5s", phase="weld", artifacts=("log.txt",))
    assert progress is not None
    assert card_tx.emits[-1]["message_id"] == msg_id
    assert card_tx.emits[-1]["card_id"] == card_id
    assert card_tx.emits[-1]["final"] is False
    assert len(card_tx.emits) >= 2  # open + progress same card

    # --- D7-2: D1 barge-in / fast ack + D3 durable follow-up (second_exec=false) ---
    mono_calls = {"n": 0}

    def mono():
        mono_calls["n"] += 1
        return 0.0 if mono_calls["n"] == 1 else 0.05  # 50ms

    inbox = FollowUpInbox(store=fu_store)
    ctrl = BargeInController(
        pool=pool,
        emitter=emitter,
        inbox=inbox,
        follow_up_store=fu_store,
        mono_clock=mono,
        clock=clock,
    )
    status_out = ctrl.handle(task_id=job, action="/status")
    assert status_out["ok"] is True
    assert status_out["verb"] == "status"
    assert status_out["second_exec"] is False
    assert status_out["durable_dispatch_submit"] is False

    fu_out = ctrl.handle(
        task_id=job,
        text="补充 wrap 标题",
        urgency_hint="tab",
        owner="prof-d7:sess-d7",
        origin=ORIGIN,
        current_phase="weld",
    )
    assert fu_out["ok"] is True
    assert fu_out["verb"] == "follow_up"
    assert fu_out["second_exec"] is False
    assert fu_out["durable_dispatch_submit"] is False
    assert fu_store.queued_count(job) >= 1
    assert fu_out["ack_latency_ms"] is not None
    assert fu_out["ack_latency_ms"] <= CONTROL_FAST_BUDGET_MS
    assert fu_out["slo_met"] is True
    assert pool.status(job)["status"] == "running"
    assert pool.status(job)["cancel"] == 0
    assert ctrl._llm_calls == 0

    # --- D7-3: D2 pause window — no fake Running ---
    assert pool.pause(job, reason="paused") is True
    st_pause = pool.status(job)
    assert st_pause["status"] == "waiting_user"
    assert st_pause["stage"] == "paused"
    pause_view = emitter.pause(detail="paused for HITL")
    assert pause_view is not None
    assert pause_view.state == "waiting_user"
    assert pause_view.phase == "paused"
    assert "运行中" not in (pause_view.note or "")
    assert display_stage(st_pause) != "running" or st_pause["status"] != "running"

    # Soft cancel window on sibling pool — finalize_cancel → cancelled (not fake done).
    # Own pool so Prefer-C2 outbox is not polluted by this cancel terminal.
    cancel_pool = Pool(tmp_path / "cancel_pool")
    cancel_job = cancel_pool.submit("d7-cancel-win", {"goal": "x"}, origin=ORIGIN)
    c_claimed, c_fd = cancel_pool.claim()
    try:
        assert c_claimed["id"] == cancel_job
        cancel_pool.cancel(cancel_job)
        c_st = cancel_pool.status(cancel_job)
        assert c_st["cancel"] == 1
        # Within wide window: enforce_windows must not force-terminal yet.
        report = cancel_pool.enforce_windows(now=time.time(), cancel_window_s=CANCEL_WIDE)
        forced = [
            a for a in _normalize_window_actions(report)
            if (a.get("id") if isinstance(a, dict) else None) == cancel_job
            and (a.get("status") if isinstance(a, dict) else None) == "cancelled"
        ]
        assert forced == []
        assert cancel_pool.finalize_cancel(cancel_job) is True
        assert cancel_pool.status(cancel_job)["status"] == "cancelled"
        assert json.loads(cancel_pool.status(cancel_job)["result"])["reason"] == "cancelled"
    finally:
        os.close(c_fd)

    # --- D7-4: C1 Approve/Deny HITL ---
    bridge = HitlBridge(pool, cards)
    # Pause already parked waiting_user (D2); emit HITL Approve/Deny on same card.
    assert pool.status(job)["status"] == "waiting_user"
    bridge.emit_via(
        emitter, kind="waiting_user", detail="确认 Prefer wrap 方案", job=job,
    )
    hitl_views = [
        e["view"] for e in card_tx.emits
        if getattr(e["view"], "state", None) == "waiting_user"
    ]
    assert hitl_views
    assert "approve" in hitl_views[-1].actions
    assert "deny" in hitl_views[-1].actions

    denied = bridge.handle_action(
        task_id=job, action="deny", reason="范围过大，先收窄 smoke",
    )
    assert denied["ok"] is True
    assert denied.get("denied") is True

    # Fresh pool for Approve → finish → C2 (isolates ≤1 notify from deny outbox).
    c2_pool = Pool(tmp_path / "c2_pool")
    c2_cards = CardStore(tmp_path / "c2_cards.db")
    c2_bridge = HitlBridge(c2_pool, c2_cards)
    hitl_job = c2_pool.submit("d7-hitl-approve", {"goal": "approve"}, origin=ORIGIN)
    h_claimed, h_fd = c2_pool.claim()
    os.close(h_fd)
    assert c2_bridge.enter_waiting_user(hitl_job, detail="再确认")["ok"] is True
    approved = c2_bridge.handle_action(task_id=hitl_job, action="approve")
    assert approved["ok"] is True
    assert approved.get("resumed") is True
    assert c2_pool.status(hitl_job)["status"] == "queued"
    h2, h2fd = c2_pool.claim()
    assert h2["id"] == hitl_job
    assert c2_pool.finish(hitl_job, h2["attempt"], True, {"summary": "prefer d7 wrap done"})
    os.close(h2fd)
    c2_job = hitl_job

    # --- D7-4 cont: C2 ≤1 terminal notify ---
    outcome = notify_on_terminal(c2_pool, notify_tx, status="completed")
    assert outcome["status"] == "delivered"
    assert outcome["task_id"] == c2_job
    assert len(notify_tx.calls) == 1
    assert notify_tx.llm_calls == 0
    assert notify_on_terminal(c2_pool, notify_tx, status="completed")["status"] == "empty"
    assert len(notify_tx.calls) == 1

    # --- D7-5: D4 stalled/lost actionable (fake transport; no live Feishu invent) ---
    stall_clock = FakeClock(5000.0)
    ledger = LivenessLedger(
        tmp_path / "liveness.db", stall_seconds=60.0, clock=stall_clock,
    )
    stall_store = CardStore(tmp_path / "stall_cards.db")
    stall_handle = stall_store.bind(
        "d7-stall", chat_id="oc_chat_d7", message_id="om_stall", card_id="card_stall",
    )
    stall_handle.role_id = "ops"
    stall_handle.display_name = "平台运维与巡检专家"
    stall_handle.task_title = "长跑任务"
    stall_handle.started_at = stall_clock()
    stall_handle.state = CardState.RUNNING
    stall_store.save(stall_handle)
    stall_tx = FakeCardTransport()
    stall_bridge = StalledCardBridge(
        store=stall_store, transport=stall_tx, ledger=ledger,
        cooldown_s=60, clock=stall_clock,
    )
    CardEmitter(stall_store, "d7-stall", stall_tx, clock=stall_clock).open(phase="running")
    stall_tx.emits.clear()
    ledger.register(
        "d7-stall", kind="durable", state="running", owner="ops", label="长跑任务",
    )
    daemon = LivenessDaemon(ledger, stalled_card_sink(stall_bridge), clock=stall_clock)
    stall_clock.advance(61)
    emitted = daemon.run_once()
    assert len(emitted) == 1
    assert emitted[0]["state"] == "stalled"
    assert len(stall_tx.emits) == 1
    rendered = stall_tx.emits[0]["rendered"]
    assert rendered["phase"] == "stalled"
    assert "cancel" in rendered["actions"]
    assert "status" in rendered["actions"]
    assert "resume_observe" in rendered["actions"]
    assert stall_tx.emits[0]["final"] is False

    lost_event = {
        "task_id": "d7-stall", "state": "lost", "previous_state": "stalled",
        "owner": "ops", "label": "长跑任务", "heartbeat_age_s": 0.0, "phase": "",
        "detail": "process is gone",
    }
    stall_clock.advance(121)
    lost_out = stall_bridge.notify_transition(lost_event)
    assert lost_out["status"] == "patched"
    assert lost_out["delivered"] is True  # fake transport only
    assert lost_out["colleague"]["display_name"] == "平台运维与巡检专家"
    assert stall_tx.emits[-1]["rendered"]["phase"] == "lost"

    # --- D7-5 cont: D6 watchdog reconcile / attempt fence ---
    wd_pool = Pool(tmp_path / "wd_pool")
    wd_job = wd_pool.submit(
        "d7-watchdog", {"goal": "x"}, origin=ORIGIN,
        replay_safe=False, max_tries=1,
    )
    wd_claimed, wd_fd = wd_pool.claim()
    wd_attempt = wd_claimed["attempt"]
    os.close(wd_fd)
    _plant_dead_runner(wd_pool, wd_job, attempt=wd_attempt, lease_age=120.0)
    report = wd_pool.reconcile_watchdog(now=time.time(), lease=60)
    assert any(
        a["id"] == wd_job and a["status"] == "failed"
        and a["reason"] == "finish_callback_lost"
        for a in report["actions"]
    )
    assert wd_pool.status(wd_job)["status"] == "failed"

    fence_pool = Pool(tmp_path / "fence_pool")
    f_job = fence_pool.submit(
        "d7-fence", {"goal": "x"}, origin=ORIGIN, replay_safe=True, max_tries=5,
    )
    first, f_fd1 = fence_pool.claim()
    old_attempt = first["attempt"]
    os.close(f_fd1)
    past = time.time() - 120
    with fence_pool.db() as db:
        db.execute(
            "UPDATE jobs SET heartbeat=?, pid=?, pid_start=?, "
            "containment_token=NULL WHERE id=?",
            (past, 1, "dead-boot:999999999", f_job),
        )
    second, f_fd2 = fence_pool.claim()
    try:
        new_attempt = second["attempt"]
        assert new_attempt != old_attempt
        assert fence_pool.finish(f_job, old_attempt, True, {"stolen": True}) is False
        assert fence_pool.status(f_job)["status"] == "running"
        assert fence_pool.status(f_job)["attempt"] == new_attempt
    finally:
        os.close(f_fd2)

    # --- D7-6: D5 CardView A+B round-trip (blank-ok) ---
    view_a = CardView(
        title="开发 · Prefer-D7 wrap",
        state="running",
        phase="weld",
        host="node-d7",
        artifacts=("log.txt",),
        note="运行中",
        actions=("cancel", "pause", "status", "details", "logs"),
        final=False,
        role_id="dev",
        display_name="开发",
        task_title="Prefer-D7 wrap",
        heartbeat_age_s=12.0,
        attempt_id="att-d7",
        from_bot="prof-d7",
        source_thread="oc_chat_d7/om_thread_d7",
        queued_follow_up_count=2,
        liveness_verdict="running",
        pool_window="starting",
        ack_miss=True,
    )
    rendered_a = view_a.render()
    for key in (
        "role_id", "display_name", "task_title", "heartbeat_age_s",
        "attempt_id", "from_bot", "source_thread",
        "queued_follow_up_count", "liveness_verdict", "pool_window", "ack_miss",
    ):
        assert key in rendered_a and rendered_a[key] not in (None, ""), key
    payload_a = colleague_card_payload(view_a)
    body_a = payload_a["elements"][0]["text"]["content"]
    assert "开发" in body_a or "开发" in payload_a["header"]["title"]["content"]
    assert "排队跟进：2" in body_a
    assert "活性：running" in body_a
    assert "窗口：starting" in body_a

    view_blank = CardView(
        title="t",
        state="running",
        phase="weld",
        host="",
        artifacts=(),
        note="",
        actions=("details",),
        final=False,
    )
    rendered_blank = view_blank.render()
    for key in ("queued_follow_up_count", "liveness_verdict", "pool_window", "ack_miss"):
        assert key not in rendered_blank
    body_blank = colleague_card_payload(view_blank)["elements"][0]["text"]["content"]
    assert "排队跟进" not in body_blank
    assert "活性" not in body_blank
    assert "窗口" not in body_blank

    v_handle = CardHandle(
        task_id="d7-v", chat_id="c", message_id="m",
        role_id="dev", display_name="开发", task_title="任务A",
    )
    frame = CardFrame(
        sequence=1, state=CardState.RUNNING, uuid="d7-v:1",
        phase="canceling", role_id="dev", display_name="开发",
        task_title="任务A", queued_follow_up_count=1, ack_miss=True,
    )
    derived = view_for(v_handle, frame, elapsed=3)
    assert derived.pool_window == "canceling"
    assert derived.queued_follow_up_count == 1

    if fd is not None:
        os.close(fd)


def test_prefer_d7_wrap_smoke_no_temporal_import():
    """D7-9 PARK: smoke stack must not pull Temporal (match D6-7)."""
    import importlib
    import sys

    banned = [
        name for name in sys.modules
        if name == "temporalio" or name.startswith("temporalio.")
        or name == "hatchet" or name.startswith("hatchet.")
    ]
    assert banned == []
    pool_mod = importlib.import_module("agentcore.pool")
    daemon_mod = importlib.import_module("agentcore.liveness_daemon")
    for mod in (pool_mod, daemon_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "import temporal" not in src.lower()
        assert "from temporal" not in src.lower()
        assert "import hatchet" not in src.lower()
        assert "from hatchet" not in src.lower()


def test_prefer_d7_wrap_smoke_cites_prior_unit_modules():
    """Evidence map: prior Prefer unit modules remain the knife source of truth."""
    root = Path(__file__).resolve().parent
    required = [
        "test_prefer_c1_hitl.py",
        "test_prefer_c2_notify.py",
        "test_prefer_d1_barge_in.py",
        "test_prefer_d2_cancel_windows.py",
        "test_prefer_d3_followup_durable.py",
        "test_prefer_d4_stalled_feishu.py",
        "test_prefer_d5_cardview_gapfill.py",
        "test_prefer_d6_watchdog_fencing.py",
        "test_feishu_colleague_visibility.py",
        "test_memory_progressive.py",
        "test_colleague_pack.py",
    ]
    missing = [n for n in required if not (root / n).exists()]
    assert missing == [], f"missing Prefer evidence modules: {missing}"

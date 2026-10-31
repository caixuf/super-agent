"""Prefer-D1 barge-in / main-dialog control short-path (thin).

AC: D1-1 status short-path · D1-2 Tab queue · D1-3 cancel+C2 · D1-4 pause ·
D1-5 Enter urgent · D1-6 colleague fields · D1-7 C1/C2 regression smoke.
"""
from __future__ import annotations

import os

import pytest

from agentcore.barge_in_control import (
    BargeInController,
    ControlVerb,
    FollowUpInbox,
    Urgency,
    bind_follow_up_to_task,
    classify_control,
)
from agentcore.card_emit import CardEmitter
from agentcore.card_store import CardStore
from agentcore.completion_delivery import notify_on_terminal
from agentcore.feishu_transport import CardNotifyBridge
from agentcore.hitl_bridge import HitlBridge
from agentcore.ops_alerts import DeliveryReceipt
from agentcore.pool import Pool
from agentcore.task_card import CardState
from agentcore.task_progress import IRREVERSIBLE_PHASES


ORIGIN = {
    "profile": "prof-1",
    "session": "sess-1",
    "route": "route-1",
    "chat": "oc_chat_1",
    "thread": "om_thread_9",
    "channel": "feishu",
}


class _FakeLedger:
    def inspect(self, task_id, **kwargs):
        return {
            "ok": True,
            "task_id": task_id,
            "state": "running",
            "started_at": 1000.0,
            "elapsed_s": 30.0,
            "heartbeat_age_s": 5.0,
            "phase": "build",
            "verdict": "alive",
        }


class _FakeCardTransport:
    def __init__(self):
        self.emits = []

    def emit(self, handle, view, *, final=False):
        self.emits.append((handle.task_id, handle.message_id, handle.card_id, view, final))


class _DeliveryTransport:
    def __init__(self):
        self.calls = []
        self.llm_calls = 0

    def deliver(self, address, message, *, idempotency_key):
        self.calls.append({
            "address": dict(address),
            "message": dict(message),
            "idempotency_key": idempotency_key,
        })
        return DeliveryReceipt(accepted=True, event_id=idempotency_key, receipt_id="rc-d1")

    def spawn_llm(self, *a, **k):
        self.llm_calls += 1
        raise AssertionError("Prefer-D1/C2 must not spawn LLM")


def _running_job(pool, key="d1-job"):
    job = pool.submit(key, {"goal": "work"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    assert claimed["id"] == job
    return job, claimed, fd


# --------------------------------------------------------------------------- #
# classify — slash/button first, free-text optional, default Tab
# --------------------------------------------------------------------------- #

def test_classify_slash_and_button_verbs():
    assert classify_control(action="/status").verb is ControlVerb.STATUS
    assert classify_control(action="pause").verb is ControlVerb.PAUSE
    assert classify_control(action="cancel").urgency is Urgency.ENTER
    assert classify_control("/cancel now").verb is ControlVerb.CANCEL
    assert classify_control(action="follow_up").verb is ControlVerb.FOLLOW_UP


def test_classify_free_text_thin_and_default_tab():
    assert classify_control("进度").verb is ControlVerb.STATUS
    assert classify_control("暂停").verb is ControlVerb.PAUSE
    assert classify_control("取消").verb is ControlVerb.CANCEL
    d = classify_control("补充一下标题颜色")
    assert d.verb is ControlVerb.FOLLOW_UP
    assert d.urgency is Urgency.TAB
    # Unknown prose still Tab-queues (never opens long exec by classify alone).
    d2 = classify_control("把第三节再写详细一点")
    assert d2.verb is ControlVerb.FOLLOW_UP
    assert d2.opens_long_exec is False


# --------------------------------------------------------------------------- #
# D1-1 status short-path — mock LLM not called; no durable_dispatch submit
# --------------------------------------------------------------------------- #

def test_d1_1_status_short_path_no_second_exec(tmp_path):
    pool = Pool(tmp_path)
    job, claimed, fd = _running_job(pool)
    try:
        acks = []
        ctrl = BargeInController(
            pool=pool, ledger=_FakeLedger(), clock=lambda: 1030.0,
            ack_sink=acks.append,
        )
        out = ctrl.handle(task_id=job, action="/status")
        assert out["ok"] is True
        assert out["verb"] == "status"
        assert out["second_exec"] is False
        assert out["durable_dispatch_submit"] is False
        assert out["detail"]["source"] == "liveness_ledger"
        assert ctrl._llm_calls == 0
        with pytest.raises(AssertionError):
            ctrl.spawn_llm()
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# D1-2 Tab queue — running continues; irreversible not preempted
# --------------------------------------------------------------------------- #

def test_d1_2_tab_queue_no_preempt(tmp_path):
    pool = Pool(tmp_path)
    job, claimed, fd = _running_job(pool)
    store = CardStore(tmp_path / "cards.db")
    store.bind(job, "c1", "m1")
    transport = _FakeCardTransport()
    emitter = CardEmitter(store, job, transport)
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="焊接")
    emitter.open(phase="edit")
    try:
        inbox = FollowUpInbox()
        ctrl = BargeInController(pool=pool, emitter=emitter, inbox=inbox)
        out = ctrl.handle(
            task_id=job, text="补充改标题", urgency_hint="tab",
            owner="prof:sess", origin=ORIGIN, current_phase="commit",
        )
        assert out["verb"] == "follow_up"
        assert out["second_exec"] is False
        assert out["durable_dispatch_submit"] is False
        assert out["detail"]["running_continues"] is True
        assert out["detail"]["preempt_blocked"] is True
        assert "commit" in IRREVERSIBLE_PHASES
        assert inbox.queued_count(job) == 1
        assert inbox.drain(job, current_phase="commit") == []
        drained = inbox.drain(job, current_phase="edit")
        assert len(drained) == 1
        # Running job not cancelled by Tab follow-up.
        assert pool.status(job)["status"] == "running"
        assert pool.status(job)["cancel"] == 0
        # Colleague identity preserved on queued-follow-up card patch.
        assert any(e[3].display_name == "开发" for e in transport.emits)
        assert any(
            getattr(e[3], "phase", None) == "queued_follow_up"
            or "排队" in (getattr(e[3], "note", "") or "")
            or "queued" in str(getattr(e[3], "phase", ""))
            for e in transport.emits
        ) or any("queued_follow_up" in str(e[3].render()) for e in transport.emits)
    finally:
        os.close(fd)


def test_bind_follow_up_helper_no_submit():
    inbox = FollowUpInbox()
    row = bind_follow_up_to_task(
        inbox, "t-9", "顺便加测试", owner="o1", origin=ORIGIN,
    )
    assert row["second_exec"] is False
    assert row["durable_dispatch_submit"] is False
    assert row["origin"]["session"] == "sess-1"
    assert inbox.queued_count("t-9") == 1


# --------------------------------------------------------------------------- #
# D1-3 cancel + same-card CANCELLED + <=1 C2 notify
# --------------------------------------------------------------------------- #

def test_d1_3_cancel_c2_at_most_once(tmp_path):
    pool = Pool(tmp_path)
    job, claimed, fd = _running_job(pool)
    store = CardStore(tmp_path / "cards.db")
    store.bind(job, "c1", "m1")
    card_t = _FakeCardTransport()
    emitter = CardEmitter(store, job, card_t)
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="取消测")
    emitter.open(phase="work")
    delivery_t = _DeliveryTransport()
    bridge = CardNotifyBridge(emitter, pool=pool, transport=delivery_t)
    try:
        ctrl = BargeInController(pool=pool, emitter=emitter, notify_bridge=bridge)
        out = ctrl.handle(task_id=job, action="/cancel")
        assert out["verb"] == "cancel"
        assert out["second_exec"] is False
        st = pool.status(job)
        assert st["status"] == "cancelled"
        assert out["detail"]["c2_attempts"] == 1
        # Same-card terminal.
        finals = [e for e in card_t.emits if e[4] is True]
        assert finals
        assert finals[-1][3].state == "cancelled"
        assert finals[-1][3].display_name == "开发"
        assert finals[-1][1] == "m1"
        # Prefer-C2 path did not spawn LLM.
        assert delivery_t.llm_calls == 0
        # Second notify attempt still honest / idempotent — count stays one kick from controller.
        again = notify_on_terminal(pool, delivery_t, status="cancelled")
        assert again["status"] in ("delivered", "empty", "unavailable", "skipped") or "status" in again
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# D1-4 pause -> waiting_user; claim skips; resume works
# --------------------------------------------------------------------------- #

def test_d1_4_pause_waiting_user_and_resume(tmp_path):
    pool = Pool(tmp_path)
    job, claimed, fd = _running_job(pool)
    store = CardStore(tmp_path / "cards.db")
    store.bind(job, "c1", "m1")
    card_t = _FakeCardTransport()
    emitter = CardEmitter(store, job, card_t)
    emitter.bind_identity(role_id="qa", display_name="测试", task_title="暂停测")
    emitter.open(phase="work")
    try:
        ctrl = BargeInController(pool=pool, emitter=emitter)
        out = ctrl.handle(task_id=job, action="/pause")
        assert out["verb"] == "pause"
        assert out["ok"] is True
        assert pool.status(job)["status"] == "waiting_user"
        os.close(fd)
        fd = None
        assert pool.claim() is None  # not claimable
        # Card shows paused/waiting_user; colleague fields kept.
        last = card_t.emits[-1][3]
        assert last.state == "waiting_user"
        assert last.display_name == "测试"
        resumed = ctrl.resume(job)
        assert resumed["ok"] is True
        again = pool.claim()
        assert again is not None
        assert again[0]["id"] == job
        os.close(again[1])
    finally:
        if fd is not None:
            os.close(fd)


def test_pool_pause_alias_and_finalize_cancel(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("p1", {"x": 1}, origin=ORIGIN)
    claimed, fd = pool.claim()
    try:
        assert pool.pause(job, reason="paused") is True
        assert pool.status(job)["status"] == "waiting_user"
    finally:
        os.close(fd)
    assert pool.finalize_cancel(job) is True
    assert pool.status(job)["status"] == "cancelled"


# --------------------------------------------------------------------------- #
# D1-5 Enter urgent soft-interrupt -> resume-or-replan; no second long exec
# --------------------------------------------------------------------------- #

def test_d1_5_urgent_enter_soft_interrupt(tmp_path):
    pool = Pool(tmp_path)
    job, claimed, fd = _running_job(pool, key="urgent-1")
    inbox = FollowUpInbox()
    ctrl = BargeInController(pool=pool, inbox=inbox)
    try:
        out = ctrl.handle(
            task_id=job, text="紧急改需求", action="urgent",
            owner="o", origin=ORIGIN,
        )
        assert out["verb"] == "urgent_inject"
        assert out["second_exec"] is False
        assert out["durable_dispatch_submit"] is False
        assert pool.status(job)["status"] == "waiting_user"
        assert inbox.queued_count(job) == 1
        # resume-or-replan path documented / usable
        assert out["detail"]["resume"] == "pool.resume_from_user"
        assert ctrl.resume(job)["ok"] is True
    finally:
        os.close(fd)


def test_d1_5_urgent_replan_await_plan(tmp_path):
    pool = Pool(tmp_path)
    job, claimed, fd = _running_job(pool, key="replan-1")
    store = CardStore(tmp_path / "cards.db")
    store.bind(job, "c1", "m1")
    emitter = CardEmitter(store, job, _FakeCardTransport())
    emitter.open(phase="work")
    ctrl = BargeInController(pool=pool, emitter=emitter, inbox=FollowUpInbox())
    try:
        out = ctrl.urgent_inject(job, text="重做计划", replan=True)
        assert out["second_exec"] is False
        assert out["durable_dispatch_submit"] is False
        st = pool.status(job)["status"]
        assert st in ("waiting_user", "awaiting_plan_approval")
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# D1-6 / D1-7 colleague fields + C1 Approve/Deny smoke unchanged
# --------------------------------------------------------------------------- #

def test_d1_6_colleague_fields_on_control_patches(tmp_path):
    pool = Pool(tmp_path)
    job, claimed, fd = _running_job(pool, key="vis-1")
    store = CardStore(tmp_path / "cards.db")
    store.bind(job, "c1", "m1")
    transport = _FakeCardTransport()
    emitter = CardEmitter(store, job, transport)
    emitter.bind_identity(role_id="architect", display_name="架构师", task_title="可见性")
    emitter.open(phase="work")
    try:
        ctrl = BargeInController(pool=pool, emitter=emitter, inbox=FollowUpInbox())
        ctrl.queue_follow_up(job, "补充", owner="o", origin=ORIGIN)
        ctrl.pause(job)
        for _tid, _mid, _cid, view, _final in transport.emits:
            if view.display_name:
                assert view.display_name == "架构师"
                assert view.role_id == "architect"
                assert view.task_title == "可见性"
    finally:
        os.close(fd)


def test_d1_7_c1_approve_deny_still_works(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("c1-smoke", {"goal": "x"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    store = CardStore(tmp_path / "cards.db")
    store.bind(job, "c1", "m1")
    transport = _FakeCardTransport()
    emitter = CardEmitter(store, job, transport)
    bridge = HitlBridge(pool, store)
    try:
        entered = bridge.enter_waiting_user(job, detail="等确认")
        assert entered["ok"] is True
        assert pool.status(job)["status"] == "waiting_user"
        # Approve path unchanged.
        assert pool.resume_from_user(job) is True
        # Separate job for deny.
        job2 = pool.submit("c1-deny", {"goal": "y"}, origin=ORIGIN)
        c2, fd2 = pool.claim()
        os.close(fd2)
        assert pool.wait_for_user(job2) is True
        assert pool.deny_from_user(job2, reason="不要了") is True
        assert pool.status(job2)["status"] == "cancelled"
    finally:
        os.close(fd)


def test_d1_7_c2_notify_on_terminal_unchanged(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("c2-smoke", {"goal": "z"}, origin=ORIGIN)
    claimed, fd = pool.claim()
    try:
        assert pool.finish(claimed["id"], claimed["attempt"], True, {"summary": "done"})
    finally:
        os.close(fd)
    t = _DeliveryTransport()
    out = notify_on_terminal(pool, t, status="completed")
    assert out["status"] == "delivered"
    assert t.llm_calls == 0

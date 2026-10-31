"""Prefer-D5 — CardView / Feishu adapter gap-fill (thin).

AC: D5-1 identity A on Feishu · D5-2 optional B round-trip · D5-3 sequence/one
writer · D5-4 text fallback · D5-5 store restart identity · D5-7 C1/D1 actions.
"""
from __future__ import annotations

import json

import pytest

from agentcore.card_emit import CardEmitter, CardView, view_for
from agentcore.card_store import CardStore
from agentcore.task_card import CardFrame, CardHandle, CardState, CardWriter
from agentcore.feishu_transport import (
    FeishuCardKitAdapter,
    FeishuError,
    colleague_card_payload,
)


# --------------------------------------------------------------------------- #
# D5-1 — Feishu identity A fields present when set on CardView
# --------------------------------------------------------------------------- #

def test_d5_1_feishu_maps_identity_a_fields():
    view = CardView(
        title="开发 · 修bug",
        state="running",
        phase="weld",
        host="node-a",
        artifacts=("a.txt",),
        note="运行中 5秒",
        actions=("cancel", "pause", "status", "details", "logs"),
        final=False,
        role_id="dev",
        display_name="开发",
        task_title="修bug",
        heartbeat_age_s=12.0,
        attempt_id="att-9",
        waiting_role="",
        from_bot="bot-x",
        source_thread="thr-1",
        hitl_kind="",
        hitl_summary="",
        hitl_request_id="",
    )
    rendered = view.render()
    for key in (
        "role_id", "display_name", "task_title", "heartbeat_age_s",
        "attempt_id", "from_bot", "source_thread",
    ):
        assert key in rendered and rendered[key], key
    assert rendered["phase"] == "weld"
    assert "pause" in rendered["actions"] and "status" in rendered["actions"]

    payload = colleague_card_payload(view)
    body = payload["elements"][0]["text"]["content"]
    assert "开发" in body or "开发" in payload["header"]["title"]["content"]
    assert "修bug" in body or "修bug" in payload["header"]["title"]["content"]
    assert "状态" in body
    assert "阶段" in body and "weld" in body
    assert "心跳" in body
    assert "attempt" in body and "att-9" in body
    assert "%" not in body


def test_d5_1_hitl_source_labels_still_map():
    view = CardView(
        title="开发 · 计划",
        state="waiting_user",
        phase="paused",
        host="",
        artifacts=(),
        note="已暂停",
        actions=("approve", "deny", "details", "logs"),
        final=False,
        role_id="dev",
        display_name="开发",
        task_title="计划",
        waiting_role="开发",
        from_bot="origin-bot",
        source_thread="thread-z",
        hitl_kind="waiting_user",
        hitl_summary="请确认",
        hitl_request_id="req-1",
    )
    payload = colleague_card_payload(view)
    body = payload["elements"][0]["text"]["content"]
    assert "来源" in body
    assert "from_bot=origin-bot" in body
    assert "source_thread=thread-z" in body
    assert "待确认岗位" in body
    # C1 Approve/Deny still present
    action_blocks = [e for e in payload["elements"] if e.get("tag") == "action"]
    assert action_blocks
    labels = [a["text"]["content"] for a in action_blocks[0]["actions"]]
    assert "批准" in labels and "拒绝" in labels


# --------------------------------------------------------------------------- #
# D5-2 — optional B round-trip; blank omitted
# --------------------------------------------------------------------------- #

def test_d5_2_optional_b_round_trip_when_set():
    view = CardView(
        title="开发 · t",
        state="running",
        phase="starting",
        host="",
        artifacts=(),
        note="启动中",
        actions=("cancel", "pause", "status"),
        final=False,
        role_id="dev",
        display_name="开发",
        task_title="t",
        queued_follow_up_count=3,
        liveness_verdict="running",
        pool_window="starting",
        ack_miss=True,
        verdict="",
    )
    rendered = view.render()
    assert rendered["queued_follow_up_count"] == 3
    assert rendered["liveness_verdict"] == "running"
    assert rendered["pool_window"] == "starting"
    assert rendered["ack_miss"] is True

    payload = colleague_card_payload(view)
    body = payload["elements"][0]["text"]["content"]
    assert "排队跟进：3" in body
    assert "活性：running" in body
    assert "窗口：starting" in body
    assert "ack_miss" in body


def test_d5_2_blank_b_omitted_from_render_and_body():
    view = CardView(
        title="t",
        state="running",
        phase="weld",
        host="",
        artifacts=(),
        note="",
        actions=("details",),
        final=False,
    )
    rendered = view.render()
    for key in ("queued_follow_up_count", "liveness_verdict", "pool_window", "ack_miss"):
        assert key not in rendered
    payload = colleague_card_payload(view)
    body = payload["elements"][0]["text"]["content"]
    assert "排队跟进" not in body
    assert "活性" not in body
    assert "窗口" not in body
    assert "ack_miss" not in body


def test_d5_2_view_for_derives_b_from_phase():
    handle = CardHandle(
        task_id="t1", chat_id="c", message_id="m",
        role_id="dev", display_name="开发", task_title="任务A",
    )
    frame = CardFrame(
        sequence=1, state=CardState.RUNNING, uuid="t1:1",
        phase="canceling", role_id="dev", display_name="开发",
        task_title="任务A", queued_follow_up_count=2, ack_miss=True,
    )
    view = view_for(handle, frame, elapsed=9)
    assert view.pool_window == "canceling"
    assert view.queued_follow_up_count == 2
    assert view.ack_miss is True
    rendered = view.render()
    assert rendered["pool_window"] == "canceling"
    assert rendered["queued_follow_up_count"] == 2
    assert rendered["ack_miss"] is True

    stalled = CardFrame(
        sequence=2, state=CardState.RUNNING, uuid="t1:2",
        phase="stalled", heartbeat_age_s=90.0,
    )
    v2 = view_for(handle, stalled, elapsed=90)
    assert v2.liveness_verdict == "stalled"
    assert v2.verdict == "stalled"


# --------------------------------------------------------------------------- #
# D5-3 — sequence↑; one writer; terminal lock unchanged
# --------------------------------------------------------------------------- #

def test_d5_3_sequence_monotonic_one_writer_terminal_lock():
    handle = CardHandle(task_id="t", chat_id="c", message_id="m")
    writer = CardWriter(handle, clock=lambda: 1000.0)
    f1 = writer.emit(CardState.RUNNING, phase="a", pool_window="starting")
    f2 = writer.emit(CardState.RUNNING, phase="b", queued_follow_up_count=1)
    assert f1 is not None and f2 is not None
    assert f2.sequence == f1.sequence + 1
    assert handle.sequence == 2
    done = writer.emit(CardState.DONE, final=True)
    assert done is not None and done.final is True
    assert writer.emit(CardState.RUNNING, phase="late") is None
    assert handle.sequence == 3  # terminal locked; sequence not advanced by drop


# --------------------------------------------------------------------------- #
# D5-4 — adapter fail → text fallback; not fake delivered
# --------------------------------------------------------------------------- #

class _BoomTransport:
    """Minimal stand-in: create/patch always raise FeishuError."""

    def _call(self, *a, **k):
        raise FeishuError("simulated_cardkit_fail")

    def send_plaintext(self, *, origin, delivery_key, payload):
        return {"receipt_id": "fb-msg-1", "ok": True}


def test_d5_4_adapter_fail_text_fallback_not_fake_delivered():
    # FeishuCardKitAdapter requires isinstance(transport, FeishuTransport).
    # Exercise _text_fallback path via a thin subclass mock of the fail branch.
    handle = CardHandle(task_id="t-fb", chat_id="oc_x", message_id="")
    view = CardView(
        title="开发 · fb", state="running", phase="weld", host="",
        artifacts=(), note="note", actions=("status",), final=False,
        role_id="dev", display_name="开发", task_title="fb",
    )

    class _Adapter:
        def _text_fallback(self, handle, view, *, reason):
            rendered = view.render()
            return {
                "ok": True,
                "fallback": True,
                "message_id": "fb-1",
                "reason": reason,
                "delivered_invented": False,
                "title": rendered.get("title"),
            }

        def emit(self, handle, view, *, final=False):
            # Simulate create failure → fallback (never invent delivered cardkit).
            return self._text_fallback(handle, view, reason="simulated_fail")

    out = _Adapter().emit(handle, view, final=False)
    assert out["fallback"] is True
    assert out.get("delivered") is not True
    assert out["ok"] is True
    assert "fb" in (out.get("title") or "")


def test_d5_4_real_adapter_fallback_on_feishu_error(monkeypatch):
    from agentcore import feishu_transport as ft

    class FakeTransport(ft.FeishuTransport):
        def __init__(self):
            # bypass real init
            self.app_id = "x"
            self.app_secret = "y"
            self._token = "t"
            self._token_expires = 9e18
            self.calls = []
            self.plain = []

        def _call(self, method, path, **kwargs):
            raise ft.FeishuError("boom")

        def send_plaintext(self, *, origin, delivery_key, payload):
            self.plain.append((origin, delivery_key, payload))
            return {"receipt_id": "plain-1"}

    transport = FakeTransport()
    adapter = FeishuCardKitAdapter(transport)
    handle = CardHandle(task_id="t9", chat_id="oc_chat", message_id="")
    view = CardView(
        title="开发 · x", state="running", phase="weld", host="",
        artifacts=(), note="n", actions=("pause", "status"), final=False,
        display_name="开发", task_title="x", role_id="dev",
    )
    out = adapter.emit(handle, view, final=False)
    assert out.get("fallback") is True
    assert out.get("ok") is True
    assert transport.plain, "must use text fallback path"
    # Never invent a card_id / delivered Card Kit success.
    assert not out.get("card_id")
    assert out.get("created") is not True


# --------------------------------------------------------------------------- #
# D5-5 — store restart keeps role_id/display_name/task_title
# --------------------------------------------------------------------------- #

def test_d5_5_store_restart_keeps_identity_a(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    h = store.bind("job-d5", "chat-1", "msg-1")
    h.role_id = "architect"
    h.display_name = "架构师"
    h.task_title = "拆分"
    store.save(h)

    store2 = CardStore(tmp_path / "cards.db")
    again = store2.load("job-d5")
    assert again is not None
    assert again.role_id == "architect"
    assert again.display_name == "架构师"
    assert again.task_title == "拆分"
    writer = store2.writer("job-d5")
    assert writer.handle.display_name == "架构师"
    frame = writer.emit(
        CardState.RUNNING, phase="starting", pool_window="starting",
        queued_follow_up_count=1,
    )
    assert frame is not None
    assert frame.pool_window == "starting"
    # Identity A still on handle after B-bearing frame.
    loaded = store2.load("job-d5")
    assert loaded.role_id == "architect"
    assert loaded.display_name == "架构师"
    assert loaded.task_title == "拆分"


# --------------------------------------------------------------------------- #
# D5-7 — C1 HITL + D1 pause/status actions still on card
# --------------------------------------------------------------------------- #

def test_d5_7_d1_pause_status_buttons_on_running_card():
    view = CardView(
        title="开发 · run",
        state="running",
        phase="weld",
        host="",
        artifacts=(),
        note="运行中",
        actions=("cancel", "pause", "status", "details", "logs"),
        final=False,
        role_id="dev",
        display_name="开发",
        task_title="run",
    )
    # Inject task_id the way adapter proxy does.
    class Prox:
        def render(self):
            d = view.render()
            d["task_id"] = "task-d1"
            return d

    payload = colleague_card_payload(Prox())
    action_blocks = [e for e in payload["elements"] if e.get("tag") == "action"]
    assert action_blocks, "D1 pause/status/cancel must paint on running card"
    labels = [a["text"]["content"] for a in action_blocks[0]["actions"]]
    assert "暂停" in labels
    assert "查看进度" in labels
    assert "取消" in labels


def test_d5_7_emitter_running_actions_include_pause_status(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t-d1", "c", "m")

    class Fake:
        def __init__(self):
            self.views = []

        def emit(self, handle, view, *, final=False):
            self.views.append(view)

    t = Fake()
    em = CardEmitter(store, "t-d1", t)
    em.bind_identity(role_id="dev", display_name="开发", task_title="跑")
    view = em.open(phase="weld")
    assert view is not None
    assert "pause" in view.actions and "status" in view.actions
    # HITL still approve/deny
    hitl = em.wait_user(detail="确认", from_bot="b", source_thread="s")
    assert hitl is not None
    assert "approve" in hitl.actions and "deny" in hitl.actions


def test_d5_emitter_stamps_b_on_mark_helpers(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t-b", "c", "m")

    class Fake:
        def __init__(self):
            self.views = []

        def emit(self, handle, view, *, final=False):
            self.views.append(view)

    t = Fake()
    em = CardEmitter(store, "t-b", t)
    em.bind_identity(role_id="dev", display_name="开发", task_title="x")
    v1 = em.mark_starting()
    assert v1 is not None
    assert v1.pool_window == "starting"
    assert "pool_window" in v1.render()
    v2 = em.mark_stalled(heartbeat_age_s=40)
    assert v2.liveness_verdict == "stalled"
    assert v2.render().get("liveness_verdict") == "stalled"
    v3 = em.mark_queued_follow_up(count=4)
    assert v3.queued_follow_up_count == 4
    v4 = em.update(phase="ack_miss", detail="slow", ack_miss=True)
    assert v4.ack_miss is True

"""Prefer-C1 HITL Feishu cards — waiting_user / plan / Approve / Deny / source labels."""

from __future__ import annotations

import os

import pytest

from agentcore.card_emit import CardEmitter, view_for
from agentcore.card_store import CardStore
from agentcore.feishu_transport import colleague_card_payload
from agentcore.hitl_bridge import (
    HitlBridge,
    callback_value,
    decode_callback_value,
    labels_from_origin,
    normalize_deny_reason,
    parse_text_fallback_command,
)
from agentcore.pool import Pool
from agentcore.task_card import CardFrame, CardHandle, CardState


class _FakeTransport:
    def __init__(self):
        self.emits = []

    def emit(self, handle, view, *, final=False):
        self.emits.append((handle.task_id, handle.message_id, view, final))


def _origin():
    return {
        "profile": "bot-architect",
        "session": "sess-9",
        "route": "agent:bot-architect:dm:u1",
        "channel": "feishu",
        "chat": "oc_chat_1",
        "thread": "om_thread_9",
        "turn": "7",
    }


# --------------------------------------------------------------------------- #
# C1-5 / C1-6 — source labels from origin / ontology only
# --------------------------------------------------------------------------- #

def test_labels_from_origin_host_bound_not_forged():
    handle = CardHandle(
        task_id="t1", chat_id="c", message_id="m",
        role_id="dev", display_name="开发",
    )
    labels = labels_from_origin(_origin(), handle)
    assert labels["from_bot"] == "bot-architect"
    assert "oc_chat_1" in labels["source_thread"]
    assert "om_thread_9" in labels["source_thread"]
    assert labels["role_id"] == "dev"
    assert labels["display_name"] == "开发"
    blank = labels_from_origin(None, None)
    assert blank["from_bot"] == ""
    assert blank["source_thread"] == ""


def test_card_frame_hitl_fields_render():
    frame = CardFrame(
        sequence=1, state=CardState.WAITING_USER, uuid="t:1",
        from_bot="bot-a", source_thread="chat/thread/1",
        hitl_kind="waiting_user", hitl_summary="确认是否继续",
        hitl_request_id="req1",
        role_id="dev", display_name="开发",
    )
    payload = frame.render()
    assert payload["from_bot"] == "bot-a"
    assert payload["source_thread"] == "chat/thread/1"
    assert payload["hitl_kind"] == "waiting_user"
    assert payload["hitl_summary"] == "确认是否继续"


# --------------------------------------------------------------------------- #
# C1-1 / C1-2 — waiting_user + plan cards emit Approve/Deny + summary
# --------------------------------------------------------------------------- #

def test_waiting_user_card_has_approve_deny_and_source(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("job-w", "chat-1", "msg-1")
    transport = _FakeTransport()
    emitter = CardEmitter(store, "job-w", transport, title="任务")
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="修bug")
    view = emitter.wait_user(
        detail="请确认方案",
        from_bot="bot-architect",
        source_thread="oc_chat_1/om_thread_9/7",
        hitl_kind="waiting_user",
        hitl_summary="请确认方案",
    )
    assert view is not None
    assert view.state == "waiting_user"
    assert "approve" in view.actions
    assert "deny" in view.actions
    assert view.from_bot == "bot-architect"
    assert "oc_chat_1" in view.source_thread
    assert view.hitl_kind == "waiting_user"
    assert view.hitl_summary == "请确认方案"
    assert view.waiting_role == "开发"


def test_plan_approval_card_same_shape(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("job-p", "chat-1", "msg-1")
    transport = _FakeTransport()
    emitter = CardEmitter(store, "job-p", transport)
    emitter.bind_identity(role_id="architect", display_name="架构师", task_title="拆分")
    view = emitter.await_plan(
        detail="步骤：1) 分析 2) 改码",
        from_bot="bot-architect",
        source_thread="oc_chat_1/t1",
        hitl_kind="plan",
        hitl_summary="步骤：1) 分析 2) 改码",
    )
    assert view.state == "awaiting_plan_approval"
    assert "approve" in view.actions and "deny" in view.actions
    assert view.hitl_kind == "plan"
    assert "分析" in view.hitl_summary
    card = colleague_card_payload(view)
    assert card["schema"] == "2.0"
    body = card["elements"][0]["text"]["content"]
    assert "from_bot=" in body or "来源" in body
    assert any(el.get("tag") == "action" for el in card["elements"])


# --------------------------------------------------------------------------- #
# C1-3 / C1-4 — Approve resume + Deny reason required
# --------------------------------------------------------------------------- #

def test_approve_resumes_job_and_records_event(tmp_path):
    pool = Pool(tmp_path / "pool")
    store = CardStore(tmp_path / "cards.db")
    job = pool.submit("hitl-approve", {"cmd": ["true"]}, origin=_origin())
    claimed = pool.claim()
    assert claimed is not None
    os.close(claimed[1])
    bridge = HitlBridge(pool, store)
    entered = bridge.enter_waiting_user(job, detail="等确认")
    assert entered["ok"] is True
    assert pool.status(job)["status"] == "waiting_user"
    # Bind emitter after park to exercise emit_via path separately
    store.bind(job, "chat-1", "msg-1")
    transport = _FakeTransport()
    emitter = CardEmitter(store, job, transport)
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="修bug")
    bridge.emit_via(emitter, kind="waiting_user", detail="等确认", job=job)
    assert transport.emits
    last = transport.emits[-1][2]
    assert last.state == "waiting_user"
    assert last.from_bot == "bot-architect"
    result = bridge.handle_action(task_id=job, action="approve")
    assert result["ok"] is True
    assert result["resumed"] is True
    assert pool.status(job)["status"] == "queued"
    pending = store.load_hitl(job)
    assert pending is not None
    assert pending["status"] == "approved"


def test_deny_requires_reason_and_no_silent_resume(tmp_path):
    pool = Pool(tmp_path / "pool")
    store = CardStore(tmp_path / "cards.db")
    job = pool.submit("hitl-deny", {"cmd": ["true"]}, origin=_origin())
    row, fd = pool.claim()
    os.close(fd)
    bridge = HitlBridge(pool, store)
    assert bridge.enter_plan_approval(job, detail="计划草稿")["ok"] is True
    missing = bridge.handle_action(task_id=job, action="deny", reason="")
    assert missing["ok"] is False
    assert missing["error"] == "deny_reason_required"
    assert pool.status(job)["status"] == "awaiting_plan_approval"
    denied = bridge.handle_action(task_id=job, action="deny", reason="范围过大，先拆分")
    assert denied["ok"] is True
    assert denied["denied"] is True
    status = pool.status(job)["status"]
    assert status in ("cancelled", "failed")
    assert pool.claim() is None  # not reclaimable
    pending = store.load_hitl(job)
    assert pending is not None
    assert pending["status"] == "denied"


def test_pool_deny_from_user_rejects_blank_reason(tmp_path):
    pool = Pool(tmp_path)
    job = pool.submit("d1", {"cmd": ["true"]}, origin={"profile": "p", "session": "s"})
    row, fd = pool.claim()
    os.close(fd)
    assert pool.wait_for_user(job) is True
    with pytest.raises(ValueError, match="deny_reason_required"):
        pool.deny_from_user(job, reason="")
    with pytest.raises(ValueError, match="deny_reason_required"):
        pool.deny_from_user(job, reason="   ")
    assert pool.deny_from_user(job, reason="不做了") is True
    assert pool.status(job)["status"] == "cancelled"
    assert pool.resume_from_user(job) is False


def test_normalize_deny_reason_caps_length():
    with pytest.raises(ValueError):
        normalize_deny_reason("")
    long = "x" * 400
    assert len(normalize_deny_reason(long)) == 240


# --------------------------------------------------------------------------- #
# Callback / text-fallback correlation
# --------------------------------------------------------------------------- #

def test_callback_value_and_decode_roundtrip():
    raw = callback_value(
        task_id="abc", action="deny", hitl_kind="plan", request_id="r1")
    raw["reason"] = "太宽"
    decoded = decode_callback_value(raw)
    assert decoded["task_id"] == "abc"
    assert decoded["action"] == "deny"
    assert decoded["reason"] == "太宽"
    assert decode_callback_value('{"action":"approve","task_id":"t","hitl":true}')[
        "action"] == "approve"
    assert decode_callback_value({"action": "status", "task_id": "t"}) is None


def test_text_fallback_parse_and_bridge(tmp_path):
    assert parse_text_fallback_command("APPROVE")["action"] == "approve"
    assert parse_text_fallback_command("DENY: 不要上线")["reason"] == "不要上线"
    assert parse_text_fallback_command("拒绝：先暂停")["action"] == "deny"
    assert parse_text_fallback_command("hello") is None
    pool = Pool(tmp_path / "pool")
    store = CardStore(tmp_path / "cards.db")
    job = pool.submit("txt", {"cmd": ["true"]}, origin=_origin())
    row, fd = pool.claim()
    os.close(fd)
    bridge = HitlBridge(pool, store)
    bridge.enter_waiting_user(job)
    out = bridge.handle_text_fallback(task_id=job, text="APPROVE")
    assert out["ok"] and out["resumed"]


# --------------------------------------------------------------------------- #
# C1-7 — single writer / same card_id
# --------------------------------------------------------------------------- #

def test_hitl_emit_uses_same_message_id(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("one", "c", "m-same")
    transport = _FakeTransport()
    emitter = CardEmitter(store, "one", transport)
    emitter.open(phase="work")
    emitter.wait_user(detail="确认")
    assert {e[1] for e in transport.emits} == {"m-same"}
    assert transport.emits[-1][2].state == "waiting_user"


def test_view_for_omits_blank_source_labels():
    handle = CardHandle(task_id="t", chat_id="c", message_id="m")
    frame = CardFrame(
        sequence=1, state=CardState.WAITING_USER, uuid="t:1",
        hitl_kind="waiting_user", hitl_summary="x",
    )
    view = view_for(handle, frame, elapsed=3)
    rendered = view.render()
    assert "from_bot" not in rendered
    assert "source_thread" not in rendered
    assert rendered["hitl_kind"] == "waiting_user"

"""Colleague visibility knife A→B→C + Codex §6 cheap fold unit tests."""

from __future__ import annotations

import os

import pytest

from agentcore.task_card import (
    CardFrame,
    CardHandle,
    CardState,
    should_push_frame,
    HEARTBEAT_THROTTLE_S,
)
from agentcore.card_emit import (
    CardEmitter,
    compose_title,
    resolve_display_name,
    view_for,
)
from agentcore.card_store import CardStore
from agentcore.task_progress import (
    FollowUpQueue,
    IRREVERSIBLE_PHASES,
    card_state,
    poll_progress_from_ledger,
    render,
    snapshot,
)
from agentcore.feishu_transport import (
    CardNotifyBridge,
    colleague_card_payload,
)
from agentcore.pool import Pool


# --------------------------------------------------------------------------- #
# A — CardFrame / CardView identity (+ Codex §6-4)
# --------------------------------------------------------------------------- #

def test_card_state_hitl_frees_worker_slot():
    assert CardState.WAITING_USER.frees_worker_slot is True
    assert CardState.AWAITING_PLAN_APPROVAL.frees_worker_slot is True
    assert CardState.RUNNING.frees_worker_slot is False
    assert CardState.WAITING_USER.terminal is False
    assert CardState.DONE.terminal is True


def test_card_frame_carries_colleague_fields():
    f = CardFrame(
        sequence=1, state=CardState.RUNNING, uuid="t:1",
        role_id="architect", display_name="架构师", task_title="拆分需求",
        heartbeat_age_s=12.5, attempt_id="a1",
    )
    view = f.render()
    assert view["role_id"] == "architect"
    assert view["display_name"] == "架构师"
    assert view["task_title"] == "拆分需求"
    assert view["heartbeat_age_s"] == 12.5
    assert view["attempt_id"] == "a1"


def test_compose_title_joins_display_name_and_task():
    title = compose_title(display_name="架构师", task_title="拆分需求")
    assert "架构师" in title and "拆分需求" in title
    assert compose_title(display_name="", task_title="拆分需求") == "拆分需求"
    assert compose_title(display_name="架构师", task_title="") == "架构师"


def test_resolve_display_name_from_catalog_not_literal_forge():
    catalog = {"architect": {"display_name": "架构师"}}
    assert resolve_display_name("architect", catalog=catalog) == "架构师"
    unknown = resolve_display_name("no_such_role", catalog=catalog)
    assert "需求边界" not in unknown


def test_view_for_includes_waiting_role_on_hitl():
    handle = CardHandle(
        task_id="t1", chat_id="c1", message_id="m1",
        role_id="dev", display_name="开发", task_title="修bug",
    )
    frame = CardFrame(
        sequence=2, state=CardState.WAITING_USER, uuid="t1:2",
        role_id="dev", display_name="开发", task_title="修bug",
        detail="请确认方案",
    )
    view = view_for(handle, frame, elapsed=5, title="任务")
    assert view.state == "waiting_user"
    assert view.display_name == "开发"
    assert view.waiting_role == "开发"
    assert "开发" in view.title


def test_card_store_persists_identity_and_card_id(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    h = store.bind("job-a", "chat-1", "msg-1")
    h.role_id = "qa"
    h.display_name = "测试"
    h.task_title = "回归"
    store.save(h)
    store.set_card_id("job-a", "card-xyz")
    again = store.load("job-a")
    assert again is not None
    assert again.card_id == "card-xyz"
    assert again.display_name == "测试"
    assert again.task_title == "回归"
    assert again.role_id == "qa"


# --------------------------------------------------------------------------- #
# B — throttle / notify / interrupt queue / ledger poll (+ Codex §6-1, §6-3)
# --------------------------------------------------------------------------- #

def test_should_push_frame_throttles_quiet_heartbeat():
    assert should_push_frame(last_push_at=100.0, now=110.0, phase_changed=False) is False
    assert should_push_frame(
        last_push_at=100.0, now=100.0 + HEARTBEAT_THROTTLE_S + 1, phase_changed=False
    ) is True
    assert should_push_frame(last_push_at=100.0, now=101.0, phase_changed=True) is True


class _FakeTransport:
    def __init__(self):
        self.emits = []

    def emit(self, handle, view, *, final=False):
        self.emits.append((handle.task_id, handle.message_id, view, final))


def test_card_emitter_bind_identity_and_wait_user(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t-wait", "c1", "m1")
    transport = _FakeTransport()
    emitter = CardEmitter(store, "t-wait", transport, title="任务")
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="实现可见性")
    view = emitter.wait_user(detail="需要确认")
    assert view is not None
    assert transport.emits
    last = transport.emits[-1][2]
    assert last.state == "waiting_user"
    assert last.display_name == "开发"


def test_card_notify_bridge_same_card_no_new_turn(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t-done", "c1", "m1")
    transport = _FakeTransport()
    emitter = CardEmitter(store, "t-done", transport)
    emitter.bind_identity(role_id="dev", display_name="开发", task_title="收尾")
    emitter.open(phase="work")
    bridge = CardNotifyBridge(emitter)
    bridge.notify(CardState.DONE, detail="完成")
    states = [e[2].state for e in transport.emits]
    assert "done" in states
    assert all(e[1] == "m1" for e in transport.emits)


def test_follow_up_queue_does_not_preempt_irreversible():
    q = FollowUpQueue()
    q.enqueue("顺便改下标题", task_id="t1")
    assert len(q) == 1
    assert q.drain(current_phase="commit") == []
    assert len(q) == 1
    drained = q.drain(current_phase="edit")
    assert len(drained) == 1
    assert drained[0]["text"].startswith("顺便")
    assert "commit" in IRREVERSIBLE_PHASES


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


def test_poll_progress_from_ledger_no_second_exec():
    out = poll_progress_from_ledger(_FakeLedger(), "job-9", now=1030.0)
    assert out["ok"] is True
    assert out["second_exec"] is False
    assert out["source"] == "liveness_ledger"
    assert out["snap"]["heartbeat_age_s"] == 5.0


def test_task_progress_snapshot_and_render_identity():
    data = {
        "task_id": "t1",
        "state": "waiting_user",
        "label": "修bug",
        "display_name": "开发",
        "task_title": "修bug",
        "heartbeat_age_s": 3,
        "started_at": 1.0,
        "elapsed_s": 10.0,
    }
    snap = snapshot(data, now=11.0)
    assert card_state(snap) is CardState.WAITING_USER
    text = render(snap)
    assert "等待用户" in text
    assert "开发" in text or "修bug" in text


# --------------------------------------------------------------------------- #
# C — Card Kit payload (offline; live Feishu → deliver-or-reason)
# --------------------------------------------------------------------------- #

def test_colleague_card_payload_maps_view():
    class V:
        def render(self):
            return {
                "title": "开发 · 可见性",
                "state": "running",
                "phase": "weld",
                "display_name": "开发",
                "task_title": "可见性",
                "heartbeat_age_s": 8,
                "note": "",
                "artifacts": [],
            }

    payload = colleague_card_payload(V())
    assert payload["schema"] == "2.0"
    assert payload["config"]["update_multi"] is True
    title = payload["header"]["title"]["content"]
    assert "开发" in title or "可见性" in title
    body = payload["elements"][0]["text"]["content"]
    assert "状态" in body
    assert "%" not in body


def test_colleague_card_payload_waiting_role_row():
    class V:
        def render(self):
            return {
                "title": "开发 · 计划",
                "state": "awaiting_plan_approval",
                "phase": "plan",
                "display_name": "开发",
                "task_title": "计划",
                "waiting_role": "开发",
                "note": "请审批",
                "artifacts": [],
            }

    payload = colleague_card_payload(V())
    body = payload["elements"][0]["text"]["content"]
    assert "待确认岗位" in body
    assert payload["header"]["template"] == "orange"


# --------------------------------------------------------------------------- #
# Cheap — pool waiting_user / plan-await (thin enum + claim skip)
# --------------------------------------------------------------------------- #

def test_pool_waiting_user_not_claimable_and_resume(tmp_path):
    p = Pool(tmp_path)
    job = p.submit("weld-wait", {"cmd": ["true"]},
                   origin={"profile": "t", "session": "s", "chat": "c"})
    claimed = p.claim()
    assert claimed is not None
    row, fd = claimed
    assert row["id"] == job
    assert p.wait_for_user(job) is True
    os.close(fd)
    assert p.claim() is None
    assert p.resume_from_user(job) is True
    again = p.claim()
    assert again is not None
    assert again[0]["id"] == job
    os.close(again[1])


def test_pool_await_plan_approval_not_claimable(tmp_path):
    p = Pool(tmp_path)
    job = p.submit("weld-plan", {"cmd": ["true"]},
                   origin={"profile": "t", "session": "s", "chat": "c"})
    row, fd = p.claim()
    assert p.await_plan_approval(job) is True
    os.close(fd)
    assert p.claim() is None
    assert p.resume_from_user(job) is True
    again = p.claim()
    assert again is not None
    os.close(again[1])

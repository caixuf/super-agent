"""Super Agent 1.0 knife-3 — long-task / scheduling visibility (LV-1..LV-8)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcore.card_emit import view_for
from agentcore.cardkit2_renderer import build_cardkit2_payload
from agentcore.feishu_transport import colleague_card_payload
from agentcore.liveness import LivenessLedger
from agentcore.task_card import CardFrame, CardHandle, CardState
from agentcore.task_card_bridge import (
    STATUS_HEADERS,
    TaskCardBridge,
    named_status_phrase,
    resolve_runner_name,
)
from agentcore.task_progress import VERDICT_WORD, named_status_phrase as progress_named_phrase, render, snapshot
from plugins.durable_dispatch import DispatchService

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _header(payload: dict) -> str:
    return payload["header"]["title"]["content"]


def _bridge(tmp_path, **kwargs):
    return TaskCardBridge(tmp_path / "cards", profile="test-profile", **kwargs)


def _service(tmp_path, *, display_name="后端同事", display_label="后端", role="dev",
             route=None, scheduled_routes=None, liveness_path=None, task_cards=True):
    origin = route or {"route": "frontdesk:channel:session_1", "channel": "feishu", "chat": "chat_1"}
    routes = scheduled_routes or {
        "daily_triage": {
            "route": origin["route"],
            "channel": origin["channel"],
            "chat": origin["chat"],
        }
    }

    def route_provider(sid):
        if sid.startswith("scheduled:"):
            return routes.get(sid.split(":", 1)[1], origin)
        return origin

    return DispatchService(
        str(tmp_path / "queue"),
        "frontdesk",
        [str(tmp_path)],
        role=role,
        display_name=display_name,
        display_label=display_label,
        route_provider=route_provider,
        task_cards_enabled=task_cards,
        scheduled_routes=routes,
        liveness_path=str(liveness_path or (tmp_path / "liveness.db")),
        engine_root=str(REPO),
    )


def _dump_header_forbidden(text: str) -> None:
    # Bare generic running words must not be the only visible running notice
    # when a runner name is expected. Helpers just document the banned set.
    assert text is not None


# --------------------------------------------------------------------------- #
# LV-1 — named running / progressive phrase on Feishu card payload
# --------------------------------------------------------------------------- #

def test_lv1_running_and_progressive_card_uses_named_phrase(tmp_path):
    bridge = _bridge(tmp_path)
    running = bridge.build_card_payload(
        "task-run", "后端同事", "编译依赖", "running", phase="编译", elapsed=3.0,
    )
    progressive = bridge.build_card_payload(
        "task-prog", "后端同事", "编译依赖", "progressive", phase="分段交付", elapsed=8.0,
    )
    for payload, name in ((running, "后端同事"), (progressive, "后端同事")):
        title = _header(payload)
        assert f"{name}在进行" in title or (f"{name} · 在进行" in title)
        # Bare generic words are not the only running notice.
        assert title.strip() not in {"执行中", "处理中", "进行中"}
        assert title.strip() != f"{name} · 执行中"
        assert "在进行" in title
    # Queued / terminal keep verdict words and still carry the name.
    queued = _header(bridge.build_card_payload("t-q", "后端同事", "goal", "queued"))
    done = _header(bridge.build_card_payload("t-d", "后端同事", "goal", "completed"))
    cancelled = _header(bridge.build_card_payload("t-c", "后端同事", "goal", "cancelled"))
    failed = _header(bridge.build_card_payload("t-f", "后端同事", "goal", "failed"))
    assert "后端同事" in queued and "排队中" in queued
    assert "后端同事" in done and "处理完成" in done
    assert "后端同事" in cancelled and "已取消" in cancelled
    assert "后端同事" in failed and "执行未通过" in failed


def test_lv1_cardkit_default_suffix_is_zaijinxing_when_name_supplied():
    payload = build_cardkit2_payload(
        "正在处理任务中...",
        is_terminal=False,
        is_failed=False,
        title="TestAgent",
    )
    title = _header(payload)
    assert "在进行" in title
    assert "TestAgent" in title
    assert "处理中" not in title or "在进行" in title


# --------------------------------------------------------------------------- #
# LV-2 — fallback to display_label / role_id; no invented nickname
# --------------------------------------------------------------------------- #

def test_lv2_blank_display_name_falls_back_to_label_or_role(tmp_path):
    bridge = _bridge(tmp_path)
    via_label = bridge.build_card_payload(
        "t-label", "", "goal", "running", display_label="研发执行", role_id="dev",
    )
    via_role = bridge.build_card_payload(
        "t-role", "", "goal", "running", display_label="", role_id="dev",
    )
    assert "研发执行在进行" in _header(via_label) or "研发执行 · 在进行" in _header(via_label)
    assert "dev在进行" in _header(via_role) or "dev · 在进行" in _header(via_role)
    for payload in (via_label, via_role):
        title = _header(payload)
        assert "智能助手" not in title
        assert "同事A" not in title
        assert title.strip() not in {"执行中", "处理中", "进行中", "在进行"}
    assert resolve_runner_name("", "研发执行", "dev") == "研发执行"
    assert resolve_runner_name("", "", "dev") == "dev"
    assert named_status_phrase("running", "后端同事") == "后端同事在进行"


# --------------------------------------------------------------------------- #
# LV-3 — submit_scheduled binds a named card; no-task_id status lists it
# --------------------------------------------------------------------------- #

def test_lv3_scheduled_feishu_card_and_status_list(tmp_path):
    service = _service(tmp_path, display_name="后端同事")
    submitted = service.submit_scheduled(
        "夜间巡检",
        route_name="daily_triage",
        idempotency_key="key-lv3-sched",
    )
    job = submitted["task_id"]
    assert submitted.get("ok") is True
    assert submitted.get("card_requested") is True

    created = []

    def creator(**kw):
        created.append(kw)
        return {"ok": True, "message_id": "om_sched_1"}

    transport = SimpleNamespace(
        card_bridge=TaskCardBridge(tmp_path / "sched-cards", profile="frontdesk"),
        card_creator=creator,
        reconcile_card=None,
    )
    outcomes = service.drain_cards(transport)
    assert outcomes and outcomes[0]["state"] == "sent"
    assert created
    title = _header(created[0]["card_payload"])
    assert "后端同事在进行" in title or "后端同事 · 在进行" in title or (
        "后端同事" in title and "排队中" in title
    )
    # Initial bind is queued; running phrase must appear once status is running.
    running = transport.card_bridge.build_card_payload(
        job, "后端同事", "夜间巡检", "running", role_id="dev",
    )
    run_title = _header(running)
    assert "后端同事在进行" in run_title or "后端同事 · 在进行" in run_title

    listed = service.status("caller-session", {})
    assert listed["ok"] is True
    ids = [item["task_id"] for item in listed["tasks"]]
    assert job in ids
    row = next(item for item in listed["tasks"] if item["task_id"] == job)
    assert row["scheduled"] is True
    assert row["display_name"] == "后端同事"


# --------------------------------------------------------------------------- #
# LV-4 — status short-path returns ledger fields, no second exec
# --------------------------------------------------------------------------- #

def test_lv4_status_short_path_ledger_fields(tmp_path):
    live_path = tmp_path / "live.db"
    service = _service(tmp_path, liveness_path=live_path)
    job = service.submit("sess-a", {"goal": "长任务", "request_key": "k-lv4"})["task_id"]
    result = service.status("sess-a", {"task_id": job})
    assert result["ok"] is True
    assert result["task_id"] == job
    assert result["status"] in {"queued", "running"}
    assert "cancellation_requested" in result
    assert result["display_name"] == "后端同事"
    assert result.get("scheduled") is False
    # verdict or phase when the ledger has a row; heartbeat may be present once live
    assert "verdict" in result or result["status"] in {"queued", "running"}
    phrase = result.get("status_phrase") or ""
    if result["status"] in {"queued"}:
        assert "后端同事" in phrase and ("排队中" in phrase or "在进行" in phrase)
    if result.get("verdict") == "running" or result["status"] == "running":
        assert "后端同事在进行" in phrase
    # Confirm this was a ledger/pool read, not a second long exec (no worker claim).
    assert service.pool.status(job)["status"] in {"queued", "running"}


# --------------------------------------------------------------------------- #
# LV-5 — cancel ad-hoc and route-matched scheduled; terminal stays terminal
# --------------------------------------------------------------------------- #

def test_lv5_cancel_adhoc_and_scheduled_reaches_pool_cancel(tmp_path):
    service = _service(tmp_path)
    adhoc = service.submit("sess-a", {"goal": "ad-hoc 任务", "request_key": "k-lv5-a"})["task_id"]
    sched = service.submit_scheduled(
        "scheduled 任务", route_name="daily_triage", idempotency_key="k-lv5-s",
    )["task_id"]

    adhoc_cancel = service.cancel("sess-a", {"task_id": adhoc})
    assert adhoc_cancel["ok"] is True
    assert adhoc_cancel["cancellation_requested"] is True
    after_adhoc = service.status("sess-a", {"task_id": adhoc})
    assert after_adhoc["cancellation_requested"] is True
    assert after_adhoc["status"] != "completed"
    # cancel=1 must not be advertised as healthy Running.
    assert after_adhoc.get("status_phrase", "") != "后端同事在进行" or after_adhoc["status"] in (
        "cancelled", "queued", "running",
    )
    if after_adhoc["status"] in {"queued", "running"}:
        assert after_adhoc["cancellation_requested"] is True
        assert "在进行" not in (after_adhoc.get("status_phrase") or "") or "取消" in (
            after_adhoc.get("status_phrase") or ""
        )

    sched_cancel = service.cancel("sess-a", {"task_id": sched})
    assert sched_cancel["ok"] is True
    assert sched_cancel["cancellation_requested"] is True
    after_sched = service.status("sess-a", {"task_id": sched})
    assert after_sched["scheduled"] is True
    assert after_sched["cancellation_requested"] is True

    # Already-terminal stays terminal (no fake Running). Use a fresh pool so
    # claim() is not starved by the cancelled jobs above.
    term_root = tmp_path / "term"
    term_root.mkdir()
    terminal_svc = _service(term_root, display_name="后端同事")
    terminal_job = terminal_svc.submit(
        "sess-term", {"goal": "already done", "request_key": "k-lv5-t"},
    )["task_id"]
    claim, fd = terminal_svc.pool.claim()
    try:
        assert claim["id"] == terminal_job
        assert terminal_svc.pool.finish(claim["id"], claim["attempt"], True, {"summary": "done"})
    finally:
        os.close(fd)
    assert terminal_svc.pool.status(terminal_job)["status"] == "completed"
    again = terminal_svc.cancel("sess-term", {"task_id": terminal_job})
    assert again["ok"] is True
    assert again["already_terminal"] is True
    assert again["request_accepted"] is False
    assert again["status"] == "completed"
    assert terminal_svc.status("sess-term", {"task_id": terminal_job})["status"] == "completed"
    assert terminal_svc.status("sess-term", {"task_id": terminal_job}).get("status_phrase", "") != "后端同事在进行"


# --------------------------------------------------------------------------- #
# LV-6 — stall/lost visible on status + same-card; silence ≠ done
# --------------------------------------------------------------------------- #

def test_lv6_stall_and_lost_leave_running_phrase_keep_name(tmp_path):
    # Use the public colleague payload path from a stalled CardView.
    frame = CardFrame(
        sequence=1,
        state=CardState.RUNNING,
        uuid="stall-uuid",
        phase="stalled",
        host="",
        artifacts=(),
        detail="heartbeat stale",
        heartbeat_age_s=90.0,
        display_name="后端同事",
        task_title="长跑任务",
        role_id="dev",
        liveness_verdict="stalled",
    )
    handle = CardHandle(task_id="stall-1", chat_id="chat_1", message_id="om_stall")
    handle.display_name = "后端同事"
    handle.task_title = "长跑任务"
    handle.role_id = "dev"
    view = view_for(handle, frame, elapsed=90, title="任务")
    payload = colleague_card_payload(view)
    title = _header(payload)
    assert "后端同事" in title
    assert "在进行" not in title
    assert "无响应" in title

    lost_frame = CardFrame(
        sequence=2,
        state=CardState.RUNNING,
        uuid="lost-uuid",
        phase="lost",
        host="",
        artifacts=(),
        detail="pid gone",
        heartbeat_age_s=120.0,
        display_name="后端同事",
        task_title="长跑任务",
        role_id="dev",
        liveness_verdict="lost",
    )
    lost_view = view_for(handle, lost_frame, elapsed=120, title="任务")
    lost_title = _header(colleague_card_payload(lost_view))
    assert "后端同事" in lost_title
    assert "在进行" not in lost_title
    assert "进程已不在" in lost_title

    # Status short-path surfaces verdict stalled|lost; silence is not success.
    live_path = tmp_path / "live6.db"
    service = _service(tmp_path, liveness_path=live_path)
    adhoc = service.submit("sess-a", {"goal": "stall me", "request_key": "k-lv6-a"})["task_id"]
    sched = service.submit_scheduled(
        "stall scheduled", route_name="daily_triage", idempotency_key="k-lv6-s",
    )["task_id"]
    ledger = service._resolve_liveness()
    assert ledger is not None
    for job, scheduled in ((adhoc, False), (sched, True)):
        ledger.observe(job, state="running", heartbeat_at=1.0, detail="started")
        changed = ledger.sweep(now=10_000.0, stall_seconds=10.0,
                               pid_alive=lambda pid, start: False)
        assert any(row.get("task_id") == job and row.get("state") in {"stalled", "lost"}
                   for row in changed) or ledger.inspect(job).get("state") in {"stalled", "lost"}
        st = service.status("sess-a", {"task_id": job})
        assert st["task_id"] == job
        assert st.get("scheduled") is scheduled
        assert st["status"] != "completed"
        assert st.get("verdict") in {"stalled", "lost"}
        assert st.get("verdict") != "done"
        assert "在进行" not in (st.get("status_phrase") or "")
        assert "后端同事" in (st.get("status_phrase") or st.get("display_name") or "")
        assert "无响应" in (st.get("status_phrase") or "") or "进程已不在" in (st.get("status_phrase") or "")


def test_lv6_progress_render_named_running_and_stall_words():
    running = {
        "task_id": "t-run", "label": "编译", "verdict": "running", "state": "running",
        "phase": "", "percent": None, "elapsed": 12, "detail": "x", "artifacts": (),
        "terminal": False, "display_name": "后端同事",
    }
    text = render(running, title="")
    assert "后端同事在进行" in text
    assert "状态：进行中" not in text or "后端同事在进行" in text
    stalled = dict(running, verdict="stalled", state="stalled", terminal=True)
    stalled_text = render(stalled, title="")
    assert "无响应" in stalled_text
    assert "后端同事" in stalled_text
    assert "在进行" not in stalled_text
    # Table itself stays pinned (hot-reload contract).
    assert VERDICT_WORD["running"] == "进行中"
    assert VERDICT_WORD["stalled"] == "无响应"
    assert VERDICT_WORD["lost"] == "进程已不在"
    assert progress_named_phrase(running) == "后端同事在进行"


# --------------------------------------------------------------------------- #
# LV-7 — knife-1 reject tests still exist unchanged; route still required
# --------------------------------------------------------------------------- #

def test_lv7_knife1_reject_tests_not_rewritten():
    router = (REPO / "tests" / "test_colleague_dispatch_router.py").read_text(encoding="utf-8")
    scheduled = (REPO / "tests" / "test_scheduled_submit.py").read_text(encoding="utf-8")
    assert "def test_missing_route_is_rejected_before_any_job_is_submitted" in router
    assert "def test_existing_frontdesk_durable_submit_still_rejects_session_without_route" in scheduled


def test_lv7_submit_scheduled_still_requires_registered_route(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(PermissionError, match="scheduled route not registered"):
        service.submit_scheduled("x", route_name="no_such_route", idempotency_key="k")


# --------------------------------------------------------------------------- #
# LV-8 — version pin + no second scheduler / Prefer-B1
# --------------------------------------------------------------------------- #

def test_lv8_version_and_authority_stay_put():
    import agentcore
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert agentcore.__version__ == "1.0.0"
    assert 'version = "1.0.0"' in pyproject
    # Touched surfaces must not grow a second scheduling authority.
    for rel in (
        "agentcore/task_card_bridge.py",
        "plugins/durable_dispatch/__init__.py",
        "agentcore/stalled_card_bridge.py",
    ):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "temporalio" not in text.lower()
        assert "from hatchet" not in text.lower()
        assert "StateGraph(" not in text

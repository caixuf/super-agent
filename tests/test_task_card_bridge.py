from __future__ import annotations

import json
from pathlib import Path
import time
import pytest

from agentcore.task_card_bridge import TaskCardBridge, _render_progress_bar


def test_progress_bar_rendering():
    assert _render_progress_bar(0.0, 10) == "[□□□□□□□□□□]"
    assert _render_progress_bar(0.5, 10) == "[■■■■■□□□□□]"
    assert _render_progress_bar(1.0, 10) == "[■■■■■■■■■■]"


def test_task_card_bridge_lifecycle(tmp_path: Path):
    patches = []

    def mock_sender(address, message_id, card_payload):
        patches.append({"address": address, "message_id": message_id, "payload": card_payload})
        return {"ok": True}

    bridge = TaskCardBridge(tmp_path / "cards", profile="test-profile", sender=mock_sender,
                            min_update_interval=0.01)

    task_id = "0123456789abcdef0123456789abcdef"
    origin = {"route": "agent:main:feishu:group:test_chat:test_sub", "chat": "oc_test", "channel": "feishu"}
    goal = "Investigate system performance and render arch docs"

    # 1. Bind task card
    res = bridge.bind_task(task_id, origin, goal, role="dev", display_name="研发执行", message_id="om_initial_001")
    assert res["ok"] is True
    assert res["message_id"] == "om_initial_001"
    card = res["card_payload"]
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "wathet"
    assert "研发执行" in card["body"]["elements"][0]["content"]

    # Verify query
    saved = bridge.get_card(task_id)
    assert saved is not None
    assert saved["status"] == "queued"
    assert saved["message_id"] == "om_initial_001"

    # 2. Update progress
    time.sleep(0.02)
    prog_res = bridge.update_progress(
        task_id, status="running", phase="依赖扫描与环境取证", progress=0.35, elapsed=12.5
    )
    assert prog_res["ok"] is True
    assert len(patches) == 1
    assert patches[0]["message_id"] == "om_initial_001"
    assert "依赖扫描与环境取证" in patches[0]["payload"]["body"]["elements"][0]["content"]

    # 3. Finalize card upon success (turn green contract)
    time.sleep(0.02)
    fin_res = bridge.finalize(
        task_id,
        status="completed",
        summary="全链路零 LangGraph 依赖，编排四层均为自研模块。",
        artifacts=["/tmp/report.html", "/tmp/preview.png"],
        elapsed=45.2,
    )
    assert fin_res["ok"] is True
    assert len(patches) == 2
    final_card = patches[1]["payload"]
    assert final_card["header"]["template"] == "green"
    assert "全链路零 LangGraph 依赖" in final_card["body"]["elements"][0]["content"]
    assert "report.html" in final_card["body"]["elements"][0]["content"]

    saved_final = bridge.get_card(task_id)
    assert saved_final["finalized"] == 1
    assert saved_final["status"] == "completed"


def test_task_card_bridge_sender_fault_tolerance(tmp_path: Path):
    def failing_sender(address, message_id, card_payload):
        raise ConnectionResetError("network disconnected")

    bridge = TaskCardBridge(tmp_path / "cards", profile="test-profile", sender=failing_sender)
    task_id = "abcdef0123456789abcdef0123456789"
    bridge.bind_task(task_id, {"route": "route1"}, "test goal", "analysis", "证据分析", message_id="om_fail_01")

    # Should not raise exception
    res = bridge.update_progress(task_id, status="running", phase="test", force_sync=True)
    assert res["ok"] is True
    assert res["patch_result"]["ok"] is False
    assert "network disconnected" in res["patch_result"]["error"]

def test_sanitize_card_text_unescapes_unicode_and_unwraps_json():
    from agentcore.task_card_bridge import _sanitize_card_text

    # Case 1: Raw escaped unicode
    assert _sanitize_card_text("核验：\\u6838\\u9a8c") == "核验：核验"

    # Case 2: JSON string containing unicode escape
    raw_json = '{"ok": true, "result": {"summary": "host-node \\u6838\\u9a8c\\uff1a\\u901a\\u8fc7"}}'
    assert _sanitize_card_text(raw_json) == "host-node 核验：通过"

    # Case 3: Prefix with JSON string
    mixed = '结果: {"ok": true, "result": {"summary": "\\u6210\\u529f"}}'
    assert _sanitize_card_text(mixed) == "结果:\n成功"

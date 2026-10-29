"""Unit tests for agentcore.card_stream."""
from __future__ import annotations

import pytest

from agentcore.card_stream import CardStreamBuilder


def test_card_stream_builder_progression():
    clock_time = 1000.0
    builder = CardStreamBuilder(
        task_id="task-stream-001",
        title="自动化研发任务",
        clock=lambda: clock_time,
    )

    # 1. Initial queued state
    card_queued = builder.render_card()
    assert card_queued["header"]["template"] == "grey"
    assert "[排队中]" in card_queued["header"]["title"]["content"]
    assert card_queued["config"]["update_multi"] is True

    # 2. Advance stage 1 to running
    clock_time += 10.0
    builder.advance("analysis", status="running", note="正在解析因果链")
    card_running = builder.render_card()
    assert card_running["header"]["template"] == "blue"
    assert "[执行中]" in card_running["header"]["title"]["content"]
    body_text = card_running["elements"][0]["text"]["content"]
    assert "[RUNNING] 需求规格与因果链拆解 (正在解析因果链)" in body_text
    assert "[WAIT] 状态机条件与用例生成" in body_text

    # 3. Complete stage 1, advance stage 2
    clock_time += 20.0
    builder.advance("analysis", status="passed", note="提取断言完成")
    builder.advance("cases", status="running", note="逆向代码生成中")
    card_step2 = builder.render_card()
    body_text_2 = card_step2["elements"][0]["text"]["content"]
    assert "[OK] 需求规格与因果链拆解 (提取断言完成)" in body_text_2
    assert "[RUNNING] 状态机条件与用例生成 (逆向代码生成中)" in body_text_2

    # 4. Final completion
    clock_time += 30.0
    builder.advance("cases", status="passed")
    builder.advance("review", status="passed", note="得分 92.0")
    builder.advance("verify", status="passed")
    builder.complete(success=True, detail="所有验收门禁通过")
    builder.add_artifact("report.json")

    card_done = builder.render_card()
    assert card_done["header"]["template"] == "green"
    assert "[已完成]" in card_done["header"]["title"]["content"]
    assert any("report.json" in el.get("text", {}).get("content", "") for el in card_done["elements"])


def test_card_stream_patch_payload():
    builder = CardStreamBuilder(task_id="task-stream-002")
    builder.advance("analysis", status="running")
    patch = builder.render_patch_payload()

    assert patch["msg_type"] == "interactive"
    assert patch["card"]["config"]["update_multi"] is True
    assert "elements" in patch["card"]
    assert len(patch["card"]["elements"]) >= 2


def test_card_stream_sync_from_liveness():
    builder = CardStreamBuilder(task_id="task-stream-003")
    live_dict = {
        "ok": True,
        "task_id": "task-stream-003",
        "state": "running",
        "phase": "cases",
        "detail": "生成用例代码第 2 轮迭代",
        "artifacts": ["contract_requirements.yaml", "manifest.json"],
    }
    builder.sync_from_liveness(live_dict)

    card = builder.render_card()
    assert card["header"]["template"] == "blue"
    body = card["elements"][0]["text"]["content"]
    assert "[RUNNING] 状态机条件与用例生成" in body
    assert any("contract_requirements.yaml" in el.get("text", {}).get("content", "") for el in card["elements"])


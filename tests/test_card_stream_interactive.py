"""Unit tests for Card 2.0/2.4 rich interactive streaming workbench."""
from __future__ import annotations

import json
import pytest

from agentcore.card_stream import (
    CardStreamBuilder,
    WORKBENCH_STAGES,
)


def test_workbench_4_stage_streaming_progression():
    clock_time = 2000.0
    wb = CardStreamBuilder.create_workbench(
        task_id="wb-task-001",
        title="Super Agent 研发工作台",
        clock=lambda: clock_time,
    )

    # 1. Queued state
    card = wb.render_card()
    assert card["config"]["update_multi"] is True
    assert card["config"]["wide_screen_mode"] is True
    assert "[排队中]" in card["header"]["title"]["content"]
    body = card["elements"][0]["text"]["content"]
    assert "[WAIT] 现场分析" in body
    assert "[WAIT] 规划推演" in body
    assert "[WAIT] 执行落盘" in body
    assert "[WAIT] 机器验证" in body

    # 2. Advance to analysis
    clock_time += 15.0
    wb.advance("analysis", status="running", note="正在解析因果约束与硬件拓扑")
    card2 = wb.render_card()
    assert "[执行中]" in card2["header"]["title"]["content"]
    body2 = card2["elements"][0]["text"]["content"]
    assert "[RUNNING] 现场分析 (正在解析因果约束与硬件拓扑)" in body2

    # 3. Advance to plan
    clock_time += 30.0
    wb.advance("analysis", status="passed", note="现场因果分析完成")
    wb.advance("plan", status="running", note="正在制定 Plan-First 蓝图")
    card3 = wb.render_card()
    body3 = card3["elements"][0]["text"]["content"]
    assert "[OK] 现场分析 (现场因果分析完成)" in body3
    assert "[RUNNING] 规划推演 (正在制定 Plan-First 蓝图)" in body3

    # 4. Advance to execution
    clock_time += 45.0
    wb.advance("plan", status="passed", note="蓝图已锁定")
    wb.advance("execution", status="running", note="落盘代码与配置改动")
    card4 = wb.render_card()
    body4 = card4["elements"][0]["text"]["content"]
    assert "[OK] 规划推演 (蓝图已锁定)" in body4
    assert "[RUNNING] 执行落盘 (落盘代码与配置改动)" in body4

    # 5. Advance to verification and complete
    clock_time += 60.0
    wb.advance("execution", status="passed")
    wb.advance("verification", status="passed", note="机器门禁退出码 0")
    wb.complete(success=True, detail="四大进阶特性验证全量通过")

    final_card = wb.render_card()
    assert final_card["header"]["template"] == "green"
    assert "[已完成]" in final_card["header"]["title"]["content"]


def test_collapsible_log_and_diff_panels():
    wb = CardStreamBuilder.create_workbench(task_id="wb-task-002")
    sample_diff = (
        "--- a/agentcore/memory.py\n"
        "+++ b/agentcore/memory.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-# old heavy 3GB mirror\n"
        "+# new ultra-lightweight entity store\n"
    )
    sample_logs = "2026-10-31 10:00:00 [PASS] unit tests all passed\n2026-10-31 10:00:01 [PASS] verify.sh code=0\n"

    wb.add_diff_panel(title="代码改动 Diff", diff_text=sample_diff)
    wb.add_collapsible_panel(title="机器验证完整日志", content=sample_logs, expanded=False, panel_type="log")

    payload = wb.render_patch_payload()
    elements = payload["card"]["elements"]

    # Verify elements contain collapsible markdown tags
    md_elements = [el["text"]["content"] for el in elements if el.get("text", {}).get("tag") == "lark_md"]
    assert len(md_elements) >= 2

    diff_elem = [m for m in md_elements if "代码改动 Diff" in m][0]
    assert "<details><summary>代码改动 Diff</summary>" in diff_elem
    assert "```diff" in diff_elem
    assert "new ultra-lightweight entity store" in diff_elem

    log_elem = [m for m in md_elements if "机器验证完整日志" in m][0]
    assert "<details><summary>机器验证完整日志</summary>" in log_elem
    assert "```log" in log_elem
    assert "verify.sh code=0" in log_elem


def test_table_fidelity_and_zero_emoji():
    wb = CardStreamBuilder.create_workbench(task_id="wb-task-003")
    headers = ["特性编号", "特性名称", "技术指标", "验收结果"]
    rows = [
        ["特性 1", "记忆流式闭环", "<5MB FTS 索引, <30ms 唤醒", "[PASS]"],
        ["特性 3", "显式规划与机器门禁", "Plan-First + 至多3轮自愈", "[PASS]"],
        ["特性 4", "富交互卡片工作台", "4 阶段流式原位 PATCH + 折叠", "[PASS]"],
        ["特性 5", "轨迹对账与 Golden Evals", "SQLite WAL 账本 + verify.sh 靶场", "[PASS]"],
    ]
    wb.add_table(headers=headers, rows=rows, title="四大进阶特性交付指标清单")

    card = wb.render_card()
    md_elements = [el["text"]["content"] for el in card["elements"] if el.get("text", {}).get("tag") == "lark_md"]
    assert len(md_elements) == 1
    table_content = md_elements[0]

    assert "| 特性编号 | 特性名称 | 技术指标 | 验收结果 |" in table_content
    assert "| --- | --- | --- | --- |" in table_content
    assert "| 特性 1 | 记忆流式闭环 | <5MB FTS 索引, <30ms 唤醒 | [PASS] |" in table_content

    # Strict Zero Emoji verification
    card_json_str = json.dumps(card, ensure_ascii=False)
    for ch in card_json_str:
        assert ord(ch) < 0x1F300 or ord(ch) > 0x1FAD6


def test_table_cell_pipe_escaping_and_nested_code_fence():
    wb = CardStreamBuilder.create_workbench(task_id="wb-task-004")
    # Table cell with pipes and newlines
    wb.add_table(
        headers=["命令", "说明"],
        rows=[
            ["cat /var/log/dmesg | grep 3090", "检查是否有掉卡\n第二行说明"],
        ],
    )
    # Diff panel with nested triple backticks
    nested_content = "```python\nprint('hello')\n```"
    wb.add_diff_panel("代码片段", nested_content)

    card = wb.render_card()
    md_elements = [el["text"]["content"] for el in card["elements"] if el.get("text", {}).get("tag") == "lark_md"]

    table_md = [m for m in md_elements if "cat /var/log/dmesg" in m][0]
    # Verify pipe inside cell is escaped
    assert r"cat /var/log/dmesg \| grep 3090" in table_md
    # Verify newline inside cell is converted to space
    assert "检查是否有掉卡 第二行说明" in table_md

    diff_md = [m for m in md_elements if "代码片段" in m][0]
    # Verify outer fence uses 4 backticks so nested 3 backticks don't break markdown
    assert "````diff" in diff_md


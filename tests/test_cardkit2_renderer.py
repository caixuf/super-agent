"""Unit tests for agentcore.cardkit2_renderer (CardKit 2.0 native streaming & final card)."""
from __future__ import annotations

import asyncio
import json
import pytest
from typing import Any, Dict, List

from agentcore.cardkit2_renderer import (
    CardStreamRenderer,
    build_cardkit2_payload,
    build_streaming_progress_body,
    extract_stages_and_clean_text,
    normalize_feishu_markdown_text,
    seal_unclosed_markdown_fences,
)


def test_extract_stages_and_clean_text():
    raw = (
        "##STAGE: 🔍 正在排查问题\n"
        "<think>正在分析日志链路</think>\n"
        "最终的分析结论文本"
    )
    stages, clean = extract_stages_and_clean_text(raw)
    assert "正在排查问题" in stages
    assert "正在分析日志链路" not in stages
    assert clean == "最终的分析结论文本"


def test_build_cardkit2_payload_in_progress():
    payload = build_cardkit2_payload(
        "正在处理任务中...",
        is_terminal=False,
        is_failed=False,
        title="TestAgent",
        stage_text="阶段 1: 数据准备",
        elapsed=12.5,
        service_label="node-alpha",
    )
    assert payload["schema"] == "2.0"
    assert payload["config"]["wide_screen_mode"] is True
    assert payload["header"]["template"] == "wathet"
    assert "在进行" in payload["header"]["title"]["content"]
    assert "TestAgent" in payload["header"]["title"]["content"]

    elements = payload["body"]["elements"]
    # 1. Collapsible panel for stages
    assert elements[0]["tag"] == "collapsible_panel"
    assert elements[0]["expanded"] is True

    # 2. Main content column with turquoise-50
    assert elements[1]["tag"] == "column_set"
    assert elements[1]["background_style"] == "turquoise-50"

    # 3. Footer with grey-50
    assert elements[2]["tag"] == "column_set"
    assert elements[2]["background_style"] == "grey-50"
    footer_text = elements[2]["columns"][0]["elements"][0]["content"]
    assert "耗时: 12.5s" in footer_text
    assert "执行节点: node-alpha" in footer_text


def test_build_cardkit2_payload_terminal_success():
    payload = build_cardkit2_payload(
        "全部 4 项断言通过",
        is_terminal=True,
        is_failed=False,
        title="TestAgent",
        stage_text="阶段 1: 完成",
        elapsed=24.0,
    )
    assert payload["header"]["template"] == "indigo"
    assert "处理完成" in payload["header"]["title"]["content"]
    elements = payload["body"]["elements"]
    # Collapsible panel should be collapsed in terminal state
    assert elements[0]["tag"] == "collapsible_panel"
    assert elements[0]["expanded"] is False
    assert elements[1]["background_style"] == "blue-50"


def test_build_cardkit2_payload_terminal_failed():
    payload = build_cardkit2_payload(
        "执行超时导致失败",
        is_terminal=True,
        is_failed=True,
        title="TestAgent",
    )
    assert payload["header"]["template"] == "carmine"
    assert "未能完成" in payload["header"]["title"]["content"]
    elements = payload["body"]["elements"]
    assert elements[0]["background_style"] == "red-50"


def test_card_stream_renderer_single_inflight_and_drain():
    async def _run():
        received_patches: List[Dict[str, Any]] = []
        patch_lock = asyncio.Lock()

        async def mock_patch(card_id: str, payload: Dict[str, Any]):
            assert not patch_lock.locked(), "Concurrency violation: multiple in-flight PATCH calls detected!"
            async with patch_lock:
                await asyncio.sleep(0.02)  # Simulate network latency
                received_patches.append(payload)

        renderer = CardStreamRenderer(
            card_id="card_12345",
            patch_fn=mock_patch,
            title="Probe",
        )

        # Rapid fire 10 updates
        for i in range(10):
            renderer.update_action(f"正在执行步骤 {i}", tool_name=f"tool_{i}")

        # Wait for all in-flight to settle
        await renderer.drain()
        renderer.close()

        # Even though 10 updates were triggered, last-write-wins collapses intermediate frames
        assert len(received_patches) >= 1
        assert len(received_patches) < 10

        # Ensure last received frame contains the latest step
        last_payload = received_patches[-1]
        main_column = last_payload["body"]["elements"][0]["columns"][0]["elements"][0]
        assert "正在执行步骤 9" in main_column["content"]

    asyncio.run(_run())


def test_card_stream_renderer_close_prevents_late_override():
    async def _run():
        received: List[str] = []

        async def mock_patch(card_id: str, payload: Dict[str, Any]):
            await asyncio.sleep(0.01)
            received.append("patch")

        renderer = CardStreamRenderer("card_abc", mock_patch)
        renderer.update_action("开始执行")
        await renderer.drain()
        renderer.close()

        # Actions after close must be dropped
        renderer.update_action("已经关闭后的动作")
        await asyncio.sleep(0.05)

        assert len(received) == 1

    asyncio.run(_run())


@pytest.mark.parametrize("raw", [
    "<think>private-reasoning-sentinel</think>",
    "<think>private-reasoning-sentinel",
    "<THINK>private-reasoning-sentinel</THINK>\n公开结论",
    "<think>##STAGE: private-reasoning-sentinel</think>\n公开结论",
    "<think>outer<think>inner</think>private-reasoning-sentinel</think>\n公开结论",
])
def test_private_reasoning_never_becomes_visible_card_content(raw):
    stages, clean = extract_stages_and_clean_text(raw)
    assert "private-reasoning-sentinel" not in stages + clean
    payload = build_cardkit2_payload(raw)
    assert "private-reasoning-sentinel" not in json.dumps(payload)


def test_private_reasoning_in_explicit_stage_is_not_published():
    payload = build_cardkit2_payload(
        "公开结论", stage_text="公开进度<think>private-stage-sentinel</think>",
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "公开进度" in serialized
    assert "private-stage-sentinel" not in serialized


def test_stream_drain_timeout_preserves_single_inflight():
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0
        patches = []

        async def patch(card_id, payload):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.set()
            try:
                await release.wait()
                patches.append(payload)
            finally:
                active -= 1

        renderer = CardStreamRenderer("card-timeout", patch)
        renderer.update_action("first")
        await started.wait()
        first_task = renderer._inflight
        try:
            with pytest.raises(TimeoutError):
                await renderer.drain(budget=0.001)
            assert renderer._inflight is first_task
            assert not first_task.done()
            renderer.update_action("second")
            await asyncio.sleep(0)
            assert peak == 1
        finally:
            renderer.close()
            release.set()
            await renderer.drain()
        assert active == 0
        assert len(patches) == 1

    asyncio.run(run())


def test_stream_drain_deadline_does_not_use_display_clock():
    async def run():
        release = asyncio.Event()

        async def patch(card_id, payload):
            await release.wait()

        renderer = CardStreamRenderer("card-clock", patch, clock=lambda: 100.0)
        renderer.update_action("first")
        started_at = asyncio.get_running_loop().time()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(renderer.drain(budget=0.001), timeout=0.5)
            assert asyncio.get_running_loop().time() - started_at < 0.25
            assert renderer._inflight is not None
        finally:
            renderer.close()
            release.set()
            await renderer.drain()

    asyncio.run(run())


def test_stream_drain_pumps_frame_queued_before_event_loop():
    received = []

    async def patch(card_id, payload):
        received.append(payload)

    renderer = CardStreamRenderer("card-before-loop", patch)
    renderer.update_action("queued before event loop")
    asyncio.run(renderer.drain())
    renderer.close()
    assert len(received) == 1


def test_stream_drain_propagates_caller_cancellation():
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def patch(card_id, payload):
            started.set()
            await release.wait()

        renderer = CardStreamRenderer("card-cancel", patch)
        renderer.update_action("first")
        await started.wait()
        waiting = asyncio.create_task(renderer.drain())
        await asyncio.sleep(0)
        waiting.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert renderer._inflight is not None
            assert not renderer._inflight.done()
        finally:
            renderer.close()
            release.set()
            await renderer.drain()

    asyncio.run(run())


def test_progress_patch_failure_is_visible_and_does_not_log_content(caplog):
    async def run():
        async def patch(card_id, payload):
            raise RuntimeError("synthetic-private-content-sentinel")

        renderer = CardStreamRenderer("card-failed", patch)
        renderer.update_action("first")
        with pytest.raises(RuntimeError, match="progress_card_update_failed"):
            await renderer.drain()
        renderer.close()

    asyncio.run(run())
    assert "synthetic-private-content-sentinel" not in caplog.text


def test_stream_drain_reports_patch_cancelled_before_start():
    async def run():
        async def patch(card_id, payload):
            raise AssertionError("cancelled patch must not start")

        renderer = CardStreamRenderer("card-prestart-cancel", patch)
        renderer.update_action("first")
        renderer._inflight.cancel()
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="progress_card_update_failed"):
            await renderer.drain()
        assert renderer._inflight is None
        renderer.close()

    asyncio.run(run())


def test_stream_drain_reports_patch_cancelled_after_start():
    async def run():
        started = asyncio.Event()

        async def patch(card_id, payload):
            started.set()
            await asyncio.Event().wait()

        renderer = CardStreamRenderer("card-running-cancel", patch)
        renderer.update_action("first")
        await started.wait()
        renderer._inflight.cancel()
        renderer.close()
        with pytest.raises(RuntimeError, match="progress_card_update_failed"):
            await renderer.drain()

    asyncio.run(run())


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True])
def test_stream_drain_rejects_invalid_budget(budget):
    async def patch(card_id, payload):
        pass

    renderer = CardStreamRenderer("card-budget", patch)
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(renderer.drain(budget=budget))


def test_normalize_feishu_markdown_text_protects_code_blocks():
    text = (
        "```python\n"
        "def run_task(**kwargs):\n"
        "    return '**not bold**'\n"
        "```\n"
        "行内代码 `**kwargs` 和 `` `**inner**` ``\n"
        "~~~~bash\n"
        "echo '**literal asterisks**'\n"
        "~~~~\n"
        "但**“文字”**属于测试。"
    )
    res = normalize_feishu_markdown_text(text)
    assert "def run_task(**kwargs):" in res
    assert "'**not bold**'" in res
    assert "`**kwargs`" in res
    assert "`` `**inner**` ``" in res
    assert "echo '**literal asterisks**'" in res
    assert "但“ **文字** ”属于测试。" in res


def test_normalize_feishu_markdown_text_chinese_punctuation_no_white_gap():
    cases = [
        ("**阶段**：", "**阶段**："),
        ("**结果**，", "**结果**，"),
        ("**结论**。", "**结论**。"),
        ("**注意**！", "**注意**！"),
        ("**疑问**？", "**疑问**？"),
        ("**说明**；", "**说明**；"),
        ("**清单**、", "**清单**、"),
        ("（**阶段**）", "（**阶段**）"),
        ("《**指南**》", "《**指南**》"),
        ("【**警告**】", "【**警告**】"),
        ("「**声明**」", "「**声明**」"),
        ("『**核心**』", "『**核心**』"),
        ("但“**文字**”属于", "但“ **文字** ”属于"),
        ("在**[OK]**后", "在 **[OK]** 后"),
        ("**阶段**：下一阶段", "**阶段**：下一阶段"),
    ]
    for inp, expected in cases:
        assert normalize_feishu_markdown_text(inp) == expected


def test_seal_unclosed_markdown_fences():
    unclosed_backtick = "```python\ndef test():\n    pass"
    assert seal_unclosed_markdown_fences(unclosed_backtick) == (
        "```python\ndef test():\n    pass\n```"
    )

    unclosed_tilde = "~~~~yaml\nkey: value"
    assert seal_unclosed_markdown_fences(unclosed_tilde) == (
        "~~~~yaml\nkey: value\n~~~~"
    )

    closed = "```python\nfoo()\n```"
    assert seal_unclosed_markdown_fences(closed) == closed

    plain = "简单纯文本没有任何围栏"
    assert seal_unclosed_markdown_fences(plain) == plain


def test_build_cardkit2_payload_streaming_seals_fences():
    streaming_text = "分析中：\n```json\n{\"status\": 1}"
    streaming_stage = "##STAGE: 检查配置\n```shell\ncat config.json"
    payload = build_cardkit2_payload(
        streaming_text,
        is_terminal=False,
        stage_text=streaming_stage,
    )
    elements = payload["body"]["elements"]
    stage_content = elements[0]["elements"][0]["columns"][0]["elements"][0]["content"]
    assert stage_content.rstrip().endswith("```")
    main_content = elements[1]["columns"][0]["elements"][0]["content"]
    assert main_content.rstrip().endswith("```")


def test_build_cardkit2_payload_native_table_orchestration():
    md_with_table = (
        "**核心执行结论**：全部 3 个子任务顺利完成。\n\n"
        "| 模块 | 状态 | 耗时 |\n"
        "| :--- | :--- | :--- |\n"
        "| 认证 | PASS | 1.2s |\n"
        "| 编排 | PASS | 2.5s |\n\n"
        "────────────────\n\n"
        "后续详细追溯记录。"
    )
    payload = build_cardkit2_payload(
        md_with_table,
        is_terminal=True,
    )
    elements = payload["body"]["elements"]
    # 1. First block is conclusion header in blue-50 column_set
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "blue-50"
    assert "核心执行结论" in elements[0]["columns"][0]["elements"][0]["content"]

    # 2. Table at root level
    assert elements[1]["tag"] == "table"
    assert len(elements[1]["rows"]) == 2

    # 3. Divider converted to native hr
    assert elements[2]["tag"] == "hr"

    # 4. Subsequent markdown at root level
    assert elements[3]["tag"] == "markdown"
    assert "后续详细追溯记录" in elements[3]["content"]


def test_build_cardkit2_payload_table_first_line():
    md = (
        "| 标题A | 标题B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n\n"
        "说明文本"
    )
    payload = build_cardkit2_payload(md, is_terminal=True)
    elements = payload["body"]["elements"]
    assert elements[0]["tag"] == "table"
    assert elements[1]["tag"] == "markdown"


def test_build_cardkit2_payload_long_text_block_whitespace_layout():
    first_part = "**概要结论**：系统架构升级已全部完成，各项指标均已达到交付基准。"
    long_body = "详细分析段落内容。\n" * 70  # Makes len > 600
    combined = f"{first_part}\n\n────────────────\n\n{long_body}"
    assert len(combined) >= 600

    payload = build_cardkit2_payload(combined, is_terminal=True)
    elements = payload["body"]["elements"]

    # First section wrapped in blue-50 column_set
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "blue-50"
    assert "概要结论" in elements[0]["columns"][0]["elements"][0]["content"]

    # Native hr separator
    assert elements[1]["tag"] == "hr"

    # Long body at top level markdown
    assert elements[2]["tag"] == "markdown"
    assert "详细分析段落内容" in elements[2]["content"]


def test_build_cardkit2_payload_metric_items_and_hero_image():
    payload = build_cardkit2_payload(
        "任务简报",
        is_terminal=True,
        metric_items=[
            ("当前阶段", "单测"),
            ("覆盖率", "98.5%"),
            ("总耗时", "12.0s"),
        ],
        hero_img_key="img_v2_hero_sample_key",
        hero_alt="架构执行全景图",
    )
    elements = payload["body"]["elements"]
    # elements[0]: main box
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "blue-50"

    # elements[1]: metric items
    metric_set = elements[1]
    assert metric_set["tag"] == "column_set"
    assert metric_set["flex_mode"] == "trisect"
    assert metric_set["background_style"] == "grey-50"
    assert len(metric_set["columns"]) == 3
    assert "**当前阶段**\n单测" in metric_set["columns"][0]["elements"][0]["content"]

    # elements[2]: hero image
    hero_img = elements[2]
    assert hero_img["tag"] == "img"
    assert hero_img["img_key"] == "img_v2_hero_sample_key"
    assert hero_img["preview"] is True
    assert hero_img["alt"]["content"] == "架构执行全景图"


def test_build_cardkit2_payload_fenced_yaml_dashes_not_split_into_hr():
    yaml_snippet = "```yaml\n---\nname: super-agent\nmode: production\n---\n```"
    prose = "配置核验说明段落。\n" * 60
    combined = f"**结论**：配置合法\n\n────────────────\n\n{yaml_snippet}\n\n{prose}"
    assert len(combined) >= 600
    payload = build_cardkit2_payload(combined, is_terminal=True)
    elements = payload["body"]["elements"]
    assert [e["tag"] for e in elements] == ["column_set", "hr", "markdown"]
    assert "---\nname: super-agent" in elements[2]["content"]



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
    assert "处理中" in payload["header"]["title"]["content"]

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

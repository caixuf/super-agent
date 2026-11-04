"""Tests for agentcore.pipeline_dag (Long-running activity lease & watchdog)."""

from __future__ import annotations

import asyncio
import pytest
from agentcore.pipeline_dag import (
    ActivityLeaseGuard,
    PipelineStageRunner,
    StageConfig,
    StageName,
    StageType,
)


def test_lightweight_stage_success():
    async def _run():
        runner = PipelineStageRunner()

        async def _quick_step(guard: ActivityLeaseGuard):
            await asyncio.sleep(0.05)
            return "route_ok"

        res = await runner.run_stage(StageName.INTENT, _quick_step)
        assert res == "route_ok"

    asyncio.run(_run())


def test_lightweight_stage_timeout():
    async def _run():
        configs = {
            StageName.INTENT: StageConfig(
                name=StageName.INTENT,
                stage_type=StageType.LIGHTWEIGHT,
                hard_timeout_s=0.1,
            )
        }
        runner = PipelineStageRunner(configs=configs)

        async def _stalled_step(guard: ActivityLeaseGuard):
            await asyncio.sleep(0.3)
            return "done"

        with pytest.raises(TimeoutError) as exc:
            await runner.run_stage(StageName.INTENT, _stalled_step)
        assert "exceeded budget" in str(exc.value)

    asyncio.run(_run())


def test_long_running_stage_survives_with_heartbeats():
    """验证长任务场景：只要有持续心跳，哪怕总时间长也不会被误杀。"""
    async def _run():
        configs = {
            StageName.INFERENCE_STREAM: StageConfig(
                name=StageName.INFERENCE_STREAM,
                stage_type=StageType.LONG_RUNNING,
                idle_silence_timeout_s=0.3,  # 静默阈值设为 0.3s
                max_total_duration_s=10.0,
            )
        }
        runner = PipelineStageRunner(configs=configs)

        async def _active_long_task(guard: ActivityLeaseGuard):
            # 持续运行 0.6 秒 (大于 0.3s 静默阈值)，但每 0.1s 续期一次
            for _ in range(6):
                await asyncio.sleep(0.1)
                guard.touch()  # 模拟持续生成 Token / 推进长任务
            return "long_task_completed"

        res = await runner.run_stage(StageName.INFERENCE_STREAM, _active_long_task)
        assert res == "long_task_completed"

    asyncio.run(_run())


def test_long_running_stage_detects_pure_silence_deadlock():
    """验证真正死锁场景：若超过静默阈值完全没有输出，看门狗精准熔断。"""
    async def _run():
        configs = {
            StageName.INFERENCE_STREAM: StageConfig(
                name=StageName.INFERENCE_STREAM,
                stage_type=StageType.LONG_RUNNING,
                idle_silence_timeout_s=0.2,  # 0.2s 零心跳即判定死锁
            )
        }
        runner = PipelineStageRunner(configs=configs)

        async def _silent_hung_task(guard: ActivityLeaseGuard):
            # 产生一次心跳后彻底挂起
            guard.touch()
            await asyncio.sleep(0.6)  # 远超 0.2s 静默阈值
            return "should_not_reach"

        with pytest.raises(TimeoutError) as exc:
            await runner.run_stage(StageName.INFERENCE_STREAM, _silent_hung_task)
        assert "hung with silence" in str(exc.value)

    asyncio.run(_run())

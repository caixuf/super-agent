"""确定性流水线 DAG 与心跳活跃看门狗 (Pipeline DAG & Activity Watchdog)。

【最高原则】：防真死锁 (静默挂死)，绝不防慢跑 (合法长任务推进)！
针对仿真对账、复杂长链路排查等重型长任务的工程特性：
1. 轻量阶段 (如接收消息/意图分类):
   - 时间确定性强，设定秒级硬预算 (如 5s)，保障前台交互即时性。
2. 重型推理与工具执行长任务阶段 (Inference & Tool Execution):
   - 绝对不用生硬的总耗时硬杀！
   - 采用【心跳活跃续期制 (Activity-Driven Lease Renewal)】：
     * 只要大模型在持续吐字 (Streaming Tokens 流出)，看门狗自动续期！
     * 只要后台工具/Docker 在产生输出或状态跳变，看门狗自动续期！
     * 支持长任务从容跑 5~15 分钟以上。
   - 仅在【连续纯静默 (Idle Silence)】超过宽容阈值 (默认 60s 零输出且零心跳) 时，
     才判定底层连接断流或真正死锁，执行安全自愈。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class StageType(str, Enum):
    """流水线阶段类型。"""
    LIGHTWEIGHT = "lightweight"    # 轻量确定性阶段 (如意图路由/前置分类)，受硬超时保护
    LONG_RUNNING = "long_running"  # 重型长任务阶段 (如推理/工具链)，受静默看门狗保护


class StageName(str, Enum):
    RECV = "recv"
    INTENT = "intent"
    CONTEXT_PREPARE = "context_prepare"
    INFERENCE_STREAM = "inference_stream"
    ACTION_EXEC = "action_exec"
    RENDER_DELIVER = "render_deliver"


@dataclass
class StageConfig:
    name: StageName
    stage_type: StageType
    hard_timeout_s: float = 10.0           # 仅对 LIGHTWEIGHT 生效的硬超时
    idle_silence_timeout_s: float = 60.0   # 对 LONG_RUNNING 生效的纯静默超时
    max_total_duration_s: float = 1800.0   # 长任务极端兜底上限 (30 分钟)


@dataclass
class StageExecutionTrace:
    name: StageName
    started_at: float
    finished_at: float = 0.0
    duration_s: float = 0.0
    passed: bool = False
    heartbeat_count: int = 0
    error: Optional[str] = None


class ActivityLeaseGuard:
    """心跳活跃续期守卫。支持长任务，只防零输出假死。"""

    def __init__(self, idle_timeout_s: float = 60.0, max_duration_s: float = 1800.0):
        self.idle_timeout_s = idle_timeout_s
        self.max_duration_s = max_duration_s
        self.started_at = time.monotonic()
        self.last_activity_at = self.started_at
        self.heartbeat_count = 0
        self._stopped = False

    def touch(self) -> None:
        """模型吐出 Token 或工具产生进展时调用，自动续期。"""
        self.last_activity_at = time.monotonic()
        self.heartbeat_count += 1

    def is_silent_dead(self) -> bool:
        """检查是否发生了纯静默超时 (连接已死但未报错)。"""
        now = time.monotonic()
        idle = now - self.last_activity_at
        total = now - self.started_at
        if idle > self.idle_timeout_s:
            return True
        if total > self.max_duration_s:
            return True
        return False


class PipelineStageRunner:
    """流水线阶段执行器，对轻量与长任务提供差异化守卫。"""

    def __init__(self, configs: Optional[Dict[StageName, StageConfig]] = None):
        self.configs = configs or self._default_configs()

    @staticmethod
    def _default_configs() -> Dict[StageName, StageConfig]:
        return {
            StageName.RECV: StageConfig(
                name=StageName.RECV,
                stage_type=StageType.LIGHTWEIGHT,
                hard_timeout_s=3.0,
            ),
            StageName.INTENT: StageConfig(
                name=StageName.INTENT,
                stage_type=StageType.LIGHTWEIGHT,
                hard_timeout_s=5.0,
            ),
            StageName.CONTEXT_PREPARE: StageConfig(
                name=StageName.CONTEXT_PREPARE,
                stage_type=StageType.LIGHTWEIGHT,
                hard_timeout_s=5.0,
            ),
            StageName.INFERENCE_STREAM: StageConfig(
                name=StageName.INFERENCE_STREAM,
                stage_type=StageType.LONG_RUNNING,
                idle_silence_timeout_s=60.0,  # 允许长任务，只要 60s 内有输出就不中断
                max_total_duration_s=1800.0,  # 允许跑满 30 分钟
            ),
            StageName.ACTION_EXEC: StageConfig(
                name=StageName.ACTION_EXEC,
                stage_type=StageType.LONG_RUNNING,
                idle_silence_timeout_s=90.0,  # 允许仿真工具长跑，90s 活跃心跳
                max_total_duration_s=1800.0,
            ),
            StageName.RENDER_DELIVER: StageConfig(
                name=StageName.RENDER_DELIVER,
                stage_type=StageType.LIGHTWEIGHT,
                hard_timeout_s=5.0,
            ),
        }

    async def run_stage(
        self,
        stage_name: StageName,
        action: Callable[[ActivityLeaseGuard], Awaitable[Any]],
    ) -> Any:
        cfg = self.configs.get(
            stage_name,
            StageConfig(name=stage_name, stage_type=StageType.LIGHTWEIGHT),
        )

        guard = ActivityLeaseGuard(
            idle_timeout_s=cfg.idle_silence_timeout_s,
            max_duration_s=cfg.max_total_duration_s,
        )

        if cfg.stage_type == StageType.LIGHTWEIGHT:
            # 轻量前置阶段：硬超时保护
            try:
                return await asyncio.wait_for(action(guard), timeout=cfg.hard_timeout_s)
            except asyncio.TimeoutError as exc:
                logger.error("轻量阶段 %s 超出硬时钟预算 (%.1fs)", stage_name.value, cfg.hard_timeout_s)
                raise TimeoutError(f"Stage {stage_name.value} exceeded budget {cfg.hard_timeout_s}s") from exc

        # 重型长任务阶段：静默看门狗并发巡检
        task = asyncio.create_task(action(guard))
        # 巡检周期：生产环境 500ms，短测试自适应
        check_interval = max(0.02, min(0.5, cfg.idle_silence_timeout_s / 3))

        while not task.done():
            await asyncio.sleep(check_interval)
            if guard.is_silent_dead():
                task.cancel()
                logger.error(
                    "重型长任务阶段 %s 触发纯静默超时 (静默 > %.1fs 零心跳)，判定底层死锁",
                    stage_name.value,
                    cfg.idle_silence_timeout_s,
                )
                raise TimeoutError(
                    f"Stage {stage_name.value} hung with silence > {cfg.idle_silence_timeout_s}s"
                )

        return await task

"""Agent 运行设计域 (ODD) 与安全包络守卫。

运行设计域 (ODD) 与最小风险降级 (MRM) 守卫契约：
1. 上下文体积守卫 (Context Envelope):
   - 500K Tokens / 2MB 安全上限，杜绝无节制膨胀拖垮上游模型。
   - 跨天自然日自动建议/触发开启新分支 (Session Reset on Date Rollover)。
   - 达到 80% 高水位 (400K Tokens) 时在卡片上输出保底提示。
2. 推理重试预算 (Error Budget):
   - 遇到上游 500/ConnectionReset/ZlibError 时，最大重试预算收敛为 1 次，
     严禁 5 次盲目指数退避让用户干等数分钟。
3. 认知分层路由契约:
   - 本地模型做轻量杂活 (意图路由/简单分类/前置分级)。
   - 远端 1M 大模型做深度长任务 (状态机分析/用例生成/代码排查)。
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 默认安全阈值 (基于 1M 模型的 50% 负荷基线)
DEFAULT_MAX_CONTEXT_TOKENS: int = 500_000
DEFAULT_MAX_CONTEXT_BYTES: int = 2 * 1024 * 1024  # 2 MB
DEFAULT_WARN_WATERMARK_RATIO: float = 0.80       # 80% 高水位触发保底提醒 (400K Tokens / 1.6MB)
DEFAULT_MAX_ERROR_RETRIES: int = 1               # 上游异常至多重试 1 次即熔断


@dataclass
class EnvelopeStatus:
    """安全包络健康状态快照。"""
    within_odd: bool
    tokens_estimated: int
    bytes_count: int
    watermark_ratio: float
    needs_compact: bool
    needs_day_reset: bool
    warning_notice: str | None = None
    mrm_triggered: bool = False
    mrm_reason: str | None = None


@dataclass
class SessionEnvelopeTracker:
    """跟踪会话的生命周期、跨天状态与上下文累积。"""
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    creation_date: str = field(
        default_factory=lambda: datetime.datetime.now().strftime("%Y-%m-%d")
    )
    total_bytes: int = 0
    max_bytes: int = DEFAULT_MAX_CONTEXT_BYTES
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS

    def check_day_rollover(self, now_date: str | None = None) -> bool:
        """检查会话是否已跨越自然日 (用于按天静默切换新 Session)。"""
        current_date = now_date or datetime.datetime.now().strftime("%Y-%m-%d")
        return current_date != self.creation_date

    def assess_envelope(self, payload_bytes: int, now: float | None = None) -> EnvelopeStatus:
        """评估当前请求是否在安全域 (ODD) 内。"""
        current_ts = now or time.time()
        self.last_active_at = current_ts
        self.total_bytes = payload_bytes

        # 估算 Tokens (英文 4 字节/Token, 中文 2~3 字节/Token, 按平均 3.5 字节估算)
        tokens_est = max(1, int(payload_bytes / 3.5))
        ratio = payload_bytes / float(self.max_bytes)
        day_rolled = self.check_day_rollover()

        needs_compact = ratio >= DEFAULT_WARN_WATERMARK_RATIO
        within_odd = payload_bytes <= self.max_bytes

        warning: str | None = None
        if needs_compact and within_odd:
            warning = (
                f"[ODD-WARN] 当前会话体量已达 {payload_bytes // 1024}KB "
                f"(~{tokens_est // 1000}k tokens，占用安全水位 {int(ratio * 100)}%)，"
                f"建议发送 /reset 开启新话题以保持极致流畅。"
            )
        elif not within_odd:
            warning = (
                f"[ODD-BREACH] 当前会话体量 ({payload_bytes // 1024}KB) 超出 500K 安全上限 "
                f"({self.max_bytes // 1024}KB)，已触发保护性降级。"
            )

        mrm_triggered = not within_odd
        mrm_reason = "CONTEXT_OVERFLOW" if not within_odd else None

        return EnvelopeStatus(
            within_odd=within_odd,
            tokens_estimated=tokens_est,
            bytes_count=payload_bytes,
            watermark_ratio=ratio,
            needs_compact=needs_compact,
            needs_day_reset=day_rolled,
            warning_notice=warning,
            mrm_triggered=mrm_triggered,
            mrm_reason=mrm_reason,
        )


class UpstreamErrorBudgetGuard:
    """管理针对上游模型请求的重试预算，防止死循环重传。"""

    def __init__(self, max_retries: int = DEFAULT_MAX_ERROR_RETRIES):
        self.max_retries = max_retries
        self._retries_used = 0

    def record_failure(self, error: Exception | str) -> bool:
        """记录一次失败。返回 True 表示允许重试，False 表示预算耗尽应立刻触发 MRM。"""
        self._retries_used += 1
        err_msg = str(error)
        logger.warning(
            "上游模型请求失败 (第 %d/%d 次): %s",
            self._retries_used,
            self.max_retries,
            err_msg,
        )
        return self._retries_used <= self.max_retries

    @property
    def should_trigger_mrm(self) -> bool:
        return self._retries_used > self.max_retries

    def get_mrm_degraded_response(self, elapsed_seconds: float) -> str:
        """生成符合零 Emoji 规范的最小风险降级答复 (MRM)。"""
        return (
            "[FAIL] 上游服务连接异常，已触发安全降级熔断 (MRM)\n"
            "────────────────────────────────\n"
            f"耗时: {elapsed_seconds:.1f}s  |  重试耗尽: {self._retries_used} 次\n"
            "诊断: 上游模型网关无响应或传输中断，已主动停止后续无谓重传。\n"
            "建议: 请检查网络连通性或稍后重发；若此前对话较长，建议输入 /reset 清空上下文。"
        )


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""突触可塑性反馈：任务终态时，对该任务用过的记忆做强化/抑制。

为什么必须有 trace 才能做（2026-10-23）：
    此前 reinforce_memory 只能被人工显式调用，账本里 1 条记录、reinforce_count=1。
    如果拿「任务 PASS」直接给所有记忆加分，等于给那些压根没被用到的记忆也记功，
    那不是学习，是噪声累积。所以这里**只处理 memory_trace 里有记录的事实**。

衰减为什么不能照搬 -0.15 / 3 次降级：
    初始 0.85，一次失败 -0.15，三次就跌破 0.40 自动 deprecated。而实测里三次失败
    完全可能全是环境问题（容器没起、端口占用、日志属主错误）。把环境噪声直接换算成
    知识损毁，是比不学习更糟的事。因此：
        - 只降级为 suspect（仍然召回，但排序靠后），不直接 deprecated；
        - 需要**连续** N 次失败才降级，中间任何一次成功即清零；
        - 环境类失败（ENV_UNAVAILABLE）不计入，只记日志。

.. warning::
   **STATUS: 未接线（NOT WIRED）。**
   本模块在仓内没有任何调用方（代码与测试均零引用），保留为已设计但尚未接入
   主链路的参考实现。不要按「它已在工作」来理解记忆引擎的行为；
   接线前请先补调用方与对应测试。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("plasticity"))

#: 连续失败多少次才降级为 suspect
DEMOTE_AFTER_CONSECUTIVE_FAILS = 5
#: 晋级为高优规则的置信度门槛
PROMOTE_AT_CONFIDENCE = 0.85
#: 环境类失败不算记忆的错
ENV_FAILURE_MARKERS = ("ENV_UNAVAILABLE", "env_unavailable", "环境不可用")


def is_env_failure(*texts: str) -> bool:
    """失败原因是环境问题吗？是的话不该惩罚记忆。"""
    blob = " ".join(str(t or "") for t in texts)
    return any(m in blob for m in ENV_FAILURE_MARKERS)


def apply_task_outcome(task_id: str, passed: bool, failure_text: str = "",
                       target: str = "") -> Dict[str, Any]:
    """按任务结果回写突触。只作用于该任务真正用过的记忆。

    返回结构化结果，调用方（钩子）不需要 try。
    """
    try:
        from . import memory_trace
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_trace 不可用，跳过可塑性反馈: %s", exc)
        return {"ok": False, "error": str(exc), "updated": 0}

    uses = memory_trace.uses_of_task(task_id)
    if not uses:
        return {"ok": True, "updated": 0, "reason": "no_memory_used"}

    if not passed and is_env_failure(failure_text):
        logger.info("任务 %s 为环境类失败，不惩罚任何记忆", task_id)
        return {"ok": True, "updated": 0, "reason": "env_failure_ignored"}

    cfg = get_config()
    # 宿主已有账本实现时用它 —— 同一个账本只能有一个 owner。
    # 引擎自带实现只在宿主没提供时兜底（独立使用场景）。
    from . import synapse
    reinforcer = cfg.reinforcer or synapse.reinforce

    results: List[Dict[str, Any]] = []
    for use in uses:
        fid = use.get("fact_id")
        if not fid:
            continue
        try:
            r = reinforcer(fid, success=bool(passed), target=target,
                           reason="" if passed else (failure_text or "")[:200])
            results.append({"fact_id": fid, "confidence": r.get("confidence"),
                            "status": r.get("status"), "action": r.get("action")})
        except Exception as exc:  # noqa: BLE001 —— 单条失败不影响其他条
            logger.warning("记忆 %s 强化失败: %s", fid, exc)
            results.append({"fact_id": fid, "error": str(exc)})

    return {"ok": True, "updated": len(results), "results": results,
            "task_id": task_id, "passed": passed}

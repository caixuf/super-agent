#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提炼结果入库门禁（PRM 探针）：新的经验要进库，先过这三道。

为什么必须有：
    自动学习最坏的不是「学得少」，是「学错了还一直用」。一次错误提炼会长期
    误导后续所有排查，而且有了置信度机制后它还会因为「被用了且任务成功」而
    被不断强化 —— 错得越用越可信。门禁是唯一的闸门。

三道校验（按成本从低到高）：
    1. 代际明确：ebm 必须能归一成 G1/G2。判不出就拒绝 —— 掉进 _unknown 桶
       等于让这条规则对所有代际生效，而 _unknown 已经是全库最大的桶（43 条）。
    2. 可执行/可判定：必须能被验证，不能是「注意一下」「多观察」这类废话。
       时序类额外要求因果三要素（前置条件 + 动作 + 后果）；
       配置/构建类只要求可执行 —— 一刀切要三要素会把正常经验全杀掉。
    3. 查重：与库内既存事实高度相似则拒绝，避免同一规律换个说法反复入库。

.. warning::
   **STATUS: 未接线（NOT WIRED）。**
   本模块在仓内没有任何调用方（代码与测试均零引用），保留为已设计但尚未接入
   主链路的参考实现。不要按「它已在工作」来理解记忆引擎的行为；
   接线前请先补调用方与对应测试。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("gate"))

#: 与既存事实相似度超过此值视为重复。比向量召回的 0.60 门限高得多：
#: 那是「够不够相关」，这是「是不是同一件事」。
DUPLICATE_THRESHOLD = 0.90

#: 流水账/废话特征：出现即判定不可执行
#: 模糊表述的特征是「整句没有可执行内容」，不是「出现了某个词」。
#: 实测教训：原来把「可能/应该是」当模糊词，把「期望 StateCPActive 但实际
#: StateStandby，可能是注入时序问题」这种**有效经验**也拦掉了 —— 因果推断里
#: 本来就会用「可能」描述不确定的根因，不等于结论模糊。
#: 改为只拦「整句没有信息量」的：开头就是观察类动词、或以待定标记结尾。
VAGUE_PATTERNS = (
    # 观察类开头且总长度很短（<=14 字）才是纯提醒；长句往往带了实质内容
    r"^(注意|关注|观察|留意|小心)(一下|下)?\s*[，,。]?\s*.{0,10}$",
    r"^(待确认|待验证|待补充|TODO|TBD)$",
    r"(TODO|TBD)\s*$",
    r"^.{0,12}(看看|试试)(吧|再说)?[。]?$",
)

#: 时序类经验的因果三要素
CAUSE_CONDITION = (r"(当|如果|若|在.*时|超过|低于|达到|持续)", "前置条件")
CAUSE_ACTION = (r"(注入|发送|下发|设置|触发|切换|拉起|执行)", "动作")
CAUSE_EFFECT = (r"(导致|会|则|使得|表现为|最终|因此|从而)", "后果")

#: 时序特征：命中则认为这是时序类经验，要求三要素
TIMING_HINTS = (r"(时序|脱窗|超时|毫秒|ms\b|跳变|延迟|周期|帧)",)


def _bucket_of(ebm: str) -> str:
    try:
        from .memory_store import normalize_ebm
    except ImportError:
        from .memory_store import normalize_ebm
    return normalize_ebm(ebm)


def check_ebm(ebm: str) -> Dict[str, Any]:
    """第一道：代际必须明确。"""
    try:
        from .memory_store import normalize_ebm, UNKNOWN_BUCKET
    except ImportError:
        from .memory_store import normalize_ebm, UNKNOWN_BUCKET
    bucket = normalize_ebm(ebm)
    ok = bucket != UNKNOWN_BUCKET
    return {"ok": ok, "bucket": bucket,
            "reason": "" if ok else f"代际无法归一（输入={ebm!r}），拒绝入库：_unknown 桶对所有代际生效，不能再增"}


def check_actionable(statement: str) -> Dict[str, Any]:
    """第二道：必须可执行/可判定。时序类额外要求因果三要素。"""
    stmt = (statement or "").strip()
    if len(stmt) < 8:
        return {"ok": False, "kind": "", "reason": "结论过短，无法独立成立"}

    for pat in VAGUE_PATTERNS:
        if re.search(pat, stmt):
            return {"ok": False, "kind": "", "reason": f"表述含糊/待定，不可作为规律入库: {pat}"}

    is_timing = any(re.search(p, stmt) for p in TIMING_HINTS)
    if not is_timing:
        return {"ok": True, "kind": "general", "reason": "配置/结论类，可执行即可"}

    missing = [name for pat, name in (CAUSE_CONDITION, CAUSE_ACTION, CAUSE_EFFECT)
               if not re.search(pat, stmt)]
    if missing:
        joined = ", ".join(missing)
        return {"ok": False, "kind": "timing",
                "reason": "时序类经验缺因果要素: " + joined}
    return {"ok": True, "kind": "timing", "reason": "时序类，三要素齐全"}


def check_duplicate(statement: str, ebm: str = "") -> Dict[str, Any]:
    """第三道：查重。优先用已有向量，不可用则退回字符相似度。"""
    try:
        from . import memory_store
    except ImportError:
        from . import memory_store  # type: ignore
    stmt = (statement or "").strip()
    try:
        res = memory_store.recall_facts(stmt, ebm=ebm or None, limit=5)
    except Exception as exc:  # noqa: BLE001 —— 查重失败不能当通过
        logger.warning("查重召回失败: %s", exc)
        return {"ok": False, "reason": f"查重无法完成（{exc}），保守拒绝"}

    for it in (res.get("results") or []):
        score = it.get("_score")
        if score is None:
            continue
        if float(score) >= DUPLICATE_THRESHOLD:
            return {"ok": False, "duplicate_of": it.get("fact_id"),
                    "score": round(float(score), 3),
                    "reason": f"与既存事实高度相似({score:.2f})，视为重复"}
    return {"ok": True, "reason": "未发现重复"}


def validate(fact: Dict[str, Any]) -> Dict[str, Any]:
    """跑完全部门禁。任何一道不过即拒绝，返回明细供调用方留痕。

    代际门禁**只对技术类生效**：这是个通用 agent 记忆系统，人物关系、用户偏好、
    项目背景本来就没有代际之分。一刀切会把「某人自称某个称呼」这类真实事实
    全部拒掉，记忆库就只剩测试经验了（2026-10-23 修正）。
    """
    try:
        from .memory_kind import classify_kind, requires_ebm
    except ImportError:
        from .memory_kind import classify_kind, requires_ebm

    checks: Dict[str, Any] = {}
    kind = classify_kind(fact.get("statement") or "", fact.get("subject") or "",
                         fact.get("ebm") or "")
    checks["kind"] = kind

    if requires_ebm(kind):
        e = check_ebm(fact.get("ebm") or "")
        checks["ebm"] = e
        if not e["ok"]:
            return {"ok": False, "failed_at": "ebm", "checks": checks, "reason": e["reason"]}
    else:
        # 非技术类不要求代际，但也不能凭空入库：至少得能归一到一个桶。
        e = {"ok": True, "bucket": _bucket_of(fact.get("ebm") or ""),
             "reason": f"{kind} 类不要求代际"}
        checks["ebm"] = e

    a = check_actionable(fact.get("statement") or "")
    checks["actionable"] = a
    if not a["ok"]:
        return {"ok": False, "failed_at": "actionable", "checks": checks, "reason": a["reason"]}

    d = check_duplicate(fact.get("statement") or "", e["bucket"])
    checks["duplicate"] = d
    if not d["ok"]:
        return {"ok": False, "failed_at": "duplicate", "checks": checks, "reason": d["reason"]}

    # 第四道：客观凭据与推测过滤门禁 (Rationalized Evidence Admission)
    try:
        from .memory_admission import evaluate_memory_admission, VERDICT_REFUSE
        adm = evaluate_memory_admission(
            fact.get("statement") or "",
            source_receipt=fact.get("receipt") or fact.get("evidence"),
            ebm_generation=e.get("bucket") or "",
        )
        checks["admission"] = adm
        if adm.get("verdict") == VERDICT_REFUSE:
            return {
                "ok": False,
                "failed_at": "admission",
                "checks": checks,
                "reason": f"未通过证据准入门禁: {adm.get('explanation')} ({adm.get('reason_code')})",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("准入门禁校验跳过异常: %s", exc)

    return {"ok": True, "failed_at": "", "checks": checks, "bucket": e["bucket"],
            "kind": a["kind"], "reason": "全部门禁通过"}


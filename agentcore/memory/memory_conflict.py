#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""矛盾自动处理：新事实与库里已有事实冲突时，自动判定「取代 / 并存 / 各自独立」。

为什么不能只靠向量相似度（2026-10-21 真实事故）：
    先写 workspace_id=G1「门限 3.0%」，再写 G2「门限 5.0%」，
    Hy-Memory 的 Extractor 只看语义相似，把 G1 那条标成 superseded ——
    它不知道 G1/G2 是两代不同的代际。所以**代际不同的永远不是矛盾**，
    这是一条规定，不是一个概率。

为什么开源 Mem0 帮不上：v2 把合并/取代移到了付费的 Dream 功能里，
    开源路径是 ADD-only，矛盾要靠读取时按时间消解。我们要的是**写入时判定**，
    这样库里不留一堆互相矛盾的事实让每次召回都去猜。

三层判定（从便宜到贵，前一层能定就不走下一层）：
    1. 代际不同 -> 并存（规则，零成本，最容易出错的一律交规则）
    2. 文本高度相似且完全相同 -> 重复（不写）
    3. 其余交给 LLM 判定：取代 / 并存 / 无关 / 不写

取代不删除：写 status=superseded + superseded_by，保留时间线。
删掉就无法回答「当时为什么这么定」。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("conflict"))

#: 相似度达到这个值且正文几乎相同，直接判重复，不必花 LLM 调用
DUPLICATE_SIMILARITY = 0.97
#: 低于这个相似度的候选不参与冲突判定（不相干的两件事比了也没用）
CANDIDATE_FLOOR = 0.55


CONFLICT_SYSTEM = """你负责判断「新记录」与「已有记录」是否矛盾。只回答关系，不改写内容。

判定标准：
- supersede：两条在说**同一件事**，但结论不同（数值改了、做法换了、结论被推翻）。
  例：旧「门限 3%」 vs 新「门限 5%」 -> 新取代旧
- coexist：两条讲的是**不同范围**，可以同时成立（不同场景/不同模块/不同阶段）。
  例：「代际甲用 A 方案」vs「代际乙用 B 方案」 -> 并存
- duplicate：两条说的是**同一件事、同一结论**，只是措辞略有不同 -> 不写新的。
  例：旧「代际乙门限 5.0%，代际甲 3.0% 不再适用」vs 新「代际乙门限 5.0%，代际甲原有 3.0% 作废」 -> 重复
- unrelated：两条其实不在讨论同一个主题。
- reject_new：新记录表述含糊、脱离上下文看不懂、或明显是临时状态 -> 不写

特别注意：数值不同**不等于**矛盾。先看它们是否约束同一对象。
只有当两条无法同时为真时才是 supersede。

输出严格 JSON（不要 markdown 代码块）：
{"relation":"supersede|duplicate|coexist|unrelated|reject_new","reason":"一句话理由"}
"""


def _norm(text: str) -> str:
    """归一化：去空白、统一全半角标点，用于「是否完全相同」的判定。"""
    t = (text or "").strip().lower()
    return re.sub(r"[\s，。；、！？,.!?;:]+", "", t)


def _similarity(a: str, b: str) -> float:
    """字符级 bigram Jaccard。中短句上足够分辨「同主题改写」和「不相干」。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ga = {na[i:i + 2] for i in range(len(na) - 1)}
    gb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def classify_by_rules(new: Dict[str, Any], old: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """规则层。能定就返回结论，定不了返回 None 交给 LLM。

    old/new 都要有 statement、workspace_id（代际桶）。
    """
    new_bucket = new.get("workspace_id") or ""
    old_bucket = old.get("workspace_id") or ""
    # 代际不同 = 两代车，永远并存。这条必须排在一切相似度判断之前。
    # 历史事故就是死在这里：只看语义相似，把 G1 的经验判给了 G2。
    if new_bucket and old_bucket and new_bucket != old_bucket:
        return {"relation": "coexist", "reason": "different_ebm_bucket", "by": "rule"}

    new_norm = _norm(new.get("statement", ""))
    old_norm = _norm(old.get("statement", ""))
    if new_norm and new_norm == old_norm:
        return {"relation": "duplicate", "reason": "identical_after_normalize",
                "similarity": 1.0, "by": "rule"}
    sim = _similarity(new.get("statement", ""), old.get("statement", ""))
    if sim >= DUPLICATE_SIMILARITY:
        return {"relation": "duplicate", "reason": "near_identical_text",
                "similarity": round(sim, 3), "by": "rule"}
    return None


def _load_chat_completions():
    """取当前可用的对话补全函数，取不到返回 None（冲突判定改为 defer）。

    引擎不自行导入 LLM 客户端，由宿主通过 MemoryConfig.llm_fn / llm_loader
    注入。历史教训：写死包名 + 补 sys.path 的加载方式，一旦失败就静默落到
    `llm_unavailable` 兜底，表现为「可疑重复一律并存」，库里不停积累同义事实。
    """
    return get_config().resolve_llm()


def judge(new: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
    """判定单条冲突。规则优先，规则定不了才走 LLM。永远有返回值。"""
    by_rule = classify_by_rules(new, old)
    if by_rule:
        return by_rule

    chat_completions = _load_chat_completions()
    if chat_completions is None:
        # An unavailable reviewer cannot establish that two facts may coexist.
        # Automatic writers surface this as a retryable/deferred outcome and
        # keep the old fact authoritative.
        return {"relation": "defer", "reason": "llm_unavailable", "by": "fallback"}

    prompt = "【新记录】\n主题: %s\n内容: %s\n\n【已有记录】\n主题: %s\n内容: %s" % (
        new.get("subject") or "(无)", new.get("statement") or "",
        old.get("subject") or "(无)", old.get("statement") or "")
    try:
        res = chat_completions(
            [{"role": "system", "content": CONFLICT_SYSTEM},
             {"role": "user", "content": prompt}],
            timeout=30.0, max_tokens=200, temperature=0.0,
        )
        verdict = parse_verdict((res or {}).get("content") or "")
        if verdict:
            verdict["by"] = "llm"
            return verdict
    except Exception as exc:
        logger.warning("冲突判定调用失败，保留旧事实并延后新候选: %s", type(exc).__name__)
    return {"relation": "defer", "reason": "judge_failed", "by": "fallback"}


VALID_RELATIONS = ("supersede", "duplicate", "coexist", "unrelated", "reject_new")


def parse_verdict(raw: str) -> Optional[Dict[str, Any]]:
    """解析模型输出，非法 relation 一律视为解析失败（由调用方兜底）。"""
    text = (raw or "").strip().replace("```", "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        # 模型输出偶尔带说明文字或截断；解析不出来就交回调用方兜底（按并存处理），
        # 但必须留痕迹——否则「判定失败」会被误读成「判定为并存」。
        logger.warning("冲突判定结果无法解析: %s", type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    relation = str(data.get("relation") or "").strip().lower()
    if relation not in VALID_RELATIONS:
        return None
    return {"relation": relation, "reason": str(data.get("reason") or "")[:200]}


def resolve(new: Dict[str, Any], candidates: List[Dict[str, Any]],
            *, conservative: bool = False,
            max_candidates: int = 16,
            time_budget_seconds: float = 10.0) -> Dict[str, Any]:
    """对新事实与其候选集做整体裁决。

    设计要点：
    - reject_new 优先级最高：新记录本身不合格就别写，不必再比。
    - supersede 只会指向**最相似的那一条**：一条新事实同时取代多条通常是模型
      过度归纳（历史教训：一条 G2 经验把三条 G1 全标掉了）。
    - 无法裁决时 action=skip / relation=defer：保留旧事实，隔离新候选。
    """
    if not candidates:
        return {"action": "write", "relation": "new", "superseded": [], "judged": []}
    if conservative and len(candidates) > max(1, int(max_candidates)):
        return {"action": "skip", "relation": "defer",
                "reason": "conflict_budget_exceeded", "superseded": [], "judged": []}

    started = time.monotonic()
    judged: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None
    best_sim = -1.0
    new_subject = _norm(new.get("subject", ""))
    for old in candidates:
        sim = _similarity(new.get("statement", ""), old.get("statement", ""))
        # 同 subject 必须进判定，不受文本相似度下限约束。
        # 数值变更是最该被 supersede 的一类（门限 3%->5%、阈值改了），但
        # 「G2 的门限定为 5.0%」和「改为 4.0%」的 bigram Jaccard 只有
        # 0.42，低于 CANDIDATE_FLOOR 就被直接筛掉 —— 旧结论永远取代不掉，
        # 库里新旧并存，每次召回都在猜哪条是真的（2026-10-23 实测）。
        same_subject = bool(new_subject) and new_subject == _norm(old.get("subject", ""))
        if sim < CANDIDATE_FLOOR and not same_subject and not conservative:
            continue
        if conservative and time.monotonic() - started >= max(0.1, float(time_budget_seconds)):
            return {"action": "skip", "relation": "defer",
                    "reason": "conflict_time_budget_exceeded",
                    "superseded": [], "judged": judged}
        v = judge(new, old)
        v["against"] = old.get("fact_id")
        v["similarity"] = round(sim, 3)
        judged.append(v)
        if v["relation"] == "reject_new":
            return {"action": "skip", "relation": "reject_new",
                    "reason": v.get("reason"), "superseded": [], "judged": judged}
        if v["relation"] == "defer":
            # Fail closed: no reviewer verdict means no write. Keeping the old
            # fact plus a deferred audit is safer than silently accumulating
            # contradictory memories.
            return {"action": "skip", "relation": "defer",
                    "reason": v.get("reason"), "against": v.get("against"),
                    "superseded": [], "judged": judged}
        if v["relation"] == "duplicate":
            # 同一结论换了个说法：不写新行，指回已有的那条。
            # 这条分支以前缺失，duplicate 会被后面一律归成 coexist —— 于是
            # 抽取措辞每次略有不同，库里就多一条同义事实（2026-10-23 实测）。
            return {"action": "skip", "relation": "duplicate",
                    "reason": v.get("reason"), "duplicate_of": v["against"],
                    "superseded": [], "judged": judged}
        if v["relation"] == "supersede" and sim > best_sim:
            best_sim, best = sim, v

    if best:
        return {"action": "write", "relation": "supersede",
                "reason": best.get("reason"),
                "superseded": [best["against"]], "judged": judged}
    return {"action": "write", "relation": "coexist" if judged else "new",
            "reason": "no_supersede_verdict", "superseded": [], "judged": judged}

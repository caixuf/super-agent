#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领域推断：按内容判断一条记忆属于哪个业务面。

为什么需要（2026-10-23）：
    实测 19573 条里 15810 条落进 general，抽查发现大部分是**平台运维文档**
    和**书籍正文**（Linux 内核、自动驾驶），它们本该归 ops / system / autodrive。

    根因：旧的推断只看 subject（标题），而书籍切块的标题是文件名和章节名，
    往往看不出领域；正文里的关键词才是决定性证据。

打分策略：
    不搞「命中第一个就返回」—— 那会对跨领域内容误判。改成**加权计分**：
    不同关键词权重不同，取最高分。全部低于阈值才落 general ——
    general 应该是「确实判不出」，不是「没查」。

业务边界：
    具体业务词表（哪些词属于哪个领域）是**宿主约定**，由 MemoryConfig
    注入（seed_domain_signals / seed_source_prefixes）。本模块只提供打分
    与优先级机制，不含任何企业的领域名、代际名或专有名词。
"""

from __future__ import annotations

from typing import Sequence, Tuple

from .config import get_config

#: 低于这个分判 general。设 2：单个弱特征命中不足以定领域。
MIN_SCORE = 2


def _signals() -> Sequence[Tuple[str, int, str]]:
    return get_config().seed_domain_signals


def _source_prefixes() -> Sequence[Tuple[str, str]]:
    return get_config().seed_source_prefixes


def _infer_by_keywords(subject: str = "", statement: str = "", ebm: str = "") -> Tuple[str, int]:
    """返回 (领域, 得分)。得分低于阈值的调用方应落 general。

    ebm 非空直接判「带代际的那个领域」—— 有代际就说明是本地业务对象的行为，
    这是最强信号。具体是哪个领域由宿主通过 era_domains 声明，不写死在引擎里。
    """
    cfg = get_config()
    _e = (ebm or "").strip()
    # 只有**真实代际**才算最强信号。unknown 桶是「判不出代际」的占位符，
    # 把它当代际会让所有条目都判成同一个领域。
    if _e and _e != cfg.unknown_bucket and cfg.era_domains:
        return str(cfg.era_domains[0]), 99

    import re
    blob = (subject or "") + " " + (statement or "")
    scores: dict = {}
    for pattern, weight, domain in _signals():
        hits = len(re.findall(pattern, blob, re.IGNORECASE))
        if hits:
            # 多个不同信号叠加，但同一条重复命中不无限累加（封顶 3 次）
            scores[domain] = scores.get(domain, 0) + weight * min(hits, 3)
    if not scores:
        return "general", 0
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0], best[1]


# ---------------------------------------------------------------------------
# 来源文件提示：书籍/文档成体系，同文件的块领域理应一致。
# 比逐块猜关键词准得多 —— 实测块级关键词只能覆盖一半。
# ---------------------------------------------------------------------------
# 用前缀匹配而不是复杂正则：书籍文件名很规整（linux-xxx.md / sensors.md），
# 拼接长正则会因为括号未闭合直接炸掉整个推断（实测踩过）。
def infer_from_source(subject: str = "") -> str:
    """按来源文件前缀推断领域。判不出返回空串。"""
    s = (subject or "").strip().lower()
    if not s:
        return ""
    for prefix, domain in _source_prefixes():
        # 既要匹配开头，也要匹配路径中段：
        # apachecn 的书路径是 docs/master-linux-sec-hard/05.md,
        # 前缀是 docs/，但目录名里明明有 linux/security 这些强信号。
        if s.startswith(prefix) or ("/" + prefix) in s or ("-" + prefix) in s:
            return domain
    return ""


def infer_domain(subject: str = "", statement: str = "", ebm: str = "") -> str:
    """领域判定优先级：来源文件 > 正文关键词 > general。

    来源优先：同一本书的块领域应当一致，而单块正文常是半句话，
    关键词命中率很低（实测块级只能覆盖一半）。

    来源命中即采信，关键词不再参与投票：
    来源回答「这本书讲什么」，关键词只回答「这一段碰巧提到什么」。
    在成体系的教材里，后者必然大量误报 —— AI 术语本来就散落在 Python、
    Linux、算法书里。实测病症：apachecn-python-zh 是 Python 教程，但正文
    embedding/prompt/LLM 密度极高，6.2 万条全被判成 ai。
    曾试过「关键词得分够高才推翻来源」，同样不成立：一句「用 embedding
    和 prompt 做大模型微调」在 Linux 内核书里也能拿 15 分。
    """
    from_src = infer_from_source(subject)
    if from_src:
        return from_src
    domain, score = _infer_by_keywords(subject, statement, ebm)
    return domain if score >= MIN_SCORE else "general"
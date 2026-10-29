#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆作用域（scope）：与代际正交的第二分桶维度。

为什么需要（2026-10-23 架构修正）：
    原先 workspace_id 只有「代际」一个维度（G1/G2/_unknown）。
    结果是把 1.6 万条书籍/文档灌进去时，只能全部落进 _unknown ——
    实测 _unknown 里堆了 3494 条书籍、3030 条文档，而 G2 桶只有 1 条。
    查「G2 门限」时，书籍里讲 Linux 中断的段落会被一起召回，
    **真正的平台经验被淹没**。

    问题不在数据多少，在**维度混用**：
      - 代际（ebm）回答「哪一代车适用」
      - 作用域（scope）回答「这是什么性质的知识」
    两者正交，必须分开存。

作用域定义：
    platform  宿主平台自有经验（G1/G2 代际 + 判不出代际的零散事实）
    reference 外部参考书/通用文档（Linux 内核、自动驾驶通识）
    ops       运维与流程知识（部署、发布、巡检）

存储方式：scope 进 tags，**不进 workspace_id**。
    这样保持 workspace_id 的语义纯粹（只有代际），
    召回时 scope 作为附加过滤条件 —— 加维度不破坏既有数据。
"""

from __future__ import annotations

from typing import Optional, Sequence

SCOPE_PLATFORM = "platform"
SCOPE_REFERENCE = "reference"
SCOPE_OPS = "ops"

ALL_SCOPES = (SCOPE_PLATFORM, SCOPE_REFERENCE, SCOPE_OPS)

#: 默认作用域。没有显式声明的一律按平台自有经验处理 ——
#: 保守选择：既有数据都是平台经验，新增的必须显式标 reference。
DEFAULT_SCOPE = SCOPE_PLATFORM

#: source 到 scope 的默认映射。让灌入侧不必每个调用点都手工传。
SOURCE_SCOPE = {
    "book_ingest": SCOPE_REFERENCE,
    "doc_ingest": SCOPE_REFERENCE,
    "internet_ingest": SCOPE_REFERENCE,
    "auto_trigger": SCOPE_PLATFORM,
    "auto_learn": SCOPE_PLATFORM,
    "system_seed": SCOPE_PLATFORM,
}


def normalize_scope(scope: Optional[str]) -> str:
    s = (scope or "").strip().lower()
    return s if s in ALL_SCOPES else DEFAULT_SCOPE


def scope_for_source(source: Optional[str]) -> str:
    """按来源推断作用域。未登记的来源按平台经验处理。"""
    return SOURCE_SCOPE.get((source or "").strip(), DEFAULT_SCOPE)


def filter_clause(scopes: Optional[Sequence[str]]) -> tuple:
    """生成召回用的 WHERE 片段与参数。

    没有任何 scope 标签的旧数据一律视为 platform —— 加这个维度之前入库的
    都是平台经验，不能因为缺标签就查不到。
    """
    wanted = [normalize_scope(s) for s in (scopes or [])]
    if not wanted:
        return "", []
    marks = ",".join("?" * len(wanted))
    sql = ("(EXISTS (SELECT 1 FROM fact_tags sc WHERE sc.fact_id = f.fact_id"
           " AND sc.key = 'scope' AND sc.value IN (" + marks + "))"
           " OR NOT EXISTS (SELECT 1 FROM fact_tags sc2 WHERE sc2.fact_id = f.fact_id"
           " AND sc2.key = 'scope'))")
    return sql, list(wanted)

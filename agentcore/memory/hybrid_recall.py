#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单进程混合召回与 RRF（Reciprocal Rank Fusion）重排器。

为什么需要这一层：
1. 消除双子进程冷启动开销：原先每次联合召回都要 fork 两个独立 Python 进程
   （recall_entity.py + recall_memory.py），单进程内直接组合 EntityStore 与
   memory_store，省去 80~150ms 解释器与连接初始化耗时。
2. 解决分数量纲不可比问题：EntityStore 产出 BM25 / 精确别名命中，memory_store
   产出 bge-m3 余弦相似度 × 突触权重。两边原始分数量纲不同，简单奇偶交错
   （zip interleave）会把高置信度的第 2 条事实排在低相关度的第 2 条规则后面。
   使用 Reciprocal Rank Fusion (RRF):
       score(d) = sum_{s in sources} weight_s / (rrf_k + rank_s(d))
   既不依赖原始分数量纲，又能让「实体规则库与事实库同时印证」的条目自动置顶。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .entity_store import EntityStore
from . import memory_store


def rrf_fuse(
    entity_hits: Sequence[Dict[str, Any]],
    memory_hits: Sequence[Dict[str, Any]],
    *,
    limit: int = 5,
    rrf_k: int = 60,
    entity_weight: float = 1.0,
    memory_weight: float = 1.0,
    interleave: bool = False,
) -> List[Dict[str, Any]]:
    """融合实体规则召回与长期事实召回结果。

    interleave=True 时保持传统奇偶交错去重顺序（向后兼容旧契约）；
    默认 interleave=False 时使用 RRF 倒数秩融合并保留禁令极性优先。
    """
    max_items = max(1, int(limit))
    if interleave:
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        e_list = list(entity_hits)
        m_list = list(memory_hits)
        for pair in zip(e_list + [None] * len(m_list), m_list + [None] * len(e_list)):
            for row in pair:
                if row is None or not isinstance(row, dict):
                    continue
                stmt = str(row.get("statement") or "").strip()
                if stmt and stmt not in seen:
                    rows.append(dict(row))
                    seen.add(stmt)
                    if len(rows) >= max_items:
                        return rows
        return rows

    k = max(1, int(rrf_k))
    merged: Dict[str, Dict[str, Any]] = {}
    order_seq = 0

    def _ingest(items: Sequence[Dict[str, Any]], source_name: str, weight: float) -> None:
        nonlocal order_seq
        for rank_idx, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                continue
            stmt = str(raw.get("statement") or "").strip()
            if not stmt:
                continue
            contrib = float(weight) / float(k + rank_idx)
            if str(raw.get("polarity") or "") == "prohibition":
                contrib += 0.002
            if stmt not in merged:
                entry = dict(raw)
                entry["statement"] = stmt
                entry["sources"] = [source_name]
                entry["_rrf_score"] = round(contrib, 6)
                entry["_first_order"] = order_seq
                order_seq += 1
                merged[stmt] = entry
            else:
                entry = merged[stmt]
                if source_name not in entry.get("sources", []):
                    entry.setdefault("sources", []).append(source_name)
                entry["_rrf_score"] = round(float(entry.get("_rrf_score", 0.0)) + contrib, 6)
                for key, val in raw.items():
                    if key not in entry or entry[key] in (None, ""):
                        entry[key] = val

    _ingest(entity_hits, "entity", entity_weight)
    _ingest(memory_hits, "memory", memory_weight)

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            -float(item.get("_rrf_score") or 0.0),
            0 if item.get("polarity") == "prohibition" else 1,
            int(item.get("_first_order") or 0),
        ),
    )
    out: List[Dict[str, Any]] = []
    for item in ranked[:max_items]:
        cleaned = dict(item)
        cleaned.pop("_first_order", None)
        out.append(cleaned)
    return out


def recall_hybrid(
    query: str,
    *,
    limit: int = 5,
    entity_db: Optional[Union[str, Path]] = None,
    ebm: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    tags: Optional[Dict[str, Any]] = None,
    kinds: Optional[Sequence[str]] = None,
    scopes: Optional[Sequence[str]] = None,
    task_id: Optional[str] = None,
    fusion: str = "rrf",
    rrf_k: int = 60,
) -> Dict[str, Any]:
    """单进程联合执行 EntityStore + memory_store 召回并做 RRF 融合。"""
    q = (query or "").strip()
    max_items = max(1, int(limit))
    if not q:
        return {
            "ok": True,
            "results": [],
            "entity_count": 0,
            "memory_count": 0,
            "fusion": fusion,
            "empty_reason": "empty_query",
        }

    estore = EntityStore(db_path=entity_db)
    entity_hits = estore.recall(q, limit=max_items)

    mem_res = memory_store.recall_facts(
        q,
        ebm=ebm,
        tags=tags,
        user_id=user_id,
        agent_id=agent_id,
        profile=profile,
        limit=max_items,
        kinds=kinds,
        scopes=scopes,
    )
    if not isinstance(mem_res, dict) or mem_res.get("ok") is not True:
        raise RuntimeError("memory_recall_failed")
    memory_hits = mem_res.get("results") or []

    fused = rrf_fuse(
        entity_hits,
        memory_hits,
        limit=max_items,
        rrf_k=rrf_k,
        interleave=(fusion == "interleave"),
    )

    if task_id:
        fact_ids = [
            str(item.get("fact_id") or "").strip()
            for item in fused
            if str(item.get("fact_id") or "").strip()
        ]
        if fact_ids:
            try:
                from . import memory_trace
                memory_trace.record(fact_ids, task_id=task_id, channel="hybrid_recall", query=q, ebm=ebm or "")
            except Exception:
                pass

    payload: Dict[str, Any] = {
        "ok": True,
        "results": fused,
        "entity_count": len(entity_hits),
        "memory_count": len(memory_hits),
        "fusion": fusion,
    }
    if not fused:
        payload["empty_reason"] = mem_res.get("empty_reason", "no_match")
    return payload

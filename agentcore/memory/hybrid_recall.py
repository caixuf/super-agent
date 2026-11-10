#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单进程混合召回、信噪比分层隔离、赫布 1-Hop 扩散激活与 RRF 重排器。

核心能力：
1. 【第0步：信噪比提纯与精确书签】
   默认优先从 `CORE_SCOPES = ('platform', 'principle', 'ops')` 召回个人与项目铁律，
   隔离 7.5 万条 `scope=reference` 电子书噪声；当命中 `file:` 或 `error:` 精确书签时自动置顶。
2. 【进化二：1-Hop 赫布突触扩散激活（Spreading Activation）】
   根据首轮命中的种子事实 `fact_id`，沿 `synapse.spreading_activation` 共激活边向外扩散 1 跳，
   自动捞出历史上共同促成任务成功的伴生经验（问 A 自动联想出 B）。
3. 【RRF 倒数秩融合】
   消除 EntityStore 与 memory_store 分数量纲差异，支持禁令极性（`prohibition`）与书签置顶加权。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .entity_store import EntityStore
from .memory_scope import CORE_SCOPES, SCOPE_REFERENCE
from . import memory_store, synapse

_LOG = logging.getLogger("superagent.agentcore.memory.hybrid_recall")


def rrf_fuse(
    entity_hits: Sequence[Dict[str, Any]],
    memory_hits: Sequence[Dict[str, Any]],
    *,
    spread_hits: Optional[Sequence[Dict[str, Any]]] = None,
    reference_hits: Optional[Sequence[Dict[str, Any]]] = None,
    query: str = "",
    limit: int = 5,
    rrf_k: int = 60,
    entity_weight: float = 1.0,
    memory_weight: float = 1.0,
    spread_weight: float = 0.85,
    reference_weight: float = 0.45,
    interleave: bool = False,
) -> List[Dict[str, Any]]:
    """融合实体规则召回、核心事实召回、1-Hop 突触扩散召回与参考书召回结果。

    interleave=True 时保持传统奇偶交错去重顺序（向后兼容旧契约）；
    默认 interleave=False 时使用 RRF 倒数秩融合，并叠加书签置顶与禁令极性优先。
    """
    max_items = max(1, int(limit))
    if interleave:
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        e_list = list(entity_hits)
        m_list = list(memory_hits) + list(spread_hits or []) + list(reference_hits or [])
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

    def _is_bookmarked(raw: Dict[str, Any]) -> bool:
        if raw.get("bookmark_pinned") is True:
            return True
        if query and memory_store._bookmark_boost(raw, query) > 0:
            return True
        return False

    def _ingest(items: Sequence[Dict[str, Any]], source_name: str, weight: float) -> None:
        nonlocal order_seq
        for rank_idx, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                continue
            stmt = str(raw.get("statement") or "").strip()
            if not stmt:
                continue
            contrib = float(weight) / float(k + rank_idx)
            polarity = str(raw.get("polarity") or (raw.get("tags") or {}).get("polarity") or "")
            if polarity == "prohibition":
                contrib += 0.002
            pinned = _is_bookmarked(raw)
            if pinned:
                contrib += 0.008
            if stmt not in merged:
                entry = dict(raw)
                entry["statement"] = stmt
                if pinned:
                    entry["bookmark_pinned"] = True
                entry["sources"] = [source_name]
                entry["_rrf_score"] = round(contrib, 6)
                entry["_first_order"] = order_seq
                order_seq += 1
                merged[stmt] = entry
            else:
                entry = merged[stmt]
                if pinned:
                    entry["bookmark_pinned"] = True
                if source_name not in entry.get("sources", []):
                    entry.setdefault("sources", []).append(source_name)
                entry["_rrf_score"] = round(float(entry.get("_rrf_score", 0.0)) + contrib, 6)
                for key, val in raw.items():
                    if key not in entry or entry[key] in (None, ""):
                        entry[key] = val

    _ingest(entity_hits, "entity", entity_weight)
    _ingest(memory_hits, "memory", memory_weight)
    if spread_hits:
        _ingest(spread_hits, "synapse_spread", spread_weight)
    if reference_hits:
        _ingest(reference_hits, "reference", reference_weight)

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            0 if item.get("bookmark_pinned") else 1,
            -float(item.get("_rrf_score") or 0.0),
            0 if (item.get("polarity") or (item.get("tags") or {}).get("polarity")) == "prohibition" else 1,
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
    exclude_reference: bool = True,
    include_reference: bool = False,
    enable_spreading: bool = True,
    enable_graph_expansion: bool = True,
    task_id: Optional[str] = None,
    fusion: str = "rrf",
    rrf_k: int = 60,
) -> Dict[str, Any]:
    """单进程联合执行 EntityStore + 核心事实库 + 1-Hop 突触扩散召回并做 RRF 融合。"""
    q = (query or "").strip()
    max_items = max(1, int(limit))
    if not q:
        return {
            "ok": True,
            "results": [],
            "entity_count": 0,
            "memory_count": 0,
            "spread_count": 0,
            "fusion": fusion,
            "empty_reason": "empty_query",
        }

    estore = EntityStore(db_path=entity_db)
    entity_hits = estore.recall(q, limit=max_items)

    # 第0步：默认只查核心作用域（platform / principle / ops），隔离 7.5 万条 reference 电子书噪音
    effective_scopes = list(scopes) if scopes is not None else (
        list(CORE_SCOPES) if (exclude_reference and not include_reference) else list(CORE_SCOPES)
    )
    mem_res = memory_store.recall_facts(
        q,
        ebm=ebm,
        tags=tags,
        user_id=user_id,
        agent_id=agent_id,
        profile=profile,
        limit=max_items,
        kinds=kinds,
        scopes=effective_scopes,
    )
    if not isinstance(mem_res, dict) or mem_res.get("ok") is not True:
        raise RuntimeError("memory_recall_failed")
    memory_hits = mem_res.get("results") or []

    # 如果显式要求补充 reference 书本（或 caller 指定 scopes 包含 reference）
    reference_hits: List[Dict[str, Any]] = []
    if scopes is None and include_reference and len(memory_hits) < max_items:
        ref_res = memory_store.recall_facts(
            q,
            ebm=ebm,
            tags=tags,
            user_id=user_id,
            agent_id=agent_id,
            profile=profile,
            limit=max(1, max_items - len(memory_hits)),
            kinds=kinds,
            scopes=[SCOPE_REFERENCE],
        )
        if isinstance(ref_res, dict) and ref_res.get("ok") is True:
            reference_hits = ref_res.get("results") or []

    # 进化二：1-Hop 赫布突触扩散激活（问 A 自动联想伴生经验 B，并对共激活候选做 RRF 协同加权）
    spread_hits: List[Dict[str, Any]] = []
    if enable_spreading:
        top_seeds = [
            str(item.get("fact_id") or "").strip()
            for item in [*memory_hits[:1], *entity_hits[:1]]
            if str(item.get("fact_id") or "").strip()
        ]
        all_seeds = [
            str(item.get("fact_id") or "").strip()
            for item in [*memory_hits, *entity_hits]
            if str(item.get("fact_id") or "").strip()
        ]
        if top_seeds:
            activated = synapse.spreading_activation(top_seeds, top_k=max(2, max_items))
            if not activated and len(all_seeds) > len(top_seeds):
                activated = synapse.spreading_activation(all_seeds, top_k=max(2, max_items))
            if activated:
                act_ids = [a["fact_id"] for a in activated]
                act_by_id = {a["fact_id"]: a for a in activated}
                loaded = memory_store.get_facts_by_ids(
                    act_ids,
                    user_id=user_id,
                    agent_id=agent_id,
                    profile=profile,
                )
                for row in loaded.get("results") or []:
                    r_tags = row.get("tags") or {}
                    if exclude_reference and not include_reference and r_tags.get("scope") == SCOPE_REFERENCE:
                        continue
                    enriched = memory_store.check_fact_freshness(dict(row))
                    meta = act_by_id.get(enriched["fact_id"], {})
                    enriched["rank_mode"] = "synapse_spread"
                    enriched["spread_score"] = meta.get("spread_score", 0.0)
                    enriched["activated_by"] = meta.get("activated_by", [])
                    spread_hits.append(enriched)

    # 进化三：强类型拓扑因果图展开（Graph Causal Expansion）
    graph_hits: List[Dict[str, Any]] = []
    if enable_graph_expansion:
        top_seed_ids = [
            str(item.get("fact_id") or "").strip()
            for item in [*memory_hits[:2], *entity_hits[:2]]
            if str(item.get("fact_id") or "").strip()
        ]
        if top_seed_ids:
            try:
                from . import memory_graph
                for sid in top_seed_ids:
                    paths = memory_graph.traverse_paths(
                        sid,
                        relation_types=[
                            memory_graph.REL_DEPENDS_ON,
                            memory_graph.REL_DERIVED_FROM,
                            memory_graph.REL_TESTS,
                        ],
                        max_depth=2,
                    )
                    graph_target_ids = [p["to_node"] for p in paths if p.get("to_node")]
                    if graph_target_ids:
                        loaded_g = memory_store.get_facts_by_ids(
                            graph_target_ids,
                            user_id=user_id,
                            agent_id=agent_id,
                            profile=profile,
                        )
                        for row in loaded_g.get("results") or []:
                            enriched = memory_store.check_fact_freshness(dict(row))
                            enriched["rank_mode"] = "graph_causal"
                            enriched["causal_origin"] = sid
                            graph_hits.append(enriched)
            except Exception as g_exc:
                _LOG.debug("拓扑图因果扩展跳过: %s", g_exc)

    fused = rrf_fuse(
        entity_hits,
        memory_hits,
        spread_hits=[*spread_hits, *graph_hits],
        reference_hits=reference_hits,
        query=q,
        limit=max_items,
        rrf_k=rrf_k,
        interleave=(fusion == "interleave"),
    )

    # 真值系统过滤：杜绝返回 superseded 淘汰知识
    fused = [
        item for item in fused
        if str(item.get("validity_state") or item.get("status") or "").upper() != "SUPERSEDED"
    ]

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
            except Exception as exc:
                _LOG.warning("failed to record memory_trace for task %s: %s", task_id, exc)

    from . import memory_scale
    db_strategy = mem_res.get("strategy")
    if not db_strategy:
        try:
            db_strategy = memory_scale.index_stats(str(memory_store._db_path())).get("strategy", "fts5_bm25_only")
        except Exception:
            db_strategy = "fts5_bm25_only"

    payload: Dict[str, Any] = {
        "ok": True,
        "results": fused,
        "entity_count": len(entity_hits),
        "memory_count": len(memory_hits),
        "spread_count": len(spread_hits),
        "reference_count": len(reference_hits),
        "fusion": fusion,
        "rrf_k": int(rrf_k),
        "strategy": db_strategy,
    }
    if not fused:
        payload["empty_reason"] = mem_res.get("empty_reason", "no_match")
    return payload

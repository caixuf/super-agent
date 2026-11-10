#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进化四：慢波睡眠巩固引擎（Slow-Wave Sleep Consolidation Engine）。

模拟生物大脑夜间慢波睡眠（SWS）的三大整理机制：
1. 海马体 -> 新皮层升维（Cortical Promotion）：
   扫描海马体（`working_fact` + `synapse.json`）中满足高置信度（>= promote_confidence）
   且经过多次真实任务强化（>= min_reinforcements）或来自显式纠错闪存（`flash_write` / `system_seed`）
   的核心事实，自动提取触发实体词与动作极性，将其晋升写入 `<2ms` 确定性召回的 `EntityStore` 新皮层规则库。
2. 同主题碎片归并（Fragment Compaction）：
   同一身份（`user_id`, `agent_id`, `workspace_id`, `subject`）下若存在多条语义高度重叠的碎片事实，
   保留最新/最高权重的一条，将冗余旧碎片标记为 `superseded`，并将其赫布共激活突触边（`co_edges`）
   迁移合并到幸存事实名下。
3. 突触稳态与休眠向量清理（Synaptic Homeostasis）：
   调用 `memory_store.prune_dormant_vectors()` 清理退役事实遗留的向量索引，防止 KNN 检索信噪比退化。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .config import get_config, logger_name
from .entity_store import EntityStore
from . import memory_store, synapse

logger = logging.getLogger(logger_name("consolidate"))

DEFAULT_PROMOTE_CONFIDENCE = 0.88
DEFAULT_MIN_REINFORCEMENTS = 1
FAST_PROMOTE_SOURCES = frozenset({"flash_write", "user_correction", "system_seed", "auto_learn"})


CONSOLIDATE_SYSTEM = """你是智能体新皮层规则提炼器（Cortical Rule Distiller）。
将一条经过多次验证的海马体经验提炼为新皮层确定性规则（EntityStore Rule）。
输出严格 JSON（不要 markdown 代码块）：
{
  "entity_name": "核心触发实体名（如文件名、模块名、命令名或概念名）",
  "aliases": ["同义词或触发词1", "触发词2"],
  "polarity": "prohibition|requirement",
  "condition_expr": "触发条件简述",
  "action_stmt": "精炼后的可执行准则"
}
"""


def _extract_entity_candidate(subject: str, statement: str, tags: Dict[str, str]) -> Dict[str, Any]:
    """优先调用宿主语义模型（`get_config().resolve_llm()`）从海马体事实中提炼新皮层实体规则；不可用时走结构化提取。"""
    subj = (subject or "").strip()
    stmt = (statement or "").strip()
    file_tag = (tags.get("file") or "").strip()
    error_tag = (tags.get("error") or "").strip()

    try:
        chat_fn = get_config().resolve_llm()
    except Exception:
        chat_fn = None

    if chat_fn is not None:
        try:
            import json as _json
            res = chat_fn(
                [
                    {"role": "system", "content": CONSOLIDATE_SYSTEM},
                    {"role": "user", "content": f"主题: {subj}\n内容: {stmt}\n标签: {tags}"},
                ],
                timeout=12.0,
                max_tokens=240,
                temperature=0.0,
            )
            raw = ((res or {}).get("content") or "").strip().replace("```json", "").replace("```", "").strip()
            s_idx, e_idx = raw.find("{"), raw.rfind("}")
            if 0 <= s_idx < e_idx:
                parsed = _json.loads(raw[s_idx : e_idx + 1])
                if isinstance(parsed, dict) and parsed.get("entity_name") and parsed.get("action_stmt"):
                    pol = str(parsed.get("polarity") or "requirement")
                    if pol not in ("prohibition", "requirement"):
                        pol = "requirement"
                    return {
                        "entity_name": str(parsed["entity_name"]).strip()[:64],
                        "entity_type": "consolidated_memory",
                        "aliases": [str(a).strip() for a in (parsed.get("aliases") or []) if str(a).strip()][:6],
                        "polarity": pol,
                        "condition_expr": str(parsed.get("condition_expr") or subj or "always")[:120],
                        "action_stmt": str(parsed["action_stmt"]).strip()[:500],
                    }
        except Exception as exc:
            logger.debug("睡眠巩固 LLM 实体提炼降级: %s", type(exc).__name__)

    # 离线兜底：利用标签与分词提取实体与别名
    entity_name = ""
    if file_tag:
        entity_name = file_tag.replace("\\", "/").rsplit("/", 1)[-1]
    elif error_tag:
        entity_name = error_tag
    elif subj and len(subj) <= 48:
        entity_name = subj
    else:
        tokens = re.findall(r"[A-Za-z0-9_.-]{3,}|[\u4e00-\u9fff]{2,8}", stmt)
        entity_name = tokens[0] if tokens else "core_rule"

    aliases: List[str] = []
    for candidate in (subj, file_tag, error_tag):
        if candidate and candidate != entity_name and candidate not in aliases:
            aliases.append(candidate)
    for token in re.findall(r"[A-Za-z0-9_.-]{3,}|[\u4e00-\u9fff]{2,6}", subj + " " + stmt):
        if token != entity_name and token not in aliases and len(aliases) < 6:
            aliases.append(token)

    polarity = tags.get("polarity") or (
        "prohibition"
        if any(w in stmt for w in ("不要", "别再", "禁止", "严禁", "不得", "不能", "不准"))
        else "requirement"
    )
    condition_expr = tags.get("file") or tags.get("error") or subj or "always"

    return {
        "entity_name": entity_name[:64],
        "entity_type": "consolidated_memory",
        "aliases": aliases[:6],
        "polarity": polarity,
        "condition_expr": condition_expr[:120],
        "action_stmt": stmt[:500],
    }


def consolidate_memories(
    *,
    entity_db: Optional[Union[str, Path]] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    promote_confidence: float = DEFAULT_PROMOTE_CONFIDENCE,
    min_reinforcements: int = DEFAULT_MIN_REINFORCEMENTS,
    compact_fragments: bool = True,
    prune_vectors: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """执行一次完整的慢波睡眠巩固周期（皮层升维 + 同主题碎片合并 + 休眠向量清理）。"""
    cfg = get_config()
    uid = user_id or cfg.default_user
    estore = EntityStore(db_path=entity_db, auto_seed=True)
    syn_map = synapse.load_synapses()

    conn = memory_store._connect()
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    promoted_ids: List[str] = []
    compacted_ids: List[str] = []

    try:
        where = ["f.user_id = ?", "f.status = 'active'"]
        params: List[Any] = [uid]
        if agent_id:
            where.append("f.agent_id = ?")
            params.append(agent_id)
        if profile:
            where.append(
                "EXISTS (SELECT 1 FROM fact_tags pr WHERE pr.fact_id = f.fact_id"
                " AND pr.key = 'profile' AND pr.value = ?)"
            )
            params.append(profile)
        where.append(
            "NOT EXISTS (SELECT 1 FROM fact_tags sc WHERE sc.fact_id = f.fact_id"
            " AND sc.key = 'scope' AND sc.value = 'reference')"
        )

        rows = conn.execute(
            "SELECT f.fact_id, f.user_id, f.agent_id, f.workspace_id, f.subject,"
            " f.statement, f.source, f.created_at"
            " FROM working_fact f WHERE " + " AND ".join(where) +
            " ORDER BY f.created_at DESC, f.rowid DESC LIMIT 2000",
            params,
        ).fetchall()

        fact_ids = [str(r["fact_id"]) for r in rows]
        tags_by_id = memory_store._tags_of_many(conn, fact_ids) if fact_ids else {}

        # ------------------------------------------------------------------
        # 阶段 1：同主题碎片整理（Fragment Compaction）
        # ------------------------------------------------------------------
        if compact_fragments and rows:
            by_topic: Dict[tuple[str, str, str], List[Dict[str, Any]]] = {}
            for r in rows:
                fid = str(r["fact_id"])
                tmap = tags_by_id.get(fid, {})
                if tmap.get("scope") == "reference":
                    continue
                subj_norm = re.sub(r"\s+", " ", str(r["subject"] or "").strip().lower())
                if not subj_norm:
                    continue
                key = (str(r["workspace_id"] or ""), subj_norm, str(tmap.get("polarity") or ""))
                by_topic.setdefault(key, []).append(dict(r))

            for _topic_key, group in by_topic.items():
                if len(group) < 2:
                    continue
                # group 已按 created_at DESC 排序，第一条是最新幸存者
                survivor = group[0]
                survivor_id = str(survivor["fact_id"])
                survivor_terms = memory_store._query_terms(survivor["statement"])
                for older in group[1:]:
                    older_id = str(older["fact_id"])
                    older_terms = memory_store._query_terms(older["statement"])
                    if not survivor_terms or not older_terms:
                        continue
                    union_cnt = len(survivor_terms | older_terms)
                    overlap = len(survivor_terms & older_terms) / float(union_cnt) if union_cnt else 0.0
                    # 完全相同或词面高度重叠（>= 0.75）的同主题旧碎片归并给最新记录
                    if older["statement"].strip() == survivor["statement"].strip() or overlap >= 0.75:
                        compacted_ids.append(older_id)
                        if not dry_run:
                            memory_store.supersede(
                                older_id,
                                survivor_id,
                                reason="sleep_consolidation_compacted",
                            )
                            synapse.merge_synapse(older_id, survivor_id)

        # ------------------------------------------------------------------
        # 阶段 2：海马体 -> 新皮层 EntityStore 确定性规则升维（Cortical Promotion）
        # ------------------------------------------------------------------
        compacted_set = set(compacted_ids)
        seed_entities: List[Dict[str, Any]] = []
        seed_rules: List[Dict[str, Any]] = []

        for r in rows:
            fid = str(r["fact_id"])
            if fid in compacted_set:
                continue
            tmap = tags_by_id.get(fid, {})
            scope = tmap.get("scope") or "platform"
            if scope == "reference":
                continue
            if tmap.get("consolidated_to_entity") == "1":
                continue

            syn_entry = syn_map.get(fid) or {}
            conf = float(syn_entry.get("confidence", synapse.DEFAULT_CONFIDENCE))
            rcount = int(syn_entry.get("reinforce_count", 0))
            source = str(r["source"] or "")

            eligible = (
                (conf >= promote_confidence and rcount >= min_reinforcements)
                or (source in FAST_PROMOTE_SOURCES and conf >= 0.80)
                or tmap.get("flash_write") == "1"
            )
            if not eligible:
                continue

            cand = _extract_entity_candidate(str(r["subject"] or ""), str(r["statement"] or ""), tmap)
            seed_entities.append({
                "name": cand["entity_name"],
                "entity_type": cand["entity_type"],
                "description": f"Consolidated from fact {fid}",
                "aliases": cand["aliases"],
            })
            seed_rules.append({
                "entity": cand["entity_name"],
                "polarity": cand["polarity"],
                "condition_expr": cand["condition_expr"],
                "action_stmt": cand["action_stmt"],
                "confidence": max(conf, 0.92),
                "source": f"sleep_consolidated:{fid}",
            })
            promoted_ids.append(fid)
            if not dry_run:
                conn.execute(
                    "INSERT OR REPLACE INTO fact_tags(fact_id, key, value) VALUES (?, 'consolidated_to_entity', '1')",
                    (fid,),
                )

        if not dry_run and promoted_ids:
            conn.commit()
            estore.import_seed_pack({"entities": seed_entities, "rules": seed_rules})
    finally:
        conn.close()

    # ------------------------------------------------------------------
    # 阶段 3：休眠向量回收（Synaptic Homeostasis）
    # ------------------------------------------------------------------
    prune_stats: Dict[str, Any] = {"embeddings_deleted": 0, "vec_deleted": 0}
    if prune_vectors:
        prune_stats = memory_store.prune_dormant_vectors(dry_run=dry_run, vacuum=False)

    # ------------------------------------------------------------------
    # 阶段 4：SkillForge 案例自动蒸馏（Case -> ProceduralSkill）
    # ------------------------------------------------------------------
    distilled_skill_ids: List[str] = []
    if not dry_run:
        try:
            from agentcore.skill_forge import SkillForge
            forge = SkillForge()
            distilled = forge.auto_distill_from_cases()
            distilled_skill_ids = [s.skill_id for s in distilled]
        except Exception as exc:
            logger.debug("SkillForge 自动蒸馏跳过: %s", type(exc).__name__)

    return {
        "ok": True,
        "dry_run": dry_run,
        "promoted_count": len(promoted_ids),
        "promoted_fact_ids": promoted_ids,
        "compacted_count": len(compacted_ids),
        "compacted_fact_ids": compacted_ids,
        "dormant_vectors_pruned": prune_stats.get("embeddings_deleted", 0),
        "skills_distilled_count": len(distilled_skill_ids),
        "distilled_skill_ids": distilled_skill_ids,
    }

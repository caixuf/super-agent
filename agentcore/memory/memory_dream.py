#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""梦境反思与离线记忆蒸馏引擎（Dream-Phase Offline Consolidation Engine）。

模拟生物脑慢波睡眠（SWS）与快速眼动（REM）期的认知提炼：
1. 自适应语义聚类（Adaptive Semantic Clustering）：
   扫描近期积累的高惊奇度或未巩固碎片，基于 Jaccard/编辑距离与领域标签，
   聚聚类成结构化「经验簇（Experience Clusters）」。
2. 反思与公理化蒸馏（Reflection & Axiom Synthesis）：
   调用大模型或启发式规则，将一个经验簇内的多条琐碎调试记录（如“WSL2 权限报错三次”、“挂载路径 UID 不一致”）
   升华提炼为一条普适的高阶架构守卫原则（General Axiom）。
3. 记忆结晶与垃圾回收（Crystallization & Synaptic Pruning）：
   - 将高阶公理持久化写入核心记忆与 EntityStore 新皮层；
   - 将簇内冗余的原始微观碎片优雅归并或标记为 superseded；
   - 调用 prune_dormant_vectors 清理失效的废弃向量，保持检索信噪比。
4. 提供开箱即用的命令行工具与守护进程接口。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .config import get_config, logger_name
from .entity_store import EntityStore
from . import memory_conflict, memory_graph, memory_store, synapse

logger = logging.getLogger(logger_name("dream"))

DREAM_SYNTHESIS_SYSTEM = """你负责在智能体离线「梦境反思期」提炼高阶架构原则。
输入是一组同一主题下的多次微观调试经验或踩坑记录。
请深入反思并提炼出一条高度概括、指导未来工程行为的「高阶守卫公理（General Axiom）」。

输出严格 JSON（不要任何 markdown 代码块）：
{
  "subject": "精炼的主题名",
  "statement": "高阶守卫公理（一两句话，直击本质，给出可执行的行为指引）",
  "polarity": "requirement|prohibition",
  "trigger_entity": "核心触发实体名",
  "rule_type": "architecture_guard|troubleshooting_rule"
}
"""


class MemoryDreamEngine:
    """梦境反思与离线记忆蒸馏引擎。"""

    def __init__(
        self,
        *,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        similarity_threshold: float = 0.45,
        min_cluster_size: int = 2,
        dry_run: bool = False,
    ) -> None:
        self.cfg = get_config()
        self.user_id = user_id or self.cfg.default_user
        self.workspace_id = workspace_id
        self.similarity_threshold = similarity_threshold
        self.min_cluster_size = min_cluster_size
        self.dry_run = dry_run

    def fetch_unconsolidated_facts(self, limit: int = 500) -> List[Dict[str, Any]]:
        """提取候选待巩固事实：状态活跃、非 reference 且尚未被升维为公理的事实。"""
        conn = memory_store._connect()
        try:
            where = ["user_id = ?", "status = 'active'", "validity_state = 'ACTIVE'"]
            params: List[Any] = [self.user_id]
            if self.workspace_id:
                eff_bucket = memory_store.normalize_ebm(self.workspace_id)
                where.append("workspace_id = ?")
                params.append(eff_bucket)
            where.append("source != 'dream_consolidation'")

            sql = (
                "SELECT fact_id, user_id, agent_id, workspace_id, subject, statement, source, created_at "
                f"FROM working_fact WHERE {' AND '.join(where)} "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params.append(limit)
            cursor = conn.execute(sql, params)
            facts = []
            for r in cursor.fetchall():
                facts.append({
                    "fact_id": r[0],
                    "user_id": r[1],
                    "agent_id": r[2],
                    "workspace_id": r[3],
                    "subject": r[4],
                    "statement": r[5],
                    "source": r[6],
                    "created_at": r[7],
                })
            return facts
        finally:
            conn.close()

    def cluster_facts(self, facts: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """按 subject 精确匹配或 statement 语义交叠自适应聚类。"""
        if not facts:
            return []

        clusters: List[List[Dict[str, Any]]] = []
        assigned = set()

        # 第一遍：按规范化 subject 聚类
        by_subject: Dict[str, List[Dict[str, Any]]] = {}
        for f in facts:
            s_norm = re.sub(r"\s+", "", str(f.get("subject") or "").lower())
            if s_norm:
                by_subject.setdefault(s_norm, []).append(f)

        for s_norm, group in by_subject.items():
            if len(group) >= self.min_cluster_size:
                clusters.append(group)
                for item in group:
                    assigned.add(item["fact_id"])

        # 第二遍：未分配事实通过文本 Jaccard 相似度聚类
        unassigned = [f for f in facts if f["fact_id"] not in assigned]
        for i, f1 in enumerate(unassigned):
            if f1["fact_id"] in assigned:
                continue
            cur_cluster = [f1]
            assigned.add(f1["fact_id"])
            for j in range(i + 1, len(unassigned)):
                f2 = unassigned[j]
                if f2["fact_id"] in assigned:
                    continue
                sim = memory_conflict._similarity(f1.get("statement", ""), f2.get("statement", ""))
                if sim >= self.similarity_threshold:
                    cur_cluster.append(f2)
                    assigned.add(f2["fact_id"])
            if len(cur_cluster) >= self.min_cluster_size:
                clusters.append(cur_cluster)

        return clusters

    def reflect_and_synthesize(self, cluster: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """对同一个簇进行反思，调用 LLM 或确定性规则生成高阶公理。"""
        sample_stmts = [f.get("statement", "") for f in cluster if f.get("statement")]
        subj = cluster[0].get("subject") or "通用经验"

        chat_fn = self.cfg.resolve_llm()
        if chat_fn is not None:
            try:
                prompt_content = f"主题: {subj}\n微观碎片经验列表:\n" + "\n".join(f"- {s}" for s in sample_stmts[:8])
                res = chat_fn(
                    [
                        {"role": "system", "content": DREAM_SYNTHESIS_SYSTEM},
                        {"role": "user", "content": prompt_content},
                    ],
                    timeout=20.0,
                    max_tokens=300,
                    temperature=0.0,
                )
                raw = ((res or {}).get("content") or "").strip().replace("```json", "").replace("```", "").strip()
                s_idx, e_idx = raw.find("{"), raw.rfind("}")
                if 0 <= s_idx < e_idx:
                    parsed = json.loads(raw[s_idx : e_idx + 1])
                    if isinstance(parsed, dict) and parsed.get("statement"):
                        return {
                            "subject": str(parsed.get("subject") or subj)[:64],
                            "statement": str(parsed["statement"]).strip()[:500],
                            "polarity": str(parsed.get("polarity") or "requirement"),
                            "trigger_entity": str(parsed.get("trigger_entity") or subj)[:64],
                            "by": "llm_reflection",
                        }
            except Exception as exc:
                logger.debug("梦境反思 LLM 提炼降级: %s", exc)

        # 离线启发式规则提炼：选取出现频率最高的动词短语或最新一条精炼结论
        longest_stmt = max(sample_stmts, key=len) if sample_stmts else subj
        return {
            "subject": subj[:64],
            "statement": f"【梦境提炼公理】围绕 {subj}：{longest_stmt}"[:500],
            "polarity": "requirement",
            "trigger_entity": subj[:64],
            "by": "heuristic_reflection",
        }

    def run_cycle(self) -> Dict[str, Any]:
        """运行一次完整的梦境反思与离线记忆蒸馏周期。"""
        started = time.monotonic()
        facts = self.fetch_unconsolidated_facts()
        if not facts:
            return {
                "ok": True,
                "facts_scanned": 0,
                "clusters_found": 0,
                "axioms_synthesized": 0,
                "compacted_fragments": 0,
                "pruned_dormant_vectors": 0,
                "elapsed_seconds": 0.0,
                "dry_run": self.dry_run,
            }

        clusters = self.cluster_facts(facts)
        axioms_created = []
        compacted_count = 0

        for cluster in clusters:
            axiom = self.reflect_and_synthesize(cluster)
            cluster_fids = [f["fact_id"] for f in cluster]

            if not self.dry_run:
                # 写入高阶公理到 working_fact
                write_res = memory_store.write_fact(
                    statement=axiom["statement"],
                    subject=axiom["subject"],
                    source="dream_consolidation",
                    user_id=self.user_id,
                    workspace_id=cluster[0].get("workspace_id") or "global",
                    validity_state="ACTIVE",
                    confidence_level=1.0,
                    derived_from_ids=cluster_fids,
                    tags={
                        "dream_axiom": "1",
                        "polarity": axiom.get("polarity", "requirement"),
                        "cluster_size": str(len(cluster)),
                    },
                )
                axiom_fid = write_res.get("fact_id")
                axioms_created.append(axiom_fid)

                # 将旧碎片软标记为 superseded 并与新公理建立拓扑图边
                for old_f in cluster:
                    old_id = old_f["fact_id"]
                    if old_id != axiom_fid:
                        memory_store.supersede(
                            old_id,
                            str(axiom_fid),
                            reason="consolidated_into_dream_axiom",
                        )
                        compacted_count += 1

                # 同步写入 EntityStore 新皮层
                try:
                    estore = EntityStore()
                    estore.upsert_entity(
                        name=axiom.get("trigger_entity") or axiom["subject"],
                        entity_type="dream_axiom",
                        aliases=[axiom["subject"]],
                    )
                except Exception as e_exc:
                    logger.debug("EntityStore 同步跳过: %s", e_exc)
            else:
                axioms_created.append("dry_run_axiom")
                compacted_count += len(cluster)

        # 突触稳态调整：清理废弃休眠向量
        pruned_info = {"dormant_vectors_removed": 0}
        if not self.dry_run:
            try:
                pruned_info = memory_store.prune_dormant_vectors()
            except Exception as p_exc:
                logger.debug("休眠向量清理跳过: %s", p_exc)

        elapsed = round(time.monotonic() - started, 3)
        return {
            "ok": True,
            "facts_scanned": len(facts),
            "clusters_found": len(clusters),
            "axioms_synthesized": len(axioms_created),
            "compacted_fragments": compacted_count,
            "pruned_dormant_vectors": pruned_info.get("dormant_vectors_removed", 0),
            "elapsed_seconds": elapsed,
            "dry_run": self.dry_run,
        }


def main():
    parser = argparse.ArgumentParser(description="记忆系统离线梦境反思引擎 (Memory Dream Engine)")
    parser.add_argument("--once", action="store_true", help="单次执行离线反思周期后退出")
    parser.add_argument("--dry-run", action="store_true", help="干运行（仅分析聚类，不写数据库）")
    parser.add_argument("--user-id", type=str, default=None, help="指定用户 ID")
    parser.add_argument("--workspace-id", type=str, default=None, help="指定工作区 ID")
    args = parser.parse_args()

    engine = MemoryDreamEngine(
        user_id=args.user_id,
        workspace_id=args.workspace_id,
        dry_run=args.dry_run,
    )
    result = engine.run_cycle()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时空拓扑轻量图网络（Lite GraphRAG & Causal Topology Engine）。

突破纯向量相似度检索的「语义相关却无因果拓扑」瓶颈：
1. 实体与事实关系边表（`memory_edges`）：
   原生在 SQLite 中维护强类型图边，毫秒级读写，零外部基础设施依赖。
2. 标准认知拓扑谓词（Standard Predicates）：
   - SUPERSEDES: 新事实取代旧事实
   - DERIVED_FROM: 结论衍生自前置事实（因果依赖）
   - DEPENDS_ON: 组件/用例/规则依赖前置实体
   - CONFLICTS_WITH: 命题对立或边界冲突
   - TESTS: 测试用例验证事实或状态机规则
   - CO_OCCURS_WITH: 赫布突触联合激活
3. 多跳递归推导（Multi-hop Recursive CTE）：
   基于 SQLite 原生递归公共表表达式（Recursive CTE），支持带有环路保护（Cycle Guard）
   的 1~3 跳因果链追踪、依赖树展开与波及面分析（Impact Blast Radius）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("graph"))

# 标准关系类型定义
REL_SUPERSEDES = "SUPERSEDES"
REL_DERIVED_FROM = "DERIVED_FROM"
REL_DEPENDS_ON = "DEPENDS_ON"
REL_CONFLICTS_WITH = "CONFLICTS_WITH"
REL_TESTS = "TESTS"
REL_CO_OCCURS = "CO_OCCURS_WITH"

ALL_RELATIONS = frozenset({
    REL_SUPERSEDES,
    REL_DERIVED_FROM,
    REL_DEPENDS_ON,
    REL_CONFLICTS_WITH,
    REL_TESTS,
    REL_CO_OCCURS,
})


def _connect() -> sqlite3.Connection:
    cfg = get_config()
    db_path = cfg.db_path() if callable(cfg.db_path) else cfg.db_path
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    ensure_graph_tables(conn)
    return conn


def ensure_graph_tables(conn: sqlite3.Connection) -> None:
    """初始化图边表与多维索引。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_edges (
            source_id   TEXT NOT NULL,
            relation    TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            weight      REAL NOT NULL DEFAULT 1.0,
            properties  TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL,
            PRIMARY KEY (source_id, relation, target_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id, relation)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id, relation)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_rel ON memory_edges(relation)")


def add_edge(
    source_id: str,
    relation: str,
    target_id: str,
    *,
    weight: float = 1.0,
    properties: Optional[Dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """向拓扑图中插入或更新一条强类型边。"""
    src = str(source_id or "").strip()
    tgt = str(target_id or "").strip()
    rel = str(relation or "").strip().upper()
    if not src or not tgt or not rel:
        return False

    props_json = json.dumps(properties or {}, ensure_ascii=False)
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    own_conn = conn is None
    c = _connect() if own_conn else conn
    try:
        c.execute(
            """
            INSERT INTO memory_edges (source_id, relation, target_id, weight, properties, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, relation, target_id) DO UPDATE SET
                weight = excluded.weight,
                properties = excluded.properties,
                created_at = excluded.created_at
            """,
            (src, rel, tgt, float(weight), props_json, now_str),
        )
        if own_conn:
            c.commit()
        return True
    except Exception as exc:
        logger.warning("添加拓扑边失败 (%s -%s-> %s): %s", src, rel, tgt, exc)
        return False
    finally:
        if own_conn:
            c.close()


def add_edges_batch(
    edges: Sequence[Tuple[str, str, str, float, Optional[Dict[str, Any]]]],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """批量添加拓扑边。"""
    if not edges:
        return 0
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    params = []
    for item in edges:
        src = str(item[0] or "").strip()
        rel = str(item[1] or "").strip().upper()
        tgt = str(item[2] or "").strip()
        w = float(item[3]) if len(item) > 3 else 1.0
        props = item[4] if len(item) > 4 and isinstance(item[4], dict) else {}
        if src and rel and tgt:
            params.append((src, rel, tgt, w, json.dumps(props, ensure_ascii=False), now_str))

    if not params:
        return 0

    own_conn = conn is None
    c = _connect() if own_conn else conn
    try:
        c.executemany(
            """
            INSERT INTO memory_edges (source_id, relation, target_id, weight, properties, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, relation, target_id) DO UPDATE SET
                weight = excluded.weight,
                properties = excluded.properties,
                created_at = excluded.created_at
            """,
            params,
        )
        if own_conn:
            c.commit()
        return len(params)
    except Exception as exc:
        logger.warning("批量添加拓扑边失败: %s", exc)
        return 0
    finally:
        if own_conn:
            c.close()


def get_neighbors(
    node_id: str,
    *,
    direction: str = "out",  # 'out' | 'in' | 'both'
    relation_types: Optional[Sequence[str]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """获取指定节点的 1-Hop 直接邻接节点。"""
    nid = str(node_id or "").strip()
    if not nid:
        return []

    own_conn = conn is None
    c = _connect() if own_conn else conn
    try:
        clauses = []
        params: List[Any] = []

        if direction in ("out", "both"):
            p_out: List[Any] = [nid]
            rel_filter = ""
            if relation_types:
                placeholders = ",".join("?" for _ in relation_types)
                rel_filter = f" AND relation IN ({placeholders})"
                p_out.extend([r.upper() for r in relation_types])
            clauses.append(
                f"SELECT source_id, relation, target_id, weight, properties, created_at, 'out' as direction FROM memory_edges WHERE source_id = ? {rel_filter}"
            )
            params.extend(p_out)

        if direction in ("in", "both"):
            p_in: List[Any] = [nid]
            rel_filter = ""
            if relation_types:
                placeholders = ",".join("?" for _ in relation_types)
                rel_filter = f" AND relation IN ({placeholders})"
                p_in.extend([r.upper() for r in relation_types])
            clauses.append(
                f"SELECT source_id, relation, target_id, weight, properties, created_at, 'in' as direction FROM memory_edges WHERE target_id = ? {rel_filter}"
            )
            params.extend(p_in)

        query = " UNION ALL ".join(clauses)
        cursor = c.execute(query, params)
        results = []
        for r in cursor.fetchall():
            try:
                props = json.loads(r[4]) if r[4] else {}
            except Exception:
                props = {}
            results.append({
                "source_id": r[0],
                "relation": r[1],
                "target_id": r[2],
                "weight": float(r[3]),
                "properties": props,
                "created_at": r[5],
                "direction": r[6],
                "neighbor_id": r[2] if r[6] == "out" else r[0],
            })
        return results
    finally:
        if own_conn:
            c.close()


def traverse_paths(
    start_id: str,
    *,
    relation_types: Optional[Sequence[str]] = None,
    max_depth: int = 3,
    direction: str = "forward",  # 'forward' (source->target) or 'reverse' (target->source)
    min_weight: float = 0.0,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """利用 SQLite 原生递归 CTE 实现多跳图路径遍历，内置环路阻断（Cycle Guard）。"""
    sid = str(start_id or "").strip()
    if not sid or max_depth < 1:
        return []

    own_conn = conn is None
    c = _connect() if own_conn else conn
    try:
        rel_condition = ""
        params_base: List[Any] = []
        if relation_types:
            placeholders = ",".join("?" for _ in relation_types)
            rel_condition = f"AND relation IN ({placeholders})"
            params_base = [r.upper() for r in relation_types]

        if direction == "forward":
            # 正向遍历: start -> ... -> end
            query = f"""
            WITH RECURSIVE traversal(depth, curr_node, relation, next_node, path, total_weight) AS (
                SELECT 1, source_id, relation, target_id, source_id || '->' || target_id, weight
                FROM memory_edges
                WHERE source_id = ? {rel_condition} AND weight >= ?
                UNION ALL
                SELECT t.depth + 1, e.source_id, e.relation, e.target_id,
                       t.path || '->' || e.target_id, t.total_weight * e.weight
                FROM traversal t
                JOIN memory_edges e ON t.next_node = e.source_id
                WHERE t.depth < ?
                  AND instr(t.path, e.target_id) = 0
                  AND (t.total_weight * e.weight) >= ?
                  {rel_condition.replace('relation', 'e.relation')}
            )
            SELECT depth, curr_node, relation, next_node, path, total_weight
            FROM traversal
            WHERE total_weight >= ?
            ORDER BY depth ASC, total_weight DESC
            """
            exec_params = [sid] + params_base + [float(min_weight), int(max_depth), float(min_weight)] + params_base + [float(min_weight)]
        else:
            # 反向遍历: ... -> start (常用于追溯谁依赖我，即受影响面分析)
            query = f"""
            WITH RECURSIVE traversal(depth, curr_node, relation, prev_node, path, total_weight) AS (
                SELECT 1, target_id, relation, source_id, target_id || '<-' || source_id, weight
                FROM memory_edges
                WHERE target_id = ? {rel_condition} AND weight >= ?
                UNION ALL
                SELECT t.depth + 1, e.target_id, e.relation, e.source_id,
                       t.path || '<-' || e.source_id, t.total_weight * e.weight
                FROM traversal t
                JOIN memory_edges e ON t.prev_node = e.target_id
                WHERE t.depth < ?
                  AND instr(t.path, e.source_id) = 0
                  AND (t.total_weight * e.weight) >= ?
                  {rel_condition.replace('relation', 'e.relation')}
            )
            SELECT depth, curr_node, relation, prev_node, path, total_weight
            FROM traversal
            WHERE total_weight >= ?
            ORDER BY depth ASC, total_weight DESC
            """
            exec_params = [sid] + params_base + [float(min_weight), int(max_depth), float(min_weight)] + params_base + [float(min_weight)]

        cursor = c.execute(query, exec_params)
        paths = []
        for r in cursor.fetchall():
            paths.append({
                "depth": int(r[0]),
                "from_node": r[1],
                "relation": r[2],
                "to_node": r[3],
                "path": r[4],
                "weight": round(float(r[5]), 4),
            })
        return paths
    finally:
        if own_conn:
            c.close()


def find_invalidation_blast_radius(
    invalidated_fact_id: str,
    *,
    max_depth: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """计算某个事实被推翻/取代后的波及影响面（Blast Radius）。
    
    顺着 DERIVED_FROM 和 DEPENDS_ON 的反向链（谁衍生自我、谁依赖我）向下游递归寻找
    所有需要被联立重核或置疑的衍生事实/用例。
    """
    fid = str(invalidated_fact_id or "").strip()
    if not fid:
        return []
    # 追溯依赖我 (DERIVED_FROM / DEPENDS_ON) 的下游节点
    paths = traverse_paths(
        fid,
        relation_types=[REL_DERIVED_FROM, REL_DEPENDS_ON],
        max_depth=max_depth,
        direction="reverse",
        conn=conn,
    )
    return paths


def remove_edge(source_id: str, relation: str, target_id: str, *, conn: Optional[sqlite3.Connection] = None) -> bool:
    """删除指定的图边。"""
    own_conn = conn is None
    c = _connect() if own_conn else conn
    try:
        c.execute(
            "DELETE FROM memory_edges WHERE source_id = ? AND relation = ? AND target_id = ?",
            (source_id, relation.upper(), target_id),
        )
        if own_conn:
            c.commit()
        return True
    finally:
        if own_conn:
            c.close()

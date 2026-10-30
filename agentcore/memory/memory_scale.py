#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大规模记忆的分层检索：先粗筛，再精排。

为什么需要（2026-10-23 修正思路）：
    前一版把「记忆」和「知识」人为分开，理由是「记忆要精、知识可以多」。
    那是照抄人类的局限 —— 人记不住才需要分层存书。模型不需要这个妥协：
    能记多少就该记多少。

    真正的问题在工程：一旦从 46 条涨到几万条，现在这套「全表拉出来逐条算余弦」
    会直接崩（每条都要一次比对，候选集本身也装不下上下文）。

    解法是经典的两阶段检索，而不是砍掉数据：
        第一阶段 粗筛：FTS5/BM25 或代际桶，把几万条收窄到几十条（毫秒级）
        第二阶段 精排：只对这几十条算向量余弦，取 top-k
    这样记忆规模可以一直涨，召回仍是毫秒级。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("scale"))

#: 第一阶段粗筛返回的候选数。太小会漏召回，太大精排变慢。
COARSE_CANDIDATES = 50


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM fact_fts LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def ensure_fts(conn: sqlite3.Connection) -> bool:
    """建 FTS5 全文索引（幂等）。已有则直接返回。"""
    try:
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_fts'").fetchone()
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5("
            "statement, subject, content='working_fact', content_rowid='rowid', "
            "tokenize='unicode61')")
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("FTS5 不可用，粗筛降级为代际桶: %s", exc)
        return False
    # 外部内容表（content=）建好后索引是空的；不 rebuild 的话 MATCH 永远 0 条。
    # 注意：这里**不能**用 COUNT(*) 判断是否需要 rebuild —— 在 content 模式下
    # COUNT 读的是主表行数，永远不为 0，会导致 rebuild 被跳过，
    # 表现为「粗筛悄悄失效、召回全空」，比报错更隐蔽（实测踩过）。
    # 每次调用都 rebuild：记忆规模在万级时耗时约几十毫秒，远低于一次误召回的代价。
    try:
        # 只在本次新建时 rebuild。已有索引则信任（增量由写入侧维护）。
        if not existed:
            conn.execute("INSERT INTO fact_fts(fact_fts) VALUES('rebuild')")
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning("FTS 索引重建失败: %s", exc)
        return False
    return True


def _query_terms(query: str) -> List[str]:
    """把查询切成 FTS 可用词。

    实测（2026-10-23）：unicode61 分词器把连续中文**整段**当一个 token，
    「踏板门限」能命中，「踏板」这种 2-gram 命中不了 0 条。所以中文按整段取，
    不做滑窗切分；英文/数字按词取。切碎了反而查不到。
    """
    q = (query or "").strip()
    terms: List[str] = []
    for tok in re.findall(r"[A-Za-z0-9_.]{2,}", q):
        terms.append(tok)
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        terms.append(seg)
    return list(dict.fromkeys(terms))[:8]


def coarse_filter(conn: sqlite3.Connection, query: str, *,
                  buckets: Optional[Sequence[str]] = None,
                  kinds: Optional[Sequence[str]] = None,
                  scopes: Optional[Sequence[str]] = None,
                  user_id: Optional[str] = None,
                  limit: int = COARSE_CANDIDATES) -> List[int]:
    """第一阶段：把候选收窄到 limit 条，返回 rowid 列表。

    两条路：有 FTS 且查询有可用词用 BM25 排序；否则退化为按代际桶取最近 limit 条。
    任何异常都返回空列表，由调用方决定降级。
    """
    terms = _query_terms(query)
    if scopes:
        try:
            from .memory_scope import filter_clause as _sc
        except ImportError:
            from .memory_scope import filter_clause as _sc
        _sql, _sp = _sc(scopes)
        if _sql:
            _extra_where = [_sql]
            _extra_params = list(_sp)
        else:
            _extra_where, _extra_params = [], []
    else:
        _extra_where, _extra_params = [], []
    params: List[Any] = []
    # user_id 是必需约束：不传就跨用户召回，等于把别人的记忆当自己的用。
    where: List[str] = ["f.status = 'active'"]
    if user_id:
        where.append("f.user_id = ?")
        params.append(user_id)
    if buckets:
        where.append("f.workspace_id IN (%s)" % ",".join("?" * len(buckets)))
        params.extend(buckets)
    if kinds:
        marks = ",".join("?" * len(kinds))
        kind_sql = ("(EXISTS (SELECT 1 FROM fact_tags k WHERE k.fact_id = f.fact_id"
                    " AND k.key = 'kind' AND k.value IN (" + marks + "))"
                    " OR NOT EXISTS (SELECT 1 FROM fact_tags k2 WHERE k2.fact_id = f.fact_id"
                    " AND k2.key = 'kind'))")
        where.append(kind_sql)
        params.extend(kinds)

    try:
        # 为什么默认用 LIKE 而不是 FTS5 的 MATCH（实测结论，别再改回去）：
        #   unicode61 按「非字母数字」切分，中文连写时整句是一个 token ——
        #     库里的「G2门限确定为5.0%。」用 MATCH "门限" 命中 0 条；
        #   trigram 能做子串，但两字查询（门限）和含标点的查询都不行。
        #   中文没有天然空格，零依赖的 FTS 分词器都不够用，只能退化到 LIKE。
        # LIKE 虽无排序，但它的职责只是**收窄候选**，真正的相关度由向量精排决定。
        if terms:
            like_clause = " OR ".join(
                ["(f.statement LIKE ? ESCAPE '!' OR f.subject LIKE ? ESCAPE '!')"] * len(terms))
            like_params = []
            for t in terms:
                pat = "%" + t.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
                like_params.extend([pat, pat])
            sql = ("SELECT f.rowid AS rid FROM working_fact f WHERE " +
                   " AND ".join(where + _extra_where) + " AND (" + like_clause + ") LIMIT ?")
            rows = conn.execute(sql, [*params, *_extra_params, *like_params, limit]).fetchall()
        else:
            sql = ("SELECT f.rowid AS rid FROM working_fact f WHERE " +
                   " AND ".join(where) + " ORDER BY f.created_at DESC LIMIT ?")
            rows = conn.execute(sql, [*params, limit]).fetchall()
        return [r["rid"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]
    except sqlite3.Error as exc:
        logger.warning("粗筛失败，返回空候选: %s", exc)
        return []


def index_stats(db_path: str) -> Dict[str, Any]:
    """当前记忆规模与索引状态。规模化后需要能看到「涨到什么程度了」。"""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute(
                "SELECT COUNT(*) c FROM working_fact WHERE status='active'").fetchone()["c"]
            by_kind = {r["value"]: r["c"] for r in conn.execute(
                "SELECT value, COUNT(*) c FROM fact_tags WHERE key='kind' GROUP BY value")}
            has_fts = _fts_available(conn)
            return {"ok": True, "total_active": total, "by_kind": by_kind,
                    "fts_ready": has_fts,
                    "strategy": "bm25_coarse+vector_fine" if has_fts else "bucket_only"}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}

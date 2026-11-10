#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大规模记忆的分层检索：SQLite FTS5 BM25 全文检索与 vec0 向量 RRF 融合支撑。

核心设计：
1. 字词与中文二元组（CJK Bigram + Unigram + 标识符）预分词索引：
   解决 SQLite 默认 unicode61 将连续中文视作整句 token、以及 trigram 无法匹配
   双字技术词（如「门限」「超时」「预算」）的问题。
2. 真实 FTS5 虚表（fact_fts）与 bm25() 排序：
   在连接初始化与写入/淘汰/激活全链路同步维护 fact_fts，检索阶段通过
   `fact_fts MATCH ?` + `ORDER BY bm25(fact_fts, 1.0, 2.0)` 走虚拟表倒排索引，
   彻底消除全表 LIKE 扫描。
3. 诚实诊断（index_stats）：
   实时探查 fact_fts 与 vec_facts 状态，输出的 strategy 字段与实际检索路径严格一致。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from .config import logger_name

logger = logging.getLogger(logger_name("scale"))

#: 第一阶段 FTS5 BM25 检索返回的候选数上限。
COARSE_CANDIDATES = 50

#: 小规模表自动对账阈值（覆盖单测直接执行裸 SQL INSERT/UPDATE working_fact 的场景）。
_AUTO_SYNC_ROW_LIMIT = 256


def tokenize_for_fts(text: str) -> str:
    """将中英文混合文本切分为供 FTS5 unicode61 索引的空格分隔词元流。

    - 英文/数字/路径标识符：提取完整 token 及子词元（如 pool.py -> pool.py, pool, py）
    - 中文连续片段：提取单字、相邻二元组（2-gram）及短整词（<=6字），兼顾双字词与短语匹配。
    """
    s = (text or "").strip().lower()
    if not s:
        return ""
    tokens: List[str] = []
    for raw in re.findall(r"[a-z0-9_./:-]{2,}", s):
        clean = raw.strip("./:-")
        if len(clean) >= 2:
            tokens.append(clean)
        for sub in re.findall(r"[a-z0-9_]{2,}", clean):
            if sub != clean:
                tokens.append(sub)
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        if len(run) == 1:
            tokens.append(run)
        else:
            if len(run) <= 6:
                tokens.append(run)
            for i in range(len(run) - 1):
                tokens.append(run[i : i + 2])
            for ch in run:
                tokens.append(ch)
    return " ".join(dict.fromkeys(tokens))


def _query_terms(query: str) -> List[str]:
    """把查询切成 FTS5 MATCH 可用词元列表。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    terms: List[str] = []
    for tok in re.findall(r"[a-z0-9_]{2,}", q):
        terms.append(tok)
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) == 1:
            terms.append(seg)
        else:
            for i in range(len(seg) - 1):
                terms.append(seg[i : i + 2])
    return list(dict.fromkeys(terms))[:16]


def build_fts_match_query(query: str) -> str:
    """构造安全的 FTS5 MATCH 布尔查询表达式（词元加双引号并以 OR 连接）。"""
    terms = _query_terms(query)
    if not terms:
        return ""
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms if t]
    return " OR ".join(quoted)


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='fact_fts'"
        ).fetchone()
        if not row:
            return False
        sql = str(row[0] or "")
        # 旧版 external-content 表（content='working_fact'）未做中文预分词，视为需迁移
        if "content='working_fact'" in sql or 'content="working_fact"' in sql:
            return False
        conn.execute("SELECT 1 FROM fact_fts LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def ensure_fts(conn: sqlite3.Connection, *, sync_all: bool = False) -> bool:
    """建 FTS5 全文索引并同步活跃事实（幂等）。"""
    if conn.in_transaction:
        return _fts_available(conn)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='fact_fts'"
        ).fetchone()
        needs_create = False
        if row is None:
            needs_create = True
        else:
            sql = str(row[0] or "")
            if "content='working_fact'" in sql or 'content="working_fact"' in sql:
                conn.execute("DROP TABLE IF EXISTS fact_fts")
                needs_create = True
        if needs_create:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5("
                "statement, subject, tokenize='unicode61')"
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning("FTS5 不可用: %s", exc)
        return False

    try:
        has_working = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='working_fact'"
        ).fetchone()
        if not has_working:
            return True
        fts_count = int(conn.execute("SELECT COUNT(*) FROM fact_fts").fetchone()[0])
        active_count = int(
            conn.execute("SELECT COUNT(*) FROM working_fact WHERE status='active'").fetchone()[0]
        )
        if needs_create or sync_all or (fts_count == 0 and active_count > 0):
            _rebuild_fts_rows(conn, sync_all=sync_all or active_count <= 2000)
        elif active_count <= _AUTO_SYNC_ROW_LIMIT and fts_count != active_count:
            _rebuild_fts_rows(conn, sync_all=True)
    except sqlite3.Error as exc:
        logger.warning("FTS5 索引同步失败: %s", exc)
        return False
    return True


def _rebuild_fts_rows(conn: sqlite3.Connection, *, sync_all: bool = True) -> int:
    """将 working_fact 中的 active 记录分词写入 fact_fts。"""
    if conn.in_transaction:
        return 0
    has_tags = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_tags'"
        ).fetchone()
        is not None
    )
    if sync_all or not has_tags:
        rows = conn.execute(
            "SELECT rowid, statement, subject FROM working_fact WHERE status='active'"
        ).fetchall()
    else:
        # 超大库首次连接快速初始化：全量索引非 reference 核心事实 + 最近 1000 条记录
        rows = conn.execute(
            "SELECT f.rowid, f.statement, f.subject FROM working_fact f "
            "WHERE f.status='active' AND NOT EXISTS ("
            "  SELECT 1 FROM fact_tags t WHERE t.fact_id=f.fact_id "
            "  AND t.key='scope' AND t.value='reference'"
            ") "
            "UNION "
            "SELECT rowid, statement, subject FROM ("
            "  SELECT f.rowid, f.statement, f.subject FROM working_fact f "
            "  WHERE f.status='active' ORDER BY f.rowid DESC LIMIT 1000"
            ")"
        ).fetchall()

    payload = [
        (int(r[0]), tokenize_for_fts(str(r[1] or "")), tokenize_for_fts(str(r[2] or "")))
        for r in rows
    ]
    conn.execute("DELETE FROM fact_fts")
    if payload:
        conn.executemany(
            "INSERT INTO fact_fts(rowid, statement, subject) VALUES (?, ?, ?)",
            payload,
        )
    conn.commit()
    return len(payload)


def sync_fts_rows(
    conn: sqlite3.Connection,
    fact_ids: Sequence[str],
    *,
    commit: bool = False,
) -> int:
    """增量同步指定 fact_id 列表到 fact_fts（支持事务内调用）。"""
    ids = [str(fid).strip() for fid in (fact_ids or []) if str(fid or "").strip()]
    if not ids or not _fts_available(conn):
        return 0
    updated = 0
    try:
        for offset in range(0, len(ids), 400):
            chunk = ids[offset : offset + 400]
            marks = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT rowid, fact_id, statement, subject, status FROM working_fact WHERE fact_id IN ({marks})",
                chunk,
            ).fetchall()
            for r in rows:
                rid = int(r[0])
                status = str(r[4] or "")
                conn.execute("DELETE FROM fact_fts WHERE rowid = ?", (rid,))
                if status == "active":
                    conn.execute(
                        "INSERT INTO fact_fts(rowid, statement, subject) VALUES (?, ?, ?)",
                        (
                            rid,
                            tokenize_for_fts(str(r[2] or "")),
                            tokenize_for_fts(str(r[3] or "")),
                        ),
                    )
                    updated += 1
        # 清理已在 working_fact 中物理删除的孤儿行（仅当小表或显式删除时）
        conn.execute(
            "DELETE FROM fact_fts WHERE rowid NOT IN (SELECT rowid FROM working_fact WHERE status='active')"
        )
        if commit and not conn.in_transaction:
            conn.commit()
    except sqlite3.Error as exc:
        logger.debug("增量同步 fact_fts 跳过: %s", exc)
    return updated


def _sync_small_table_if_needed(conn: sqlite3.Connection) -> None:
    """若 working_fact 为小规模表且存在裸 SQL 修改，自动对齐 fact_fts（无差异时零写入）。"""
    if conn.in_transaction or not _fts_available(conn):
        return
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM working_fact").fetchone()[0])
        if total > _AUTO_SYNC_ROW_LIMIT:
            return
        wf_rows = conn.execute(
            "SELECT rowid, statement, subject FROM working_fact WHERE status='active'"
        ).fetchall()
        expected = {
            int(r[0]): (tokenize_for_fts(str(r[1] or "")), tokenize_for_fts(str(r[2] or "")))
            for r in wf_rows
        }
        fts_rows = conn.execute("SELECT rowid, statement, subject FROM fact_fts").fetchall()
        actual = {int(r[0]): (str(r[1] or ""), str(r[2] or "")) for r in fts_rows}
        if expected == actual:
            return
        for rid in set(actual) - set(expected):
            conn.execute("DELETE FROM fact_fts WHERE rowid = ?", (rid,))
        for rid, (t_stmt, t_subj) in expected.items():
            if actual.get(rid) != (t_stmt, t_subj):
                conn.execute("DELETE FROM fact_fts WHERE rowid = ?", (rid,))
                conn.execute(
                    "INSERT INTO fact_fts(rowid, statement, subject) VALUES (?, ?, ?)",
                    (rid, t_stmt, t_subj),
                )
        conn.commit()
    except sqlite3.Error:
        pass


def fts_bm25_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    buckets: Optional[Sequence[str]] = None,
    kinds: Optional[Sequence[str]] = None,
    scopes: Optional[Sequence[str]] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    shared_legacy: bool = False,
    limit: int = COARSE_CANDIDATES,
) -> List[Dict[str, Any]]:
    """执行真实 SQLite FTS5 MATCH + bm25(fact_fts, 1.0, 2.0) 排序检索。"""
    match_expr = build_fts_match_query(query)
    if not match_expr:
        return []
    if not _fts_available(conn):
        if not ensure_fts(conn):
            return []
    _sync_small_table_if_needed(conn)

    where: List[str] = ["fact_fts MATCH ?", "f.status = 'active'"]
    params: List[Any] = [match_expr]

    if user_id:
        where.append("f.user_id = ?")
        params.append(user_id)
    if shared_legacy and agent_id is not None and profile is not None:
        where.append(
            "(NOT EXISTS (SELECT 1 FROM fact_tags trust"
            " WHERE trust.fact_id=f.fact_id AND trust.key='source_trust'"
            " AND trust.value='completed_user_turn')"
            " OR (f.agent_id=? AND EXISTS (SELECT 1 FROM fact_tags own"
            " WHERE own.fact_id=f.fact_id AND own.key='profile'"
            " AND own.value=?)))"
        )
        params.extend([str(agent_id), str(profile)])
    else:
        if agent_id is not None:
            where.append("f.agent_id = ?")
            params.append(str(agent_id))
        if profile is not None:
            where.append(
                "EXISTS (SELECT 1 FROM fact_tags pr WHERE pr.fact_id=f.fact_id"
                " AND pr.key='profile' AND pr.value=?)"
            )
            params.append(str(profile))
    if buckets:
        where.append("f.workspace_id IN (%s)" % ",".join("?" * len(buckets)))
        params.extend(buckets)
    if scopes:
        from .memory_scope import filter_clause as _sc

        _sql, _sp = _sc(scopes)
        if _sql:
            where.append(_sql)
            params.extend(_sp)
    if kinds:
        marks = ",".join("?" * len(kinds))
        kind_sql = (
            "(EXISTS (SELECT 1 FROM fact_tags k WHERE k.fact_id = f.fact_id"
            " AND k.key = 'kind' AND k.value IN (" + marks + "))"
            " OR NOT EXISTS (SELECT 1 FROM fact_tags k2 WHERE k2.fact_id = f.fact_id"
            " AND k2.key = 'kind'))"
        )
        where.append(kind_sql)
        params.extend(kinds)

    sql = (
        "SELECT f.rowid AS rid, f.fact_id AS fact_id, bm25(fact_fts, 1.0, 2.0) AS bm25_score "
        "FROM fact_fts "
        "JOIN working_fact f ON f.rowid = fact_fts.rowid "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY bm25(fact_fts, 1.0, 2.0) ASC, f.created_at DESC "
        "LIMIT ?"
    )
    try:
        rows = conn.execute(sql, [*params, max(1, int(limit))]).fetchall()
        out: List[Dict[str, Any]] = []
        for idx, r in enumerate(rows, start=1):
            rid = r["rid"] if isinstance(r, sqlite3.Row) else r[0]
            fid = r["fact_id"] if isinstance(r, sqlite3.Row) else r[1]
            score = r["bm25_score"] if isinstance(r, sqlite3.Row) else r[2]
            out.append(
                {
                    "rowid": int(rid),
                    "fact_id": str(fid),
                    "bm25_score": float(score or 0.0),
                    "bm25_rank": idx,
                }
            )
        return out
    except sqlite3.Error as exc:
        logger.warning("FTS5 BM25 检索失败: %s", exc)
        return []


def explain_fts_query_plan(
    conn: sqlite3.Connection,
    query: str,
    *,
    user_id: Optional[str] = None,
    limit: int = COARSE_CANDIDATES,
) -> List[str]:
    """返回 FTS5 BM25 查询的 SQLite EXPLAIN QUERY PLAN 物理执行计划明细。"""
    match_expr = build_fts_match_query(query) or '"placeholder"'
    where = ["fact_fts MATCH ?", "f.status = 'active'"]
    params: List[Any] = [match_expr]
    if user_id:
        where.append("f.user_id = ?")
        params.append(user_id)
    sql = (
        "EXPLAIN QUERY PLAN "
        "SELECT f.rowid AS rid, f.fact_id AS fact_id, bm25(fact_fts, 1.0, 2.0) AS bm25_score "
        "FROM fact_fts "
        "JOIN working_fact f ON f.rowid = fact_fts.rowid "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY bm25(fact_fts, 1.0, 2.0) ASC LIMIT ?"
    )
    rows = conn.execute(sql, [*params, max(1, int(limit))]).fetchall()
    return [str(r[3] if len(r) > 3 else r[-1]) for r in rows]


def coarse_filter(
    conn: sqlite3.Connection,
    query: str,
    *,
    buckets: Optional[Sequence[str]] = None,
    kinds: Optional[Sequence[str]] = None,
    scopes: Optional[Sequence[str]] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    shared_legacy: bool = False,
    limit: int = COARSE_CANDIDATES,
) -> List[int]:
    """第一阶段粗筛：通过 FTS5 MATCH + bm25() 排序返回候选 rowid 列表。

    当查询为空或无可用词元时，退化为按代际桶与时间倒序取最近 limit 条。
    """
    match_expr = build_fts_match_query(query)
    if match_expr:
        hits = fts_bm25_search(
            conn,
            query,
            buckets=buckets,
            kinds=kinds,
            scopes=scopes,
            user_id=user_id,
            agent_id=agent_id,
            profile=profile,
            shared_legacy=shared_legacy,
            limit=limit,
        )
        return [h["rowid"] for h in hits]

    where: List[str] = ["f.status = 'active'"]
    params: List[Any] = []
    if user_id:
        where.append("f.user_id = ?")
        params.append(user_id)
    if buckets:
        where.append("f.workspace_id IN (%s)" % ",".join("?" * len(buckets)))
        params.extend(buckets)
    if scopes:
        from .memory_scope import filter_clause as _sc

        _sql, _sp = _sc(scopes)
        if _sql:
            where.append(_sql)
            params.extend(_sp)
    if kinds:
        marks = ",".join("?" * len(kinds))
        kind_sql = (
            "(EXISTS (SELECT 1 FROM fact_tags k WHERE k.fact_id = f.fact_id"
            " AND k.key = 'kind' AND k.value IN (" + marks + "))"
            " OR NOT EXISTS (SELECT 1 FROM fact_tags k2 WHERE k2.fact_id = f.fact_id"
            " AND k2.key = 'kind'))"
        )
        where.append(kind_sql)
        params.extend(kinds)
    try:
        sql = (
            "SELECT f.rowid AS rid FROM working_fact f WHERE "
            + " AND ".join(where)
            + " ORDER BY f.created_at DESC LIMIT ?"
        )
        rows = conn.execute(sql, [*params, max(1, int(limit))]).fetchall()
        return [int(r["rid"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows]
    except sqlite3.Error as exc:
        logger.warning("粗筛失败，返回空候选: %s", exc)
        return []


def index_stats(db_path: str) -> Dict[str, Any]:
    """当前记忆规模与 FTS5 / vec0 索引状态，确保 strategy 与真实执行路径 100% 一致。"""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM working_fact WHERE status='active'"
                ).fetchone()["c"]
            )
            by_kind = {
                r["value"]: r["c"]
                for r in conn.execute(
                    "SELECT value, COUNT(*) c FROM fact_tags WHERE key='kind' GROUP BY value"
                )
            }
            has_fts = _fts_available(conn)
            fts_rows = (
                int(conn.execute("SELECT COUNT(*) FROM fact_fts").fetchone()[0])
                if has_fts
                else 0
            )
            try:
                from . import vec_index

                vstats = vec_index.stats(conn)
            except Exception:
                vstats = {"loaded": False, "rows": 0}
            vec_ready = bool(vstats.get("loaded")) and int(vstats.get("rows") or 0) > 0
            fts_effective = has_fts and (fts_rows > 0 or total == 0)
            if fts_effective and vec_ready:
                strategy = "fts5_bm25+vec0_rrf"
            elif fts_effective:
                strategy = "fts5_bm25_only"
            elif vec_ready:
                strategy = "vec0_only"
            else:
                strategy = "bucket_only"
            return {
                "ok": True,
                "total_active": total,
                "by_kind": by_kind,
                "fts_ready": fts_effective,
                "fts_rows": fts_rows,
                "vec_ready": vec_ready,
                "vec_rows": int(vstats.get("rows") or 0),
                "strategy": strategy,
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}

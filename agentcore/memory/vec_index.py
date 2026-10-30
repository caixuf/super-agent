#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sqlite-vec 向量索引层：把「逐条算余弦」下沉到 SQLite 扩展里的 C 实现。

为什么需要（为互联网数据扩充做准备）：
    现有方案是 fact_embeddings 存 BLOB + Python 里逐条点乘。几千条时是毫秒级，
    但要把互联网资料成批灌进来，规模会到几十万条 —— 那时 Python 循环算余弦
    会从毫秒级掉到秒级。

实测（本机 RTX5060，1024 维，sqlite-vec v0.1.9）：
    1 万条   KNN 11.7ms
    10 万条  KNN 103.6ms
    注意：vec0 是**暴力全扫**（纯 C + SIMD 加速），没有 ANN 索引，
    所以不是网传的 8ms —— 但相比 Python 逐条点乘仍是数量级提升。

设计原则：**可选加载，失败即回退**。
    .so 加载失败（架构不符/权限/未提供）时返回 None，调用方继续走原有路径。
    记忆是核心能力，不能因为一个可选优化加载不上就整体不可用。
"""

from __future__ import annotations

import logging
import sqlite3
import time
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("vec_index"))

#: 扩展文件名与搜索路径。部署时把 vec0.so 放到这些位置之一即可。
EXT_NAME = "vec0"
SEARCH_DIRS = (
    Path(__file__).resolve().parent / "vendor",
    Path(__file__).resolve().parent.parent / "vendor",
    Path("/usr/local/lib"),
)

_LOADED_CACHE: Dict[str, bool] = {}


def find_extension() -> Optional[Path]:
    """找 vec0 扩展。优先项目内 vendor/，其次系统路径。"""
    for d in SEARCH_DIRS:
        for name in (EXT_NAME + ".so", EXT_NAME):
            p = d / name
            if p.is_file():
                return p
    return None


def load_into(conn: sqlite3.Connection) -> bool:
    """把扩展加载进连接。失败返回 False，调用方回退。"""
    if _LOADED_CACHE.get("ok"):
        try:
            conn.load_extension(str(_LOADED_CACHE.get("path")))
            return True
        except sqlite3.Error as exc:
            logger.debug("复用扩展句柄失败: %s", exc)
            _LOADED_CACHE.clear()

    ext = find_extension()
    if ext is None:
        logger.debug("未找到 sqlite-vec 扩展，向量层回退 Python 计算")
        return False
    if not hasattr(conn, "enable_load_extension"):
        logger.warning("当前 Python 构建不支持 load_extension，向量层回退")
        return False
    try:
        conn.enable_load_extension(True)
        conn.load_extension(str(ext))
        conn.execute("SELECT vec_version()").fetchone()
    except (sqlite3.Error, AttributeError) as exc:
        logger.warning("加载 sqlite-vec 失败（回退 Python 计算）: %s", exc)
        return False
    finally:
        try:
            conn.enable_load_extension(False)   # 用完立刻关掉，缩小攻击面
        except sqlite3.Error as exc:
            logger.debug("关闭 load_extension 失败: %s", exc)
    _LOADED_CACHE.update({"ok": True, "path": str(ext)})
    return True


def ensure_table(conn: sqlite3.Connection, dim: int = 1024) -> bool:
    """建 vec0 虚拟表（幂等）。未加载扩展直接返回 False。"""
    if not load_into(conn):
        return False
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_facts USING vec0("
            "fact_id TEXT PRIMARY KEY, embedding float[%d] distance_metric=cosine)" % dim)
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("建 vec_facts 失败: %s", exc)
        return False


def serialize(vector: Sequence[float]) -> bytes:
    return struct.pack("%df" % len(vector), *vector)


def upsert(conn: sqlite3.Connection, items: Sequence[Tuple[str, Sequence[float]]],
           dim: int = 1024, batch: int = 200, retries: int = 5) -> int:
    """批量写入/更新向量。返回成功条数。

    两个必须处理的现实（2026-10-23 实测踩过）：
    1. **一次写几千条会长时间持锁**，与查询或另一个导入进程并发时直接
       `database is locked`，而失败只打了条 warning，导致 156 条静默缺索引。
       改为分批提交，每批 200 条，持锁时间从分钟级降到毫秒级。
    2. **失败不能一走了之**。锁是瞬态的，忙等重试几次基本都能成功；
       真重试完还失败才放弃，并且把失败条数报出去，不装作成功。
    """
    if not items or not ensure_table(conn, dim):
        return 0
    written = 0
    pending = [(fid, vec) for fid, vec in items if len(vec) == dim]
    if len(pending) != len(items):
        logger.debug("跳过维度不符的向量 %d 条", len(items) - len(pending))

    for i in range(0, len(pending), batch):
        chunk = pending[i:i + batch]
        for attempt in range(retries):
            try:
                for fid, vec in chunk:
                    conn.execute("DELETE FROM vec_facts WHERE fact_id = ?", (fid,))
                    conn.execute(
                        "INSERT INTO vec_facts(fact_id, embedding) VALUES (?, ?)",
                        (fid, serialize(vec)))
                conn.commit()
                written += len(chunk)
                break
            except sqlite3.OperationalError as exc:
                # database is locked / busy：瞬态，退避后重试
                if attempt == retries - 1:
                    logger.warning("向量索引写入放弃（已重试 %d 次）: %s", retries, exc)
                    try:
                        conn.rollback()
                    except sqlite3.Error as rb:
                        logger.debug("回滚失败: %s", rb)
                else:
                    time.sleep(0.2 * (attempt + 1))
            except sqlite3.Error as exc:
                logger.warning("向量索引写入失败（非锁问题）: %s", exc)
                break
    return written



def upsert_current(conn: sqlite3.Connection, items: Sequence[Tuple[str, Sequence[float], str, str]],
                   *, user_id: str, model: str, dim: int, batch: int = 200) -> Dict[str, Any]:
    """Index rebuilt vectors only while their source fact and embedding still match.

    A separate writer may have committed after the rebuild wrote fact_embeddings.
    The validation and index update therefore share the same write transaction.
    This method expects no open transaction and commits at most one batch at a time.
    """
    result: Dict[str, Any] = {"available": False, "indexed": 0, "skipped_changed": 0, "failed": 0}
    if not items:
        return result
    if conn.in_transaction:
        raise ValueError("upsert_current requires no active transaction")
    if not ensure_table(conn, dim):
        return result
    result["available"] = True
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        indexed = skipped = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            for fid, vec, subject, statement in chunk:
                blob = serialize(vec)
                current = conn.execute(
                    "SELECT 1 FROM working_fact f JOIN fact_embeddings e ON e.fact_id=f.fact_id "
                    "WHERE f.fact_id=? AND f.user_id=? AND f.status='active' "
                    "AND f.subject IS ? AND f.statement IS ? AND e.model=? AND e.vector=?",
                    (fid, user_id, subject, statement, model, blob)).fetchone()
                if current is None:
                    skipped += 1
                    continue
                conn.execute("DELETE FROM vec_facts WHERE fact_id=?", (fid,))
                conn.execute("INSERT INTO vec_facts(fact_id, embedding) VALUES (?, ?)", (fid, blob))
                indexed += 1
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            result["failed"] += len(chunk)
            logger.warning("重建向量索引批次失败: %s", exc)
            continue
        result["indexed"] += indexed
        result["skipped_changed"] += skipped
    return result


def knn(conn: sqlite3.Connection, query_vec: Sequence[float], *,
        limit: int = 50, dim: int = 1024) -> List[Tuple[str, float]]:
    """KNN 检索，返回 [(fact_id, distance)]。未加载扩展返回空列表。"""
    if not load_into(conn):
        return []
    try:
        rows = conn.execute(
            "SELECT fact_id, distance FROM vec_facts WHERE embedding MATCH ?"
            " ORDER BY distance LIMIT ?", (serialize(query_vec), limit)).fetchall()
        return [(r[0], float(r[1])) for r in rows]
    except sqlite3.Error as exc:
        logger.warning("KNN 检索失败，回退: %s", exc)
        return []


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    ext = find_extension()
    info: Dict[str, Any] = {"extension_found": ext is not None,
                            "extension_path": str(ext) if ext else "",
                            "loaded": False, "rows": 0}
    if ext is None:
        return info
    info["loaded"] = load_into(conn)
    if info["loaded"]:
        try:
            info["rows"] = conn.execute("SELECT COUNT(*) FROM vec_facts").fetchone()[0]
            info["version"] = conn.execute("SELECT vec_version()").fetchone()[0]
        except sqlite3.Error as exc:
            logger.debug("读取向量索引统计失败: %s", exc)
    return info

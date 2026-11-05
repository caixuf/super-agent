#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量编解码、余弦计算与休眠向量垃圾回收（Dormant Vector GC）辅助模块。"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from typing import Any, Dict, List, Optional, Sequence

from .config import get_config


def pack_vector(vec: Sequence[float]) -> bytes:
    """浮点列表打包为二进制 float32 字节流（小端存储）。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> List[float]:
    """二进制字节流解包为 float 列表。"""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def valid_embedding(vec: Sequence[float], dim: Optional[int] = None) -> bool:
    """Reject malformed vectors before they can enter the cache or database."""
    target_dim = dim if dim is not None else get_config().embed_dim
    try:
        return (
            len(vec) == target_dim
            and all(math.isfinite(float(value)) for value in vec)
            and len(pack_vector(vec)) == target_dim * 4
        )
    except (TypeError, ValueError, OverflowError, struct.error):
        return False


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0
    except Exception:
        return 0.0


def delete_vectors_for_facts(
    conn: sqlite3.Connection,
    fact_ids: Sequence[str],
) -> Dict[str, int]:
    """从 fact_embeddings 与 vec_facts 中删除指定 fact_id 的向量，释放存储与 KNN 索引。"""
    ids = [str(fid).strip() for fid in fact_ids if str(fid).strip()]
    if not ids:
        return {"embeddings_deleted": 0, "vec_deleted": 0}
    emb_deleted = 0
    vec_deleted = 0
    for offset in range(0, len(ids), 400):
        chunk = ids[offset : offset + 400]
        marks = ",".join("?" for _ in chunk)
        try:
            cur = conn.execute(
                f"DELETE FROM fact_embeddings WHERE fact_id IN ({marks})",
                chunk,
            )
            emb_deleted += max(0, cur.rowcount)
        except sqlite3.Error:
            pass
        try:
            cur_v = conn.execute(
                f"DELETE FROM vec_facts WHERE fact_id IN ({marks})",
                chunk,
            )
            vec_deleted += max(0, cur_v.rowcount)
        except sqlite3.Error:
            pass
    return {"embeddings_deleted": emb_deleted, "vec_deleted": vec_deleted}


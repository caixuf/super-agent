#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可扩展的分桶注册表：维度与取值都是**数据**，不是写死的枚举。

为什么不能写死（2026-10-23，用户第三次指出）：
    前两版把 DOMAINS 写成元组常量，每加一个领域都要改代码、跑测试、发版。
    而记忆种类会持续增加 —— 这等于把「记忆系统的自动化」又破坏掉了：
    系统能自动学习，却学不出一个新的领域分类。

设计：
    1. 维度（domain / genre / ebm）与取值都存在 SQLite 表 registry 里；
    2. 首次使用时用一份**种子数据**初始化，之后由数据驱动；
    3. 新取值自动登记 —— 写入时出现未见过的 domain，自动加入注册表，
       不需要改代码；
    4. 只有「哪些领域带代际」这一条语义规则需要人工声明（这是业务判断，
       不是分类数据），其余全部自动化。

边界：
    自动登记不等于放任。新领域首次出现时登记但不激活（status=observed），
    在巡检里可见；确认有用后转 active。避免模型随口编一个领域名就永久留在库里。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("registry"))

#: 种子数据由 MemoryConfig 注入（seed_domains / seed_genres / era_domains），
#: 引擎不内置任何企业的领域名或代际名。只用于首次初始化库，之后以库里的为准。
DEFAULT_DOMAIN = "general"
DEFAULT_GENRE = "experience"


def _db_path() -> Path:
    """维度注册表路径（与事实库同目录，迁移/备份不会漏）。"""
    return get_config().registry_db_path()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS registry (
            dimension   TEXT NOT NULL,
            value       TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            has_ebm     INTEGER NOT NULL DEFAULT 0,
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            PRIMARY KEY (dimension, value)
        )
    """)
    conn.commit()
    return conn


def _ensure_seeded(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) c FROM registry").fetchone()["c"]
    if n:
        return
    cfg = get_config()
    # 带代际的领域由宿主声明（era_domains）；它们同时是合法领域取值。
    era = {str(d).strip().lower() for d in cfg.era_domains}
    domains = list(dict.fromkeys(
        [*cfg.seed_domains, *cfg.era_domains]))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for v in domains:
        conn.execute(
            "INSERT OR IGNORE INTO registry(dimension,value,status,has_ebm,note,created_at)"
            " VALUES(?,?,?,?,?,?)",
            ("domain", v, "active", 1 if str(v).strip().lower() in era else 0,
             "seed", now))
    for v in cfg.seed_genres:
        conn.execute(
            "INSERT OR IGNORE INTO registry(dimension,value,status,has_ebm,note,created_at)"
            " VALUES(?,?,?,?,?,?)", ("genre", v, "active", 0, "seed", now))
    conn.commit()


def _values(dimension: str, only_active: bool = True) -> List[str]:
    conn = _connect()
    try:
        _ensure_seeded(conn)
        sql = "SELECT value FROM registry WHERE dimension=? "
        if only_active:
            sql += "AND status='active' "
        sql += "ORDER BY value"
        return [r["value"] for r in conn.execute(sql, (dimension,)).fetchall()]
    finally:
        conn.close()


def domains(only_active: bool = True) -> List[str]:
    return _values("domain", only_active)


def genres(only_active: bool = True) -> List[str]:
    return _values("genre", only_active)


def register(dimension: str, value: str, *, note: str = "",
             status: str = "observed", has_ebm: Optional[bool] = None) -> Dict[str, Any]:
    """登记一个新取值。已存在则原样返回（不覆盖人工改过的状态）。

    新值默认 observed 而不是 active：允许系统自己长出新分类，但需要经过确认
    才会进入正式检索范围。否则模型随口编个名字就永久留在库里。
    """
    dim = (dimension or "").strip()
    val = (value or "").strip().lower()
    if not dim or not val:
        return {"ok": False, "error": "dimension 和 value 都不能为空"}
    conn = _connect()
    try:
        _ensure_seeded(conn)
        row = conn.execute(
            "SELECT * FROM registry WHERE dimension=? AND value=?", (dim, val)).fetchone()
        if row:
            return {"ok": True, "existed": True, "value": val, "status": row["status"]}
        conn.execute(
            "INSERT INTO registry(dimension,value,status,has_ebm,note,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (dim, val, status, 1 if has_ebm else 0, note,
             time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
        logger.info("登记新%s: %s（%s）", dim, val, status)
        return {"ok": True, "existed": False, "value": val, "status": status}
    finally:
        conn.close()


def has_ebm(domain: str) -> bool:
    """该领域是否参与代际分桶。查不到按不参与处理（保守）。"""
    conn = _connect()
    try:
        _ensure_seeded(conn)
        row = conn.execute(
            "SELECT has_ebm FROM registry WHERE dimension='domain' AND value=?",
            ((domain or "").strip().lower(),)).fetchone()
        return bool(row and row["has_ebm"])
    finally:
        conn.close()


def normalize(dimension: str, value: Optional[str]) -> str:
    """归一：已登记的直接用，未登记的**自动登记为 observed** 并原样保留。

    保留原值而不是退回默认：退回默认会让所有新领域都变成 general，
    自动分类等于没做。observed 状态保证它不污染正式检索，但数据不丢。
    """
    v = (value or "").strip().lower()
    if not v:
        return DEFAULT_DOMAIN if dimension == "domain" else DEFAULT_GENRE
    known = _values(dimension, only_active=False)
    if v not in known:
        register(dimension, v, note="auto")
    return v


def describe() -> Dict[str, Any]:
    conn = _connect()
    try:
        _ensure_seeded(conn)
        rows = conn.execute(
            "SELECT dimension, value, status, has_ebm FROM registry"
            " ORDER BY dimension, value").fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            out.setdefault(r["dimension"], []).append({
                "value": r["value"], "status": r["status"], "has_ebm": bool(r["has_ebm"])})
        return {"dimensions": out, "db": str(_db_path())}
    finally:
        conn.close()


def filter_clause(dimension: str, values: Optional[Sequence[str]]) -> Tuple[str, list]:
    """生成某维度的过滤 SQL。缺标签的旧数据按默认值解释。"""
    if dimension not in ("domain", "genre"):
        return "", []
    wanted = [v.strip().lower() for v in (values or []) if v and v.strip()]
    if not wanted:
        return "", []
    default = DEFAULT_DOMAIN if dimension == "domain" else DEFAULT_GENRE
    marks = ",".join("?" * (len(wanted) + 1))
    sql = ("(EXISTS (SELECT 1 FROM fact_tags d WHERE d.fact_id = f.fact_id"
           " AND d.key = ? AND d.value IN (" + marks + "))"
           " OR NOT EXISTS (SELECT 1 FROM fact_tags d2 WHERE d2.fact_id = f.fact_id"
           " AND d2.key = ?))")
    params = [dimension, *wanted, default, dimension]
    return sql, params

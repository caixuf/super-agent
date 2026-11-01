#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/memory_store.py — 确定性记忆存储（绕开 hy_memory 的 LLM 抽取层）。

## 为什么要绕开

HyMemoryClient.add() 走 Extractor V2，用它自己的 LLM 判定「新事实是否取代旧事实」。
实测轨迹：先写 workspace_id=G1 的「门限 3.0%」，再写 workspace_id=G2 的
「门限 5.0%」，前者被自动标成 superseded —— 因为它只看语义，不知道 G1/G2 是
两代不同的代际。代际差异在它眼里只是「同一个结论的不同说法」。

结论：记忆的写入策略必须由我们决定，不能交给一个不知道 G1/G2 区别的抽取器。

## 这一层做什么

- **workspace_id 硬隔离**：代际（G1/G2/_unknown）写入即分桶，跨代永不互相覆盖；
- **tags 开放式 KV**：不限维度（topic / signal / vehicle / 任意 key）；
- **status 由我们控制**：只有显式调用淘汰时才改，抽取器说了不算。

## 为什么不把标签塞进 working_fact

working_fact 是 hy_memory 自己的表。往里加列（ALTER TABLE ADD COLUMN tags）能跑，
但 hy_memory 升级重建表时那一列会静默消失 —— 记忆标签是长期资产，不能寄存在
别人的表结构上。标签因此落在独立表 fact_tags，只通过 fact_id 外键关联：
上游怎么改表都不影响，读的时候一次 JOIN 补齐。

fact_tags 用 (fact_id, key) 复合主键 + (key, value) 索引：同一 fact 的同一个
维度只有一个值（不留歧义），按任一 key=value 过滤都是一次等值查找。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import struct
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import get_config, logger_name

EBM_UNKNOWN = "unknown"


def _pack_vector(vec: Sequence[float]) -> bytes:
    """浮点列表打包为二进制 float32 字节流（小端存储）。"""
    return struct.pack(f"<{len(vec)}f", *vec)



def _valid_embedding(vec: Sequence[float]) -> bool:
    """Reject malformed vectors before they can enter the cache or database."""
    try:
        return (len(vec) == get_config().embed_dim
                and all(math.isfinite(float(value)) for value in vec)
                and len(_pack_vector(vec)) == get_config().embed_dim * 4)
    except (TypeError, ValueError, OverflowError, struct.error):
        return False


def _unpack_vector(blob: bytes) -> List[float]:
    """二进制字节流解包为 float 列表。"""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


#: 无代际信息的经验一律进这个桶，不臆造代际。
UNKNOWN_BUCKET = "_unknown"


def _db_path() -> Path:
    """事实库路径。目录由宿主通过 MemoryConfig.data_dir 指定。"""
    return get_config().db_path()


#: 批量灌入时放宽 SQLite 耐久性。
#:
#: 实测（远端宿主，2026-10-23）：默认 synchronous=FULL 每条 commit 都要 fsync，
#: 该盘 fsync 均值 73ms、峰值 117ms，写入被钉在 1.4~8.7 条/秒；改成 NORMAL
#: 后同一库同表跑到 474 条/秒（54 倍）。灌书是「可重跑」的批处理，崩了重灌
#: 即可，不值得为每条记录付一次 fsync。
#:
#: 只在显式设 MEMORY_BULK_INGEST=1 时生效 —— 交互式写记忆（对话、auto_learn）
#: 必须保持默认 FULL，那条记录丢了就是真丢了。
_BULK_INGEST = os.getenv("MEMORY_BULK_INGEST", "").strip() == "1"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    if _BULK_INGEST:
        # NORMAL 在 WAL 下仍能保证库不损坏，只可能在断电时丢最后几个事务。
        conn.execute("PRAGMA synchronous = NORMAL;")
        # 灌入时不频繁 checkpoint，减少 fsync 次数
        conn.execute("PRAGMA wal_autocheckpoint = 20000;")
    # sqlite-vec 是**连接级**扩展：每个新连接都要单独加载，
    # 否则只有加载过的那次能用，换个连接就报 no such module: vec0
    # （实测：reindex 成功了，但下一次 recall 新建连接读不到索引）。
    # 加载失败静默降级 —— 向量索引是可选加速层。
    try:
        from . import vec_index
        vec_index.load_into(conn)
        # 建表必须在连接工厂里做，不能指望调用方记得先调 ensure_table：
        # 新建库第一次 recall 时表不存在 → KNN 抛 "no such table" → 回退 LIKE，
        # 而 LIKE 对上中文长句基本是空集，最终表现为「写入成功但永远召回 0 命中」。
        vec_index.ensure_table(conn, dim=get_config().embed_dim)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(logger_name("store")).debug("vec 扩展未加载: %s", exc)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS working_fact (
            fact_id                TEXT PRIMARY KEY,
            user_id                TEXT NOT NULL,
            agent_id               TEXT NOT NULL DEFAULT 'default_agent',
            session_id             TEXT,
            workspace_id           TEXT,
            branch                 TEXT,
            source                 TEXT NOT NULL DEFAULT 'default',
            statement              TEXT NOT NULL,
            files                  TEXT NOT NULL DEFAULT '[]',
            subject                TEXT NOT NULL DEFAULT '',
            details                TEXT NOT NULL DEFAULT '',
            fact_link              TEXT,
            derived_experience_ids TEXT NOT NULL DEFAULT '[]',
            status                 TEXT NOT NULL DEFAULT 'active',
            fact_chain_summary     TEXT NOT NULL DEFAULT '',
            created_at             TEXT NOT NULL
        )
        """
    )
    # 标签独立表：不侵入上游表结构。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_tags (
            fact_id  TEXT NOT NULL,
            key      TEXT NOT NULL,
            value    TEXT NOT NULL,
            PRIMARY KEY (fact_id, key)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_tags_kv ON fact_tags(key, value)")
    # working_fact 曾只有 fact_id 主键索引，而几乎所有热路径都按非主键列过滤：
    #   - 每条 write_fact 先做一次 (user_id,workspace_id,subject,statement) 去重查询
    #   - 召回/统计/巡检反复 `status='active'`
    # 没有索引就是全表扫描。实测远端宿主（7.5 万条）建索引前：
    #   去重查询 7.54ms/次、status 计数 9.51ms/次 —— 灌入被钉在 2.4 条/秒；
    #   建索引后：0.01ms（754 倍）/ 0.51ms（18 倍），灌入 6.8 条/秒。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_status ON working_fact(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_dedup"
        " ON working_fact(user_id, workspace_id, subject, statement)")
    # 按来源批量作废/统计（nightly 重灌的阶段 1）走这条
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_source ON working_fact(source, status)")
    # 向量持久化表：保存事实文本的高维嵌入，写入即持久化，召回时避免重复请求模型
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_embeddings (
            fact_id    TEXT PRIMARY KEY,
            model      TEXT NOT NULL,
            dim        INTEGER NOT NULL,
            vector     BLOB NOT NULL,
            text_hash  TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn



def normalize_ebm(ebm: Optional[str]) -> str:
    """代际归一成桶名。认不出的进 unknown_bucket。

    识别规则由 MemoryConfig 提供（era_pattern / era_prefix / unknown_bucket），
    这样同一个引擎既能服务只有「G1/G2」这类代际体系的业务，也能服务于
    「v1/v2 版本」「gen3/gen4 硬件」这类完全不同的代际体系。

    绝不默认当成「都适用」—— 判不出代际还让它对所有代际生效，
    等于用一个未知范围的规则去覆盖已知范围。
    """
    return get_config().normalize_era(ebm)


def _clean_tags(tags: Optional[Dict[str, Any]]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in (tags or {}).items() if v not in (None, "", [])}


def _tags_of(conn: sqlite3.Connection, fact_id: str) -> Dict[str, str]:
    return {r["key"]: r["value"] for r in
            conn.execute("SELECT key, value FROM fact_tags WHERE fact_id=?", (fact_id,))}


def _tags_of_many(conn: sqlite3.Connection, fact_ids: Sequence[str]) -> Dict[str, Dict[str,str]]:
    ids=list(dict.fromkeys(fact_ids));result={fid:{} for fid in ids}
    # Stay below SQLite's conservative variable limit, including old installs.
    for offset in range(0,len(ids),400):
        chunk=ids[offset:offset+400]
        rows=conn.execute('SELECT fact_id,key,value FROM fact_tags WHERE fact_id IN ('+
                          ','.join('?' for _ in chunk)+')',chunk)
        for row in rows:
            fid, k, v = row[0], row[1], row[2]
            if fid in result:
                result[fid][k] = v
    return result


def _put_tags(conn: sqlite3.Connection, fact_id: str, tags: Dict[str, str]) -> None:
    for k, v in tags.items():
        conn.execute(
            "INSERT INTO fact_tags(fact_id, key, value) VALUES (?,?,?)"
            " ON CONFLICT(fact_id, key) DO UPDATE SET value=excluded.value",
            (fact_id, k, v))


def write_fact(
    statement: str,
    *,
    ebm: Optional[str] = None,
    tags: Optional[Dict[str, Any]] = None,
    subject: str = "",
    details: str = "",
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: str = "manual",
    branch: Optional[str] = None,
    fact_id: Optional[str] = None,
    resolve_conflict: bool = False,
    initial_status: str = 'active',
    dedupe_by_statement: bool = False,
    schedule_background_index: bool = True,
) -> Dict[str, Any]:
    """确定性写入一条经验。同 (workspace_id, subject, statement) 视为同一条，幂等跳过。

    resolve_conflict=True 时先和新事实的候选集做矛盾裁决（tools/memory_conflict）：
    - supersede：写新事实，并把被取代的那条标 status='superseded'（不删除）；
    - reject_new：新记录含糊/矛盾无解，不写；
    - coexist / unrelated：照常写。

    默认关闭，是为了不改变既有调用点的语义（尤其是批量导入和测试夹具）。
    自动触发链路（tools/memory_trigger）显式打开它。
    """
    stmt = (statement or "").strip()
    if initial_status not in ('active', 'candidate') or (initial_status == 'candidate' and resolve_conflict):
        raise ValueError('invalid initial fact status')
    if initial_status == 'candidate' and dedupe_by_statement:
        raise ValueError('candidate publishers require source-scoped identity')
    if not stmt:
        return {"ok": False, "error": "statement 为空"}

    # A deterministic candidate ID is the replay/identity boundary. Check it
    # before activation, background indexing or monthly reporting so a later
    # model call can never hijack or duplicate an existing owned fact.
    cfg = get_config()
    user_id = user_id or cfg.default_user
    agent_id = agent_id or cfg.default_agent
    source = source or "manual"
    bucket = normalize_ebm(ebm)
    if initial_status == "candidate" and fact_id:
        conn = _connect()
        try:
            known = conn.execute(
                "SELECT fact_id,user_id,agent_id,workspace_id,subject,statement,source,status"
                " FROM working_fact WHERE fact_id=?", (fact_id,)).fetchone()
        finally:
            conn.close()
        if known is not None:
            expected = dict(user_id=user_id, agent_id=agent_id, workspace_id=bucket,
                            subject=subject, statement=stmt, source=source)
            if any(known[key] != value for key, value in expected.items()):
                raise ValueError("candidate_fact_identity_conflict")
            return {"ok": True, "written": False,
                    "reason": "duplicate" if known["status"] == "active" else "candidate_exists",
                    "fact_id": fact_id, "workspace_id": bucket,
                    "status": known["status"]}

    # 身份默认值在调用时取，不用函数签名默认值：
    # 签名默认值在模块导入时就固化了，宿主后来 configure() 注入的配置不生效。
    cfg = get_config()
    user_id = user_id or cfg.default_user
    agent_id = agent_id or cfg.default_agent
    source = source or "manual"

    bucket = normalize_ebm(ebm)
    tag_map = _clean_tags(tags)
    if subject:
        tag_map.setdefault('subject_key', re.sub(r'[^a-z0-9_\u4e00-\u9fff]', '', subject.lower()))
    # ebm 同时进 workspace_id 与标签：前者做物理分桶，后者让调用方
    # 能用统一接口按任意维度查（含 ebm 本身）。
    tag_map["ebm"] = bucket
    # 记录类别，供召回按场景过滤（技术诊断不该召回人物事实）。
    # 调用方显式给了就用给的，否则按文本判定。
    try:
        from .memory_kind import classify_kind
    except ImportError:
        from .memory_kind import classify_kind
    tag_map.setdefault("kind", classify_kind(stmt, subject, ebm))
    try:
        from .memory_kind import classify_observation_class, TAG_OBSERVATION_CLASS
    except ImportError:
        from .memory_kind import classify_observation_class, TAG_OBSERVATION_CLASS
    _obs = classify_observation_class(stmt, subject, tag_map)
    if _obs:
        tag_map.setdefault(TAG_OBSERVATION_CLASS, _obs)
    # 作用域与代际正交：代际回答「哪一代车」，scope 回答「什么性质的知识」。
    # 外部书籍/文档必须标 reference，否则会和平台经验混在同一个召回池里互相稀释。
    try:
        from .memory_scope import scope_for_source
    except ImportError:
        from .memory_scope import scope_for_source
    tag_map.setdefault("scope", scope_for_source(source))

    # 三维分桶：domain(领域) / genre(体裁) / ebm(代际)。缺项取默认，旧数据不受影响。
    # 注意顺序：genre 由 scope 推导，必须在 scope 写入之后。
    try:
        from .memory_buckets import SCOPE_TO_GENRE, KIND_TO_DOMAIN, has_ebm
        tag_map.setdefault("genre", SCOPE_TO_GENRE.get(tag_map["scope"], "experience"))
        # 领域推断优先看**来源文件路径**（tags.origin），而不是 subject：
        # subject 存的是标题路径（如「by chatGPT (python) (1/3)」），
        # 完全看不出属于哪本书哪个领域。origin 才是文件名/相对路径。
        try:
            from .domain_infer import infer_domain as _infer
            _src_hint = str(tag_map.get("origin") or "")
            dom = _infer(_src_hint, stmt, ebm or "")
            if dom == "general":
                dom = _infer(subject or "", stmt, ebm or "")
        except Exception:  # noqa: BLE001 —— 推断失败退回旧逻辑
            dom = KIND_TO_DOMAIN.get(tag_map.get("kind") or "", "general")
        tag_map.setdefault("domain", dom)
        # 代际只对宿主领域有意义。Linux/AI/DDS 这些知识没有「哪一代」的概念，
        # 给它们打 ebm 只会让 _unknown 桶无限膨胀（实测 6530 条里只有 1 条真有代际）。
        # 只有「调用方显式传了 ebm」或「领域本身就带代际」时才保留 ebm 标签。
        # 自动推断出来的默认领域不该顺手把 ebm 删掉 —— 调用方明说要 G1/G2 就该尊重。
        # 显式 = 参数传了，或 tags 里带了 ebm 键（老调用方常用 tags 传）
        _explicit_ebm = bool((ebm or "").strip()) or ("ebm" in (tags or {}))
        if not has_ebm(dom) and not _explicit_ebm:
            tag_map.pop("ebm", None)
    except Exception as exc:  # noqa: BLE001 —— 分桶元数据缺失不该阻断写入
        logging.getLogger(logger_name("store")).debug("分桶标签跳过: %s", exc)

    conn = _connect()
    try:
        # Curated publishers supply a content-addressed id. Replays must check
        # that exact identity before the general statement deduplication query,
        # which otherwise scans a large owner's library once per section.
        if initial_status == 'candidate' and fact_id:
            known = conn.execute(
                'SELECT fact_id,user_id,workspace_id,subject,statement,source FROM working_fact WHERE fact_id=?',
                (fact_id,)).fetchone()
            if known is not None:
                if any(known[k] != v for k,v in dict(user_id=user_id,workspace_id=bucket,
                        subject=subject,statement=stmt,source=source).items()):
                    raise ValueError('candidate_fact_identity_conflict')
                return {'ok':True,'written':False,'reason':'duplicate',
                        'fact_id':fact_id,'workspace_id':bucket,'tags':_tags_of(conn,fact_id)}
        # Candidate publishers own only their source, never a matching manual fact.
        dedupe_source = ' AND source=?' if initial_status == 'candidate' else ''
        duplicate_clause = (' WHERE user_id=? AND workspace_id=? AND statement=?' if dedupe_by_statement
                            else ' WHERE user_id=? AND workspace_id=? AND subject=? AND statement=?')
        duplicate_params = [user_id, bucket, stmt] if dedupe_by_statement else [user_id, bucket, subject, stmt]
        if dedupe_source:
            duplicate_params.append(source)
        dup = conn.execute(
            "SELECT fact_id FROM working_fact" + duplicate_clause + dedupe_source,
            duplicate_params,
        ).fetchone()
        if dup:
            return {"ok": True, "written": False, "reason": "duplicate",
                    "fact_id": dup["fact_id"], "workspace_id": bucket,
                    "tags": _tags_of(conn, dup["fact_id"])}
        verdict: Dict[str, Any] = {}
        if resolve_conflict:
            try:
                from . import memory_conflict
            except ImportError:
                from . import memory_conflict  # type: ignore
            candidates = _conflict_candidates(conn, stmt, bucket, user_id)
            verdict = memory_conflict.resolve(
                {"statement": stmt, "subject": subject, "workspace_id": bucket},
                candidates)
            if verdict.get("action") == "skip":
                return {"ok": True, "written": False, "reason": "rejected_by_conflict",
                        "workspace_id": bucket, "conflict": verdict}

        fid = fact_id or str(uuid.uuid4())
        conn.execute(
            "INSERT INTO working_fact"
            " (fact_id, user_id, agent_id, session_id, workspace_id, branch, source,"
            "  statement, files, subject, details, fact_link, derived_experience_ids,"
            "  status, fact_chain_summary, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, user_id, agent_id, "default_session", bucket, branch, source,
             stmt, "[]", subject, details, None, "[]",
             initial_status, "", time.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        _put_tags(conn, fid, tag_map)
        superseded: List[str] = []
        if verdict.get("relation") == "supersede":
            for old_id in verdict.get("superseded") or []:
                cur = conn.execute(
                    "UPDATE working_fact SET status='superseded', fact_chain_summary=?"
                    " WHERE fact_id=? AND status='active'",
                    ("superseded_by:%s %s" % (fid, verdict.get("reason") or ""), old_id))
                if cur.rowcount > 0:
                    superseded.append(old_id)
        conn.commit()
        out = {"ok": True, "written": True, "fact_id": fid,
               "workspace_id": bucket, "tags": tag_map}
        if resolve_conflict:
            out["conflict"] = verdict
            out["superseded"] = superseded

        # 写入即预热/持久化向量。用非 daemon 线程 + atexit 汇合：
        # daemon 线程会在进程退出时被直接杀掉，向量就静默丢了 —— 常驻服务里
        # 看不出来，但脚本、CLI、一次性任务里每次写都丢索引，召回恒为 0。
        # 这里保证「进程正常退出前，已提交的写入都把向量落盘」。
        # Candidate rows use the synchronous admission index path instead: an
        # event must never become done while an index write is merely queued.
        if schedule_background_index:
            _submit_background(_bg_embed, fid, subject or "", stmt)

        # Candidate rows are isolated until activation. Their metadata may be
        # model-derived, so they must not reach the durable monthly report yet.
        if initial_status == "active":
            try:
                from . import monthly_log as _monthly_log
                _monthly_log.maybe_append_from_write(
                    out, statement=stmt, subject=subject, tags=tag_map)
            except Exception as exc:  # noqa: BLE001 -- log must never break write
                logging.getLogger(logger_name("store")).debug(
                    "monthly_log hook skipped: %s", exc)

        return out
    finally:
        conn.close()



#: 召回门限按类别分。0.60 是按 45 条技术经验的两簇分布定的（正例 0.68~0.73 /
#: 负例 0.40~0.53）；通用记忆是自然语言事实，提问句与陈述句的余弦天然偏低 ——
#: 实测「菜虚飞是什么」对正确事实仅 0.57、李龙飞那条 0.604。同一门限会误杀大半。
#: 分开定：技术类保持 0.60（防止把无关经验塞进诊断），通用类放到 0.52。
_RECALL_FLOOR_BY_KIND = {"technical": 0.60, "personal": 0.52, "context": 0.55}

#: 外部书籍/文档语料的额外门限。书籍正文与任何中文问句都有天然的语义重叠
#: （实测「随便聊聊」也能命中 0.638 的 Python 教材段落、「今天天气不错」命中
#: 讲天气例句的段落），而平台自身的决策事实（scope=platform）分数分布完全不同。
#: 两者共用一个门限的结果是：闲聊也在注入书籍片段，把真正的决策挤出去。
#: 所以 reference 语料要更高的相似度才配进对话上下文。
_REFERENCE_SCOPE_FLOOR = float(os.environ.get("MEMORY_REFERENCE_MIN_SCORE", "0.78") or 0.78)

#: 外部语料要求的查询词元命中比例。见 _passes_floor 的说明。
_REFERENCE_TERM_RATIO = float(os.environ.get("MEMORY_REFERENCE_TERM_RATIO", "0.4") or 0.4)

#: 去掉通用词后至少还要剩这么多个词元，才认为这句问话有技术信息量。
_REFERENCE_MIN_TERMS = int(os.environ.get("MEMORY_REFERENCE_MIN_TERMS", "3") or 3)

#: 不属于任何技术领域的通用词。它们在闲聊里高频出现，命中它们不构成
#: 「这段在讲我问的事」的证据。宁可少召回，也不要拿教材里的天气例句
#: 假装懂天气。
_GENERIC_TERMS = frozenset("""
今天 明天 昨天 现在 时候 天天 天气 不错 好吗 什么 怎么 为何 为什么
这个 那个 一下 一些 可以 应该 需要 没有 就是 还是 但是 因为 所以
谢谢 你好 请问 帮我 看看 知道 觉得 认为 其实 真的 非常 比较
""".split())


def get_facts_by_ids(
    fact_ids: Sequence[str],
    *,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    include_inactive: bool = False,
) -> Dict[str, Any]:
    """Thin helper: load full working_fact rows (+ tags) for explicit ids.

    Used by progressive L2/L3. Does not invent ranking — order follows *fact_ids*.
    Unknown ids are reported honestly in ``missing_ids``; no silent fabrication.
    """
    ids: List[str] = []
    seen = set()
    for raw in fact_ids or []:
        fid = str(raw or "").strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        ids.append(fid)
    if not ids:
        return {"ok": True, "results": [], "missing_ids": [], "total": 0}

    user_id = user_id or get_config().default_user
    conn = _connect()
    try:
        marks = ",".join("?" * len(ids))
        where = ["fact_id IN (" + marks + ")", "user_id = ?"]
        params: List[Any] = list(ids) + [user_id]
        if agent_id is not None:
            where.append("agent_id = ?")
            params.append(str(agent_id))
        if profile is not None:
            where.append("EXISTS (SELECT 1 FROM fact_tags pr WHERE"
                         " pr.fact_id=working_fact.fact_id AND pr.key='profile'"
                         " AND pr.value=?)")
            params.append(str(profile))
        if not include_inactive:
            where.append("status = 'active'")
        rows = conn.execute(
            "SELECT * FROM working_fact WHERE " + " AND ".join(where),
            params,
        ).fetchall()
        by_id = {r["fact_id"]: dict(r) for r in rows}
        tags_by = _tags_of_many(conn, list(by_id.keys()))
        results: List[Dict[str, Any]] = []
        missing: List[str] = []
        for fid in ids:
            row = by_id.get(fid)
            if row is None:
                missing.append(fid)
                continue
            row = dict(row)
            row["tags"] = tags_by.get(fid, {})
            results.append(row)
        return {"ok": True, "results": results, "missing_ids": missing, "total": len(results)}
    finally:
        conn.close()


def _confidence_weights(fact_ids: Sequence[Optional[str]]) -> Dict[str, float]:
    """从突触账本取置信度权重，供召回排序使用。

    为什么必须接这一步：强化（reinforce_memory）写的是 memory_synapses.json，
    而召回排序原本只看余弦 —— 两边不通，导致「任务成功 → 置信度涨 → 行为不变」。
    没有这个连接，可塑性机制等于装饰品。

    权重映射（乘在相似度上，只影响排序，不改变能否通过门限）：
      active 1.0 / suspect 0.5 / deprecated 0.3 / 未记录 1.0。
      未记录给 1.0：没被用过不等于不可信。
    """
    out: Dict[str, float] = {}
    try:
        from . import synapse
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(logger_name("store")).debug("突触账本不可用: %s", exc)
        return out
    # 读取也要走宿主提供的账本：写入端已切到宿主的实现，
    # 读取端若还读引擎自带路径，就会出现「强化写进去了、排序却读不到」。
    loader = get_config().synapse_loader or synapse.load_synapses
    try:
        syn = loader() or {}
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(logger_name("store")).debug("读取突触账本失败: %s", exc)
        return out
    for fid in fact_ids:
        if not fid:
            continue
        entry = syn.get(fid)
        if not entry:
            continue
        status = str(entry.get("status") or "active")
        # 权重要够狠才真的影响排序：实测 0.85 压不动一条相似度差 0.24 的条目
        # （0.8756×0.85=0.744 仍高于 0.6303），等于降级白做。
        # suspect：明显可疑但仍可能有用 —— 给 0.5，跌出 top-1 但不消失。
        # deprecated：已作废 —— 0.3，基本沉底。
        if status == "deprecated":
            out[fid] = 0.3
        elif status == "suspect":
            out[fid] = 0.5
        else:
            out[fid] = 1.0
    return out


def _knn_rowids(conn: sqlite3.Connection, query: str, *,
                buckets: Optional[Sequence[str]] = None,
                kinds: Optional[Sequence[str]] = None,
                scopes: Optional[Sequence[str]] = None,
                user_id: Optional[str] = None,
                agent_id: Optional[str] = None,
                profile: Optional[str] = None,
                limit: int = 200) -> Optional[List[int]]:
    """用 sqlite-vec 做语义粗筛，返回 rowid 列表。不可用时返回 None（调用方回退）。

    为什么这层比 LIKE 重要：LIKE 是字面子串匹配，中文没有空格，
    「李龙飞是谁」匹配不上「李龙飞自称菜虚飞…」；而 KNN 是语义的，能命中。
    数据规模越大，字面匹配越不可靠 —— 这是为互联网数据扩充做的前置。

    除向量相似外，仍要套用代际桶/类别/状态过滤：向量只会给出「像」的，
    不会给出「该不该出现在这次查询里」。
    """
    q = (query or "").strip()
    if not q:
        return None
    try:
        from . import vec_index
    except ImportError:
        try:
            from . import vec_index  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(logger_name("store")).debug("vec_index 不可用: %s", exc)
            return None

    if not vec_index.find_extension():
        return None
    try:
        qvec = _embed_with_cache([q])
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(logger_name("store")).debug("查询向量化失败: %s", exc)
        return None
    if not qvec:
        return None

    hits = vec_index.knn(conn, qvec[0], limit=limit)
    if not hits:
        return None   # 索引空或加载失败 —— 交给 LIKE 兜底

    # 桶稀释问题（2026-10-23 灌入 3000 条后暴露）：
    # KNN 是全局取的，_unknown 桶条目一多就会把 G2 桶的精确结果挤出 top-N。
    # 实测 ebm=G2 查询时，真正讲门限的那条排到了第 4。
    # 修法：指定了代际桶时，额外**按桶补捞**一份，保证小桶不被大桶淹没。
    if buckets:
        try:
            from . import vec_index as _vi
            extra = _vi.knn(conn, qvec[0], limit=limit * 3)
            have = {fid for fid, _ in hits}
            hits = list(hits) + [(fid, d) for fid, d in extra if fid not in have]
        except Exception as exc:  # noqa: BLE001 —— 补捞失败不影响主结果
            logging.getLogger(logger_name("store")).debug("KNN 补捞失败: %s", exc)

    fids = list(dict.fromkeys(fid for fid, _dist in hits))
    # IN(200 ids) + user/status can make SQLite prefer a low-cardinality
    # owner index and scan the owner's entire library. Drive the join from
    # the bounded candidate list instead: each inner lookup is one unique PK.
    values = ",".join("(?)" for _ in fids)
    where = ["f.fact_id = candidate.fact_id", "f.status = 'active'", "f.user_id = ?"]
    params: List[Any] = [*fids, user_id]
    _append_strict_identity(where, params, agent_id=agent_id, profile=profile)
    if buckets:
        where.append("f.workspace_id IN (%s)" % ",".join("?" * len(buckets)))
        params.extend(buckets)
    if scopes:
        # 两条召回路径（KNN / LIKE）都要过滤 scope，少一处就会串池。
        try:
            from .memory_scope import filter_clause as _sc
        except ImportError:
            from .memory_scope import filter_clause as _sc
        _sql, _params = _sc(scopes)
        if _sql:
            where.append(_sql)
            params.extend(_params)
    if kinds:
        kmarks = ",".join("?" * len(kinds))
        where.append(
            "(EXISTS (SELECT 1 FROM fact_tags k WHERE k.fact_id = f.fact_id"
             " AND k.key = 'kind' AND k.value IN (" + kmarks + "))"
             " OR NOT EXISTS (SELECT 1 FROM fact_tags k2 WHERE k2.fact_id = f.fact_id"
             " AND k2.key = 'kind'))")
        params.extend(kinds)
    rows = conn.execute(
        "WITH candidate_ids(fact_id) AS (VALUES " + values + ") "
        "SELECT f.rowid AS rid FROM candidate_ids candidate CROSS JOIN working_fact f WHERE "
        + " AND ".join(where),
        params).fetchall()
    return [r["rid"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]


def _query_terms(query: str) -> set:
    """把查询切成用于词面重叠判定的词元。

    中文不靠空格分词，但这里不需要真分词：按二元组(character bigram)取，
    「踏板门限」-> {踏板, 板门, 门限}，足够判断「这段话里有没有在讲这个」。
    英文/数字按整词取。
    """
    q = (query or "").lower()
    terms = set()
    for w in re.findall(r"[a-z0-9_]{2,}", q):
        terms.add(w)
    han = re.findall(r"[\u4e00-\u9fff]+", q)
    for run in han:
        if len(run) == 1:
            continue
        for i in range(len(run) - 1):
            terms.add(run[i:i + 2])
    return terms


#: 中文虚词/代词/语气词。含这类字的二元组大多是**跨词边界**拼出来的假词元，
#: 例如「讲个笑话」会切出「个笑」「讲个」，它们能轻易在任意文本里命中，
#: 却是纯噪声。整词本身是虚词的（「怎么」「什么」）已在 _GENERIC_TERMS 里。
_STOP_CHARS = frozenset("的了着过吗呢吧啊呀哦嗯和与及或者就都也还很太更最不没要有在把被让给对从向往因此且"
                            "个讲说做个些种件条下上来去出到做干嘛什啥一二三四五六七八九十")


def _is_noise_term(t: str) -> bool:
    """跨词边界拼出来的二元组：含虚词/代词字，且不是常见技术词的一部分。

    这条判据只用于**放宽**，不用于收紧：真正的技术词（「门限」「组播」「指针」）
    不含量词字，不受影响；而「个笑」「很配」「的了」这类会被剔除。
    """
    return any(ch in _STOP_CHARS for ch in t)


def _passes_floor(it: Dict[str, Any], floor: float, query: str = "") -> bool:
    """一条候选能否进结果集。

    两道关：
    1. 相似度下限（原有行为）。
    2. **外部书籍语料的词面重叠要求**。为什么不能只靠分数：实测同一库里
       「hermes 的网关怎么配」最高 0.61、「今天天气不错」最高 0.67、「随便聊聊」
       0.64 —— 余弦在高维空间里挤成一团，闲聊的分数甚至更高，任何单一阈值
       都会同时误杀真问题、放过闲聊。词面重叠才是「这段是不是在讲这件事」的
       直接证据。平台自身的决策事实（scope=platform）不设此关：它们本来就
       短，且是人工确认过的。
    """
    sc = it.get("_score")
    if sc is None:
        return True
    sc = float(sc)
    if sc < floor:
        return False
    if (it.get("tags") or {}).get("scope") != "reference":
        return True
    terms = _query_terms(query)
    if not terms:
        return True
    text = ((it.get("subject") or "") + " " + (it.get("statement") or "")).lower()
    # 只看**区分度高的词元**：像「今天」「天气」「不错」这种任意两句寒暄都会
    # 出现的词，命中再多也说明不了领域相关。实测「今天天气不错」的二元组
    # {今天,天天,天气,气不,不错} 里有 3 个能命中讲天气预报的教材段落，
    # 光靠比例判据挡不住。所以先剔除高频泛用词，再要求命中比例。
    terms = {t for t in terms if t not in _GENERIC_TERMS and not _is_noise_term(t)}
    if len(terms) < _REFERENCE_MIN_TERMS:
        # 去掉泛用词后剩不下什么，说明这句问话本身没有技术信息量
        # （「今天天气不错」只剩 {天气, 气不}）。此时只认很高的相似度。
        return sc >= max(floor, _REFERENCE_SCOPE_FLOOR)
    # ASCII identifiers are whole tokens: Pi is not MPI, TCP is not TCPIP.
    # Chinese bigrams still use substring overlap because they have no spaces.
    hit = sum(1 for t in terms if (
        bool(re.search(r'(?<![a-z0-9_])' + re.escape(t) + r'(?![a-z0-9_])', text))
        if re.fullmatch(r'[a-z0-9_]+', t) else t in text))
    if hit / len(terms) >= _REFERENCE_TERM_RATIO:
        return True
    return sc >= max(floor, _REFERENCE_SCOPE_FLOOR)


def _recall_floor(kinds: Optional[Sequence[str]]) -> str:
    """按请求的类别取门限。未限定类别时取最宽松的，避免通用事实被技术门限误杀。"""
    if not kinds:
        return "0.52"
    vals = [_RECALL_FLOOR_BY_KIND.get(str(k), 0.60) for k in kinds]
    return str(min(vals))


def _append_strict_identity(where: List[str], params: List[Any], *,
                            agent_id: Optional[str], profile: Optional[str]) -> None:
    if agent_id is not None:
        where.append("f.agent_id = ?")
        params.append(str(agent_id))
    if profile is not None:
        where.append(
            "EXISTS (SELECT 1 FROM fact_tags pr WHERE pr.fact_id=f.fact_id"
            " AND pr.key='profile' AND pr.value=?)")
        params.append(str(profile))


def find_active_fact_strict(*, statement: str, user_id: str, agent_id: str,
                            profile: str, ebm: Optional[str] = None,
                            exclude_fact_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Find a semantic duplicate only inside one user/agent/profile boundary."""
    stmt = (statement or "").strip()
    if not stmt or not all(isinstance(v, str) and v for v in (user_id, agent_id, profile)):
        raise ValueError("strict_fact_identity_required")
    bucket = normalize_ebm(ebm)
    conn = _connect()
    try:
        where = ["f.user_id=?", "f.agent_id=?", "f.workspace_id=?",
                 "f.statement=?", "f.status='active'"]
        params: List[Any] = [user_id, agent_id, bucket, stmt]
        _append_strict_identity(where, params, agent_id=None, profile=profile)
        if exclude_fact_id:
            where.append("f.fact_id<>?")
            params.append(exclude_fact_id)
        row = conn.execute(
            "SELECT f.* FROM working_fact f WHERE " + " AND ".join(where)
            + " ORDER BY f.created_at DESC LIMIT 1", params).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


AUTONOMY_CONFLICT_SCAN_BUDGET = 256
AUTONOMY_CONFLICT_JUDGE_BUDGET = 16
AUTONOMY_CONFLICT_TIME_BUDGET_SECONDS = 10.0


def autonomy_conflict_budget_status(*, user_id: str, agent_id: str,
                                     profile: str,
                                     ebm: Optional[str] = None) -> Dict[str, Any]:
    """Check the all-active identity window before writing a candidate."""
    bucket = normalize_ebm(ebm)
    conn = _connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM working_fact f WHERE f.user_id=? AND f.agent_id=?"
            " AND f.status='active'"
            " AND EXISTS (SELECT 1 FROM fact_tags pr WHERE pr.fact_id=f.fact_id"
            " AND pr.key='profile' AND pr.value=?)",
            [user_id, agent_id, profile]).fetchone()[0]
        if count > AUTONOMY_CONFLICT_SCAN_BUDGET:
            return {"ok": False, "reason": "conflict_budget_exceeded",
                    "active_count": int(count)}
        return {"ok": True, "active_count": int(count)}
    finally:
        conn.close()


def _autonomy_conflict_candidates(conn: sqlite3.Connection, *, statement: str,
                                  bucket: str, user_id: str, agent_id: str,
                                  profile: str) -> Dict[str, Any]:
    """Collect a bounded, identity-scoped candidate window conservatively.

    New and old preferences can share a topic word while having very low
    character Jaccard. We therefore scan a bounded all-active window (not only
    the latest eight) and pass only records with a meaningful token overlap to
    the existing conflict judge. An exhausted scan budget is unresolved, never
    "no conflict".
    """
    buckets = [bucket] if bucket == UNKNOWN_BUCKET else [bucket, UNKNOWN_BUCKET]
    marks = ",".join("?" for _ in buckets)
    where = ["user_id=?", "agent_id=?", "status='active'",
             "workspace_id IN (" + marks + ")"]
    params: List[Any] = [user_id, agent_id, *buckets]
    profile_clause = (" AND EXISTS (SELECT 1 FROM fact_tags pr"
                      " WHERE pr.fact_id=working_fact.fact_id"
                      " AND pr.key='profile' AND pr.value=?)")
    count = conn.execute(
        "SELECT COUNT(*) FROM working_fact WHERE " + " AND ".join(where)
        + profile_clause, [*params, profile]).fetchone()[0]
    if count > AUTONOMY_CONFLICT_SCAN_BUDGET:
        return {"ok": False, "reason": "conflict_budget_exceeded",
                "candidates": [], "active_count": int(count)}
    rows = conn.execute(
        "SELECT fact_id,workspace_id,subject,statement FROM working_fact"
        " WHERE " + " AND ".join(where) + profile_clause
        + " ORDER BY created_at DESC", [*params, profile]).fetchall()
    new_terms = {t for t in _query_terms(statement) if not _is_noise_term(t)}
    candidates = []
    for row in rows:
        old_terms = {t for t in _query_terms(row["statement"])
                     if not _is_noise_term(t)}
        if new_terms and old_terms and new_terms & old_terms:
            candidates.append(dict(row))
    return {"ok": True, "candidates": candidates, "active_count": int(count)}


def activate_candidate_fact(*, fact_id: str, user_id: str, agent_id: str,
                            profile: str, statement: str, subject: str,
                            source: str, ebm: Optional[str] = None,
                            verify_recall: bool = True) -> Dict[str, Any]:
    """Atomically activate a durable candidate after index/conflict checks.

    The candidate row is invisible to recall until this function commits it as
    active. Conflict review happens before taking the write lock; the final
    transaction rechecks the candidate, duplicate owner, and supersede targets.
    On any unresolved verdict the candidate remains isolated.
    """
    stmt = (statement or "").strip()
    if not stmt or not all(isinstance(v, str) and v for v in
                           (fact_id, user_id, agent_id, profile, source)):
        return {"ok": False, "written": False, "reason": "strict_fact_identity_required"}
    bucket = normalize_ebm(ebm)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT fact_id,user_id,agent_id,workspace_id,subject,statement,source,status"
            " FROM working_fact WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            return {"ok": False, "written": False, "reason": "candidate_missing"}
        if (row["user_id"] != user_id or row["agent_id"] != agent_id
                or row["workspace_id"] != bucket or row["subject"] != subject
                or row["statement"] != stmt or row["source"] != source):
            return {"ok": False, "written": False, "reason": "candidate_identity_conflict"}
        profile_tag = conn.execute(
            "SELECT value FROM fact_tags WHERE fact_id=? AND key='profile'",
            (fact_id,)).fetchone()
        if profile_tag is None or profile_tag["value"] != profile:
            return {"ok": False, "written": False, "reason": "profile_provenance_missing"}
        if row["status"] == "active":
            return {"ok": True, "written": False, "reason": "duplicate",
                    "fact_id": fact_id, "workspace_id": bucket, "status": "active"}
        if row["status"] != "candidate":
            return {"ok": False, "written": False, "reason": "candidate_not_isolated"}

        duplicate = find_active_fact_strict(
            statement=stmt, user_id=user_id, agent_id=agent_id, profile=profile,
            ebm=ebm, exclude_fact_id=fact_id)
        if duplicate is not None:
            return {"ok": True, "written": False, "reason": "duplicate",
                    "fact_id": duplicate["fact_id"],
                    "fact_subject": duplicate.get("subject") or "",
                    "fact_statement": duplicate.get("statement") or "",
                    "workspace_id": bucket, "status": "active"}
        # If a previous attempt indexed this candidate and committed activation
        # just before the caller crashed, the same deterministic fact_id is
        # already active. Treat that exact owned row as a verified replay, not
        # as a new write.
        own_active = conn.execute(
            "SELECT f.fact_id FROM working_fact f WHERE f.fact_id=? AND f.status='active'"
            " AND f.user_id=? AND f.agent_id=? AND f.workspace_id=?"
            " AND f.subject=? AND f.statement=? AND f.source=?"
            " AND EXISTS (SELECT 1 FROM fact_tags pr WHERE pr.fact_id=f.fact_id"
            " AND pr.key='profile' AND pr.value=?)",
            (fact_id, user_id, agent_id, bucket, subject, stmt, source, profile)).fetchone()
        if own_active is not None:
            return {"ok": True, "written": False, "reason": "duplicate",
                    "fact_id": fact_id, "workspace_id": bucket, "status": "active"}

        from . import memory_conflict
        snapshot_buckets = ([bucket] if bucket == UNKNOWN_BUCKET
                            else [bucket, UNKNOWN_BUCKET])
        snapshot_marks = ",".join("?" for _ in snapshot_buckets)
        snapshot_sql = (
            "SELECT fact_id FROM working_fact f WHERE f.user_id=? AND f.agent_id=?"
            " AND f.status='active' AND f.workspace_id IN (" + snapshot_marks + ")"
            " AND EXISTS (SELECT 1 FROM fact_tags pr WHERE pr.fact_id=f.fact_id"
            " AND pr.key='profile' AND pr.value=?) ORDER BY fact_id")
        snapshot_params = [user_id, agent_id, *snapshot_buckets, profile]
        snapshot = tuple(r[0] for r in conn.execute(
            snapshot_sql, snapshot_params).fetchall())
        candidate_window = _autonomy_conflict_candidates(
            conn, statement=stmt, bucket=bucket, user_id=user_id,
            agent_id=agent_id, profile=profile)
        if candidate_window.get("ok") is not True:
            return {"ok": False, "written": False, "retryable": True,
                    "reason": candidate_window.get("reason") or "conflict_unresolved",
                    "workspace_id": bucket, "status": "candidate",
                    "fact_id": fact_id}
        candidates = candidate_window.get("candidates") or []
        verdict = memory_conflict.resolve(
            {"statement": stmt, "subject": subject, "workspace_id": bucket},
            candidates, conservative=True,
            max_candidates=AUTONOMY_CONFLICT_JUDGE_BUDGET,
            time_budget_seconds=AUTONOMY_CONFLICT_TIME_BUDGET_SECONDS)
        relation = verdict.get("relation")
        if verdict.get("action") != "write":
            if relation == "duplicate":
                duplicate_id = str(verdict.get("duplicate_of") or "")
                old = next((row for row in candidates
                            if row.get("fact_id") == duplicate_id), None)
                if old is not None:
                    return {"ok": True, "written": False, "reason": "duplicate",
                            "fact_id": duplicate_id,
                            "fact_subject": old.get("subject") or "",
                            "fact_statement": old.get("statement") or "",
                            "workspace_id": bucket, "status": "active"}
            reason = str(verdict.get("reason") or "conflict_unresolved")
            return {"ok": False, "written": False, "retryable": True,
                    "reason": reason, "conflict": verdict, "workspace_id": bucket,
                    "status": "candidate", "fact_id": fact_id}

        superseded: List[str] = []
        conn.execute("BEGIN IMMEDIATE")
        current_snapshot = tuple(r[0] for r in conn.execute(
            snapshot_sql, snapshot_params).fetchall())
        if current_snapshot != snapshot:
            conn.rollback()
            return {"ok": False, "written": False, "retryable": True,
                    "reason": "conflict_snapshot_changed", "workspace_id": bucket,
                    "status": "candidate", "fact_id": fact_id}
        current = conn.execute(
            "SELECT status,user_id,agent_id,workspace_id,subject,statement,source"
            " FROM working_fact WHERE fact_id=?", (fact_id,)).fetchone()
        if (current is None or current["status"] != "candidate" or current["user_id"] != user_id
                or current["agent_id"] != agent_id or current["workspace_id"] != bucket
                or current["subject"] != subject or current["statement"] != stmt
                or current["source"] != source):
            conn.rollback()
            return {"ok": False, "written": False, "reason": "candidate_changed"}
        # Recheck the duplicate under the write lock; this is the linearization
        # point that prevents two replay workers from activating twin facts.
        dup = conn.execute(
            "SELECT f.fact_id FROM working_fact f WHERE f.user_id=? AND f.agent_id=?"
            " AND f.workspace_id=? AND f.statement=? AND f.status='active'"
            " AND f.fact_id<>? AND EXISTS (SELECT 1 FROM fact_tags pr"
            " WHERE pr.fact_id=f.fact_id AND pr.key='profile' AND pr.value=?) LIMIT 1",
            (user_id, agent_id, bucket, stmt, fact_id, profile)).fetchone()
        if dup is not None:
            conn.rollback()
            return {"ok": True, "written": False, "reason": "duplicate",
                    "fact_id": dup["fact_id"], "workspace_id": bucket,
                    "status": "active"}
        conn.execute(
            "UPDATE working_fact SET status='active' WHERE fact_id=? AND status='candidate'",
            (fact_id,))
        for old_id in verdict.get("superseded") or []:
            cur = conn.execute(
                "UPDATE working_fact SET status='superseded',fact_chain_summary=?"
                " WHERE fact_id=? AND status='active' AND user_id=? AND agent_id=?"
                " AND EXISTS (SELECT 1 FROM fact_tags pr WHERE pr.fact_id=working_fact.fact_id"
                " AND pr.key='profile' AND pr.value=?)",
                ("superseded_by:%s %s" % (fact_id, verdict.get("reason") or ""),
                 old_id, user_id, agent_id, profile))
            if cur.rowcount:
                superseded.append(old_id)
        if verify_recall:
            # Verify through the real recall path while the activation and any
            # supersede updates are still uncommitted. A failure rolls the
            # whole transaction back: the candidate stays isolated and the old
            # fact remains active.
            try:
                recalled = recall_facts(
                    subject or stmt[:120], user_id=user_id, agent_id=agent_id,
                    profile=profile, ebm=ebm or None, limit=20, _connection=conn)
            except Exception:
                recalled = {"ok": False}
            hit_ids = [str(row.get("fact_id") or "")
                       for row in (recalled.get("results") or [])
                       if isinstance(row, dict)]
            if recalled.get("ok") is not True or fact_id not in hit_ids:
                conn.rollback()
                return {"ok": False, "written": False, "retryable": True,
                        "reason": "recall_verification_failed",
                        "workspace_id": bucket, "status": "candidate",
                        "fact_id": fact_id}
        conn.commit()
        out = {"ok": True, "written": True, "fact_id": fact_id,
               "workspace_id": bucket, "conflict": verdict,
               "superseded": superseded, "status": "active"}
        try:
            from . import monthly_log as _monthly_log
            _monthly_log.maybe_append_from_write(
                out, statement=stmt, subject=subject, tags=_tags_of(conn, fact_id))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(logger_name("store")).debug(
                "monthly_log hook skipped: %s", exc)
        return out
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _conflict_candidates(conn: sqlite3.Connection, statement: str,
                         bucket: str, user_id: str, limit: int = 8) -> List[Dict[str, Any]]:
    """挑出可能要和新事实比对的旧事实。

    候选必须包含**同代际桶 + _unknown 桶**：一个 G2 的新结论可能有资格取代
    之前记录在 _unknown 里的通用经验，但不能取代 G1 的（那条由 conflict 规则层
    直接判 coexist，根本不该成为候选）。代际不同的候选在规则层被挡掉，这里
    仍然捞进来是为了让判定结果留痕。
    """
    buckets = [bucket] if bucket == UNKNOWN_BUCKET else [bucket, UNKNOWN_BUCKET]
    marks = ",".join("?" * len(buckets))
    rows = conn.execute(
        "SELECT fact_id, workspace_id, subject, statement FROM working_fact"
        " WHERE user_id=? AND status='active' AND workspace_id IN (" + marks + ")"
        " ORDER BY created_at DESC LIMIT ?",
        [user_id, *buckets, limit]).fetchall()
    return [dict(r) for r in rows]


def _kw_alias_hits(conn: sqlite3.Connection, fact_ids: List[str], query: str) -> Dict[str, int]:
    """统计每条事实命中了多少个 keyword 标签（key 形如 kw:<触发词>）。

    为什么需要它：embedding 模型对中文短问句区分力有限，实测「为什么不能把空结果
    说成没有」会被「能力需端到端验证」抢走。标签是人给的确定性信号，用它做精确
    命中比调权重可靠。
    """
    if not fact_ids or not (query or "").strip():
        return {}
    q = (query or "").lower()
    hits: Dict[str, int] = {}
    marks = ",".join("?" * len(fact_ids))
    try:
        rows = conn.execute(
            "SELECT fact_id, key FROM fact_tags WHERE fact_id IN (" + marks + ") AND key LIKE 'kw:%'",
            fact_ids).fetchall()
    except sqlite3.Error:
        return {}
    for r in rows:
        word = r["key"][3:].lower()
        if word and word in q:
            hits[r["fact_id"]] = hits.get(r["fact_id"], 0) + 1
    return hits



def recall_facts(
    query: str = "",
    *,
    ebm: Optional[str] = None,
    tags: Optional[Dict[str, Any]] = None,
    include_unknown: bool = True,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    shared_legacy: bool = False,
    limit: int = 5,
    kinds: Optional[Sequence[str]] = None,
    scopes: Optional[Sequence[str]] = None,
    domains: Optional[Sequence[str]] = None,
    genres: Optional[Sequence[str]] = None,
    _connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """按代际 + 任意 KV 标签召回。空结果给出 reason，不返回假的成功。

    kinds 限定记忆类别（technical/personal/context）：技术诊断传 ["technical"]，
    避免「李龙飞自称菜虚飞」这类人物事实在排查门限时抢位；闲聊问答传 None 取全部。
    """
    # 入口统一解析身份。签名默认值是 None（哨兵），这里解析成当前配置的默认值。
    # 必须只解析一次、且覆盖函数内所有下游调用（粗筛/计数/打分）：
    # 只在 WHERE 子句里兜底、却把原始 None 传给下游，就会「库里明明有数据，
    # COUNT 出来 0，报 no_match」—— 看起来像没数据，其实是在按 NULL 查。
    user_id = user_id or get_config().default_user
    conn = _connection if _connection is not None else _connect()
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    try:
        buckets: List[str] = []
        if ebm:
            b = normalize_ebm(ebm)
            buckets.append(b)
            if include_unknown and b != UNKNOWN_BUCKET:
                buckets.append(UNKNOWN_BUCKET)
        where: List[str] = ["f.user_id = ?", "f.status = 'active'"]
        params: List[Any] = [user_id]
        if shared_legacy:
            # Trusted team CLI path: legacy manually shared facts remain team
            # visible, while automatic completed-turn facts stay private to the
            # exact agent/profile that produced them.
            where.append(
                "(NOT EXISTS (SELECT 1 FROM fact_tags trust"
                " WHERE trust.fact_id=f.fact_id AND trust.key='source_trust'"
                " AND trust.value='completed_user_turn')"
                " OR (f.agent_id=? AND EXISTS (SELECT 1 FROM fact_tags own"
                " WHERE own.fact_id=f.fact_id AND own.key='profile'"
                " AND own.value=?)))")
            params.extend([str(agent_id), str(profile)])
        else:
            _append_strict_identity(where, params, agent_id=agent_id, profile=profile)
        if buckets:
            where.append("f.workspace_id IN (%s)" % ",".join("?" * len(buckets)))
            params.extend(buckets)
        want = _clean_tags(tags)
        for i, (k, v) in enumerate(sorted(want.items())):
            alias = "t%d" % i
            # 值支持前缀匹配：value 以 / 结尾表示前缀（如 principle/ 匹配
            # principle/epistemics）。层级维度因此可以存成 principle/epistemics，
            # 既能按大类整体召回，也能精确到子类，不必为每层键各开一个 tag。
            if str(v).endswith("/"):
                where.append("EXISTS (SELECT 1 FROM fact_tags %s"
                             " WHERE %s.fact_id = f.fact_id AND %s.key = ? AND %s.value LIKE ? ESCAPE '!')"
                             % (alias, alias, alias, alias))
                escaped = str(v).replace("!", "!!").replace("%", "!%").replace("_", "!_")
                params.extend([k, escaped + "%"])
            else:
                where.append("EXISTS (SELECT 1 FROM fact_tags %s"
                             " WHERE %s.fact_id = f.fact_id AND %s.key = ? AND %s.value = ?)"
                             % (alias, alias, alias, alias))
                params.extend([k, v])

        # 领域 / 体裁过滤（三维分桶的后两维）。
        # 用途：查平台门限时只要 automotive，查 Linux 通识时只要 system，互不干扰。
        try:
            from .memory_buckets import filter_clause as _bclause
        except ImportError:
            from .memory_buckets import filter_clause as _bclause
        for _dim, _vals in (("domain", domains), ("genre", genres)):
            if _vals:
                _bsql, _bparams = _bclause(_dim, _vals)
                if _bsql:
                    where.append(_bsql)
                    params.extend(_bparams)

        # 作用域过滤：技术查询默认只要 platform，避免被书籍/文档淹没。
        # 不传 scopes 时**不过滤**，保持既有调用点语义不变。
        if scopes:
            try:
                from .memory_scope import filter_clause as _scope_clause
            except ImportError:
                from .memory_scope import filter_clause as _scope_clause
            _sql, _params = _scope_clause(scopes)
            if _sql:
                where.append(_sql)
                params.extend(_params)

        want_kinds = [str(k) for k in (kinds or []) if str(k).strip()]
        if want_kinds:
            # 类别存在 tags 的 kind 键上；没有任何 kind 标签的旧事实视为技术类
            # （加这个字段之前入库的都是测试经验）。
            marks = ",".join("?" * len(want_kinds))
            where.append(
                "(EXISTS (SELECT 1 FROM fact_tags k WHERE k.fact_id = f.fact_id"
                " AND k.key = 'kind' AND k.value IN (" + marks + "))"
                " OR NOT EXISTS (SELECT 1 FROM fact_tags k2 WHERE k2.fact_id = f.fact_id"
                " AND k2.key = 'kind'))")
            params.extend(want_kinds)

        # An explicit named topic is an address lookup, not a similarity guess.
        # Honor every identity/scope filter before returning its active entries.
        subject_key = re.sub(r'[^a-z0-9_\u4e00-\u9fff]', '', (query or '').lower())
        if subject_key:
            exact = conn.execute(
                'SELECT DISTINCT f.* FROM fact_tags sk INDEXED BY idx_fact_tags_kv '
                'CROSS JOIN working_fact f ON f.fact_id=sk.fact_id '
                "WHERE sk.key IN ('subject_key','subject_alias_0','subject_alias_1','subject_alias_2',"
                "'subject_alias_3','subject_alias_4','subject_alias_5','subject_alias_6','subject_alias_7')"
                " AND sk.value=? AND " + ' AND '.join(where) +
                ' ORDER BY f.created_at DESC LIMIT ?',
                [subject_key, *params, max(1, int(limit))]).fetchall()
            lookup_mode = 'exact_subject'
            if not exact:
                # Reviewed CJK topic names remain addresses inside a sentence.
                # Read the small alias index, never the whole fact corpus.
                exact = conn.execute(
                    'SELECT DISTINCT f.* FROM fact_tags sk INDEXED BY idx_fact_tags_kv '
                    'CROSS JOIN working_fact f ON f.fact_id=sk.fact_id '
                    "WHERE sk.key IN ('subject_alias_0','subject_alias_1','subject_alias_2',"
                    "'subject_alias_3','subject_alias_4','subject_alias_5','subject_alias_6','subject_alias_7')"
                    " AND length(sk.value)>=4 AND sk.value GLOB '*[一-鿿]*' AND instr(?,sk.value)>0 AND "
                    + ' AND '.join(where) + ' ORDER BY f.created_at DESC LIMIT ?',
                    [subject_key, *params, max(1, int(limit))]).fetchall()
                lookup_mode = 'curated_alias'
            if not exact:
                identifiers = list(dict.fromkeys(re.findall(r'[a-z0-9_]{2,}', (query or '').lower())))
                addressed = {}
                for token in [t for t in identifiers if t not in _GENERIC_TERMS][:8]:
                    candidates = conn.execute(
                        'SELECT f.* FROM fact_tags ct INDEXED BY idx_fact_tags_kv '
                        'CROSS JOIN working_fact f ON f.fact_id=ct.fact_id '
                        "WHERE ct.key='curated_document' AND "+' AND '.join(where)+
                        " AND f.source GLOB 'curated_profile:*' AND lower(f.subject) LIKE ? ESCAPE '!' LIMIT 128",
                        [*params, '%'+token.replace('_', '!_')+'%']).fetchall()
                    matches = [r for r in candidates if re.search(
                        r'(?<![a-z0-9_])'+re.escape(token)+r'(?![a-z0-9_])', r['subject'].lower())]
                    if len(matches) == 1:
                        addressed[matches[0]['fact_id']] = matches[0]
                if addressed:
                    exact = list(addressed.values())[:max(1, int(limit))]
                    lookup_mode = 'curated_identifier'
            if not exact:
                phrases = [term.lower() for term in re.findall(r'[a-zA-Z0-9_\u4e00-\u9fff]{4,}', query or '')
                           if term.lower() not in _GENERIC_TERMS
                           and not (re.fullmatch(r'[a-zA-Z0-9_]+', term) and len(term) < 5)][:8]
                if phrases:
                    clauses = ' OR '.join("lower(f.statement) LIKE ? ESCAPE '!'" for _ in phrases)
                    patterns = ['%'+term.replace('!', '!!').replace('%', '!%').replace('_', '!_')+'%'
                                for term in phrases]
                    candidates = conn.execute(
                        'SELECT f.* FROM fact_tags ct INDEXED BY idx_fact_tags_kv '
                        'CROSS JOIN working_fact f ON f.fact_id=ct.fact_id '
                        "WHERE ct.key='curated_document' AND "+' AND '.join(where)+
                        " AND f.source GLOB 'curated_profile:*' AND ("+clauses+')'
                        ' ORDER BY f.created_at DESC LIMIT 2', [*params, *patterns]).fetchall()
                    # A generic term appearing in several curated sections is not
                    # an address. Let semantic ranking resolve that ambiguity.
                    if len(candidates) == 1:
                        exact = candidates
                        lookup_mode = 'curated_phrase'
            if exact:
                entries = []
                for row in exact:
                    item = dict(row)
                    item['tags'] = _tags_of(conn, item['fact_id'])
                    item['rank_mode'] = lookup_mode
                    item['apply_match'] = ('legacy_unknown' if item['workspace_id'] == UNKNOWN_BUCKET
                                           else 'exact_ebm' if buckets and item['workspace_id'] == buckets[0]
                                           else 'unscoped_ebm')
                    entries.append(item)
                return {'ok': True, 'results': entries, 'total': len(entries),
                        'searched_buckets': buckets, 'searched_tags': want}

        # 两阶段检索的第一阶段：先粗筛再全取。
        # 记忆库从 46 条涨到几万条后，把全表拉进 Python 再逐条比对会直接拖垮召回。
        # 有 FTS 时用 BM25 把候选收窄到 COARSE_CANDIDATES（毫秒级）；
        # 没有 FTS 或查询无可用词时，退化为「按时间取最近 N 条」，与原行为一致。
        # 第二阶段（向量精排）仍在 _rank 里做，用的是 fact_embeddings 持久化向量。
        rid_list = None
        if (query or "").strip():
            # 粗筛优先走向量 KNN（语义召回），LIKE 只在 KNN 不可用时兜底。
            # 为什么必须这样：中文被 unicode61 当整段，LIKE 处理的是**字面子串**，
            # 「李龙飞是谁」永远匹配不上「李龙飞自称菜虚虚飞...」，而语义检索可以。
            # 这也是做规模化最关键的一步：数据越多，字面匹配越不可靠。
            rid_list = _knn_rowids(conn, query, buckets=buckets or None,
                                   kinds=want_kinds or None, scopes=scopes,
                                   user_id=user_id,
                                   agent_id=None if shared_legacy else agent_id,
                                   profile=None if shared_legacy else profile)
            if rid_list is None:
                try:
                    from .memory_scale import COARSE_CANDIDATES, coarse_filter
                except ImportError:
                    try:
                        from .memory_scale import COARSE_CANDIDATES, coarse_filter
                    except Exception as exc:  # noqa: BLE001 —— 拿不到就退回全量
                        logging.getLogger(logger_name("store")).debug("粗筛模块不可用: %s", exc)
                        COARSE_CANDIDATES = 50
                        coarse_filter = None
                if coarse_filter is not None:
                    rid_list = coarse_filter(conn, query or "", buckets=buckets or None,
                                             kinds=want_kinds or None, scopes=scopes,
                                             user_id=user_id,
                                             limit=COARSE_CANDIDATES)
        if rid_list is not None:
            if not rid_list:
                rows = []
            else:
                marks = ",".join("?" * len(rid_list))
                where.append("f.rowid IN (" + marks + ")")
                params.extend(rid_list)
                rows = conn.execute(
                    "SELECT f.* FROM working_fact f WHERE " + " AND ".join(where) +
                    " ORDER BY f.created_at DESC", params).fetchall()
        else:
            rows = conn.execute(
                "SELECT f.* FROM working_fact f WHERE " + " AND ".join(where) +
                " ORDER BY f.created_at DESC", params).fetchall()
        if not rows:
            total_any = conn.execute(
                "SELECT COUNT(*) c FROM working_fact WHERE user_id=? AND status='active'",
                (user_id,)).fetchone()[0]
            if total_any == 0:
                reason = "no_match"
            elif want:
                reason = "no_match_for_tags"
            elif rid_list is not None and not rid_list:
                # 粗筛全滤掉 = 关键词一个都没命中。这比「代际桶里没有」更准确：
                # 桶是存在的，只是没有任何一条提到这些词。报错原因要指向真正的那一层。
                reason = "no_keyword_match"
            else:
                reason = "no_match_for_ebm"
            return {"ok": True, "results": [], "searched_buckets": buckets,
                    "searched_tags": want, "empty_reason": reason}

        picked: List[Dict[str, Any]] = []
        fact_ids = [r[0] if isinstance(r, tuple) else r['fact_id'] for r in rows]
        tags_by_id = _tags_of_many(conn, fact_ids)
        for r in rows:
            if isinstance(r, tuple):
                cols = [col[1] for col in conn.execute("PRAGMA table_info(working_fact)").fetchall()]
                d = dict(zip(cols, r)) if len(cols) == len(r) else {"fact_id": r[0]}
            else:
                d = dict(r)
            d["tags"] = tags_by_id.get(d['fact_id'], {})
            d["apply_match"] = ("legacy_unknown" if (d.get("workspace_id") or UNKNOWN_BUCKET) == UNKNOWN_BUCKET
                                else "exact_ebm" if buckets and d.get("workspace_id") == buckets[0]
                                else "unscoped_ebm")
            picked.append(d)
        # 只有查询指定了代际时才按代际优先；否则 exact_ebm 是本次筛选的产物，
        # 拿它当排序依据会把 G1/G2 那两条无关经验强行顶到所有查询的第一位
        # （实测：任何查询都返回「门限-制动踏板」）。
        ranked = (_rank(picked, query, prefer_ebm=bool(ebm), connection=conn)
                  if _connection is not None else _rank(picked, query, prefer_ebm=bool(ebm)))
        # 相关性下限：向量召回永远能凑出 top-N，但「总能返回」不等于「相关」。
        # 实测「完全无关的词汇xyz123」在 45 条库里也照样返回 3 条；这类噪声塞进
        # 诊断结果反而制造干扰。低于门限的一律不返回，由 empty_reason 说明。
        # 门限按实测分布定：bge-m3 对中文的基线相似度偏高，无关文本也有 0.40~0.53
        # （不像英文那样趋近 0）。实测 45 条库上 正例 0.68~0.73 / 负例 0.40~0.53，
        # 0.60 落在两簇之间，既能滤掉无关又不误伤。
        floor = float(os.environ.get("MEMORY_RECALL_MIN_SCORE", "") or _recall_floor(want_kinds))
        # 没有分数（向量不可用、退回关键词排序）时不套用门限：那种情况本来就没有
        # 可比的相似度，硬套会把关键词命中的结果也挡掉。
        results = [it for it in ranked if _passes_floor(it, floor, query)][: max(1, int(limit))]
        payload = {"ok": True, "results": results, "total": len(results),
                   "searched_buckets": buckets, "searched_tags": want}
        if not results and not ranked:
            payload["empty_reason"] = "no_keyword_match"
        elif not results and ranked:
            # 有候选但没有一条达到相关下限 —— 不是库里没有，而是都不够像。
            payload["empty_reason"] = "below_score_floor"
            payload["best_score"] = float(ranked[0].get("_score") or 0.0)
            payload["score_floor"] = floor
        return payload
    finally:
        if _connection is None:
            conn.close()



# ---------------------------------------------------------------------------
# 候选集内向量排序
# ---------------------------------------------------------------------------
#
# 纯关键词排序对口语化提问很弱（「踏板踩下去为什么退出」命不中「门限」），
# 但直接上向量检索会把 G1 的 3.0% 和 G2 的 5.0% 一起捞回来 —— 这正是当初
# 出问题的地方。折中：**先用 SQL 按代际桶 + 标签硬收窄，再只在这个候选集
# 内部用向量排序**。隔离性和关联性各拿一半，且候选集是确定的，不会凭空多出
# 别的代际的经验。


# ---------------------------------------------------------------------------
# 向量化后端
# ---------------------------------------------------------------------------
# 2026-10-23 实测（远端宿主发起，同一批文本）：
#   硅基流动 bge-m3（外网）  单次召回 81s
#   本机 ollama bge-m3       热后 0.077s，维度同为 1024
# 差 1000 倍，而且外网还会抖。默认走本地 ollama；连不上再回外网，
# 都不行就降级关键词排序（原有行为）。
#
# 选 bge-m3 而不是更小的 nomic-embed-text：后者是 768 维，而库里已有 46 条
# 事实按 1024 维算，换维度等于全部重算；bge-m3 的中文能力也明显更强。

DEFAULT_OLLAMA_BASE = os.environ.get("MEMORY_OLLAMA_BASE_URL",
                                    "http://127.0.0.1:11434").strip()
DEFAULT_EMBED_MODEL = os.environ.get("MEMORY_EMBEDDER_MODEL", "bge-m3").strip()

_EMBED_WARMED = False
_EMBED_LOCK = threading.Lock()


def _embed_one_batch(base: str, part: Sequence[str]) -> Optional[List[List[float]]]:
    """请求一次 /api/embed。失败返回 None，由调用方决定重试还是降级。"""
    payload = json.dumps({"model": DEFAULT_EMBED_MODEL,
                          "input": list(part)}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/embed", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 连不上/被拒都交给调用方处理
        logging.getLogger(logger_name("store")).debug("ollama 单片失败: %s", exc)
        return None
    vecs = data.get("embeddings") or []
    if len(vecs) != len(part):
        logging.getLogger(logger_name("store")).warning(
            "ollama 返回向量数不匹配: 期望 %d 实得 %d", len(part), len(vecs))
        return None
    return [list(v) for v in vecs]


def _embed_via_ollama(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """走本地 ollama 的 /api/embed（批量一次请求）。失败返回 None 交给上层兜底。"""
    base = DEFAULT_OLLAMA_BASE
    if not base:
        return None
    if not texts:
        return []
    # 必须分片：一次把上千条正文丢给 /api/embed，ollama 直接 400
    # （实测整批 938 条 → HTTP Error 400: Bad Request，而 200 条一片稳定 2.7s）。
    # 不分片的后果不是「慢」而是「整批失败」—— reindex 全量退回降级，
    # 新灌进来的数据一条都没向量，KNN 永远是旧的。
    out: List[List[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        part = list(texts[i:i + _EMBED_BATCH])
        vecs = _embed_one_batch(base, part)
        if vecs is None:
            # 单片失败不立刻放弃整批：实测这种 400 是**瞬时**的（同一批数据
            # 隔一会儿原样重发就成功），而一旦整体降级，几千条全都没有向量。
            # 退避重试两次，仍失败才降级，把「偶发」和「真坏」分开。
            for attempt in range(2):
                time.sleep(0.5 * (attempt + 1))
                vecs = _embed_one_batch(base, part)
                if vecs is not None:
                    break
        if vecs is None:
            logging.getLogger(logger_name("store")).warning(
                "ollama 向量化失败（第 %d 片，%d 条），重试后仍失败", i // _EMBED_BATCH, len(part))
            return None
        out.extend(vecs)
    return out


def warmup_embedder() -> Dict[str, Any]:
    """预热嵌入后端：把模型加载进显存，避免首次召回等 5s 模型加载。

    实测首次 4.79s 是模型加载成本，之后 0.077s。MCP 守护进程启动时调一次，
    用户就永远碰不到那 5 秒。
    """
    global _EMBED_WARMED
    t0 = time.time()
    vecs = _embed_via_ollama(["预热"])
    ok = bool(vecs)
    with _EMBED_LOCK:
        _EMBED_WARMED = ok
    return {"ok": ok, "backend": "ollama", "model": DEFAULT_EMBED_MODEL,
            "base_url": DEFAULT_OLLAMA_BASE, "elapsed": round(time.time() - t0, 3),
            "dims": len(vecs[0]) if vecs else 0}


#: 单次 /api/embed 的最大条数。实测 200 条 2.7s 稳定，再大就开始被 ollama 拒。
_EMBED_BATCH = 200

#: 嵌入结果缓存：key 是文本内容（不是 fact_id）—— 同一句话在不同事实里也只算一次
#: 值存 **packed float32 字节**而不是 Python float list：1024 维一条，list 版约 32KB，
#: packed 版 4KB —— 4000 条从 ~128MB 降到 ~16MB。取值时才解包成 list。
_EMBED_CACHE: Dict[str, bytes] = {}
_EMBED_CACHE_LOCK = threading.Lock()
_EMBED_CACHE_CONFIG = None
_EMBED_CACHE_BINDING = None
_EMBED_CACHE_MAX = 4000


# ---------------------------------------------------------------------------
# 后台向量写入：可汇合的执行器
# ---------------------------------------------------------------------------
#: 在飞的后台写线程。atexit 会等它们收尾，避免进程退出时丢索引。
_PENDING: "set[threading.Thread]" = set()
_PENDING_LOCK = threading.Lock()


def _submit_background(fn, *args) -> None:
    """起一个非 daemon 线程跑后台任务，并登记以便退出前汇合。"""
    def _runner():
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 —— 后台任务失败不能影响主流程
            logging.getLogger(logger_name("store")).debug("后台向量写入失败: %s", exc)
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(threading.current_thread())

    t = threading.Thread(target=_runner, daemon=False, name="agentcore-embed")
    with _PENDING_LOCK:
        _PENDING.add(t)
    t.start()


def flush_background(timeout: float = 30.0) -> int:
    """等待所有在飞的后台向量写入完成。返回仍未完成的线程数。

    测试和一次性脚本应当在断言（或退出）前调用它；
    进程正常退出时 atexit 也会自动调一次。
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while True:
        with _PENDING_LOCK:
            pending = list(_PENDING)
        if not pending:
            return 0
        remain = deadline - _time.monotonic()
        if remain <= 0:
            return len(pending)
        for t in pending:
            t.join(max(0.0, min(remain, 1.0)))


def _atexit_flush() -> None:
    flush_background(timeout=30.0)


atexit.register(_atexit_flush)


def _bg_embed(fid: str, subject: str, stmt: str) -> None:
    full_text = ((subject or "") + " " + stmt).strip()
    vecs = _embed_with_cache([full_text])
    if vecs and len(vecs) == 1:
        _persist_fact_embedding(fid, full_text, vecs[0])


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_EMBEDDING_UNGUARDED = object()


def _persist_fact_embedding(fact_id: str, text: str, vec: Sequence[float], model: str = DEFAULT_EMBED_MODEL,
                            *, expected_user_id: Optional[str] = None,
                            expected_embedding=_EMBEDDING_UNGUARDED,
                            expected_index=_EMBEDDING_UNGUARDED) -> None:
    """Persist a background result only while its source fact still matches."""
    if not fact_id or model != DEFAULT_EMBED_MODEL or not _valid_embedding(vec):
        logging.getLogger(logger_name("store")).warning("忽略无效或模型不匹配的记忆向量")
        return
    blob = _pack_vector(vec)
    thash = _text_hash(text)
    conn = _connect()
    try:
        # ensure_table commits internally; it must run before the write lock.
        index_available = False
        try:
            from . import vec_index
            index_available = vec_index.ensure_table(conn, dim=len(vec))
        except Exception as exc:  # noqa: BLE001 —— optional acceleration
            logging.getLogger(logger_name("store")).debug("建向量表失败: %s", exc)
        conn.execute("BEGIN IMMEDIATE")
        fact = conn.execute(
            "SELECT subject,statement,status,user_id FROM working_fact WHERE fact_id=?", (fact_id,)
        ).fetchone()
        if (not fact or fact["status"] not in ("active", "candidate")
                or (expected_user_id is not None and fact["user_id"] != expected_user_id)):
            conn.rollback()
            return
        current_text = ((fact["subject"] or "") + " " + (fact["statement"] or "")).strip()
        if current_text != text:
            conn.rollback()
            return
        if expected_embedding is not _EMBEDDING_UNGUARDED:
            previous = conn.execute(
                "SELECT model,dim,vector,text_hash,updated_at FROM fact_embeddings WHERE fact_id=?",
                (fact_id,)).fetchone()
            if (tuple(previous) if previous is not None else None) != expected_embedding:
                conn.rollback()
                return
        if expected_index is not _EMBEDDING_UNGUARDED:
            if not index_available:
                conn.rollback()
                return
            previous_index = conn.execute('SELECT embedding FROM vec_facts WHERE fact_id=?', (fact_id,)).fetchone()
            if (previous_index[0] if previous_index is not None else None) != expected_index:
                conn.rollback()
                return
        conn.execute(
            "INSERT INTO fact_embeddings(fact_id, model, dim, vector, text_hash, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(fact_id) DO UPDATE SET model=excluded.model, dim=excluded.dim,"
            " vector=excluded.vector, text_hash=excluded.text_hash, updated_at=excluded.updated_at",
            (fact_id, model, len(vec), blob, thash, time.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        if index_available:
            # Keep the existing index and embedding together if either write
            # fails: committing only the new embedding would leave KNN using
            # an older vector without any persistent indication of staleness.
            conn.execute("DELETE FROM vec_facts WHERE fact_id=?", (fact_id,))
            conn.execute("INSERT INTO vec_facts(fact_id, embedding) VALUES (?, ?)", (fact_id, blob))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        logging.getLogger(logger_name("store")).warning("持久化向量失败: %s", exc)
    finally:
        conn.close()


def ensure_fact_index(fact_id: str, *, user_id: str, agent_id: str,
                      subject: str, statement: str,
                      expected_statuses: Sequence[str] = ("active", "candidate")) -> Dict[str, Any]:
    """Synchronously persist and verify one fact's durable embedding/index.

    The normal write path deliberately warms embeddings in the background, but
    an automated queue event may only be acknowledged after its index is
    durable. This helper reuses the canonical embedding and vec tables; it does
    not create another index or database.
    """
    expected_user = str(user_id or "").strip()
    expected_agent = str(agent_id or "").strip()
    fact = str(fact_id or "").strip()
    if not fact or not expected_user or not expected_agent:
        return {"ok": False, "reason": "identity_required"}
    full_text = ((subject or "") + " " + (statement or "")).strip()
    if not full_text:
        return {"ok": False, "reason": "fact_text_required"}
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT user_id,agent_id,subject,statement,status FROM working_fact"
            " WHERE fact_id=?", (fact,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "reason": "fact_missing"}
    if (row["user_id"] != expected_user or row["agent_id"] != expected_agent
            or row["status"] not in tuple(expected_statuses)):
        return {"ok": False, "reason": "fact_identity_mismatch"}
    if ((row["subject"] or "") != (subject or "")
            or (row["statement"] or "") != (statement or "")):
        return {"ok": False, "reason": "fact_content_mismatch"}
    vectors = _embed_with_cache([full_text])
    if not vectors or len(vectors) != 1 or not _valid_embedding(vectors[0]):
        return {"ok": False, "reason": "embedding_unavailable", "retryable": True}
    try:
        from . import vec_index
        index_available = vec_index.find_extension() is not None
    except Exception:  # noqa: BLE001
        index_available = False
    _persist_fact_embedding(fact, full_text, vectors[0], expected_user_id=expected_user)
    conn = _connect()
    try:
        embedding = conn.execute(
            "SELECT model,dim,text_hash FROM fact_embeddings WHERE fact_id=?",
            (fact,)).fetchone()
        expected_hash = _text_hash(full_text)
        if (embedding is None or embedding["model"] != DEFAULT_EMBED_MODEL
                or int(embedding["dim"]) != len(vectors[0])
                or embedding["text_hash"] != expected_hash):
            return {"ok": False, "reason": "embedding_not_persisted", "retryable": True}
        indexed = False
        if index_available:
            try:
                indexed = conn.execute(
                    "SELECT 1 FROM vec_facts WHERE fact_id=?", (fact,)).fetchone() is not None
            except sqlite3.Error:
                indexed = False
            if not indexed:
                return {"ok": False, "reason": "vector_index_incomplete", "retryable": True}
        return {"ok": True, "fact_id": fact, "embedded": True,
                "indexed": indexed, "index_available": index_available}
    finally:
        conn.close()


def _get_persisted_embeddings(fact_ids: Sequence[str], model: str = DEFAULT_EMBED_MODEL,
                              *, connection=None) -> Dict[str, Tuple[List[float], str]]:
    """批量从数据库获取已持久化的向量。返回 {fact_id: (vector, text_hash)}"""
    if not fact_ids:
        return {}
    conn = connection if connection is not None else _connect()
    try:
        marks = ",".join("?" * len(fact_ids))
        rows = conn.execute(
            f"SELECT fact_id, vector, text_hash FROM fact_embeddings WHERE fact_id IN ({marks}) AND model=?",
            [*fact_ids, model],
        ).fetchall()
        out = {}
        for r in rows:
            try:
                out[r["fact_id"]] = (_unpack_vector(r["vector"]), r["text_hash"])
            except Exception as exc:  # noqa: BLE001 —— 单条向量损坏不该拖垮整批
                # 留痕：坏向量会被当成「未缓存」重算，必须看得出发生过
                logging.getLogger(logger_name("store")).warning(
                    "持久化向量解析失败，该条将重算 fact_id=%s: %s", r["fact_id"], exc)
        return out
    except Exception:
        return {}
    finally:
        if connection is None:
            conn.close()


def reindex_embeddings(user_id: Optional[str] = None, model: str = DEFAULT_EMBED_MODEL, force: bool = False) -> Dict[str, Any]:
    """批量/增量为库中已有事实构建持久化向量（自动化与规模化支撑）。
    
    默认只对尚未生成向量或文本发生变化的事实进行向量化。
    """
    # The configured embedder does not accept a per-call model argument. Using
    # another label here would store vectors from that embedder under an
    # unverified identity and can block the journal-backed read view forever.
    if model != DEFAULT_EMBED_MODEL:
        return {"ok": False, "error": "embedding_model_mismatch"}
    user_id = user_id or get_config().default_user
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT f.fact_id, f.subject, f.statement, e.text_hash FROM working_fact f"
            " LEFT JOIN fact_embeddings e ON f.fact_id = e.fact_id AND e.model = ?"
            " WHERE f.user_id = ? AND f.status = 'active'",
            (model, user_id),
        ).fetchall()
        
        to_embed = []
        for r in rows:
            text = ((r["subject"] or "") + " " + (r["statement"] or "")).strip()
            thash = _text_hash(text)
            if force or not r["text_hash"] or r["text_hash"] != thash:
                to_embed.append((r["fact_id"], text, r["subject"], r["statement"]))
                
        if not to_embed:
            return {"ok": True, "total": len(rows), "updated": 0, "message": "all up-to-date"}
            
        # 批量向量化
        texts = [t[1] for t in to_embed]
        vecs = _embed_with_cache(texts)
        if not vecs or len(vecs) != len(texts):
            return {"ok": False, "error": "向量化后端失败", "attempted": len(texts)}
            
        updated = 0
        skipped_changed = 0
        accepted = []
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        # Generation can take minutes. Hold a write transaction only during the
        # final check and write, so a corrected or retired fact cannot receive a
        # vector for its former contents.
        conn.execute('BEGIN IMMEDIATE')
        for (fid, text, subject, statement), vec in zip(to_embed, vecs):
            current = conn.execute(
                "SELECT 1 FROM working_fact WHERE fact_id=? AND user_id=? AND status='active' "
                "AND subject IS ? AND statement IS ?", (fid, user_id, subject, statement)).fetchone()
            if current is None:
                skipped_changed += 1
                continue
            blob = _pack_vector(vec)
            thash = _text_hash(text)
            conn.execute(
                "INSERT INTO fact_embeddings(fact_id, model, dim, vector, text_hash, updated_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(fact_id) DO UPDATE SET model=excluded.model, dim=excluded.dim,"
                " vector=excluded.vector, text_hash=excluded.text_hash, updated_at=excluded.updated_at",
                (fid, model, len(vec), blob, thash, now),
            )
            updated += 1
            accepted.append((fid, vec, subject, statement))
        conn.commit()

        # 同步灌进 vec0 索引：批量导入互联网数据后，这里是唯一让 KNN 生效的地方。
        index_result = {"available": False, "indexed": 0, "skipped_changed": 0, "failed": 0}
        try:
            from . import vec_index
            index_result = vec_index.upsert_current(
                conn, accepted, user_id=user_id, model=model,
                dim=len(accepted[0][1]) if accepted else 1024)
        except Exception as exc:  # noqa: BLE001 —— 可选加速层
            logging.getLogger(logger_name("store")).warning("向量索引写入失败: %s", exc)
            index_result["failed"] = len(accepted)

        complete = not (skipped_changed or index_result["skipped_changed"] or index_result["failed"])
        result = {"ok": complete, "total": len(rows), "updated": updated,
                  "skipped_changed": skipped_changed, "indexed": index_result["indexed"],
                  "index_available": index_result["available"],
                  "index_skipped_changed": index_result["skipped_changed"],
                  "index_failed": index_result["failed"], "model": model}
        if skipped_changed:
            result["error"] = "facts_changed_during_embedding"
        elif index_result["skipped_changed"] or index_result["failed"]:
            result["error"] = "vector_index_incomplete"
        return result
    finally:
        conn.close()



def _embedding_binding(cfg):
    return (cfg.embed_fn, tuple(cfg.embed_fallbacks or ()), cfg.embed_fallbacks_same_model,
            cfg.embed_dim, DEFAULT_EMBED_MODEL)


def _same_embedding_binding(left, right):
    return (left is not None and left[0] is right[0] and len(left[1]) == len(right[1])
            and all(a is b for a, b in zip(left[1], right[1])) and left[2:] == right[2:])


def _unpack_cached(cached: Sequence[Optional[bytes]]) -> Optional[List[List[float]]]:
    """把 packed 缓存还原成 float list；任一条损坏即返回 None（调用方按失败处理）。"""
    out: List[List[float]] = []
    for blob in cached:
        if blob is None:
            return None
        try:
            out.append(_unpack_vector(blob))
        except Exception:  # noqa: BLE001 —— 缓存损坏不该拖垮召回
            return None
    return out


def _evict_embed_cache_locked() -> None:
    """超限时**逐条逐出**最旧的条目，而不是整表清空。

    整表清空会让缓存周期性归零：紧接着的一批请求全部 miss，缓存等于没有。
    逐出只丢最旧的那几条，命中率和内存占用都稳定。调用者持有 _EMBED_CACHE_LOCK。
    """
    while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
        oldest = next(iter(_EMBED_CACHE))
        _EMBED_CACHE.pop(oldest, None)


def _embed_with_cache(texts: Sequence[str]) -> Optional[List[List[float]]]:

    """带缓存的批量向量化：只有缓存未命中的部分才真正请求后端。"""
    if not texts:
        return []
    global _EMBED_CACHE_CONFIG, _EMBED_CACHE_BINDING
    cfg = get_config()
    binding = _embedding_binding(cfg)
    with _EMBED_CACHE_LOCK:
        if _EMBED_CACHE_CONFIG is not cfg or not _same_embedding_binding(_EMBED_CACHE_BINDING, binding):
            _EMBED_CACHE.clear()
            _EMBED_CACHE_CONFIG = cfg
            _EMBED_CACHE_BINDING = binding
        cached = [_EMBED_CACHE.get(t) for t in texts]
    missing = [t for t, v in zip(texts, cached) if v is None]
    if not missing:
        return _unpack_cached(cached)  # type: ignore[arg-type]

    fresh = _embed_texts(missing)
    if fresh is None or len(fresh) != len(missing):
        return None
    if not all(_valid_embedding(vec) for vec in fresh):
        logging.getLogger(logger_name("store")).warning("嵌入后端返回了无效向量")
        return None
    with _EMBED_CACHE_LOCK:
        if (get_config() is not cfg or _EMBED_CACHE_CONFIG is not cfg
                or not _same_embedding_binding(_EMBED_CACHE_BINDING, binding)
                or not _same_embedding_binding(_embedding_binding(cfg), binding)):
            return None
        for text, vec in zip(missing, fresh):
            _EMBED_CACHE[text] = _pack_vector(vec)
        _evict_embed_cache_locked()
        cached = [_EMBED_CACHE.get(t) for t in texts]
    if any(v is None for v in cached):
        return None
    return _unpack_cached(cached)  # type: ignore[arg-type]


def _embed_texts(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """使用主嵌入后端；仅在宿主明确声明同模型空间时启用兜底。

    未配置注入函数时，内置 Ollama 使用当前配置的模型标识。
    """
    if not texts:
        return []
    cfg = get_config()
    injected = cfg.embed_fn
    if injected is not None:
        try:
            out = injected(list(texts))
            if out and len(out) == len(texts):
                return [list(v) for v in out]
            logging.getLogger(logger_name("store")).warning(
                "embed_fn 返回长度不符（%s != %s）",
                len(out or []), len(texts))
        except Exception as exc:  # noqa: BLE001 —— 不暴露输入内容
            logging.getLogger(logger_name("store")).warning("embed_fn 调用失败: %s", type(exc).__name__)
        if not cfg.embed_fallbacks_same_model:
            return None
    vecs = _embed_via_ollama(texts)
    if vecs is not None:
        return vecs
    if cfg.embed_fallbacks_same_model:
        return _embed_via_host_fallbacks(texts)
    return None


def _embed_via_host_fallbacks(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """宿主提供的兜底向量化后端（本地 ollama 不可用时使用）。

    实现由 MemoryConfig.embed_fallbacks 注入 —— 引擎**不 import 任何宿主的
    嵌入 SDK**，否则这个库就绑死在某一家的依赖上了。
    返回 None 是契约（调用方降级到关键词排序），调用方据此区分「后端不可用」
    与「无相关条目」。
    """
    for i, fn in enumerate(get_config().embed_fallbacks or ()):
        try:
            out = fn(list(texts))
        except Exception as exc:  # noqa: BLE001 —— 网络/配置/服务未起一律降级
            logging.getLogger(logger_name("store")).debug(
                "兜底嵌入后端 #%d 失败: %s", i, exc)
            continue
        if out and len(out) == len(texts):
            return [list(v) for v in out]
        logging.getLogger(logger_name("store")).warning(
            "兜底嵌入后端 #%d 返回长度不符（%s != %s），跳过",
            i, len(out or []), len(texts))
    return None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0
    except Exception:
        return 0.0


def _rank(items: Sequence[Dict[str, Any]], query: str, prefer_ebm: bool = True,
          *, connection=None) -> List[Dict[str, Any]]:
    """在给定候选集内排序：有可用向量就按余弦，否则退回关键词重合度。

    注意排序**只作用于传入的 items**：调用方已经按代际桶和标签过滤过了，
    这里不会引入候选集之外的记忆。
    """
    q = set(w.lower() for w in (query or "").replace("/", " ").split() if len(w) > 1)

    def kw_score(it):
        text = (it.get("subject") or "") + " " + (it.get("statement") or "")
        low = text.lower()
        # subject 命中算 2 分：它是人为给的标签，命中其含义远强于正文里偶然出现。
        subj = (it.get("subject") or "").lower()
        hit = sum(2 if w in subj else (1 if w in low else 0) for w in q)
        return (hit, it.get("created_at") or "")

    qtext = (query or "").strip()
    if len(qtext) >= 2 and items:
        # 直接嵌原文（subject + statement），不做任何加权或截断：
        cand = [((it.get("subject") or "") + " " + (it.get("statement") or "")).strip()
                for it in items]
        fact_ids = [it.get("fact_id") or "" for it in items]
        
        # 1. 尝试从 SQLite 持久化表获取已存在的向量
        persisted = (_get_persisted_embeddings(fact_ids, connection=connection)
                     if connection is not None else _get_persisted_embeddings(fact_ids))
        
        # 2. 检查哪些候选文本需要补算向量
        missing_texts = []
        missing_indices = []
        cvs: List[Optional[List[float]]] = [None] * len(items)
        
        for idx, (fid, ctext) in enumerate(zip(fact_ids, cand)):
            if fid and fid in persisted:
                vec, thash = persisted[fid]
                if thash == _text_hash(ctext):
                    cvs[idx] = vec
            if cvs[idx] is None:
                missing_indices.append(idx)
                missing_texts.append(ctext)
                
        # 3. 只有未持久化命中 / query 才请求向量模型
        req_texts = [qtext] + missing_texts
        fresh_vecs = _embed_with_cache(req_texts)
        
        if fresh_vecs and len(fresh_vecs) == len(req_texts):
            qv = fresh_vecs[0]
            fresh_cvs = fresh_vecs[1:]
            for idx, fvec in zip(missing_indices, fresh_cvs):
                cvs[idx] = fvec
                # 异步或顺手持久化补充
                fid = fact_ids[idx]
                if fid:
                    _persist_fact_embedding(fid, cand[idx], fvec)
                    
            scored = []
            for it, cv in zip(items, cvs):
                if cv is None:
                    continue
                it = dict(it)
                it["rank_mode"] = "vector"
                scored.append((_cosine(qv, cv), it))

            # 纯向量排序。此前为弥补 embo-01 对中文短句的弱区分力，叠过
            # 「subject 重复加权」「关键词混合」「kw: 触发词标签」三层补偿；
            # 换成 bge-m3 后纯余弦已能正确排序（实测目标条目 0.73 vs 次优 0.60），
            # 这些补偿反而在打乱顺序，一并撤掉。代际精确匹配仍然优先。
            # 排序 = 代际精确优先 > 相似度 × 置信度权重。
            # 没有置信度参与时，强化只是数字变化，行为完全不变 —— 那等于没学。
            weights = _confidence_weights([it.get("fact_id") for _, it in scored])
            scored.sort(
                key=lambda x: (
                    0 if (prefer_ebm and x[1].get("apply_match") == "exact_ebm") else 1,
                    -(x[0] * weights.get(x[1].get("fact_id"), 1.0)),
                ))
            for sc, it in scored:
                it["rank_mode"] = "vector"
                it["_score"] = round(sc, 4)
            return [x[1] for x in scored]
    out = []
    for it in items:
        if qtext and kw_score(it)[0] == 0:
            continue
        it = dict(it)
        it["rank_mode"] = "keyword"
        out.append(it)
    # 先按代际精确匹配分组，再在组内按关键词重合度降序。
    # 用两次排序而不是复合 key：kw_score 的第二项是时间字符串，不能取负。
    out.sort(key=kw_score, reverse=True)
    if prefer_ebm:
        out.sort(key=lambda it: 0 if it.get("apply_match") == "exact_ebm" else 1)
    return out



def drop_fact(fact_id: str, reason: str = "manual") -> Dict[str, Any]:
    """显式淘汰。status 只由这里改，抽取器无权置 superseded。"""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE working_fact SET status='deprecated', fact_chain_summary=? WHERE fact_id=?",
            (reason, fact_id))
        conn.commit()
        return {"ok": cur.rowcount > 0, "fact_id": fact_id, "reason": reason}
    finally:
        conn.close()


def supersede(old_fact_id: str, new_fact_id: str, reason: str = "") -> Dict[str, Any]:
    """被新事实取代。**不删除**：status=superseded + 记下取代者，保留时间线。

    和 drop_fact 的区别：deprecated 是「这条作废了」；superseded 是「这条被
    更新的结论顶掉了」—— 后者仍然要能回答「当时为什么这么定」，所以必须留
    指向新记录的引用。
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE working_fact SET status='superseded', fact_chain_summary=?"
            " WHERE fact_id=? AND status='active'",
            ("superseded_by:%s%s" % (new_fact_id, (" " + reason) if reason else ""),
             old_fact_id))
        conn.commit()
        return {"ok": cur.rowcount > 0, "fact_id": old_fact_id,
                "superseded_by": new_fact_id, "reason": reason}
    finally:
        conn.close()


def list_tags(user_id: Optional[str] = None) -> Dict[str, List[str]]:
    """列出所有已用标签值，供调用方发现可用的过滤维度。"""
    user_id = user_id or get_config().default_user
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT t.key AS key, t.value AS value FROM fact_tags t"
            " JOIN working_fact f ON f.fact_id = t.fact_id"
            " WHERE f.user_id = ? AND f.status = 'active'", (user_id,)).fetchall()
        agg: Dict[str, set] = {}
        for r in rows:
            agg.setdefault(r["key"], set()).add(r["value"])
        return {k: sorted(v) for k, v in sorted(agg.items())}
    finally:
        conn.close()


def migrate_tags_column(dry_run: bool = True) -> Dict[str, Any]:
    """把旧版 working_fact.tags 列（若存在）搬到 fact_tags，然后不动原列。

    只在确认该列确实存在时才读；列不存在就是干净环境，直接返回 0。
    原列保持原样（不去 ALTER DROP），避免动上游表结构。
    """
    conn = _connect()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(working_fact)")]
        if "tags" not in cols:
            return {"ok": True, "migrated": 0, "reason": "no_legacy_column"}
        rows = conn.execute(
            "SELECT fact_id, tags FROM working_fact WHERE tags IS NOT NULL AND tags != '{}'"
        ).fetchall()
        moved = 0
        for r in rows:
            try:
                data = json.loads(r["tags"] or "{}")
            except (ValueError, TypeError) as exc:
                # 坏 tags 跳过迁移是安全的，但要能查：否则"迁移后标签少了"无从解释。
                logging.getLogger(logger_name("store")).warning(
                    "标签迁移跳过畸形记录 fact_id=%s: %s", r["fact_id"], exc)
                continue
            if not isinstance(data, dict):
                continue
            if dry_run:
                moved += 1
                continue
            _put_tags(conn, r["fact_id"], _clean_tags(data))
            moved += 1
        if not dry_run:
            conn.commit()
        return {"ok": True, "migrated": moved, "dry_run": dry_run}
    finally:
        conn.close()


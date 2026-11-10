#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进化一：多维启发式惊奇度打分门禁（Heuristic Surprise Gate）与「报错/纠错 -> 修复」因果三元组捕获。

理论参考与工程边界说明：
- 理论灵感源自 Google Research 的 Titans（Learning to Memorize at Test Time, arXiv:2501.00663）惊奇度驱动记忆门控
  及其演进架构 HOPE（Nested Learning / Continuum Memory System 多频谱连续体记忆与自指更新）。
- 本模块在 Agent 工程层（SQLite + vec0，非神经网络权重梯度更新）将其落地为确定性的「多维启发式惊奇度打分门禁
  （Heuristic Surprise Gate）」，并与高频工作事实（memory_store）、中频突触可塑性（memory_plasticity / synapse）、
  低频技能蒸馏固化（memory_consolidate / skill_forge）共同组成三频段连续体记忆系统（Continuum Memory System）。

架构设计（双层语义模型驱动，摒弃死板正则墙）：
1. 向量语义新颖度（Embedding Cosine Distance）：
   调用 `memory_store._embed_with_cache` 与 `memory_store._cosine`，在 bge-m3 向量空间计算
   新候选事实与既有记忆池的真实语义余弦距离：
       S_novelty = 1.0 - max_{old} cos(v_new, v_old)
   仅在离线无嵌入后端（embed_fn 返回 None）时退回字符二元组 Jaccard 距离。
2. 语义大模型认知评估（LLM Semantic Judge via `get_config().resolve_llm()`）：
   调用宿主注入的语义模型（如 flash 模型）直接理解 `(user_message, assistant_reply)` 的语义意图，
   输出结构化 JSON：
   - 是否属于显式纠错 / 长期铁律（`is_mandate`）
   - 是否属于推翻旧认知的信念翻转（`is_belief_flip`）
   - 是否包含「报错/异常 -> 根因与修复」因果闭环（`is_causal_fix`）
   - 自动提炼标准化「四格错题卡」（`when` / `do` / `why` / `subject` / `polarity`）及 `files`/`errors` 书签。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("surprise"))

SURPRISE_FLASH_THRESHOLD = 0.65
SURPRISE_ADMIT_THRESHOLD = 0.35

SURPRISE_SYSTEM = """你是智能体海马体记忆系统的「多维启发式惊奇度与因果三元组评估器（Heuristic Surprise & Causal Extractor）」。
请对用户输入与助手回复进行深度语义理解，判断该轮交互是否包含值得写入长期记忆的高惊奇度信息（如用户当面纠错、长期铁律指令、方案/参数翻转、或排障根因闭环），并提取标准化四格错题卡。

输出严格 JSON（不要 markdown 代码块）：
{
  "is_noise_or_question": false,
  "is_mandate": true,
  "is_belief_flip": false,
  "is_causal_fix": false,
  "polarity": "prohibition|requirement",
  "subject": "简短主题词（如文件名、模块名或规则主题）",
  "statement": "提炼后的独立完整原子规则或事实陈述",
  "when": "触发场景、文件名或报错标识",
  "do": "必须执行或严禁执行的动作",
  "why": "原因、根因或证据",
  "files": ["关联文件名或路径"],
  "errors": ["关联报错码或异常名"]
}
"""


def _parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def extract_bookmarks(text: str) -> Dict[str, List[str]]:
    """提取文本中出现的文件路径标识（`files`）与报错/异常符号（`errors`）。"""
    content = str(text or "")
    files: List[str] = []
    for token in re.findall(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}", content):
        cleaned = token.strip("./")
        ext = cleaned.rsplit(".", 1)[-1].lower() if "." in cleaned else ""
        if ext in {
            "py", "sh", "yaml", "yml", "json", "toml", "md",
            "sql", "cpp", "hpp", "h", "c", "cc", "js", "ts", "txt",
        } and cleaned not in files:
            files.append(cleaned)

    errors: List[str] = []
    for token in re.findall(r"\b[A-Za-z0-9_]{4,96}\b", content):
        if token in files or any(token.endswith("." + e) for e in ("py", "sh", "json", "md", "yaml")):
            continue
        is_err_class = token[0].isupper() and any(
            token.endswith(suf) for suf in ("Error", "Exception", "Warning", "Fault", "Failure")
        )
        is_snake_code = "_" in token and token.lower() == token and token.count("_") >= 2
        is_upper_code = "_" in token and token.isupper() and len(token) >= 6
        if (is_err_class or is_snake_code or is_upper_code) and token not in errors:
            errors.append(token)
    return {"files": files[:8], "errors": errors[:8]}


def _semantic_novelty(statement: str, existing_statements: Optional[Sequence[str]]) -> float:
    """优先使用嵌入模型计算向量余弦新颖度距离 1 - max_cos；嵌入不可用时退回 bigram Jaccard。"""
    stmt = (statement or "").strip()
    if not stmt:
        return 0.0
    candidates = [str(s).strip() for s in (existing_statements or ()) if str(s or "").strip()]
    if not candidates:
        return 1.0

    try:
        from . import memory_store
        vecs = memory_store._embed_with_cache([stmt, *candidates[:32]])
        if vecs and len(vecs) == 1 + len(candidates[:32]):
            v_new = vecs[0]
            max_cos = max(
                (max(0.0, float(memory_store._cosine(v_new, v_old))) for v_old in vecs[1:]),
                default=0.0,
            )
            return round(max(0.0, min(1.0, 1.0 - max_cos)), 4)
    except Exception as exc:
        logger.debug("向量新颖度计算降级: %s", type(exc).__name__)

    # 离线兜底：使用 memory_conflict._similarity 计算字符二元组相似度
    try:
        from .memory_conflict import _similarity
        max_sim = max((_similarity(stmt, old) for old in candidates), default=0.0)
        return round(max(0.0, min(1.0, 1.0 - max_sim)), 4)
    except Exception:
        return 1.0


def _llm_semantic_analyze(user_message: str, assistant_reply: str = "") -> Optional[Dict[str, Any]]:
    """调用宿主语义模型（`get_config().resolve_llm()`）分析对话意图与因果结构。"""
    try:
        chat_fn = get_config().resolve_llm()
    except Exception:
        chat_fn = None
    if chat_fn is None:
        return None

    prompt = f"【用户输入】\n{user_message or '(空)'}\n\n【助手回复】\n{assistant_reply or '(无)'}"
    try:
        res = chat_fn(
            [
                {"role": "system", "content": SURPRISE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            timeout=15.0,
            max_tokens=350,
            temperature=0.0,
        )
        parsed = _parse_json_object((res or {}).get("content") or "")
        if parsed is not None:
            return parsed
    except Exception as exc:
        logger.debug("语义模型惊奇度分析跳过，使用本地轻量兜底: %s", type(exc).__name__)
    return None


def _offline_semantic_fallback(text: str) -> Dict[str, Any]:
    """当未配置在线/本地 `llm_fn`（如纯离线单元测试）时的语义特征兜底。"""
    raw = (text or "").strip()
    if not raw or len(raw) < 4:
        return {
            "is_noise_or_question": True,
            "is_mandate": False,
            "is_belief_flip": False,
            "is_causal_fix": False,
            "polarity": "requirement",
        }
    is_question = raw.endswith(("?", "？")) or raw.startswith(("为什么", "怎么", "如何", "是否", "能不能"))
    is_short_ack = len(raw) <= 8 and any(
        w == raw.rstrip("。！!~ ") for w in ("继续", "好的", "收到", "谢谢", "ok", "yes", "no", "跑一下")
    )
    if is_question or is_short_ack or any(w in raw for w in ("本轮", "这轮", "这次先", "暂时先")):
        return {
            "is_noise_or_question": True,
            "is_mandate": False,
            "is_belief_flip": False,
            "is_causal_fix": False,
            "polarity": "requirement",
        }

    mandate_cues = ("记住", "记一下", "记下来", "牢记", "以后一律", "今后一律", "统一规定", "统一采用", "永远不要", "绝对不要", "以后不要", "以后别", "以后必须")
    flip_cues = ("不再", "不要再", "别再", "禁止", "严禁", "不得", "改用", "改成", "换成", "切换为", "升级为", "替代", "必须", "一律")
    causal_cues = ("是因为", "根因是", "原因是", "导致", "修复为", "解决方式")
    prohibition_cues = ("不要", "别再", "禁止", "严禁", "不得", "不能", "不准", "废弃", "停用")

    is_mandate = any(c in raw for c in mandate_cues) or (
        ("不对" in raw or "错了" in raw) and any(c in raw for c in ("应该", "而是", "现在是", "必须", "统一"))
    )
    is_flip = any(c in raw for c in flip_cues)
    is_causal = any(c in raw for c in causal_cues) and any(
        e in raw.lower() for e in ("报错", "异常", "失败", "超时", "冲突", "error", "exception", "failed", "timeout")
    )
    polarity = "prohibition" if any(c in raw for c in prohibition_cues) else "requirement"
    return {
        "is_noise_or_question": False,
        "is_mandate": is_mandate,
        "is_belief_flip": is_flip,
        "is_causal_fix": is_causal,
        "polarity": polarity,
    }


def compute_surprise(
    statement: str,
    *,
    existing_statements: Optional[Sequence[str]] = None,
    user_message: str = "",
    assistant_reply: str = "",
) -> Dict[str, Any]:
    """计算一条候选事实的多维启发式惊奇度分数与组成明细（向量余弦新颖度 + 语义大模型评估）。"""
    stmt = (statement or "").strip()
    combined = (user_message + "\n" + stmt).strip() if user_message else stmt
    bookmarks = extract_bookmarks(combined + "\n" + (assistant_reply or ""))

    if not stmt:
        return {
            "score": 0.0,
            "novelty": 0.0,
            "conflict_bonus": 0.0,
            "mandate_bonus": 0.0,
            "causal_bonus": 0.0,
            "flash_eligible": False,
            "admit_eligible": False,
            "bookmarks": bookmarks,
            "by": "empty",
        }

    llm_verdict = _llm_semantic_analyze(user_message or stmt, assistant_reply)
    analysis = llm_verdict if llm_verdict is not None else _offline_semantic_fallback(combined)
    by_mode = "llm" if llm_verdict is not None else "fallback"

    if analysis.get("is_noise_or_question") is True:
        return {
            "score": 0.0,
            "novelty": 0.0,
            "conflict_bonus": 0.0,
            "mandate_bonus": 0.0,
            "causal_bonus": 0.0,
            "flash_eligible": False,
            "admit_eligible": False,
            "bookmarks": bookmarks,
            "by": by_mode,
        }

    novelty = _semantic_novelty(stmt, existing_statements)
    mandate_bonus = 0.42 if analysis.get("is_mandate") else 0.0
    conflict_bonus = 0.25 if analysis.get("is_belief_flip") else 0.0
    causal_bonus = 0.22 if analysis.get("is_causal_fix") else 0.0

    # 合并 LLM 识别出的附加文件/错误书签
    for f in analysis.get("files") or ():
        if isinstance(f, str) and f.strip() and f.strip() not in bookmarks["files"]:
            bookmarks["files"].append(f.strip())
    for e in analysis.get("errors") or ():
        if isinstance(e, str) and e.strip() and e.strip() not in bookmarks["errors"]:
            bookmarks["errors"].append(e.strip())

    bookmark_bonus = 0.08 if (bookmarks["files"] or bookmarks["errors"]) else 0.0
    raw_score = 0.35 * novelty + mandate_bonus + conflict_bonus + causal_bonus + bookmark_bonus
    if novelty < 0.05 and mandate_bonus == 0.0:
        raw_score *= 0.2
    score = round(min(1.0, max(0.0, raw_score)), 4)

    return {
        "score": score,
        "novelty": novelty,
        "conflict_bonus": conflict_bonus,
        "mandate_bonus": mandate_bonus,
        "causal_bonus": causal_bonus,
        "flash_eligible": bool(mandate_bonus > 0.0 and score >= SURPRISE_FLASH_THRESHOLD),
        "admit_eligible": bool(score >= SURPRISE_ADMIT_THRESHOLD),
        "bookmarks": bookmarks,
        "analysis": analysis,
        "by": by_mode,
    }


def should_flash_write(user_message: str) -> bool:
    """判断用户当前输入是否包含需要毫秒级同步闪存（Flash-Write）的显式纠错或长期铁律。"""
    msg = (user_message or "").strip()
    if len(msg) < 6 or len(msg) > 4000:
        return False
    res = compute_surprise(msg, user_message=msg)
    return bool(res.get("flash_eligible"))


def extract_causal_triplet(
    user_message: str,
    assistant_reply: str = "",
) -> Optional[Dict[str, Any]]:
    """通过语义大模型（及离线兜底）从对话中提取标准化「四格错题卡」因果结构。"""
    user_text = (user_message or "").strip()
    reply_text = (assistant_reply or "").strip()
    if not user_text:
        return None

    surprise = compute_surprise(user_text, user_message=user_text, assistant_reply=reply_text)
    analysis = surprise.get("analysis") or {}
    if not surprise.get("admit_eligible") and not analysis.get("is_causal_fix"):
        return None

    bookmarks = surprise["bookmarks"]

    # 如果语义模型已经直接给出了结构化三元组，优先采用模型提炼结果
    llm_stmt = str(analysis.get("statement") or "").strip()
    llm_subj = str(analysis.get("subject") or "").strip()
    llm_when = str(analysis.get("when") or "").strip()
    llm_do = str(analysis.get("do") or "").strip()
    llm_why = str(analysis.get("why") or "").strip()
    polarity = str(analysis.get("polarity") or "").strip()
    if polarity not in ("prohibition", "requirement"):
        polarity = "prohibition" if any(
            w in (llm_stmt or user_text) for w in ("不要", "别再", "禁止", "严禁", "不得", "不能")
        ) else "requirement"

    if not llm_stmt:
        sentences = [s.strip() for s in re.split(r"[。\n！!；;]+", user_text) if len(s.strip()) >= 6]
        target = sentences[0] if sentences else user_text
        for prefix in ("请记住：", "请记住:", "记住：", "记住:", "记住，", "记住,", "注意：", "注意:"):
            if target.startswith(prefix):
                target = target[len(prefix) :].strip()
                break
        llm_stmt = target

    if not llm_subj:
        if bookmarks["files"]:
            llm_subj = Path_basename(bookmarks["files"][0])
        elif bookmarks["errors"]:
            llm_subj = bookmarks["errors"][0]
        else:
            llm_subj = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", llm_stmt[:24]).strip("_") or "user_rule"

    if not llm_when:
        when_parts: List[str] = []
        if bookmarks["files"]:
            when_parts.append("file:" + ",".join(bookmarks["files"][:2]))
        if bookmarks["errors"]:
            when_parts.append("error:" + ",".join(bookmarks["errors"][:2]))
        llm_when = " | ".join(when_parts) if when_parts else "general"

    tags: Dict[str, str] = {
        "scope": "platform",
        "kind": "technical",
        "polarity": polarity,
        "surprise_score": str(surprise["score"]),
    }
    if bookmarks["files"]:
        tags["file"] = bookmarks["files"][0]
    if bookmarks["errors"]:
        tags["error"] = bookmarks["errors"][0]

    return {
        "subject": llm_subj[:64],
        "statement": llm_stmt[:500],
        "when": llm_when[:160],
        "do": (llm_do or llm_stmt)[:500],
        "why": (llm_why or "semantic_surprise_capture")[:240],
        "polarity": polarity,
        "surprise": surprise["score"],
        "flash_eligible": surprise["flash_eligible"],
        "bookmarks": bookmarks,
        "tags": tags,
        "by": surprise.get("by", "fallback"),
    }


def Path_basename(path_str: str) -> str:
    raw = str(path_str or "").replace("\\", "/").strip("/")
    return raw.rsplit("/", 1)[-1] if "/" in raw else raw


def flash_write_explicit_mandate(
    user_message: str,
    assistant_reply: str = "",
    *,
    data_dir: str,
    user_id: str,
    agent_id: str,
    profile: str,
    entity_db: Optional[str] = None,
) -> Dict[str, Any]:
    """毫秒级同步闪存（Flash-Write）：当用户下达显式纠错或长期铁律时，直写海马体与新皮层 EntityStore。

    不修改全局 MemoryConfig 单例，不阻塞网关，确保下一轮对话 (<2ms) 即刻生效。
    """
    import hashlib
    import sqlite3
    import time
    from pathlib import Path

    if not should_flash_write(user_message):
        return {"ok": True, "written": False, "reason": "not_flash_eligible"}

    triplet = extract_causal_triplet(user_message, assistant_reply)
    if not triplet or not triplet.get("statement"):
        return {"ok": True, "written": False, "reason": "no_triplet_extracted"}

    root = Path(str(data_dir or ""))
    db_file = root / "coding_memory_cv.db"
    if not root.is_absolute() or not root.is_dir():
        return {"ok": False, "written": False, "reason": "invalid_data_dir"}

    stmt = str(triplet["statement"]).strip()
    subj = str(triplet["subject"] or "user_mandate").strip()
    polarity = str(triplet.get("polarity") or "requirement")
    digest = hashlib.sha256(f"{user_id}|{agent_id}|{profile}|{stmt}".encode("utf-8")).hexdigest()[:24]
    fact_id = f"flash-{digest}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    subject_key = re.sub(r"[^a-z0-9_\u4e00-\u9fff]", "", subj.lower())

    conn = sqlite3.connect(str(db_file), timeout=3.0)
    try:
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
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT fact_id FROM working_fact WHERE user_id=? AND agent_id=? AND statement=? AND status='active' LIMIT 1",
            (user_id, agent_id, stmt),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            fact_id = str(existing[0])
            already = True
        else:
            already = False
            conn.execute(
                "INSERT OR REPLACE INTO working_fact"
                " (fact_id, user_id, agent_id, session_id, workspace_id, branch, source,"
                "  statement, files, subject, details, fact_link, derived_experience_ids,"
                "  status, fact_chain_summary, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_id,
                    user_id,
                    agent_id,
                    "flash_session",
                    "_unknown",
                    "main",
                    "flash_write",
                    stmt,
                    json.dumps(triplet["bookmarks"].get("files", []), ensure_ascii=False),
                    subj,
                    json.dumps({"when": triplet["when"], "why": triplet["why"]}, ensure_ascii=False),
                    None,
                    "[]",
                    "active",
                    "",
                    now_iso,
                ),
            )
            tag_rows = [
                (fact_id, "profile", profile),
                (fact_id, "scope", "platform"),
                (fact_id, "kind", "technical"),
                (fact_id, "polarity", polarity),
                (fact_id, "flash_write", "1"),
                (fact_id, "surprise_score", str(triplet["surprise"])),
            ]
            if subject_key:
                tag_rows.append((fact_id, "subject_key", subject_key))
            if triplet["bookmarks"].get("files"):
                tag_rows.append((fact_id, "file", triplet["bookmarks"]["files"][0]))
            if triplet["bookmarks"].get("errors"):
                tag_rows.append((fact_id, "error", triplet["bookmarks"]["errors"][0]))
            conn.executemany(
                "INSERT OR REPLACE INTO fact_tags(fact_id, key, value) VALUES (?, ?, ?)",
                tag_rows,
            )
            # 同步更新 FTS5 索引（若存在）以保证关键词/粗筛立即可见
            try:
                has_fts = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='working_fact_fts'"
                ).fetchone()
                if has_fts:
                    rid_row = conn.execute(
                        "SELECT rowid FROM working_fact WHERE fact_id=?", (fact_id,)
                    ).fetchone()
                    if rid_row is not None:
                        conn.execute(
                            "INSERT OR REPLACE INTO working_fact_fts(rowid, fact_id, subject, statement)"
                            " VALUES (?, ?, ?, ?)",
                            (rid_row[0], fact_id, subj, stmt),
                        )
            except sqlite3.Error:
                pass
            conn.commit()
    finally:
        conn.close()

    # 若提供了新皮层 EntityStore 路径，同步晋升为 <2ms 实体规则
    entity_promoted = False
    if entity_db and Path(entity_db).is_absolute():
        try:
            from .entity_store import EntityStore
            estore = EntityStore(db_path=entity_db, auto_seed=True)
            estore.import_seed_pack({
                "entities": [{
                    "name": subj[:64],
                    "entity_type": "flash_mandate",
                    "description": f"Flash-write mandate {fact_id}",
                    "aliases": (triplet["bookmarks"].get("files", []) + triplet["bookmarks"].get("errors", []))[:4],
                }],
                "rules": [{
                    "entity": subj[:64],
                    "polarity": polarity,
                    "condition_expr": triplet["when"][:120],
                    "action_stmt": stmt[:500],
                    "confidence": 0.96,
                    "source": f"flash_write:{fact_id}",
                }],
            })
            entity_promoted = True
        except Exception as exc:
            logger.debug("Flash-Write EntityStore 同步跳过: %s", type(exc).__name__)

    return {
        "ok": True,
        "written": not already,
        "fact_id": fact_id,
        "subject": subj,
        "statement": stmt,
        "polarity": polarity,
        "entity_promoted": entity_promoted,
    }

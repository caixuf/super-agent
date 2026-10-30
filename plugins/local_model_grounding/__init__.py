"""Hermes plugin: ground an expert assistant's turns with two local models.

What it does per turn (``pre_llm_call``):

1. Recovers the most recent failure text from the turn's message history.
   Only one log per call is ever sent to the decider; nothing is batched.
2. Asks decider-4b which failure class it is, with a calibrated confidence.
   A verdict is decisive only at or above the configured threshold, and the
   threshold can be raised but never lowered below 0.6.
3. When a decisive verdict says environment, data or dependency, injects a
   deterministic instruction: do not edit assertions to make the case green;
   verify the missing input and report it. A non-decisive verdict injects
   nothing and is recorded for audit, so uncertainty can never masquerade as
   "it is just the environment".
4. Recalls dated precedents from docs/handover through bge-m3 and appends them
   with their date and source, explicitly labelled as history rather than
   current truth.

Hard rules:

- Both models are decision/embedding models. This plugin never asks either to
  generate prose, code, or explanations.
- Every call is audited to an append-only JSONL with source, options, choice,
  confidence, threshold, elapsed milliseconds, and whether it was injected.
- No indexing, gateway or disk failure may raise into the runtime: the hook
  returns ``None`` and the turn proceeds exactly as before.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import precedent
from .client import MIN_CONFIDENCE_FLOOR, LocalModelClient, append_jsonl

log = logging.getLogger(__name__)

#: Fixed candidate set. Every realistic class is present, including "other",
#: because a decider forced to choose from an incomplete list will still choose.
PRESCREEN_OPTIONS: tuple[str, ...] = (
    "环境问题",
    "数据缺失",
    "依赖服务异常",
    "断言不匹配",
    "时序竞争",
    "需求未实现",
    "其他/无法归类",
)
PRESCREEN_QUESTION = "这条仿真用例失败的首要根因属于哪一类？"

#: Decisive verdicts in these classes stop the "edit the case until green" loop.
#: They do not prove the case is correct; they only prove the next action is to
#: verify the environment/input rather than rewrite assertions.
SKIP_CATEGORIES = frozenset({"环境问题", "数据缺失", "依赖服务异常"})

_ERROR_RE = re.compile(
    r"(ERROR|FATAL|Traceback|AssertionError|FAIL|timed out|not found|"
    r"No such file|Permission denied|refused|OOM|Killed|exit code)",
    re.IGNORECASE,
)
_MAX_FAILURE_CHARS = 1600
_MAX_HISTORY_MESSAGES = 8
_MAX_INJECTED_HASHES = 16
#: 保留的会话级判定条数上限。两张表都按会话累积，长跑网关会一直涨。
_MAX_VERDICT_SESSIONS = 256
_MAX_INJECTED_SESSIONS = 256
_INDEX_CHECK_SECONDS = 60.0


def _condense(text: str, limit: int = _MAX_FAILURE_CHARS) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    kept = [line for line in raw.splitlines() if _ERROR_RE.search(line)]
    picked = "\n".join(kept) if kept else raw
    if len(picked) > limit:
        picked = "..." + picked[-limit:]
    return picked


def _message_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            return "\n".join(parts)
    return ""


def recent_failure(conversation_history: Optional[Sequence[Any]],
                   user_message: str = "") -> str:
    """Most recent message that looks like a failure, or an empty string.

    Deliberately narrow: a turn without a failure-looking message must not call
    the decider at all, otherwise every chat turn pays an inference.
    """
    messages = list(conversation_history or [])[-_MAX_HISTORY_MESSAGES:]
    for item in reversed(messages):
        text = _message_text(item)
        if text and _ERROR_RE.search(text):
            return _condense(text)
    if isinstance(user_message, str) and _ERROR_RE.search(user_message):
        return _condense(user_message)
    return ""


def _config_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(os.pathsep) if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _threshold(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = MIN_CONFIDENCE_FLOOR
    return max(MIN_CONFIDENCE_FLOOR, parsed)


def _prescreen_text(verdict: Dict[str, Any], threshold: float) -> str:
    choice = verdict.get("choice", "")
    confidence = float(verdict.get("confidence", 0.0) or 0.0)
    return (
        "### 本地初筛（decider-4b，仅本轮参考）\n"
        f"判定: {choice}（confidence={confidence:.2f}, threshold={threshold:.2f}, "
        f"model={verdict.get('model') or 'decider-4b'}）\n"
        "含义: 这条失败不像用例写法问题。不要为了变绿去改断言、删 check 或凑动作。\n"
        "下一步: 先核对日志里的缺失项（输入数据、地图、配置、依赖服务是否就绪），"
        "把缺口和证据报告给用户；只有确认失败确由用例写法引起时才修改用例。"
    )


class Grounding:
    """Stateful hook body; one instance per plugin load."""

    def __init__(self, ctx: Any) -> None:
        self.profile = getattr(ctx, "profile_name", "") or ""
        self.client = LocalModelClient(
            ctx.get_config("gateway_url", ""),
            token=ctx.get_config("token", ""),
            token_file=ctx.get_config("token_file", ""),
            timeout=ctx.get_config("timeout", 10.0),
        )
        self.audit_path = str(ctx.get_config("audit_path", "") or "")
        self.index_dir = str(ctx.get_config("index_dir", "") or "")
        self.docs_roots = _config_list(ctx.get_config("docs_roots", []))
        self.top_k = int(ctx.get_config("top_k", precedent.DEFAULT_TOP_K) or precedent.DEFAULT_TOP_K)
        self.min_score = float(ctx.get_config("min_score", precedent.DEFAULT_MIN_SCORE)
                              or precedent.DEFAULT_MIN_SCORE)
        self.min_confidence = _threshold(ctx.get_config("min_confidence", MIN_CONFIDENCE_FLOOR))
        self.max_context_chars = int(ctx.get_config("max_context_chars", 4000) or 4000)
        self._lock = threading.Lock()
        self._verdicts: Dict[str, Dict[str, Any]] = {}
        self._injected: Dict[str, set] = {}
        self._index_ok = False
        self._index_checked = 0.0
        self._index_building = False
        self._last_build_attempt = 0.0
        self.rebuild_interval = float(ctx.get_config("rebuild_min_interval_s", 600) or 0)

    # -- audit -------------------------------------------------------------
    def _audit(self, row: Dict[str, Any]) -> None:
        row = dict(row)
        row.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        row["plugin"] = "local-model-grounding"
        row["profile"] = self.profile
        append_jsonl(self.audit_path, row)

    # -- index -------------------------------------------------------------
    def _roots(self) -> List[Path]:
        return [Path(p).expanduser() for p in self.docs_roots]

    def _index_fresh(self) -> bool:
        """Cheap freshness probe, cached for a minute. Never starts a build.

        Docs change whenever another agent commits a handover, and a rebuild
        re-embeds the whole corpus (minutes of gateway traffic). A live turn must
        therefore never trigger a build inline: it either uses the current index
        or goes without precedents.
        """
        if not self.index_dir or not self.docs_roots or not self.client.configured:
            return False
        now = time.time()
        with self._lock:
            if now - self._index_checked < _INDEX_CHECK_SECONDS:
                return self._index_ok
            self._index_checked = now
            try:
                self._index_ok = precedent.index_ready(self.index_dir, self._roots())
            except Exception as exc:
                self._index_ok = False
                log.warning("local-model-grounding index probe failed: %s", type(exc).__name__)
            return self._index_ok

    def build_index_now(self) -> Optional[str]:
        if not self.index_dir or not self.docs_roots or not self.client.configured:
            return None
        self._last_build_attempt = time.time()
        path = precedent.build_index(self.index_dir, self._roots(), self.client)
        with self._lock:
            self._index_ok = bool(path)
            self._index_checked = time.time()
        return path

    def _launch_build(self, *, ignore_throttle: bool) -> None:
        if not self.index_dir or not self.docs_roots or not self.client.configured:
            return
        with self._lock:
            if self._index_building:
                return
            now = time.time()
            if not ignore_throttle and now - self._last_build_attempt < self.rebuild_interval:
                return
            self._index_building = True
            self._last_build_attempt = now

        def _work() -> None:
            try:
                path = self.build_index_now()
                self._audit({"kind": "index", "built": bool(path), "path": path or "",
                             "chunks_root": self.docs_roots})
            except Exception as exc:
                log.warning("local-model-grounding index build failed: %s", type(exc).__name__)
                self._audit({"kind": "index", "built": False, "error": type(exc).__name__})
            finally:
                with self._lock:
                    self._index_building = False

        threading.Thread(target=_work, name="local-model-grounding-index", daemon=True).start()

    def start_index_build(self) -> None:
        """Initial warm-up at plugin load; ignores the rebuild throttle."""
        if not self._index_fresh():
            self._launch_build(ignore_throttle=True)

    def ensure_index_async(self) -> None:
        """Turn-time guard: refresh in the background, never inline."""
        if not self._index_fresh():
            self._launch_build(ignore_throttle=False)

    # -- decider -----------------------------------------------------------
    @staticmethod
    def _trim_session_keys_locked(table: Dict[str, Any], limit: int) -> None:
        """把会话级缓存压在上限内，丢最旧的。调用者持有 self._lock。

        丢掉的只是**缓存**：判定会被重新算，注入去重集合丢了则同一段上下文
        可能被注入两次 —— 后者由 max_context_chars 之外的事实重算成本兜住，
        但仍优于让表无限增长。
        """
        while len(table) > limit:
            oldest = next(iter(table))
            table.pop(oldest, None)

    def _decide(self, failure: str, session_id: str, source: str) -> Optional[Dict[str, Any]]:
        digest = hashlib.sha256(failure.encode("utf-8", errors="replace")).hexdigest()[:16]
        with self._lock:
            cached = self._verdicts.get(session_id)
        if cached and cached.get("digest") == digest:
            return cached

        verdict = self.client.decide(failure, PRESCREEN_QUESTION, PRESCREEN_OPTIONS)
        if verdict is None:
            self._audit({"kind": "decider", "session_id": session_id, "source": source,
                         "available": False, "reason": "gateway_unavailable",
                         "options": list(PRESCREEN_OPTIONS),
                         "threshold": self.min_confidence, "injected": False})
            return None

        confidence = float(verdict.get("confidence", 0.0) or 0.0)
        choice = str(verdict.get("choice", "") or "")
        decisive = confidence >= self.min_confidence
        skip = decisive and choice in SKIP_CATEGORIES
        record = {
            "digest": digest,
            "source": source,
            "choice": choice,
            "confidence": confidence,
            "decisive": decisive,
            "skip_llm": skip,
            "model": verdict.get("model", ""),
            "elapsed_ms": verdict.get("elapsed_ms", 0.0),
        }
        with self._lock:
            self._verdicts[session_id] = record
            self._trim_session_keys_locked(self._verdicts, _MAX_VERDICT_SESSIONS)
        self._audit({
            "kind": "decider", "session_id": session_id, "source": source,
            "available": True, "options": list(PRESCREEN_OPTIONS),
            "choice": choice, "confidence": round(confidence, 4),
            "probs": {k: round(v, 4) for k, v in (verdict.get("probs") or {}).items()},
            "threshold": self.min_confidence, "decisive": decisive,
            "skip_llm": skip, "model": verdict.get("model", ""),
            "elapsed_ms": verdict.get("elapsed_ms", 0.0),
            "injected": False,
        })
        return record

    # -- hook --------------------------------------------------------------
    def pre_llm_call(self, *, session_id: str = "", user_message: str = "",
                     conversation_history: Optional[Sequence[Any]] = None,
                     **kwargs: Any) -> Optional[Dict[str, str]]:
        try:
            self.ensure_index_async()
            failure = recent_failure(conversation_history, user_message)
            segments: List[str] = []

            if failure:
                verdict = self._decide(failure, session_id or "unknown", "conversation_history")
                if verdict and verdict.get("skip_llm"):
                    segments.append(_prescreen_text(verdict, self.min_confidence))

            if self._index_fresh():
                query = "\n\n".join(part for part in (user_message, failure) if part)
                hits = precedent.retrieve(query, self.index_dir, self.client,
                                          top_k=self.top_k, min_score=self.min_score)
                block = precedent.format_block(hits)
                if block:
                    segments.append(block)
                    self._audit({"kind": "precedent", "session_id": session_id,
                                 "query_chars": len(query), "hits": [
                                     {"path": h["path"], "date": h["date"],
                                      "score": round(h["score"], 4)} for h in hits],
                                 "injected": True})

            if not segments:
                return None
            context = "\n\n".join(segments)
            if len(context) > self.max_context_chars:
                context = context[:self.max_context_chars].rstrip() + "\n...(已截断)"

            digest = hashlib.sha256(context.encode("utf-8", errors="replace")).hexdigest()[:16]
            with self._lock:
                seen = self._injected.setdefault(session_id, set())
                if digest in seen:
                    return None
                seen.add(digest)
                if len(seen) > _MAX_INJECTED_HASHES:
                    self._injected[session_id] = set(list(seen)[-_MAX_INJECTED_HASHES:])
                self._trim_session_keys_locked(self._injected, _MAX_INJECTED_SESSIONS)
            return {"context": context}
        except Exception as exc:  # a grounding hint must never break a turn
            log.warning("local-model-grounding hook failed: %s", type(exc).__name__)
            return None


def _disabled_by_env() -> bool:
    return os.environ.get("LOCAL_MODEL_GROUNDING_OFF", "").strip() not in ("", "0", "false", "False")


def register(ctx: Any) -> None:
    if ctx.get_config("enabled", False) is not True:
        return
    if _disabled_by_env():
        log.info("local-model-grounding disabled by LOCAL_MODEL_GROUNDING_OFF")
        return
    grounding = Grounding(ctx)
    if not grounding.client.configured:
        log.warning("local-model-grounding enabled without gateway_url; no hooks registered")
        return
    ctx.register_hook("pre_llm_call", grounding.pre_llm_call)
    grounding.start_index_build()

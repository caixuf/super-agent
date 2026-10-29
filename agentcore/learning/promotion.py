#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选晋升：把 approved 候选写成正式事实，并做写入后的召回探针。

为什么单独一层
--------------
候选层（``candidates.py``）刻意不 import 记忆引擎：审查通过只产出
``as_fact_proposal()`` 载荷。把候选变成事实必须是一次**显式、可审计**的动作，
所以放在本模块：

    approved 候选 → 具名审查者确认 → write_fact（稳定身份）→ 召回探针

边界：

- 只有 ``approved`` 候选能晋升；pending / rejected / conflicted /
  needs_human / 未知状态（如 withdrawn）一律拒绝，且拒绝发生在打开事实库之前。
- 本模块**不改候选账本**：不调用审查、不写回候选记录，也不把候选标成已确认；
  ``Candidate.is_confirmed_fact`` 仍然恒为 ``False``。
- 写入身份稳定：``fact_id = stable_fact_id(candidate_id)``，并把
  candidate_id / source_id / source_url / source_hash / 审查者写进标签。
  重试时记忆库按 (user, workspace, subject, statement) 命中已有行并返回同一
  fact_id，因此晋升是幂等的（``written=False, deduped=True``）。
- 探针先 ``flush_background`` 再召回：向量写入是异步的，不汇合就断言会假阴性。
- 跨用户隔离是安全断言：同一 fact_id 不得在另一个 user_id 下被召回。

本阶段明确不做：真实审查界面/通知、真正的抽取器、命中后的置信度强化。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .candidates import (
    STATUS_APPROVED,
    CandidateError,
    InvalidCandidate,
    UnknownCandidate,
)

__all__ = [
    "PROMOTION_SOURCE",
    "PROBE_HIT",
    "PROBE_NO_MATCH",
    "PROBE_ERROR",
    "PROBE_SCOPE_LEAK",
    "PromotionError",
    "CandidateNotPromotable",
    "PromotionWriteFailed",
    "PromotionResult",
    "RecallProbeResult",
    "promote_candidate",
    "recall_probe",
    "stable_fact_id",
]

#: 晋升写入的默认来源标签；记忆库据此把 scope 判成 platform（宿主自有经验）。
PROMOTION_SOURCE = "t9_promotion"

PROBE_HIT = "hit"
PROBE_NO_MATCH = "no_match"
PROBE_ERROR = "error"
PROBE_SCOPE_LEAK = "scope_leak"

#: 稳定 fact_id 的前缀。加前缀是为了让人一眼看出这条事实来自候选晋升，
#: 而不是抽取器或手工写入。
_FACT_ID_PREFIX = "candfact_"
_FACT_ID_HEX = 32
_MAX_REVIEWER_CHARS = 128
_SCOPE_PROBE_USER = "__t9_recall_probe_other__"


# ── 异常 ────────────────────────────────────────────────────────────────────
class PromotionError(CandidateError):
    """晋升流程的拒绝/失败基类；调用方捕获 ``CandidateError`` 即可。"""


class CandidateNotPromotable(PromotionError):
    """候选当前状态/证据不允许进入正式记忆（不是写入失败）。"""


class PromotionWriteFailed(PromotionError):
    """正式记忆库拒绝了写入或没有返回可用结果。"""


# ── 结果对象 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PromotionResult:
    """一次晋升的结果。``deduped`` 表示该候选此前已经晋升过（幂等重试）。"""

    candidate_id: str
    fact_id: str
    written: bool
    deduped: bool
    provenance_match: bool
    source: str
    reviewer: str
    statement: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.fact_id) and (self.written or self.deduped)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "fact_id": self.fact_id,
            "written": self.written,
            "deduped": self.deduped,
            "provenance_match": self.provenance_match,
            "source": self.source,
            "reviewer": self.reviewer,
            "statement": self.statement,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RecallProbeResult:
    """写入后的召回探针结果。

    ``status`` 取值：``hit`` / ``no_match`` / ``error`` / ``scope_leak``。
    后两者都意味着这次晋升不能算「已生效」：``ok`` 为 ``False``。
    """

    fact_id: str
    query: str
    status: str
    matched: bool
    fact_ids: Tuple[str, ...] = ()
    empty_reason: Optional[str] = None
    error: Optional[str] = None
    scope_checked: bool = False
    scope_isolated: Optional[bool] = None
    leaked_to: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == PROBE_HIT and self.scope_isolated is not False

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "query": self.query,
            "status": self.status,
            "matched": self.matched,
            "fact_ids": list(self.fact_ids),
            "empty_reason": self.empty_reason,
            "error": self.error,
            "scope_checked": self.scope_checked,
            "scope_isolated": self.scope_isolated,
            "leaked_to": self.leaked_to,
        }


# ── 纯函数/内部工具 ─────────────────────────────────────────────────────────
def stable_fact_id(candidate_id: str) -> str:
    """候选 → 事实的稳定身份。同一候选重试必须落到同一个 fact_id。"""
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise InvalidCandidate("candidate_id 不能为空")
    digest = hashlib.sha256(candidate_id.strip().encode("utf-8")).hexdigest()[:_FACT_ID_HEX]
    return _FACT_ID_PREFIX + digest


def _reviewer_identity(reviewer: Any) -> str:
    if not isinstance(reviewer, str):
        raise InvalidCandidate("reviewer 必须是字符串")
    identity = reviewer.strip()
    if not identity:
        raise InvalidCandidate("reviewer 不能为空：晋升必须留下具名审查者")
    if len(identity) > _MAX_REVIEWER_CHARS:
        raise InvalidCandidate("reviewer 超过 %d 字符" % _MAX_REVIEWER_CHARS)
    if any(ch in identity for ch in "\r\n\t"):
        raise InvalidCandidate("reviewer 不得包含换行/制表符")
    return identity


def _source_identity(source: Any) -> str:
    if not isinstance(source, str) or not source.strip():
        raise InvalidCandidate("source 不能为空")
    value = source.strip()
    if any(ch in value for ch in "\r\n\t"):
        raise InvalidCandidate("source 不得包含换行/制表符")
    return value


def _resolve_memory_store(memory_store):
    """默认走正式记忆库模块；测试/宿主可注入同形状对象。"""
    if memory_store is not None:
        return memory_store
    from ..memory import memory_store as module  # 延迟 import：只有晋升时才需要事实库
    return module


def _load_candidate(store, candidate_id: str):
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise InvalidCandidate("candidate_id 不能为空")
    getter = getattr(store, "get", None)
    if not callable(getter):
        raise InvalidCandidate("store 必须提供 get(candidate_id)")
    candidate = getter(candidate_id)
    if candidate is None:
        raise UnknownCandidate("未知候选：%s" % candidate_id)
    return candidate


def _promotion_tags(candidate, reviewer: str, source: str) -> Dict[str, str]:
    """写进正式记忆的溯源标签。值全部是确定性字符串，便于按候选反查。"""
    tags = {
        "candidate_id": candidate.candidate_id,
        "candidate_domain": candidate.domain,
        "source_id": candidate.source_id,
        "source_url": candidate.source_url,
        "source_hash": candidate.source_hash,
        "retrieved_at": candidate.retrieved_at,
        "statement_fingerprint": candidate.statement_fingerprint,
        "promotion_reviewer": reviewer,
        "promotion_source": source,
        "promotion_stage": "t9",
    }
    return {k: v for k, v in tags.items() if v}


def _owner_identity(memory_store, user_id: Optional[str]) -> str:
    if user_id:
        return str(user_id)
    getter = getattr(memory_store, "get_config", None)
    value = None
    if callable(getter):
        try:
            value = getattr(getter(), "default_user", None)
        except Exception:  # noqa: BLE001 —— 解析不到默认用户不影响隔离探针
            value = None
    return str(value) if value else "__default_user__"


def _other_scope_user(owner: str) -> str:
    other = _SCOPE_PROBE_USER
    return other + "_2" if other == owner else other


def _error_probe(fact_id: str, query: str, message: str,
                 matched: bool = False, fact_ids: Tuple[str, ...] = (),
                 empty_reason: Optional[str] = None,
                 scope_checked: bool = False,
                 scope_isolated: Optional[bool] = None) -> RecallProbeResult:
    return RecallProbeResult(
        fact_id=fact_id, query=query, status=PROBE_ERROR, matched=matched,
        fact_ids=fact_ids, empty_reason=empty_reason, error=message,
        scope_checked=scope_checked, scope_isolated=scope_isolated)


def _result_identity(payload: Mapping[str, Any]) -> Tuple[str, ...]:
    rows = payload.get("results") or ()
    ids = []
    for row in rows:
        if isinstance(row, Mapping):
            value = row.get("fact_id")
            if value:
                ids.append(str(value))
    return tuple(ids)


# ── 晋升 ────────────────────────────────────────────────────────────────────
def promote_candidate(store, candidate_id: str, *, reviewer: str,
                      memory_store=None, source: str = PROMOTION_SOURCE,
                      user_id: Optional[str] = None) -> PromotionResult:
    """把一条 **approved** 候选写成正式事实。

    参数
    ----
    store:
        候选库（``CandidateStore`` 或提供 ``get(candidate_id)`` 的同形状对象）。
    candidate_id:
        只有 ``review_status == approved`` 且证据完整的候选才会被写入。
    reviewer:
        本次晋升的具名审查者（写入溯源标签；不会用来修改候选状态）。
    memory_store:
        记忆库模块/对象（``write_fact`` / ``recall_facts`` / ``flush_background``）。
        默认 ``agentcore.memory.memory_store``。
    source:
        写入来源，默认 ``t9_promotion``。
    user_id:
        写入的 user 作用域；``None`` 时由记忆库配置解析。

    返回 ``PromotionResult``。重试同一候选返回
    ``written=False, deduped=True`` 且 fact_id 不变；候选本身永远保持原状态。
    """
    reviewer = _reviewer_identity(reviewer)
    source = _source_identity(source)

    candidate = _load_candidate(store, candidate_id)
    if candidate.review_status != STATUS_APPROVED:
        raise CandidateNotPromotable(
            "候选 %s 状态为 %s，只有 %s 才能晋升"
            % (candidate.candidate_id, candidate.review_status, STATUS_APPROVED))
    if not candidate.evidence_complete:
        raise CandidateNotPromotable(
            "候选 %s 证据不完整，不能写入正式记忆" % candidate.candidate_id)

    # 唯一合法的交接面：候选层产出的待写入载荷。
    proposal = candidate.as_fact_proposal()
    if proposal.get("confirmed_fact") is not False or proposal.get("requires_memory_write") is not True:
        raise PromotionError("候选 %s 的入库载荷违约" % candidate.candidate_id)

    statement = proposal["statement"]
    fact_id = stable_fact_id(candidate.candidate_id)
    tags = _promotion_tags(candidate, reviewer, source)
    memory = _resolve_memory_store(memory_store)

    try:
        raw = memory.write_fact(
            statement,
            subject=candidate.subject or "",
            source=source,
            tags=tags,
            fact_id=fact_id,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 写入失败要带原始异常交给上层审计
        raise PromotionWriteFailed(
            "候选 %s 写入正式记忆失败：%s" % (candidate.candidate_id, exc)) from exc

    if not isinstance(raw, Mapping):
        raise PromotionWriteFailed(
            "候选 %s 写入返回了非映射结果：%r" % (candidate.candidate_id, raw))
    if not raw.get("ok"):
        raise PromotionWriteFailed(
            "候选 %s 写入被记忆库拒绝：%s"
            % (candidate.candidate_id, raw.get("error") or raw))

    written = bool(raw.get("written"))
    resolved_id = str(raw.get("fact_id") or "")
    if not resolved_id:
        raise PromotionWriteFailed("候选 %s 写入未返回 fact_id" % candidate.candidate_id)

    stored_tags = raw.get("tags") or {}
    provenance_match = str(stored_tags.get("candidate_id") or "") == candidate.candidate_id
    return PromotionResult(
        candidate_id=candidate.candidate_id,
        fact_id=resolved_id,
        written=written,
        deduped=not written,
        provenance_match=provenance_match,
        source=source,
        reviewer=reviewer,
        statement=statement,
        reason=str(raw.get("reason") or ""),
    )


# ── 写入后探针 ──────────────────────────────────────────────────────────────
def recall_probe(fact_id: str, query: str, *, memory_store=None,
                 user_id: Optional[str] = None, ebm: Optional[str] = None,
                 limit: int = 10, tags: Optional[Dict[str, str]] = None,
                 check_scope: bool = True) -> RecallProbeResult:
    """汇合后台向量写入后召回，验证 ``fact_id`` 真的能被读回。

    - ``hit``：命中且（若检查）没有跨用户泄漏；
    - ``no_match``：召回正常但没有这条（向量未落盘、门限过滤、作用域写错等）；
    - ``error``：后台汇合失败或召回接口抛错/返回 ``ok=False``；
    - ``scope_leak``：本用户命中，但用另一个 user_id 也能召回到同一条。

    ``check_scope`` 默认开启：没有隔离断言的「写入成功」只是前半句。
    """
    if not isinstance(fact_id, str) or not fact_id.strip():
        raise InvalidCandidate("fact_id 不能为空")
    fact_id = fact_id.strip()
    if not isinstance(query, str):
        raise InvalidCandidate("query 必须是字符串")

    memory = _resolve_memory_store(memory_store)
    try:
        pending = memory.flush_background()
    except Exception as exc:  # noqa: BLE001
        return _error_probe(fact_id, query,
                            "flush_background 失败：%s: %s" % (type(exc).__name__, exc))
    if isinstance(pending, int) and pending > 0:
        return _error_probe(fact_id, query,
                            "后台向量写入未汇合：仍有 %d 个线程" % pending)

    try:
        payload = memory.recall_facts(query, user_id=user_id, ebm=ebm,
                                      tags=tags, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _error_probe(fact_id, query,
                            "recall_facts 失败：%s: %s" % (type(exc).__name__, exc))

    if not isinstance(payload, Mapping):
        return _error_probe(fact_id, query,
                            "recall_facts 返回了非映射结果：%r" % (payload,))
    if not payload.get("ok"):
        return _error_probe(fact_id, query,
                            "recall_facts 返回失败：%s" % (payload.get("error") or payload,))

    ids = _result_identity(payload)
    matched = fact_id in ids
    empty_reason = payload.get("empty_reason")
    status = PROBE_HIT if matched else PROBE_NO_MATCH

    scope_checked = False
    scope_isolated: Optional[bool] = None
    leaked_to: Optional[str] = None
    if check_scope:
        owner = _owner_identity(memory, user_id)
        other = _other_scope_user(owner)
        try:
            other_payload = memory.recall_facts(query, user_id=other, ebm=ebm,
                                                tags=tags, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return _error_probe(
                fact_id, query,
                "跨用户探针失败：%s: %s" % (type(exc).__name__, exc),
                matched=matched, fact_ids=ids, empty_reason=empty_reason,
                scope_checked=True)
        scope_checked = True
        if not isinstance(other_payload, Mapping):
            return _error_probe(
                fact_id, query,
                "跨用户探针返回了非映射结果：%r" % (other_payload,),
                matched=matched, fact_ids=ids, empty_reason=empty_reason,
                scope_checked=True)
        if not other_payload.get("ok"):
            return _error_probe(
                fact_id, query,
                "跨用户探针返回失败：%s" % (other_payload.get("error") or other_payload,),
                matched=matched, fact_ids=ids, empty_reason=empty_reason,
                scope_checked=True)
        other_ids = _result_identity(other_payload)
        scope_isolated = fact_id not in other_ids
        if not scope_isolated:
            leaked_to = other
            status = PROBE_SCOPE_LEAK

    return RecallProbeResult(
        fact_id=fact_id,
        query=query,
        status=status,
        matched=matched,
        fact_ids=ids,
        empty_reason=empty_reason,
        scope_checked=scope_checked,
        scope_isolated=scope_isolated,
        leaked_to=leaked_to,
    )

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选知识：带来源快照与逐字引文锚定的自学习中间层。

为什么需要这一层
----------------
检索来的网页/文档属于**不可信数据**：它可能过期、被篡改，甚至整段内容就是
在诱导调用方「忽略先前的指令」。把这类文本直接写进正式记忆，等于让外部内容
获得持久化的话语权。所以中间必须隔一层**候选知识**：

    来源快照（内容寻址） → 候选（逐字引文锚定） → 独立审查 →（后续阶段）正式库

本模块只负责前两步与审查门禁，边界写死：

- **不联网**：只接收调用方已经抓到的快照文本，不发起任何请求。
- **不定时**：没有 sleep / Timer / 调度；时间只作为证据字段和审计时间戳。
- **不写正式记忆**：不 import 记忆引擎、不打开事实库。审查通过只产出一份
  ``as_fact_proposal()`` 待写入载荷，真正入库由后续阶段显式完成。
- **候选不是事实**：``Candidate.is_confirmed_fact`` 恒为 ``False``；
  ``review_status`` 再高也只表示「允许进入下一步」，不表示已经入库。

关键判定
--------
1. 逐字引文：``literal_quote`` 必须能在快照原文里**原样**定位，不做大小写或
   空白归一化。归一化会放过「改写过的句子」，那正是伪造引文的入口。
2. 来源身份：``source_id = sha256(url + content_hash)``。同一来源重复灌入只保留
   一份快照与一份候选；不同 URL 上的相同内容视为不同来源。
3. 不可自动批准：创建时只能是 ``pending``；审查必须给出具名且非自动化的审查者，
   且不能是候选作者本人；审批时重新校验快照、哈希、引文与冲突。
4. 冲突：同一 ``subject`` 下出现不同 statement 即冲突，冲突未解决前双方都不能
   通过审批；显式 reject 掉一方后才解除。当前不做语义相似度推断。
5. 撤销：只有 ``approved`` 候选能被撤回为 ``revoked``（终态），且必须给出
   具名、非作者的审查者与理由；晋升只接受 ``approved``，撤销后不会再写入正式
   记忆。撤销复用同一条审查审计轨迹（decision=``revoke``）。

剩余工作（本阶段明确不做）
--------------------------
检索任务与预算、抓取器、审查交互、正式记忆写入（``write_fact``）、写入后的召回
探针、跨 scope 泄漏验证、基于语义的冲突检测。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "Candidate",
    "CandidateStore",
    "GroundingReport",
    "IngestResult",
    "SourceRecord",
    "STATUS_PENDING",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "STATUS_CONFLICTED",
    "STATUS_NEEDS_HUMAN",
    "STATUS_REVOKED",
    "STATUSES",
    "TERMINAL_STATUSES",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "DECISION_REVOKE",
    "CandidateError",
    "InvalidCandidate",
    "QuoteNotGrounded",
    "EvidenceMismatch",
    "UnknownCandidate",
    "UnknownSource",
    "ReviewerError",
    "ReviewerNotIndependent",
    "AutoApprovalForbidden",
    "ReviewBlocked",
    "canonical_timestamp",
    "normalize_domain",
    "normalize_source_url",
    "quote_grounded",
    "source_identity",
]

# ── 状态与决策 ──────────────────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CONFLICTED = "conflicted"
STATUS_NEEDS_HUMAN = "needs_human"
#: 已批准候选被撤回：终态，且晋升只接受 approved，所以撤销后不可入库。
STATUS_REVOKED = "revoked"

STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED,
            STATUS_CONFLICTED, STATUS_NEEDS_HUMAN, STATUS_REVOKED)

#: 终态：一旦进入就不再接受审查（要改结论只能新建候选）。
TERMINAL_STATUSES = (STATUS_APPROVED, STATUS_REJECTED, STATUS_REVOKED)

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_REVOKE = "revoke"

#: 参与「冲突」判定的状态：未解决的相反说法仍然挡住审批。
_CONFLICTING_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_CONFLICTED,
                         STATUS_NEEDS_HUMAN)

#: 这些「审查者」名字代表自动化流程本身，不能给出人工结论。
_AUTOMATED_REVIEWERS = frozenset({"auto", "automatic", "system", "machine",
                                  "self", "agentcore", "_auto"})

# ── 硬边界（证据必须是原子、可复审的）─────────────────────────────────────
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_QUOTE_CHARS = 2000
_MAX_STATEMENT_CHARS = 1000
_MAX_IDENTITY_CHARS = 128
_MAX_URL_CHARS = 2048

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*$")
_UNTRUSTED_BEGIN = "<<<UNTRUSTED_SOURCE_DATA>>>"
_UNTRUSTED_END = "<<<END_UNTRUSTED_SOURCE_DATA>>>"


# ── 异常 ────────────────────────────────────────────────────────────────────
class CandidateError(ValueError):
    """候选知识层的所有拒绝都以这个基类抛出。"""


class InvalidCandidate(CandidateError):
    """字段缺失、越界或形态不合法。"""


class QuoteNotGrounded(CandidateError):
    """引文无法在来源快照中逐字定位 —— 伪造/改写引文一律拒绝。"""


class EvidenceMismatch(CandidateError):
    """调用方声明的内容哈希与快照实际哈希不一致。"""


class UnknownCandidate(CandidateError):
    """候选 ID 不存在。"""


class UnknownSource(CandidateError):
    """来源 ID 不存在。"""


class ReviewerError(CandidateError):
    """审查者身份不合法。"""


class ReviewerNotIndependent(ReviewerError):
    """审查者与候选作者是同一个身份。"""


class AutoApprovalForbidden(ReviewerError):
    """自动化身份不得给出审批结论。"""


class ReviewBlocked(CandidateError):
    """证据不足或存在未解冲突，审批被挡下。"""

    def __init__(self, reasons, candidate: "Candidate"):
        self.reasons: Tuple[str, ...] = tuple(reasons)
        self.candidate = candidate
        super().__init__("审批被拒绝：" + ", ".join(self.reasons))


# ── 纯函数：校验与身份 ──────────────────────────────────────────────────────
def _canonical(text: str) -> str:
    """仅用于比较（claim key / 指纹），**绝不**用于引文定位。"""
    return " ".join((text or "").split()).casefold()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_text(value, field: str, *, limit: int, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidCandidate("%s 必须是字符串" % field)
    if "\x00" in value:
        raise InvalidCandidate("%s 不得包含 NUL 字符" % field)
    if not allow_blank and not value.strip():
        raise InvalidCandidate("%s 不能为空" % field)
    if len(value) > limit:
        raise InvalidCandidate("%s 超过长度上限 %d" % (field, limit))
    return value


def normalize_domain(value: str) -> str:
    """domain/scope 用小写自由标识；只允许安全字符，避免拼进查询时被注入。"""
    raw = _require_text(value, "domain", limit=64).strip().lower()
    if not _DOMAIN_RE.match(raw):
        raise InvalidCandidate(
            "domain 只允许小写字母/数字与 ._:/-，且必须以字母或数字开头：%r" % raw)
    return raw


def normalize_source_url(value: str) -> str:
    """来源 URL 只接受 http(s)，且拒绝空白与控制字符（不可信数据入口）。"""
    raw = _require_text(value, "source_url", limit=_MAX_URL_CHARS).strip()
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw):
        raise InvalidCandidate("source_url 不得包含空白/控制字符")
    scheme, sep, rest = raw.partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        raise InvalidCandidate("source_url 必须是 http(s) URL：%r" % raw)
    if not rest.strip("/"):
        raise InvalidCandidate("source_url 缺少主机部分：%r" % raw)
    return raw


def canonical_timestamp(value, field: str = "retrieved_at") -> str:
    """把时间规范成 UTC ISO-8601（``...Z``）；无法解析即视为证据缺失。

    无时区的输入按 UTC 解释 —— 宁可写清楚，也不让本地时区悄悄漂移。
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidCandidate("%s 不能为空：时间属于证据的一部分" % field)
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise InvalidCandidate("%s 不是合法 ISO-8601 时间：%r" % (field, raw)) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_identity(value, field: str) -> str:
    raw = _require_text(value, field, limit=_MAX_IDENTITY_CHARS).strip()
    if not raw:
        raise InvalidCandidate("%s 不能为空" % field)
    if any(ch in raw for ch in "\r\n\t"):
        raise InvalidCandidate("%s 不得包含换行/制表符" % field)
    return raw


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def quote_grounded(snapshot_text: str, literal_quote: str) -> bool:
    """逐字判定：引文必须原样出现在快照里。纯空白引文不算证据。"""
    if not isinstance(snapshot_text, str) or not isinstance(literal_quote, str):
        return False
    if not literal_quote.strip():
        return False
    return literal_quote in snapshot_text


def source_identity(source_url: str, source_hash: str) -> str:
    """来源身份 = URL + 内容哈希。URL 不同即不同来源，不按内容去重。"""
    return hashlib.sha256(("%s\n%s" % (source_url, source_hash)).encode("utf-8")).hexdigest()


def _candidate_identity(*, source_id: str, domain: str, claim_key: str,
                        statement: str, literal_quote: str) -> str:
    material = "\x00".join((source_id, domain, claim_key,
                            _canonical(statement), literal_quote))
    return "cand_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# ── 数据对象 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SourceRecord:
    """快照元数据。原文按内容哈希落在 ``<root>/sources/<hash>.txt``。"""

    source_id: str
    source_url: str
    source_hash: str
    retrieved_at: str
    byte_size: int

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "source_url": self.source_url,
                "source_hash": self.source_hash, "retrieved_at": self.retrieved_at,
                "byte_size": self.byte_size}

    @classmethod
    def from_dict(cls, data: dict) -> "SourceRecord":
        try:
            return cls(source_id=str(data["source_id"]),
                       source_url=str(data["source_url"]),
                       source_hash=str(data["source_hash"]),
                       retrieved_at=str(data["retrieved_at"]),
                       byte_size=int(data["byte_size"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidCandidate("来源记录损坏：%r" % (data,)) from exc


@dataclass(frozen=True)
class Candidate:
    """一条候选知识。**永远不是已确认事实**，只表示「有来源、待审查」。"""

    candidate_id: str
    statement: str
    literal_quote: str
    source_url: str
    source_hash: str
    retrieved_at: str
    domain: str
    review_status: str = STATUS_PENDING
    reviewer: Optional[str] = None
    reviewed_at: Optional[str] = None
    subject: Optional[str] = None
    source_id: str = ""
    author: Optional[str] = None
    review_note: str = ""

    # ---- 派生视图 ----
    @property
    def scope(self) -> str:
        """T9 字段表写作 domain/scope：同一维度，存储字段名是 ``domain``。"""
        return self.domain

    @property
    def claim_key(self) -> str:
        """冲突比较用的键：显式 subject 优先，否则退化为 statement 本身。"""
        base = self.subject if self.subject else self.statement
        return _canonical(base)

    @property
    def statement_fingerprint(self) -> str:
        return _hash_text(_canonical(self.statement))

    @property
    def is_confirmed_fact(self) -> bool:
        """结构性事实：候选层不产出已确认事实，任何状态下都是 ``False``。"""
        return False

    @property
    def is_terminal(self) -> bool:
        return self.review_status in TERMINAL_STATUSES

    @property
    def evidence_complete(self) -> bool:
        required = (self.candidate_id, self.statement, self.literal_quote,
                    self.source_url, self.source_hash, self.retrieved_at,
                    self.domain, self.source_id)
        if not all(isinstance(v, str) and v.strip() for v in required):
            return False
        try:
            canonical_timestamp(self.retrieved_at)
        except InvalidCandidate:
            return False
        return True

    # ---- 不可信数据视图 ----
    def untrusted_block(self) -> str:
        """把外部文本包成**数据块**。调用方不得把块内文字当作指令执行。"""
        return "\n".join((
            _UNTRUSTED_BEGIN,
            "source_url: %s" % self.source_url,
            "source_hash: %s" % self.source_hash,
            "quote: %s" % self.literal_quote,
            "statement(proposed): %s" % self.statement,
            _UNTRUSTED_END,
        ))

    # ---- 后续阶段的交接面 ----
    def as_fact_proposal(self) -> dict:
        """产出「待写入正式记忆」的载荷；本模块不会自己写。

        只有 approved 候选才有资格进入下一步；即便如此，返回的载荷也显式带
        ``confirmed_fact=False`` 与 ``requires_memory_write=True``，
        让后续写库步骤成为一次**独立的、可审计的**动作。
        """
        if self.review_status != STATUS_APPROVED:
            raise CandidateError(
                "候选 %s 当前状态为 %s，不能生成入库载荷" % (self.candidate_id,
                                                             self.review_status))
        return {
            "candidate_id": self.candidate_id,
            "statement": self.statement,
            "literal_quote": self.literal_quote,
            "source_url": self.source_url,
            "source_hash": self.source_hash,
            "source_id": self.source_id,
            "retrieved_at": self.retrieved_at,
            "domain": self.domain,
            "scope": self.scope,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "confirmed_fact": False,
            "requires_memory_write": True,
        }

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in (
            "candidate_id", "statement", "literal_quote", "source_url",
            "source_hash", "retrieved_at", "domain", "review_status", "reviewer",
            "reviewed_at", "subject", "source_id", "author", "review_note")}

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate":
        known = {field.name for field in fields(cls)}
        missing = sorted(known - set(data))
        if missing:
            raise InvalidCandidate("候选记录缺字段：%s" % ",".join(missing))
        values = {name: data[name] for name in known}
        if values["review_status"] not in STATUSES:
            raise InvalidCandidate("候选状态不合法：%r" % (values["review_status"],))
        return cls(**values)


@dataclass(frozen=True)
class IngestResult:
    candidate: Candidate
    created: bool
    source_created: bool
    snapshot_created: bool


@dataclass(frozen=True)
class GroundingReport:
    candidate_id: str
    ok: bool
    source_present: bool
    snapshot_present: bool
    hash_matches: bool
    quote_grounded: bool
    reasons: Tuple[str, ...]


# ── 候选库 ──────────────────────────────────────────────────────────────────
class CandidateStore:
    """候选库：append-only JSONL + 内容寻址快照目录。

    目录布局（全部落在调用方给定的 root 下，没有默认路径，永不碰正式记忆库）::

        <root>/candidates.jsonl   候选账本（后出现的记录覆盖先前的同 ID 状态）
        <root>/sources.jsonl      来源元数据
        <root>/reviews.jsonl      每次审查尝试的审计轨迹
        <root>/sources/<sha>.txt  原始快照（按内容哈希寻址）

    没有网络、没有后台线程、没有定时器；``clock`` 只为审计时间可注入。
    """

    def __init__(self, root, *, clock: Optional[Callable[[], str]] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._snapshot_dir = self.root / "sources"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._candidates_path = self.root / "candidates.jsonl"
        self._sources_path = self.root / "sources.jsonl"
        self._reviews_path = self.root / "reviews.jsonl"
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        self._candidates: Dict[str, Candidate] = {}
        self._sources: Dict[str, SourceRecord] = {}
        self._attempts: List[dict] = []
        self._load()

    # ── 读取 ──
    def __len__(self) -> int:
        return len(self._candidates)

    def get(self, candidate_id: str) -> Candidate:
        with self._lock:
            return self._require(candidate_id)

    def list(self, *, status: Optional[str] = None,
             domain: Optional[str] = None) -> Tuple[Candidate, ...]:
        if status is not None and status not in STATUSES:
            raise InvalidCandidate("未知状态：%r" % (status,))
        wanted_domain = normalize_domain(domain) if domain is not None else None
        with self._lock:
            items = [c for c in self._candidates.values()
                     if (status is None or c.review_status == status)
                     and (wanted_domain is None or c.domain == wanted_domain)]
        return tuple(sorted(items, key=lambda c: c.candidate_id))

    def sources(self) -> Tuple[SourceRecord, ...]:
        with self._lock:
            return tuple(sorted(self._sources.values(), key=lambda s: s.source_id))

    def source(self, source_id: str) -> SourceRecord:
        with self._lock:
            record = self._sources.get(source_id)
        if record is None:
            raise UnknownSource("未知来源：%s" % source_id)
        return record

    def snapshot_path(self, source_id: str) -> Path:
        record = self.source(source_id)
        return self._snapshot_path(record.source_hash)

    def snapshot_text(self, source_id: str) -> Optional[str]:
        record = self.source(source_id)
        return self._read_snapshot(record.source_hash)

    def attempts(self, candidate_id: Optional[str] = None) -> Tuple[dict, ...]:
        """审查审计轨迹（含被挡下的尝试）。"""
        with self._lock:
            rows = list(self._attempts)
        if candidate_id is not None:
            rows = [row for row in rows if row.get("candidate_id") == candidate_id]
        return tuple(rows)

    # ── 写入：灌入 ──
    def ingest(self, *, statement, literal_quote, source_url, retrieved_at, domain,
               snapshot, source_hash=None, subject=None, author=None,
               review_status: str = STATUS_PENDING) -> IngestResult:
        """把一份已抓到的快照变成候选；证据不合格即抛异常，不落盘。

        ``review_status`` 只允许 ``pending`` —— 创建路径**没有**审批能力，
        否则「先入库后审查」的捷径会立刻被用起来。
        """
        if review_status != STATUS_PENDING:
            raise InvalidCandidate(
                "候选创建时只能是 %s，实际为 %r" % (STATUS_PENDING, review_status))

        text = self._decode_snapshot(snapshot)
        computed_hash = _hash_text(text)
        if source_hash is not None:
            declared = _require_text(source_hash, "source_hash", limit=128).strip().lower()
            if declared != computed_hash:
                raise EvidenceMismatch(
                    "source_hash 与快照实际哈希不一致：声明 %s，实际 %s"
                    % (declared, computed_hash))
        url = normalize_source_url(source_url)
        retrieved = canonical_timestamp(retrieved_at)
        normalized_domain = normalize_domain(domain)

        statement = _require_text(statement, "statement",
                                  limit=_MAX_STATEMENT_CHARS).strip()
        literal_quote = _require_text(literal_quote, "literal_quote",
                                      limit=_MAX_QUOTE_CHARS)
        if not quote_grounded(text, literal_quote):
            raise QuoteNotGrounded(
                "引文无法在来源快照中逐字定位（不做归一化匹配）；拒绝写入候选")

        subject_norm = None
        if subject is not None:
            subject_norm = _require_text(subject, "subject", limit=200).strip()
        author_norm = _normalize_identity(author, "author") if author is not None else None

        source_id = source_identity(url, computed_hash)
        candidate_id = _candidate_identity(
            source_id=source_id, domain=normalized_domain,
            claim_key=_canonical(subject_norm or statement),
            statement=statement, literal_quote=literal_quote)
        candidate = Candidate(
            candidate_id=candidate_id,
            statement=statement,
            literal_quote=literal_quote,
            source_url=url,
            source_hash=computed_hash,
            retrieved_at=retrieved,
            domain=normalized_domain,
            review_status=STATUS_PENDING,
            reviewer=None,
            reviewed_at=None,
            subject=subject_norm,
            source_id=source_id,
            author=author_norm,
            review_note="",
        )

        with self._lock:
            existing = self._candidates.get(candidate_id)
            if existing is not None:
                return IngestResult(candidate=existing, created=False,
                                    source_created=False, snapshot_created=False)
            snapshot_created = self._write_snapshot(computed_hash, text)
            source_created = source_id not in self._sources
            if source_created:
                record = SourceRecord(source_id=source_id, source_url=url,
                                      source_hash=computed_hash,
                                      retrieved_at=retrieved,
                                      byte_size=len(text.encode("utf-8")))
                self._sources[source_id] = record
                self._append_jsonl(self._sources_path, record.to_dict())
            self._candidates[candidate_id] = candidate
            self._append_jsonl(self._candidates_path, candidate.to_dict())
        return IngestResult(candidate=candidate, created=True,
                            source_created=source_created,
                            snapshot_created=snapshot_created)

    # ── 写入：审查 ──
    def review(self, candidate_id: str, *, reviewer: str, decision: str,
               note: str = "", now: Optional[str] = None) -> Candidate:
        """独立审查。approve 前会重新验证快照/哈希/引文/冲突。

        ``reviewer`` 必填且不得是自动化身份或候选作者本人；缺失或冲突证据
        一律抛 :class:`ReviewBlocked`，候选保持不可入库状态。
        """
        reviewer = self._reviewer_identity(reviewer)
        decision_norm = (decision or "").strip().lower()
        if decision_norm not in (DECISION_APPROVE, DECISION_REJECT):
            raise InvalidCandidate("decision 只能是 approve 或 reject：%r" % (decision,))
        reviewed_at = canonical_timestamp(now, "reviewed_at") if now else self._clock()
        note = _require_text(note, "note", limit=2000, allow_blank=True)

        with self._lock:
            candidate = self._require(candidate_id)
            if candidate.is_terminal:
                raise CandidateError(
                    "候选 %s 已终结于 %s，不能再次审查" % (candidate_id,
                                                         candidate.review_status))
            if candidate.author and _canonical(candidate.author) == _canonical(reviewer):
                raise ReviewerNotIndependent(
                    "审查者 %r 与候选作者是同一身份，违反独立审查" % reviewer)

            if decision_norm == DECISION_APPROVE:
                reasons = tuple(self._approval_blockers(candidate))
                if reasons:
                    status = (STATUS_CONFLICTED
                              if any(r.startswith("conflicting_evidence") for r in reasons)
                              else candidate.review_status)
                    blocked = replace(candidate, review_status=status)
                    if status != candidate.review_status:
                        self._save(blocked)
                    self._record_attempt(candidate_id, decision_norm, reviewer,
                                         reviewed_at, "blocked", reasons, note)
                    raise ReviewBlocked(reasons, blocked)
                updated = replace(candidate, review_status=STATUS_APPROVED,
                                  reviewer=reviewer, reviewed_at=reviewed_at,
                                  review_note=note)
                outcome = "approved"
            else:
                updated = replace(candidate, review_status=STATUS_REJECTED,
                                  reviewer=reviewer, reviewed_at=reviewed_at,
                                  review_note=note)
                outcome = "rejected"

            self._save(updated)
            self._record_attempt(candidate_id, decision_norm, reviewer, reviewed_at,
                                 outcome, (), note)
            return updated

    def revoke(self, candidate_id: str, *, reviewer: str, reason: str,
               now: Optional[str] = None) -> Candidate:
        """撤回一条 **approved** 候选：状态置 ``revoked``，附具名审查者与理由。

        撤销是终态且**不可入库**：晋升只接受 ``approved``，因此撤销后
        :func:`~agentcore.learning.promotion.promote_candidate` 会在打开事实库
        之前拒绝。未批准的候选请用 ``reject``；重复撤销或再审查一律拒绝。
        """
        reviewer = self._reviewer_identity(reviewer)
        reason = _require_text(reason, "reason", limit=2000).strip()
        revoked_at = canonical_timestamp(now, "reviewed_at") if now else self._clock()
        with self._lock:
            candidate = self._require(candidate_id)
            if candidate.review_status in (STATUS_REVOKED, STATUS_REJECTED):
                raise CandidateError(
                    "候选 %s 已终结于 %s，不能再次撤销"
                    % (candidate_id, candidate.review_status))
            if candidate.review_status != STATUS_APPROVED:
                raise CandidateError(
                    "候选 %s 状态为 %s，只有 %s 才能撤销"
                    % (candidate_id, candidate.review_status, STATUS_APPROVED))
            if candidate.author and _canonical(candidate.author) == _canonical(reviewer):
                raise ReviewerNotIndependent(
                    "审查者 %r 与候选作者是同一身份，不能撤销" % reviewer)
            updated = replace(candidate, review_status=STATUS_REVOKED,
                              reviewer=reviewer, reviewed_at=revoked_at,
                              review_note=reason)
            self._save(updated)
            self._record_attempt(candidate_id, DECISION_REVOKE, reviewer,
                                 revoked_at, "revoked", (), reason)
            return updated

    def escalate(self, candidate_id: str, *, reviewer: str, reason: str,
                 now: Optional[str] = None) -> Candidate:
        """证据不足/有冲突时转人工：状态置 ``needs_human``，不改变证据本身。"""
        reviewer = self._reviewer_identity(reviewer)
        reason = _require_text(reason, "reason", limit=2000).strip()
        escalated_at = canonical_timestamp(now, "reviewed_at") if now else self._clock()
        with self._lock:
            candidate = self._require(candidate_id)
            if candidate.is_terminal:
                raise CandidateError(
                    "候选 %s 已终结于 %s，不能转人工" % (candidate_id,
                                                       candidate.review_status))
            updated = replace(candidate, review_status=STATUS_NEEDS_HUMAN,
                              reviewer=reviewer, reviewed_at=escalated_at,
                              review_note=reason)
            self._save(updated)
            self._record_attempt(candidate_id, "escalate", reviewer, escalated_at,
                                 "needs_human", (reason,), reason)
            return updated

    # ── 复审 ──
    def recheck(self, candidate_id: str) -> GroundingReport:
        """只读复核：快照是否仍在、哈希是否未变、引文是否仍逐字可定位。"""
        with self._lock:
            candidate = self._require(candidate_id)
            source_present = candidate.source_id in self._sources
            record = self._sources.get(candidate.source_id)
            text = self._read_snapshot(record.source_hash) if record else None
        reasons: List[str] = []
        if not source_present:
            reasons.append("missing_source_record")
        if text is None:
            reasons.append("missing_source_snapshot")
        hash_matches = bool(text is not None and record is not None
                            and _hash_text(text) == candidate.source_hash
                            and record.source_hash == candidate.source_hash)
        if text is not None and not hash_matches:
            reasons.append("source_hash_mismatch")
        grounded = bool(text is not None and quote_grounded(text, candidate.literal_quote))
        if text is not None and not grounded:
            reasons.append("quote_not_grounded")
        return GroundingReport(
            candidate_id=candidate_id,
            ok=not reasons,
            source_present=source_present,
            snapshot_present=text is not None,
            hash_matches=hash_matches,
            quote_grounded=grounded,
            reasons=tuple(reasons),
        )

    # ── 内部：审批门禁 ──
    def _approval_blockers(self, candidate: Candidate) -> List[str]:
        reasons: List[str] = []
        record = self._sources.get(candidate.source_id)
        if record is None:
            reasons.append("missing_source_record")
        if not candidate.evidence_complete:
            reasons.append("incomplete_evidence")
        text = self._read_snapshot(record.source_hash) if record else None
        if text is None:
            reasons.append("missing_source_snapshot")
        else:
            if (_hash_text(text) != candidate.source_hash
                    or record.source_hash != candidate.source_hash):
                reasons.append("source_hash_mismatch")
            if not quote_grounded(text, candidate.literal_quote):
                reasons.append("quote_not_grounded")
        if record is not None and record.source_url != candidate.source_url:
            reasons.append("source_identity_mismatch")

        conflicts = sorted(
            other.candidate_id for other in self._candidates.values()
            if other.candidate_id != candidate.candidate_id
            and other.domain == candidate.domain
            and other.claim_key == candidate.claim_key
            and other.statement_fingerprint != candidate.statement_fingerprint
            and other.review_status in _CONFLICTING_STATUSES)
        if conflicts:
            reasons.append("conflicting_evidence:" + ",".join(conflicts))
        return reasons

    # ── 内部：持久化 ──
    def _require(self, candidate_id: str) -> Candidate:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise UnknownCandidate("未知候选：%s" % candidate_id)
        return candidate

    def _save(self, candidate: Candidate) -> None:
        self._candidates[candidate.candidate_id] = candidate
        self._append_jsonl(self._candidates_path, candidate.to_dict())

    def _record_attempt(self, candidate_id: str, decision: str, reviewer: str,
                        at: str, outcome: str, reasons, note: str) -> None:
        self._attempts.append({
            "candidate_id": candidate_id,
            "decision": decision,
            "reviewer": reviewer,
            "at": at,
            "outcome": outcome,
            "reasons": list(reasons),
            "note": note,
        })
        self._append_jsonl(self._reviews_path, self._attempts[-1])

    def _reviewer_identity(self, reviewer) -> str:
        identity = _normalize_identity(reviewer, "reviewer")
        if _canonical(identity) in _AUTOMATED_REVIEWERS:
            raise AutoApprovalForbidden(
                "审查者 %r 是自动化身份，不能给出审批结论；缺证据/冲突时转人工" % identity)
        return identity

    def _snapshot_path(self, source_hash: str) -> Path:
        return self._snapshot_dir / ("%s.txt" % source_hash)

    def _read_snapshot(self, source_hash: str) -> Optional[str]:
        path = self._snapshot_path(source_hash)
        try:
            return path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as exc:
            raise EvidenceMismatch("快照不是合法 UTF-8：%s" % path) from exc

    def _write_snapshot(self, source_hash: str, text: str) -> bool:
        path = self._snapshot_path(source_hash)
        payload = text.encode("utf-8")
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def _decode_snapshot(self, snapshot) -> str:
        if isinstance(snapshot, bytes):
            try:
                text = snapshot.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidCandidate("snapshot 必须是 UTF-8 文本") from exc
        elif isinstance(snapshot, str):
            text = snapshot
        else:
            raise InvalidCandidate("snapshot 必须是 str 或 bytes")
        if not text.strip():
            raise InvalidCandidate("snapshot 不能为空：没有原文就没有逐字证据")
        if len(text.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            raise InvalidCandidate("snapshot 超过大小上限 %d 字节" % _MAX_SNAPSHOT_BYTES)
        return text

    @staticmethod
    def _append_jsonl(path: Path, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load(self) -> None:
        for lineno, row in self._iter_jsonl(self._sources_path):
            record = SourceRecord.from_dict(row)
            self._sources[record.source_id] = record
        for lineno, row in self._iter_jsonl(self._candidates_path):
            candidate = Candidate.from_dict(row)
            # 后出现的记录是该 ID 的最新状态（审查是 append-only 的事件流）。
            self._candidates[candidate.candidate_id] = candidate
        for _, row in self._iter_jsonl(self._reviews_path):
            self._attempts.append(row)

    @staticmethod
    def _iter_jsonl(path: Path):
        try:
            handle = open(path, "r", encoding="utf-8")
        except FileNotFoundError:
            return
        with handle:
            for lineno, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CandidateError("账本损坏：%s:%d" % (path, lineno)) from exc
                if not isinstance(row, dict):
                    raise CandidateError("账本损坏：%s:%d 不是 JSON 对象" % (path, lineno))
                yield lineno, row

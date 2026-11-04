#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automatic, evidence-aware admission over the canonical turn queue.

This module is the P1 orchestration boundary. It deliberately reuses the
existing terminal spool, ``TurnEventQueue`` lease worker, extractor, conflict
resolver and fact store. It adds no scheduler and no second fact database.

The dialogue is untrusted data. Model output is a proposal, not a verdict. The
deterministic review below is versioned and host-owned; model confidence tags
are ignored. A candidate is admitted, rejected, or quarantined (deferred), and
every outcome is audited without copying candidate text into logs/reports.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import memory_store, memory_trigger
from .config import get_config
from .memory_admission import has_credential_leak
from .memory_kind import classify_kind
from .runtime_bridge import TurnEventQueue
from .turn_spool import is_automated_turn

POLICY_VERSION = "memory_autonomy_policy/v1"
AUDIT_SCHEMA_VERSION = "memory_autonomy_audit/v1"

ADMIT = "admit"
REJECT = "reject"
DEFER = "defer"

_DURABLE_PREFERENCE = re.compile(
    r"以后|今后|长期|永久|始终|每次|默认|一律|所有(?:回答|回复|文档|输出)?|"
    r"我(?:喜欢|习惯|偏好|不喜欢|讨厌|反感)|"
    r"(?:回答|回复|文档|沟通|称呼|工具).{0,16}(?:简洁|清楚|详细|中文|英文|讲人话)|"
    r"(?:请|要|必须|应当).{0,12}(?:记住|遵守|保持|采用)",
    re.IGNORECASE,
)
_DECISION = re.compile(r"决定|定了|已定|拍板|采用|改用|改成|换成|确定用|已确认", re.IGNORECASE)
_TEMPORARY = re.compile(
    r"本轮|这轮|本次|这次|此次|当前问题|这个问题|暂时|今天先|"
    r"这(?:次|轮).{0,18}(?:不要|别|不用|只|仅)|"
    r"\bthis (?:turn|reply|request)\b|\bfor now\b",
    re.IGNORECASE,
)
_EXTERNAL_ATTRIBUTION = re.compile(
    r"(?:文档|网页|文章|资料|别人|有人|助手|模型|对方|网上).{0,20}"
    r"(?:说|建议|认为|写到|声称|主张)|据说|据称|引用|转述|假设|例如|比如",
    re.IGNORECASE,
)
_CLAIMED_EXECUTION = re.compile(
    r"实测|已验证|验证通过|测试通过|复现|运行结果|执行结果|根因是|结论是",
    re.IGNORECASE,
)
_QUESTION_TAIL = re.compile(r"(?:^|[\s。！!])[^。！!\n]{0,80}[?？]\s*$")
_QUESTION_START = re.compile(r"^(?:为什么|怎么|如何|是否|能不能|可不可以|有没有)")
_UNCERTAIN = re.compile(
    r"可能|也许|或许|大概|似乎|好像|还没(?:确定|想好|决定)|尚未(?:确定|决定)|"
    r"待定|不确定|需要再(?:想|考虑)|\b(?:maybe|perhaps|might)\b",
    re.IGNORECASE)
_QUOTED_REJECTION = re.compile(
    r"(?:不能|不要|别|不得).{0,12}(?:把|将).{0,20}(?:当成|当作|算作).{0,20}(?:我的决定|我(?:的)?偏好)")
_NEGATION_OF_COMMITMENT = re.compile(
    r"我不同意|我不接受|我没确认|还没决定|未决定|不是我的决定|不能当成我的决定",
    re.IGNORECASE,
)


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reason(code: str) -> str:
    """Return a bounded stable reason code, never arbitrary model text."""
    value = str(code or "unknown").strip().lower()
    return value if re.fullmatch(r"[a-z0-9_]{1,64}", value) else "unknown"


def _valid_receipt(receipt: Any, *, statement: str, event: Dict[str, Any]) -> bool:
    """Accept only a successful host receipt bound to this event and statement."""
    if not isinstance(receipt, dict):
        return False
    if receipt.get("success") is not True:
        return False
    exit_code = receipt.get("exit_code")
    if exit_code not in (None, 0) or isinstance(exit_code, bool):
        return False
    event_key = str(receipt.get("event_key") or receipt.get("event_id") or "")
    if event_key != str(event.get("idempotency_key") or ""):
        return False
    statement_sha = hashlib.sha256(statement.encode("utf-8")).hexdigest()
    if receipt.get("statement_sha256") != statement_sha:
        return False
    required = ("commit", "task_id", "run_id", "artifact_sha256")
    return any(isinstance(receipt.get(key), str) and receipt.get(key).strip()
               for key in required)


def _quote_context(user_message: str, quote: str) -> str:
    """Return complete sentence segments containing the literal quote.

    A literal substring is not enough evidence when the same sentence carries a
    negation, uncertainty marker, or question qualification.
    """
    message = str(user_message or "")
    value = str(quote or "").strip()
    if not value or value not in message:
        return ""
    delimiters = "。！？?!\n"
    contexts = []
    start = 0
    while True:
        index = message.find(value, start)
        if index < 0:
            break
        left = max((message.rfind(ch, 0, index) for ch in delimiters), default=-1) + 1
        ends = [message.find(ch, index + len(value)) for ch in delimiters]
        right = min((end for end in ends if end >= 0), default=len(message) - 1)
        contexts.append(message[left:right + 1].strip())
        start = index + max(1, len(value))
    return "\n".join(item for item in contexts if item)


_SUBJECT_ALIAS_TERMS = (
    "门限", "阈值", "脱窗", "时序", "状态机", "跳转", "注入", "触发条件",
    "编译", "构建", "镜像", "制品", "用例", "断言", "matcher", "topic",
)


def derive_candidate_metadata(candidate: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    """Derive store metadata without trusting model-generated subject/tags/ebm.

    Topic aliases are extracted deterministically from the anchored statement.
    They let a short exact query such as ``门限`` address the fact without
    weakening global semantic recall thresholds or trusting model metadata.
    """
    statement = str((candidate or {}).get("statement") or "").strip()
    cfg = get_config()
    ebm = cfg.unknown_bucket
    match = re.search(cfg.era_pattern, statement, re.IGNORECASE)
    if match:
        ebm = "%s%s" % (cfg.era_prefix, match.group(1))
    subject = statement[:40]
    aliases = []
    for term in _SUBJECT_ALIAS_TERMS:
        if term.lower() in statement.lower() and term not in aliases:
            aliases.append(term)
    tags = {
        "profile": str(event.get("profile") or ""),
        "source_trust": "completed_user_turn",
        "source_event_sha256": str(event.get("content_sha256") or ""),
        "autonomy_policy": POLICY_VERSION,
    }
    for index, alias in enumerate(aliases[:8]):
        tags["subject_alias_%d" % index] = alias.lower()
    return {"statement": statement, "subject": subject, "ebm": ebm,
            "tags": tags}


def review_candidate(candidate: Dict[str, Any], event: Dict[str, Any], *,
                     source_receipt: Optional[Dict[str, Any]] = None,
                     user_context: Optional[str] = None) -> Dict[str, Any]:
    """Deterministically admit/reject/defer one evidence-bound candidate.

    The exact user quote established by ``propose_facts`` is source evidence,
    not truth. Technical execution claims require a successful host receipt
    bound to both this event and this exact statement.
    """
    statement = str((candidate or {}).get("statement") or "").strip()
    context = str(user_context or statement).strip()
    if not statement:
        return {"decision": REJECT, "reason_code": "empty_candidate"}
    if has_credential_leak(statement) or has_credential_leak(context):
        return {"decision": REJECT, "reason_code": "credential_leak"}
    # Query strings are common in durable technical decisions (URLs). Only a
    # question formed by the statement's final segment is rejected.
    if (_QUESTION_START.search(context) or _QUESTION_TAIL.search(context) or
            _QUESTION_START.search(statement) or _QUESTION_TAIL.search(statement)):
        return {"decision": REJECT, "reason_code": "question_not_fact"}
    if (_NEGATION_OF_COMMITMENT.search(context) or _QUOTED_REJECTION.search(context) or
            _NEGATION_OF_COMMITMENT.search(statement) or _QUOTED_REJECTION.search(statement)):
        return {"decision": REJECT, "reason_code": "unconfirmed_or_rejected"}
    uncertain_text = context if _UNCERTAIN.search(context) else statement
    uncertain_normalized = re.sub(r"尽可能", "", uncertain_text)
    if _UNCERTAIN.search(uncertain_normalized):
        return {"decision": DEFER, "reason_code": "uncertain_not_confirmed"}
    if (_TEMPORARY.search(context) or memory_trigger.turn_scoped_constraint(context) or
            _TEMPORARY.search(statement) or memory_trigger.turn_scoped_constraint(statement)):
        return {"decision": REJECT, "reason_code": "turn_scoped_instruction"}
    if _EXTERNAL_ATTRIBUTION.search(context) or _EXTERNAL_ATTRIBUTION.search(statement):
        return {"decision": DEFER, "reason_code": "external_attribution_untrusted"}

    kind = classify_kind(statement)
    durable_preference = bool(_DURABLE_PREFERENCE.search(statement))
    explicit_decision = bool(_DECISION.search(statement))
    has_receipt = _valid_receipt(source_receipt, statement=statement, event=event)
    # A claimed execution result never gains authority from the wording of the
    # decision itself. Receipt binding is checked before durable phrasing.
    if _CLAIMED_EXECUTION.search(statement):
        if has_receipt:
            return {"decision": ADMIT, "reason_code": "execution_receipt_attached",
                    "kind": kind, "evidence_basis": "execution_receipt"}
        return {"decision": DEFER, "reason_code": "technical_evidence_missing",
                "kind": kind, "evidence_basis": "unverified"}
    if durable_preference:
        return {"decision": ADMIT, "reason_code": "explicit_durable_preference",
                "kind": kind, "evidence_basis": "user_decision"}
    if explicit_decision:
        return {"decision": ADMIT, "reason_code": "explicit_user_decision",
                "kind": kind, "evidence_basis": "user_decision"}
    if kind == "technical":
        return {"decision": DEFER, "reason_code": "technical_evidence_missing",
                "kind": kind, "evidence_basis": "unverified"}
    return {"decision": DEFER, "reason_code": "durability_not_established",
            "kind": kind, "evidence_basis": "unverified"}


class FrozenReviewStore:
    """Private, append-only frozen extraction/review payloads.

    Files are mode 0600 beneath a mode 0700 directory. Credential candidates are
    redacted before persistence. No payload is written to logs or reports.
    """

    def __init__(self, queue: TurnEventQueue):
        self.root = queue.path.parent / "memory_autonomy_frozen"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _path(self, event_key: str) -> Path:
        digest = hashlib.sha256(
            ("%s\0%s" % (event_key, POLICY_VERSION)).encode("utf-8")).hexdigest()
        return self.root / (digest + ".json")

    def load(self, event_key: str, *,
             event_content_sha256: Optional[str] = None,
             identity_sha256: Optional[str] = None) -> Optional[Dict[str, Any]]:
        path = self._path(event_key)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (payload.get("schema") != "memory_autonomy_frozen/v1"
                or payload.get("policy_version") != POLICY_VERSION):
            raise ValueError("frozen_review_corrupt")
        expected = hashlib.sha256(
            json.dumps(payload.get("reviews"), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
        if payload.get("event_key") != event_key or payload.get("digest") != expected:
            raise ValueError("frozen_review_corrupt")
        if event_content_sha256 is not None and payload.get("event_content_sha256") != event_content_sha256:
            raise ValueError("frozen_review_event_mismatch")
        if identity_sha256 is not None and payload.get("identity_sha256") != identity_sha256:
            raise ValueError("frozen_review_identity_mismatch")
        reviews = payload.get("reviews")
        if not isinstance(reviews, dict):
            raise ValueError("frozen_review_corrupt")
        return {"schema": payload.get("schema"),
                "policy_version": payload.get("policy_version"),
                "event_key": event_key,
                "event_content_sha256": payload.get("event_content_sha256"),
                "identity_sha256": payload.get("identity_sha256"),
                "digest": payload.get("digest"), **reviews}

    def freeze(self, event_key: str, review: Dict[str, Any], *,
               event_content_sha256: str, identity_sha256: str) -> Dict[str, Any]:
        path = self._path(event_key)
        if not isinstance(review, dict):
            raise ValueError("frozen_review_invalid")
        if not event_content_sha256 or not identity_sha256:
            raise ValueError("frozen_review_binding_required")
        body = json.dumps(review, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        payload = {"schema": "memory_autonomy_frozen/v1", "policy_version": POLICY_VERSION,
                   "event_key": event_key, "reviews": review,
                   "event_content_sha256": event_content_sha256,
                   "identity_sha256": identity_sha256,
                   "digest": hashlib.sha256(body.encode("utf-8")).hexdigest()}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        if path.exists():
            existing = self.load(event_key,
                                 event_content_sha256=event_content_sha256,
                                 identity_sha256=identity_sha256)
            expected_loaded = {
                "schema": payload["schema"],
                "policy_version": payload["policy_version"],
                "event_key": event_key,
                "event_content_sha256": event_content_sha256,
                "identity_sha256": identity_sha256,
                "digest": payload["digest"],
                **review,
            }
            if existing != expected_loaded:
                raise ValueError("frozen_review_conflict")
            return review
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".review-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
            except FileExistsError:
                existing = self.load(event_key,
                                     event_content_sha256=event_content_sha256,
                                     identity_sha256=identity_sha256)
                expected_loaded = {
                    "schema": payload["schema"],
                    "policy_version": payload["policy_version"],
                    "event_key": event_key,
                    "event_content_sha256": event_content_sha256,
                    "identity_sha256": identity_sha256,
                    "digest": payload["digest"],
                    **review,
                }
                if existing != expected_loaded:
                    raise ValueError("frozen_review_conflict")
        finally:
            if os.path.exists(name):
                os.unlink(name)
        try:
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
        return review


class AutonomyAudit:
    """Small audit table in the canonical event queue database.

    It stores event/candidate digests and stable reason codes only. Candidate
    statements, quotes, credentials and exception text never enter this table
    or ordinary logs.
    """

    def __init__(self, queue: TurnEventQueue):
        self.queue = queue
        self._ensure_schema()

    def _connect(self):
        return self.queue._connect()

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_autonomy_audit ("
                " event_key TEXT NOT NULL, candidate_id TEXT NOT NULL,"
                " policy_version TEXT NOT NULL, schema_version TEXT NOT NULL,"
                " event_content_sha256 TEXT NOT NULL, identity_sha256 TEXT NOT NULL,"
                " decision TEXT NOT NULL, reason_code TEXT NOT NULL,"
                " final_decision TEXT, final_reason_code TEXT,"
                " fact_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,"
                " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
                " PRIMARY KEY(event_key,candidate_id,policy_version))")
            existing = {row["name"] for row in conn.execute(
                "PRAGMA table_info(memory_autonomy_audit)")}
            for name, declaration in (("final_decision", "TEXT"),
                                      ("final_reason_code", "TEXT")):
                if name not in existing:
                    conn.execute('ALTER TABLE memory_autonomy_audit ADD COLUMN "%s" %s'
                                 % (name, declaration))
            conn.execute(
                "CREATE INDEX IF NOT EXISTS memory_autonomy_audit_decision"
                " ON memory_autonomy_audit(decision,updated_at)")
            conn.commit()
        finally:
            conn.close()

    def record(self, *, event_key: str, event_content_sha256: str,
               event: Dict[str, Any], candidate: Dict[str, Any],
               decision: str, reason_code: str, fact_id: Optional[str] = None) -> None:
        candidate_digest = _digest(candidate)
        identity = {key: event.get(key) for key in ("profile", "user_id", "agent_id")}
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO memory_autonomy_audit"
                " (event_key,candidate_id,policy_version,schema_version,"
                "  event_content_sha256,identity_sha256,decision,reason_code,"
                "  final_decision,final_reason_code,fact_id,attempts,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)"
                " ON CONFLICT(event_key,candidate_id,policy_version) DO UPDATE SET"
                "  final_decision=excluded.final_decision,"
                "  final_reason_code=excluded.final_reason_code,"
                "  fact_id=COALESCE(memory_autonomy_audit.fact_id,excluded.fact_id),"
                "  attempts=memory_autonomy_audit.attempts+1, updated_at=excluded.updated_at",
                (event_key, candidate_digest, POLICY_VERSION, AUDIT_SCHEMA_VERSION,
                 event_content_sha256, _digest(identity), decision,
                 _reason(reason_code), decision, _reason(reason_code), fact_id,
                 now, now))
            conn.commit()
        finally:
            conn.close()

    def rows(self) -> list:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM memory_autonomy_audit ORDER BY updated_at,event_key,candidate_id")]
        finally:
            conn.close()


class AutonomousMemoryPipeline:
    """Turn-event runner: extract -> review -> conflict -> write/index -> recall."""

    def __init__(self, *, queue: Optional[TurnEventQueue] = None,
                 model: Optional[Callable[..., Any]] = None,
                 receipt_resolver: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None):
        self.queue = queue
        self._model = model
        self._receipt_resolver = receipt_resolver
        self.model_calls = 0
        self._audit = AutonomyAudit(queue) if queue is not None else None
        self._frozen = FrozenReviewStore(queue) if queue is not None else None

    def _content(self, messages, **options) -> str:
        timeout = float(options.get("timeout_seconds", 45.0))
        if self._model is not None:
            # The injected candidate model is a small adapter used by
            # propose_facts; it accepts timeout_seconds only.
            self.model_calls += 1
            result = self._model(messages, timeout_seconds=timeout)
        else:
            chat = get_config().resolve_llm()
            if not callable(chat):
                raise RuntimeError("provider_unavailable")
            self.model_calls += 1
            result = chat(messages, timeout=timeout, max_tokens=800, temperature=0.0)
        if isinstance(result, str):
            return result
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            return result["content"]
        raise RuntimeError("invalid_model_result")

    def _event_audit(self, event, candidate, decision, reason_code, fact_id=None):
        if self._audit is not None:
            self._audit.record(
                event_key=str(event.get("idempotency_key") or ""),
                event_content_sha256=str(event.get("content_sha256") or ""),
                event=event, candidate=candidate, decision=decision,
                reason_code=reason_code, fact_id=fact_id)

    def _frozen_candidates(self, event: Dict[str, Any]) -> Dict[str, Any]:
        key = str(event.get("idempotency_key") or "")
        if not key:
            raise ValueError("event_key_required")
        if self._frozen is None:
            return {"candidates": [], "transient": True}
        identity = {name: event.get(name)
                    for name in ("profile", "user_id", "agent_id")}
        identity_digest = _digest(identity)
        frozen = self._frozen.load(
            key, event_content_sha256=str(event.get("content_sha256") or ""),
            identity_sha256=identity_digest)
        if frozen is not None:
            return frozen
        proposed = memory_trigger.propose_facts(
            event.get("user_message") or "",
            event.get("assistant_reply") or "",
            model=self._content,
            turn_status="completed",
        )
        if proposed.get("ok") is not True:
            reason = _reason(proposed.get("reason") or "extraction_failed")
            transient = {
                "model_call_failed", "candidate_model_failed", "provider_unavailable",
                "model_invalid_json", "model_invalid_facts", "model_invalid_fact_entry",
                "model_output_truncated",
            }
            # Backend/extraction failures are retryable and must not freeze a
            # permanently failed review before the provider recovers.
            if reason in transient:
                return {"proposed_ok": False, "proposed_reason": reason,
                        "candidates": [], "transient_failure": True}
        raw = [c for c in proposed.get("candidates") or [] if isinstance(c, dict)]
        safe = []
        for candidate in raw:
            statement = str(candidate.get("statement") or "").strip()
            if has_credential_leak(statement):
                # Never persist a secret-bearing candidate. The reject outcome is
                # stable and needs no retry.
                safe.append({"redacted": True, "reason_code": "credential_leak"})
            else:
                safe.append({"statement": statement,
                             "user_quote": str(candidate.get("user_quote") or "").strip()})
        review = {
            "proposed_ok": bool(proposed.get("ok")),
            "proposed_reason": _reason(proposed.get("reason") or ""),
            "candidates": safe,
        }
        self._frozen.freeze(
            key, review,
            event_content_sha256=str(event.get("content_sha256") or ""),
            identity_sha256=identity_digest)
        return review

    def _verify_index(self, fact_id: str, event: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
        return memory_store.ensure_fact_index(
            fact_id, user_id=event.get("user_id"), agent_id=event.get("agent_id"),
            subject=metadata["subject"], statement=metadata["statement"],
            expected_statuses=("candidate", "active"))

    def _verify_recall(self, fact_id: str, event: Dict[str, Any],
                       metadata: Dict[str, Any]) -> bool:
        verify = memory_store.recall_facts(
            metadata["subject"] or metadata["statement"][:120],
            user_id=event.get("user_id"), agent_id=event.get("agent_id"),
            profile=event.get("profile"), ebm=metadata["ebm"] or None, limit=20)
        return (verify.get("ok") is True
                and fact_id in [str(row.get("fact_id") or "")
                                for row in verify.get("results") or []])

    def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if event.get("status") != "completed":
            return {"ok": False, "retryable": False, "reason": "turn_not_completed",
                    "written": 0, "deferred": 0, "rejected": 0,
                    "model_calls": self.model_calls}
        if is_automated_turn(event.get("turn_id")):
            return {"ok": True, "skipped": True, "reason": "automated_turn",
                    "written": 0, "deferred": 0, "rejected": 0,
                    "model_calls": self.model_calls}
        try:
            frozen = self._frozen_candidates(event)
        except Exception:
            return {"ok": False, "retryable": True, "reason": "extraction_failed",
                    "written": 0, "deferred": 0, "rejected": 0,
                    "model_calls": self.model_calls}
        if frozen.get("transient") is True:
            return {"ok": False, "retryable": False,
                    "reason": "autonomy_queue_required", "written": 0,
                    "deferred": 0, "rejected": 0, "model_calls": self.model_calls}
        if not frozen.get("proposed_ok", True):
            reason = _reason(frozen.get("proposed_reason") or "extraction_failed")
            return {"ok": False, "retryable": reason in {
                        "model_call_failed", "candidate_model_failed", "provider_unavailable",
                        "model_invalid_json", "model_invalid_facts", "model_invalid_fact_entry",
                        "model_output_truncated"}, "reason": reason,
                    "written": 0, "deferred": 0, "rejected": 0,
                    "model_calls": self.model_calls}
        candidates = frozen.get("candidates")
        if not isinstance(candidates, list):
            return {"ok": False, "retryable": False, "reason": "frozen_review_invalid",
                    "written": 0, "deferred": 0, "rejected": 0,
                    "model_calls": self.model_calls}
        if not candidates:
            return {"ok": True, "skipped": True,
                    "reason": _reason(frozen.get("proposed_reason") or "no_fact_extracted"),
                    "written": 0, "deferred": 0, "rejected": 0,
                    "model_calls": self.model_calls}

        receipt = None
        if self._receipt_resolver is not None:
            try:
                receipt = self._receipt_resolver(event)
            except Exception:
                receipt = None
        written = deferred = rejected = 0
        retryable = False
        results = []
        source = "turn:" + str(event.get("idempotency_key") or "")
        for candidate in candidates:
            statement = str(candidate.get("statement") or "").strip()
            if candidate.get("redacted"):
                rejected += 1
                self._event_audit(event, candidate, REJECT, "credential_leak")
                results.append({"decision": REJECT, "reason": "credential_leak"})
                continue
            quote_context = _quote_context(
                event.get("user_message") or "", candidate.get("user_quote") or "")
            verdict = review_candidate(candidate, event, source_receipt=receipt,
                                       user_context=quote_context or statement)
            decision = verdict["decision"]
            reason = _reason(verdict["reason_code"])
            if decision == REJECT:
                rejected += 1
                self._event_audit(event, candidate, REJECT, reason)
                results.append({"decision": REJECT, "reason": reason})
                continue
            if decision == DEFER:
                deferred += 1
                self._event_audit(event, candidate, DEFER, reason)
                results.append({"decision": DEFER, "reason": reason})
                continue

            metadata = derive_candidate_metadata(candidate, event)
            statement = metadata["statement"]
            budget = memory_store.autonomy_conflict_budget_status(
                user_id=event.get("user_id"), agent_id=event.get("agent_id"),
                profile=event.get("profile"), ebm=metadata["ebm"] or None)
            if budget.get("ok") is not True:
                deferred += 1
                retryable = True
                code = _reason(budget.get("reason") or "conflict_budget_exceeded")
                self._event_audit(event, candidate, DEFER, code)
                results.append({"decision": DEFER, "reason": code})
                continue
            identity_material = [event.get("profile"), event.get("user_id"),
                                 event.get("agent_id"), event.get("idempotency_key"),
                                 metadata["ebm"], statement]
            fact_id = "auto_auto_" + hashlib.sha256(
                json.dumps(identity_material, ensure_ascii=False, sort_keys=True)
                .encode("utf-8")).hexdigest()
            try:
                write = memory_store.write_fact(
                    statement, ebm=metadata["ebm"] or None, tags=metadata["tags"],
                    subject=metadata["subject"], user_id=event.get("user_id"),
                    agent_id=event.get("agent_id"), source=source,
                    fact_id=fact_id, initial_status="candidate",
                    schedule_background_index=False)
            except Exception:
                retryable = True
                self._event_audit(event, candidate, ADMIT, "write_failed")
                results.append({"decision": ADMIT, "reason": "write_failed"})
                continue
            if write.get("ok") is not True:
                retryable = True
                self._event_audit(event, candidate, ADMIT, "write_failed")
                results.append({"decision": ADMIT, "reason": "write_failed"})
                continue
            candidate_id = str(write.get("fact_id") or fact_id)
            if not write.get("written"):
                # A deterministic replay may find the candidate or its active
                # successor. Neither shortcut may skip index/recall verification.
                if write.get("reason") not in {"duplicate", "candidate_exists"}:
                    retryable = True
                    self._event_audit(event, candidate, ADMIT, "duplicate_identity_conflict")
                    results.append({"decision": ADMIT,
                                    "reason": "duplicate_identity_conflict"})
                    continue
                candidate_id = str(write.get("fact_id") or fact_id)
            index = self._verify_index(candidate_id, event, metadata)
            if not index.get("ok"):
                retryable = True
                code = _reason(index.get("reason") or "index_failed")
                already_active = (not write.get("written")
                                  and write.get("status") == "active")
                audit_code = "duplicate_verification_failed" if already_active else code
                self._event_audit(event, candidate, ADMIT, audit_code, candidate_id)
                results.append({"decision": ADMIT, "reason": audit_code,
                                "fact_id": candidate_id})
                continue
            if not write.get("written") and write.get("status") == "active":
                if not self._verify_recall(candidate_id, event, metadata):
                    retryable = True
                    self._event_audit(event, candidate, ADMIT,
                                      "duplicate_verification_failed", candidate_id)
                    results.append({"decision": ADMIT,
                                    "reason": "duplicate_verification_failed",
                                    "fact_id": candidate_id})
                    continue
                self._event_audit(event, candidate, ADMIT, "duplicate_fact", candidate_id)
                results.append({"decision": ADMIT, "reason": "duplicate_fact",
                                "fact_id": candidate_id})
                continue
            try:
                activation = memory_store.activate_candidate_fact(
                    fact_id=candidate_id, user_id=event.get("user_id"),
                    agent_id=event.get("agent_id"), profile=event.get("profile"),
                    statement=statement, subject=metadata["subject"],
                    source=source, ebm=metadata["ebm"] or None)
            except Exception:
                retryable = True
                self._event_audit(event, candidate, DEFER, "conflict_judge_failed", candidate_id)
                results.append({"decision": DEFER,
                                "reason": "conflict_judge_failed",
                                "fact_id": candidate_id})
                continue
            if activation.get("ok") is not True:
                retryable = True
                code = _reason(activation.get("reason") or "activation_failed")
                self._event_audit(event, candidate, DEFER, code, candidate_id)
                results.append({"decision": DEFER, "reason": code,
                                "fact_id": candidate_id})
                continue
            if not activation.get("written"):
                if activation.get("reason") == "duplicate":
                    duplicate_id = str(activation.get("fact_id") or candidate_id)
                    duplicate_meta = {
                        "statement": str(activation.get("fact_statement") or ""),
                        "subject": str(activation.get("fact_subject") or ""),
                        "ebm": metadata["ebm"],
                    }
                    if (duplicate_meta["statement"]
                            and self._verify_index(duplicate_id, event,
                                                   duplicate_meta).get("ok") is True
                            and self._verify_recall(duplicate_id, event,
                                                    duplicate_meta)):
                        self._event_audit(event, candidate, ADMIT,
                                          "duplicate_fact", duplicate_id)
                        results.append({"decision": ADMIT,
                                        "reason": "duplicate_fact",
                                        "fact_id": duplicate_id})
                        continue
                    retryable = True
                    self._event_audit(event, candidate, ADMIT,
                                      "duplicate_verification_failed", duplicate_id)
                    results.append({"decision": ADMIT,
                                    "reason": "duplicate_verification_failed",
                                    "fact_id": duplicate_id})
                    continue
                conflict = activation.get("conflict") or {}
                relation = str(conflict.get("relation") or "")
                if relation in {"defer", "reject_new"} or str(
                        activation.get("reason") or "").startswith("conflict_"):
                    deferred += 1
                    code = _reason(conflict.get("reason") or "conflict_unresolved")
                    retryable = retryable or code in {"judge_failed", "llm_unavailable"}
                    self._event_audit(event, candidate, DEFER, code, candidate_id)
                    results.append({"decision": DEFER, "reason": code,
                                    "fact_id": candidate_id})
                    continue
                retryable = True
                self._event_audit(event, candidate, DEFER, "activation_failed", candidate_id)
                results.append({"decision": DEFER, "reason": "activation_failed",
                                "fact_id": candidate_id})
                continue
            active_id = str(activation.get("fact_id") or candidate_id)
            written += 1
            self._event_audit(event, candidate, ADMIT, reason, active_id)
            results.append({"decision": ADMIT, "reason": reason,
                            "fact_id": active_id})

        if retryable:
            return {"ok": False, "retryable": True,
                    "reason": "autonomous_admission_incomplete",
                    "written": written, "deferred": deferred, "rejected": rejected,
                    "results": results, "model_calls": self.model_calls}
        return {"ok": True, "written": written, "deferred": deferred,
                "rejected": rejected, "results": results,
                "model_calls": self.model_calls}


__all__ = [
    "ADMIT", "REJECT", "DEFER", "POLICY_VERSION", "AUDIT_SCHEMA_VERSION",
    "review_candidate", "derive_candidate_metadata", "FrozenReviewStore",
    "AutonomyAudit", "AutonomousMemoryPipeline",
]

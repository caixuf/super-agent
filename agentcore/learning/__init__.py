#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentcore.learning —— 自学习中间层。

当前阶段只导出**候选知识**：来源快照 + 逐字引文 + 独立审查门禁。
它不联网、不定时、不写正式记忆，也永远不把候选当成已确认事实。

后续阶段（检索任务、审查交互、``write_fact`` 入库、召回探针）另行接入，
届时仍应复用这里的 :class:`Candidate` / :class:`CandidateStore` 作为唯一
证据形态，而不是另起一套候选表。
"""

from __future__ import annotations

from .candidates import (
    DECISION_APPROVE,
    DECISION_REJECT,
    DECISION_REVOKE,
    STATUS_APPROVED,
    STATUS_CONFLICTED,
    STATUS_NEEDS_HUMAN,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_REVOKED,
    STATUSES,
    TERMINAL_STATUSES,
    AutoApprovalForbidden,
    Candidate,
    CandidateError,
    CandidateStore,
    EvidenceMismatch,
    GroundingReport,
    IngestResult,
    InvalidCandidate,
    QuoteNotGrounded,
    ReviewBlocked,
    ReviewerError,
    ReviewerNotIndependent,
    SourceRecord,
    UnknownCandidate,
    UnknownSource,
    canonical_timestamp,
    normalize_domain,
    normalize_source_url,
    quote_grounded,
    source_identity,
)

__all__ = [
    "Candidate",
    "CandidateStore",
    "SourceRecord",
    "IngestResult",
    "GroundingReport",
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

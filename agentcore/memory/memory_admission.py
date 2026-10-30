"""Rationalized long-term memory admission governance for Super-Agent.

Implements strict evidence-based fact admission adapted from open-source governance
standards (Keep / Refuse / Defer). Prevents hallucinated conclusions, ungrounded
speculations, and credential leaks from polluting the long-term memory store.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional, Tuple

MEMORY_ADMISSION_SCHEMA_VERSION: Final[str] = "memory_admission/v2"

# Formal admission verdicts
VERDICT_KEEP: Final[str] = "KEEP"       # Fully grounded with hard evidence
VERDICT_REFUSE: Final[str] = "REFUSE"   # Speculation, credential leak, or vague statement
VERDICT_DEFER: Final[str] = "DEFER"     # Plausible observation needing second verification

# Credential protection patterns
_CREDENTIAL_PATTERNS: Final[Tuple[re.Pattern, ...]] = (
    re.compile(r"-----BEGIN [A-Z0-9_-]+ PRIVATE KEY-----"),
    re.compile(r"(?:api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-\.]{16,}['\"]", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9_\-\.]{30,}\b", re.IGNORECASE),
    re.compile(r"\bAKID[A-Za-z0-9]{16,}\b"),
)

# Hard evidence markers (commit hashes, numeric thresholds, explicit signals, task/case identifiers)
_COMMIT_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_NUMERIC_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUMERIC_METRIC_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|hz|deg|m/s|km/h|%|rpm|fps)\b", re.IGNORECASE)
_EXPLICIT_VALUE_ASSIGN_RE = re.compile(r"[\w\.]+\s*(?:==?|!=|>=?|<=?)\s*['\"]?\w+['\"]?")

# Speculation signals without hard grounding
_SPECULATION_KEYWORDS: Final[Tuple[str, ...]] = (
    "可能是",
    "应该是因为",
    "大概率是",
    "盲猜",
    "猜测",
    "似乎",
    "可能存在",
    "据推测",
)


def has_credential_leak(text: str) -> bool:
    """Return True if text contains private keys, bearer tokens, or secrets."""
    if not isinstance(text, str):
        return False
    return any(p.search(text) for p in _CREDENTIAL_PATTERNS)


def extract_evidence_anchors(text: str) -> List[str]:
    """Extract verifiable anchors (commits, hex values, measured metrics, condition equations)."""
    if not isinstance(text, str):
        return []
    anchors: List[str] = []

    for commit in _COMMIT_HASH_RE.findall(text):
        if not commit.isdigit():  # Avoid pure numbers like 20261029
            anchors.append(f"commit:{commit}")
            break

    for hex_val in _NUMERIC_HEX_RE.findall(text):
        anchors.append(f"hex:{hex_val}")

    for metric in _NUMERIC_METRIC_RE.findall(text):
        anchors.append(f"metric:{metric}")

    for eq in _EXPLICIT_VALUE_ASSIGN_RE.findall(text):
        anchors.append(f"expr:{eq.strip()}")

    return anchors


def evaluate_memory_admission(
    statement: str,
    *,
    source_receipt: Optional[Dict[str, Any]] = None,
    ebm_generation: str = "",
) -> Dict[str, Any]:
    """Adjudicate whether a proposed memory item should be kept, refused, or deferred.

    Pure function adhering to zero-hallucination discipline:
    - Block credentials unconditionally (VERDICT_REFUSE)
    - Block pure subjective speculations lacking measurable evidence (VERDICT_REFUSE)
    - Promote statements with concrete evidence anchors to VERDICT_KEEP
    - Defer ambiguous statements lacking external receipt to VERDICT_DEFER
    """
    stmt = (statement or "").strip()
    if not stmt or len(stmt) < 8:
        return {
            "schema_version": MEMORY_ADMISSION_SCHEMA_VERSION,
            "verdict": VERDICT_REFUSE,
            "reason_code": "EMPTY_OR_TOO_SHORT",
            "explanation": "Fact statement is empty or too short to be independently verifiable.",
            "evidence_anchors": [],
        }

    # 1. Security Check
    if has_credential_leak(stmt):
        return {
            "schema_version": MEMORY_ADMISSION_SCHEMA_VERSION,
            "verdict": VERDICT_REFUSE,
            "reason_code": "CREDENTIAL_LEAK",
            "explanation": "Statement contains sensitive credentials or secret tokens.",
            "evidence_anchors": [],
        }

    # 2. Evidence extraction
    anchors = extract_evidence_anchors(stmt)
    receipt_anchors = []
    if source_receipt and isinstance(source_receipt, dict):
        if source_receipt.get("commit"):
            receipt_anchors.append(f"receipt_commit:{source_receipt['commit']}")
        if source_receipt.get("task_id"):
            receipt_anchors.append(f"receipt_task:{source_receipt['task_id']}")
        if source_receipt.get("exit_code") is not None:
            receipt_anchors.append(f"receipt_exit:{source_receipt['exit_code']}")

    all_anchors = anchors + receipt_anchors

    # 3. Speculation detection
    is_speculative = any(w in stmt for w in _SPECULATION_KEYWORDS)
    if is_speculative and not all_anchors:
        return {
            "schema_version": MEMORY_ADMISSION_SCHEMA_VERSION,
            "verdict": VERDICT_REFUSE,
            "reason_code": "SPECULATION_WITHOUT_EVIDENCE",
            "explanation": "Statement contains speculative conjectures without any measurable evidence anchor.",
            "evidence_anchors": [],
        }

    # 4. Final Verdict Determination
    if all_anchors:
        return {
            "schema_version": MEMORY_ADMISSION_SCHEMA_VERSION,
            "verdict": VERDICT_KEEP,
            "reason_code": "GROUNDED_EVIDENCE_ATTACHED",
            "explanation": f"Statement is grounded with {len(all_anchors)} concrete verifiable evidence anchor(s).",
            "evidence_anchors": all_anchors,
        }

    # If no hard anchors but statement is clear and factual
    if len(stmt) >= 15 and not is_speculative:
        return {
            "schema_version": MEMORY_ADMISSION_SCHEMA_VERSION,
            "verdict": VERDICT_DEFER,
            "reason_code": "AWAITING_REPRO_EVIDENCE",
            "explanation": "Plausible observation; deferred until reinforced by execution log or commit anchor.",
            "evidence_anchors": [],
        }

    return {
        "schema_version": MEMORY_ADMISSION_SCHEMA_VERSION,
        "verdict": VERDICT_REFUSE,
        "reason_code": "INSUFFICIENT_SUBSTANCE",
        "explanation": "Statement lacks sufficient substance or grounding to enter long-term memory.",
        "evidence_anchors": [],
    }


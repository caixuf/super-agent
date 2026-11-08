"""Pure failure classification and self-healing remediation decisions.

Ported and adapted from open-source failure mender principles:
- Classifies failures into closed deterministic kinds.
- Decides actionable next steps: retry, replan, stop, escalate.
- Enforces strict no-blind-retry policies on permanent or permission denials.
- Produces deterministic failure digests and structured escalation proposals.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Final, Mapping

FAILURE_MENDER_SCHEMA_VERSION: Final[str] = "failure_mender_decision/v1"
FAILURE_ESCALATION_SCHEMA_VERSION: Final[str] = "failure_escalation_request/v1"

FAILURE_KINDS: Final[tuple[str, ...]] = (
    "transient",
    "persistent",
    "permanent",
    "external_wait",
    "unknown",
)
FAILURE_ACTIONS: Final[tuple[str, ...]] = ("retry", "replan", "stop", "escalate")
_MAX_REASON_LENGTH: Final[int] = 120

#: Raven-inspired WHY failure classes for recursive self-improvement and causal diagnosis
WHY_CLASSES: Final[tuple[str, ...]] = (
    "STOPPED_BEFORE_VERIFY",
    "PATH_OWNERSHIP_VIOLATION",
    "SPECIFICATION_MISMATCH",
    "TIMEOUT_WITHOUT_PROGRESS",
    "SYNTAX_OR_IMPORT_ERROR",
    "FLAKY_RESOURCE",
    "UNCLASSIFIED",
)

_TRANSIENT_PATTERNS: Final[tuple[str, ...]] = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "rate limit",
    "too many requests",
    "service unavailable",
    "broken pipe",
    "temporary failure",
    "resource temporarily unavailable",
    "eagain",
)
_PERSISTENT_PATTERNS: Final[tuple[str, ...]] = (
    "conflict",
    "stale",
    "revision",
    "out of date",
    "concurrent",
    "worktree busy",
    "lock busy",
    "locked",
    "needs_rebase",
)
_EXTERNAL_WAIT_PATTERNS: Final[tuple[str, ...]] = (
    "consent required",
    "awaiting approval",
    "credential required",
    "authentication required",
    "login required",
    "operator confirmation required",
)
_PERMANENT_PATTERNS: Final[tuple[str, ...]] = (
    "permission denied",
    "unauthorized",
    "forbidden",
    "invalid",
    "malformed",
    "unsupported",
    "not found",
    "syntax error",
    "cannot open",
    "outside roots",
    "policy violation",
    "illegal instruction",
)


def _safe_source(value: str) -> str:
    return " ".join(str(value).split())[:_MAX_REASON_LENGTH]


def classify_failure(error: BaseException | str) -> tuple[str, str]:
    """Return ``(kind, reason_code)`` using deterministic signals."""
    message = str(error).casefold()

    if isinstance(error, ValueError):
        return "permanent", "invalid_input"

    if (
        isinstance(error, (PermissionError, FileNotFoundError, TypeError))
        or any(p in message for p in _PERMANENT_PATTERNS)
    ):
        return "permanent", "invalid_or_unauthorized_request"

    if any(p in message for p in _EXTERNAL_WAIT_PATTERNS):
        return "external_wait", "external_authorization_or_approval"

    if any(p in message for p in _PERSISTENT_PATTERNS):
        return "persistent", "state_conflict_requires_replan"

    if isinstance(error, (TimeoutError, ConnectionError, BrokenPipeError)) or any(
        p in message for p in _TRANSIENT_PATTERNS
    ):
        return "transient", "provider_or_transport_unavailable"

    return "unknown", "unclassified_failure"


def diagnose_failure_why(
    error: BaseException | str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Diagnose the root cause 'WHY' of an execution failure, inspired by Raven evolver taxonomy.

    Categorizes failures into canonical root-cause classes:
    - STOPPED_BEFORE_VERIFY: execution finished without running or passing assertions
    - PATH_OWNERSHIP_VIOLATION: colleague attempted to write to forbidden/unauthorized files
    - SPECIFICATION_MISMATCH: verification checks or contract tests failed
    - TIMEOUT_WITHOUT_PROGRESS: process ran out of budget before producing required outputs
    - SYNTAX_OR_IMPORT_ERROR: code syntax, module import, or type resolution error
    - FLAKY_RESOURCE: transient transport, network, or external provider downtime
    - UNCLASSIFIED: unknown or unmapped failure
    """
    msg = str(error).casefold()
    ctx = context or {}

    # 1. Path ownership violation
    if "role_ownership_violation" in msg or "path_in_never_list" in msg or "unlisted_path_write_denied" in msg:
        return {
            "why": "PATH_OWNERSHIP_VIOLATION",
            "explanation": "Colleague role attempted to write to a path outside its authorized ownership matrix",
            "recommended_action": "restrict_to_authorized_worktree_paths",
            "remediation_code": "R_ENFORCE_PATH_OWNERSHIP",
        }

    # 2. Stopped before verify / missing verification
    if ctx.get("verification_run") is False or "stopped_before_verify" in msg or "unverified_completion" in msg:
        return {
            "why": "STOPPED_BEFORE_VERIFY",
            "explanation": "Task terminated or completed without driving required verification gates",
            "recommended_action": "re_execute_with_mandatory_verification_step",
            "remediation_code": "R_MANDATORY_VERIFICATION",
        }

    # 3. Verification checks failed / specification mismatch
    if (
        "verification_checks_failed" in msg
        or "assertionerror" in msg
        or ctx.get("verification_receipt", {}).get("all_passed") is False
    ):
        return {
            "why": "SPECIFICATION_MISMATCH",
            "explanation": "Code or artifacts failed verification gate assertions against the specification",
            "recommended_action": "review_red_team_feedback_and_replan",
            "remediation_code": "R_REPLAN_SPEC_ALIGNMENT",
        }

    # 4. Syntax or import error
    if any(p in msg for p in ("syntaxerror", "syntax error", "importerror", "modulenotfounderror", "nameerror")):
        return {
            "why": "SYNTAX_OR_IMPORT_ERROR",
            "explanation": "Code contains fatal syntax defect or unresolved module import dependency",
            "recommended_action": "run_linter_and_fix_syntax_in_worktree",
            "remediation_code": "R_STATIC_SYNTAX_FIX",
        }

    # 5. Timeout without progress
    if "timed out" in msg or "timeout" in msg:
        return {
            "why": "TIMEOUT_WITHOUT_PROGRESS",
            "explanation": "Task exceeded allocated time budget without reaching completion state",
            "recommended_action": "increase_timeout_or_split_task_into_subtasks",
            "remediation_code": "R_EXTEND_BUDGET_OR_SPLIT",
        }

    # 6. Flaky resource / transient
    if any(p in msg for p in _TRANSIENT_PATTERNS):
        return {
            "why": "FLAKY_RESOURCE",
            "explanation": "External provider, transport socket, or system resource temporarily unavailable",
            "recommended_action": "exponential_backoff_retry",
            "remediation_code": "R_TRANSIENT_RETRY",
        }

    return {
        "why": "UNCLASSIFIED",
        "explanation": "Unclassified failure requiring manual operator inspection",
        "recommended_action": "escalate_to_human_or_operator",
        "remediation_code": "R_ESCALATE_UNKNOWN",
    }


def decide_failure(
    error: BaseException | str,
    *,
    attempt: int = 0,
    max_retries: int = 2,
    source: str = "",
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an actionable decision without exposing sensitive credentials or unparsed traces."""
    if isinstance(attempt, bool) or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    if isinstance(max_retries, bool) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")

    kind, reason_code = classify_failure(error)

    if kind == "transient":
        action = "retry" if attempt < max_retries else "escalate"
    elif kind == "persistent":
        action = "replan"
    elif kind == "permanent":
        action = "stop"
    elif kind == "external_wait":
        action = "escalate"
    else:
        action = "escalate"

    text = str(error)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    why_diag = diagnose_failure_why(error, context=context)

    return {
        "schema_version": FAILURE_MENDER_SCHEMA_VERSION,
        "kind": kind,
        "reason_code": reason_code,
        "action": action,
        "attempt": attempt,
        "max_retries": max_retries,
        "retry_allowed": action == "retry",
        "source": _safe_source(source),
        "failure_sha256": digest,
        "why_diagnosis": why_diag,
        "claim_boundary": (
            "A mender decision is a policy recommendation, not proof that "
            "a retry, replan, or escalation occurred."
        ),
    }


def validate_failure_decision(decision: Mapping[str, object] | object) -> list[str]:
    """Validate a failure decision schema."""
    if not isinstance(decision, Mapping):
        return ["decision must be an object"]

    errors: list[str] = []
    if decision.get("schema_version") != FAILURE_MENDER_SCHEMA_VERSION:
        errors.append("schema_version is invalid")
    if decision.get("kind") not in FAILURE_KINDS:
        errors.append("kind is invalid")
    if decision.get("action") not in FAILURE_ACTIONS:
        errors.append("action is invalid")

    for key in ("attempt", "max_retries"):
        value = decision.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{key} must be a non-negative integer")

    if decision.get("retry_allowed") is not (decision.get("action") == "retry"):
        errors.append("retry_allowed must match action")

    if not re.fullmatch(r"[0-9a-f]{64}", str(decision.get("failure_sha256", ""))):
        errors.append("failure_sha256 must be a sha256 digest")

    boundary = str(decision.get("claim_boundary", "")).casefold()
    if "not" not in boundary:
        errors.append("claim_boundary must state a limitation")

    return errors


def build_escalation_request(decision: Mapping[str, object] | object) -> dict[str, Any] | None:
    """Prepare a safe escalation request proposal for non-retry decisions."""
    errors = validate_failure_decision(decision)
    if errors:
        raise ValueError(f"cannot build an escalation request from an invalid decision: {errors}")

    record = decision  # type: ignore
    action = str(record["action"])
    if action not in {"escalate", "replan"}:
        return None

    digest = str(record["failure_sha256"])
    seed_id = f"failure-{digest[:12]}"
    reason_code = str(record["reason_code"])

    return {
        "schema_version": FAILURE_ESCALATION_SCHEMA_VERSION,
        "seed_id": seed_id,
        "title": f"Investigate {reason_code}",
        "labels": ["failure", "escalation", str(record["kind"])],
        "description": (
            f"Investigate the classified {record['kind']} failure ({reason_code}) "
            "before modifying retry thresholds or re-dispatching."
        ),
        "reason_code": reason_code,
        "source": str(record.get("source", "")),
        "failure_sha256": digest,
        "recommended_action": action,
        "claim_boundary": (
            "This is a task proposal; it is not proof that an issue was created "
            "or that escalation occurred."
        ),
    }


def escalate_timeout(
    job_id: str,
    timeout_seconds: float,
    attempt: int = 0,
    *,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Helper for handling execution timeouts with progressive escalation."""
    err_text = f"execution timed out after {timeout_seconds:.1f} seconds (job={job_id})"
    decision = decide_failure(
        err_text,
        attempt=attempt,
        max_retries=max_retries,
        source=f"timeout_escalation:{job_id}",
    )
    escalation = build_escalation_request(decision)
    return {
        "job_id": job_id,
        "timeout_seconds": timeout_seconds,
        "decision": decision,
        "escalation_request": escalation,
    }

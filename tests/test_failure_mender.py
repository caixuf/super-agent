"""Unit tests for agentcore.failure_mender."""
from __future__ import annotations

import pytest

from agentcore.failure_mender import (
    FAILURE_ACTIONS,
    FAILURE_ESCALATION_SCHEMA_VERSION,
    FAILURE_KINDS,
    FAILURE_MENDER_SCHEMA_VERSION,
    build_escalation_request,
    classify_failure,
    decide_failure,
    escalate_timeout,
    validate_failure_decision,
)


def test_classify_failure_transient():
    kind, code = classify_failure(TimeoutError("operation timed out after 30s"))
    assert kind == "transient"
    assert code == "provider_or_transport_unavailable"

    kind, code = classify_failure(ConnectionError("connection reset by peer"))
    assert kind == "transient"

    kind, code = classify_failure("HTTP 429: rate limit exceeded, retry later")
    assert kind == "transient"

    kind, code = classify_failure("service unavailable, temporary failure")
    assert kind == "transient"


def test_classify_failure_persistent():
    kind, code = classify_failure("worktree busy: another process is executing")
    assert kind == "persistent"
    assert code == "state_conflict_requires_replan"

    kind, code = classify_failure("stale revision conflict detected")
    assert kind == "persistent"


def test_classify_failure_external_wait():
    kind, code = classify_failure("credential required: API token is not configured")
    assert kind == "external_wait"
    assert code == "external_authorization_or_approval"

    kind, code = classify_failure("operator confirmation required before mutation")
    assert kind == "external_wait"


def test_classify_failure_permanent():
    kind, code = classify_failure(PermissionError("permission denied on /root/data"))
    assert kind == "permanent"
    assert code == "invalid_or_unauthorized_request"

    kind, code = classify_failure(FileNotFoundError("target script not found"))
    assert kind == "permanent"

    kind, code = classify_failure(ValueError("invalid payload structure"))
    assert kind == "permanent"
    assert code == "invalid_input"

    kind, code = classify_failure("syntax error in generated script")
    assert kind == "permanent"

    # Precedence test: permission error with timeout words must stay permanent
    kind, code = classify_failure(PermissionError("Permission denied: request timed out waiting for token"))
    assert kind == "permanent"
    assert code == "invalid_or_unauthorized_request"

    dec = decide_failure(PermissionError("Permission denied: connection timed out"), attempt=0, max_retries=3)
    assert dec["kind"] == "permanent"
    assert dec["action"] == "stop"
    assert dec["retry_allowed"] is False


def test_classify_failure_unknown():
    kind, code = classify_failure("unexpected mysterious behavior")
    assert kind == "unknown"
    assert code == "unclassified_failure"


def test_decide_failure_transient_retry():
    dec = decide_failure("temporary network timeout", attempt=0, max_retries=2, source="session_runner")
    assert dec["schema_version"] == FAILURE_MENDER_SCHEMA_VERSION
    assert dec["kind"] == "transient"
    assert dec["action"] == "retry"
    assert dec["retry_allowed"] is True
    assert dec["attempt"] == 0
    assert dec["max_retries"] == 2
    assert len(dec["failure_sha256"]) == 64
    assert dec["source"] == "session_runner"
    assert "not" in dec["claim_boundary"].lower()


def test_decide_failure_transient_exhausted():
    dec = decide_failure("temporary network timeout", attempt=2, max_retries=2)
    assert dec["kind"] == "transient"
    assert dec["action"] == "escalate"
    assert dec["retry_allowed"] is False


def test_decide_failure_permanent_stop():
    dec = decide_failure("permission denied: execution blocked", attempt=0, max_retries=3)
    assert dec["kind"] == "permanent"
    assert dec["action"] == "stop"
    assert dec["retry_allowed"] is False


def test_decide_failure_persistent_replan():
    dec = decide_failure("worktree busy: concurrent lock held")
    assert dec["kind"] == "persistent"
    assert dec["action"] == "replan"
    assert dec["retry_allowed"] is False


def test_decide_failure_invalid_arguments():
    with pytest.raises(ValueError, match="attempt"):
        decide_failure("error", attempt=-1)
    with pytest.raises(ValueError, match="attempt"):
        decide_failure("error", attempt=True)
    with pytest.raises(ValueError, match="max_retries"):
        decide_failure("error", max_retries=-2)
    with pytest.raises(ValueError, match="max_retries"):
        decide_failure("error", max_retries=False)


def test_validate_failure_decision_valid():
    dec = decide_failure("connection reset", attempt=0, max_retries=1)
    errors = validate_failure_decision(dec)
    assert errors == []


def test_validate_failure_decision_tampered():
    dec = decide_failure("connection reset", attempt=0, max_retries=1)

    bad1 = dict(dec, schema_version="bad_version")
    assert "schema_version is invalid" in validate_failure_decision(bad1)

    bad2 = dict(dec, kind="unknown_kind")
    assert "kind is invalid" in validate_failure_decision(bad2)

    bad3 = dict(dec, retry_allowed=False)  # Mismatch with action="retry"
    assert "retry_allowed must match action" in validate_failure_decision(bad3)

    bad4 = dict(dec, failure_sha256="not_a_digest")
    assert "failure_sha256 must be a sha256 digest" in validate_failure_decision(bad4)

    bad5 = dict(dec, claim_boundary="This is absolute proof of retry.")
    assert "claim_boundary must state a limitation" in validate_failure_decision(bad5)


def test_build_escalation_request():
    # Retry decision produces no escalation request
    retry_dec = decide_failure("connection reset", attempt=0, max_retries=2)
    assert build_escalation_request(retry_dec) is None

    # Permanent stop produces no escalation request
    stop_dec = decide_failure("permission denied", attempt=0)
    assert build_escalation_request(stop_dec) is None

    # Exhausted transient escalates
    escalate_dec = decide_failure("connection reset", attempt=2, max_retries=2, source="http_pool")
    req = build_escalation_request(escalate_dec)
    assert req is not None
    assert req["schema_version"] == FAILURE_ESCALATION_SCHEMA_VERSION
    assert req["seed_id"].startswith("failure-")
    assert req["recommended_action"] == "escalate"
    assert req["source"] == "http_pool"
    assert "escalation" in req["labels"]

    # Replan decision creates replan escalation proposal
    replan_dec = decide_failure("worktree busy lock contention")
    req_replan = build_escalation_request(replan_dec)
    assert req_replan is not None
    assert req_replan["recommended_action"] == "replan"

    with pytest.raises(ValueError, match="cannot build an escalation request"):
        build_escalation_request({"invalid": "record"})


def test_escalate_timeout_helper():
    result = escalate_timeout(job_id="job-789", timeout_seconds=45.0, attempt=1, max_retries=1)
    assert result["job_id"] == "job-789"
    assert result["timeout_seconds"] == 45.0
    assert result["decision"]["action"] == "escalate"
    assert result["escalation_request"] is not None
    assert "provider_or_transport_unavailable" in result["escalation_request"]["reason_code"]

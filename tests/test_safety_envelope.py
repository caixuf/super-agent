"""Tests for agentcore.safety_envelope (ODD envelope and retry budget)."""

from __future__ import annotations

import pytest
from agentcore.safety_envelope import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_TOKENS,
    DEFAULT_MAX_ERROR_RETRIES,
    SessionEnvelopeTracker,
    UpstreamErrorBudgetGuard,
)


def test_safety_envelope_normal_payload():
    tracker = SessionEnvelopeTracker(session_id="test-session-1")
    # 50 KB payload
    status = tracker.assess_envelope(50 * 1024)
    assert status.within_odd is True
    assert status.needs_compact is False
    assert status.mrm_triggered is False
    assert status.warning_notice is None


def test_safety_envelope_watermark_warning():
    tracker = SessionEnvelopeTracker(session_id="test-session-2")
    # 1.7 MB payload (> 80% watermark of 2 MB)
    status = tracker.assess_envelope(int(DEFAULT_MAX_CONTEXT_BYTES * 0.85))
    assert status.within_odd is True
    assert status.needs_compact is True
    assert status.warning_notice is not None
    assert "[ODD-WARN]" in status.warning_notice
    assert "/reset" in status.warning_notice


def test_safety_envelope_breach_mrm():
    tracker = SessionEnvelopeTracker(session_id="test-session-3")
    # 2.5 MB payload (> 2 MB default ceiling)
    status = tracker.assess_envelope(int(DEFAULT_MAX_CONTEXT_BYTES * 1.25))
    assert status.within_odd is False
    assert status.mrm_triggered is True
    assert status.mrm_reason == "CONTEXT_OVERFLOW"
    assert "[ODD-BREACH]" in status.warning_notice


def test_safety_envelope_day_rollover():
    tracker = SessionEnvelopeTracker(
        session_id="test-session-day",
        creation_date="2026-11-02",
    )
    assert tracker.check_day_rollover("2026-11-03") is True
    assert tracker.check_day_rollover("2026-11-02") is False


def test_upstream_error_budget_guard():
    guard = UpstreamErrorBudgetGuard(max_retries=1)
    assert guard.should_trigger_mrm is False

    # First retry allowed
    allowed = guard.record_failure("500 Internal Server Error")
    assert allowed is True
    assert guard.should_trigger_mrm is False

    # Second retry exhausted -> trigger MRM
    allowed_second = guard.record_failure("ZlibError: decompression failed")
    assert allowed_second is False
    assert guard.should_trigger_mrm is True

    mrm_resp = guard.get_mrm_degraded_response(elapsed_seconds=4.2)
    assert "[FAIL]" in mrm_resp
    assert "安全降级熔断 (MRM)" in mrm_resp
    assert "耗时: 4.2s" in mrm_resp


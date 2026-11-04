"""Deterministic handoff receipts never interpret model text or old history."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
JOB = "a" * 32


@pytest.fixture
def release(monkeypatch):
    monkeypatch.setitem(sys.modules, "superagent_frontdesk",
                        SimpleNamespace(frontdesk_enabled=lambda **kwargs: True))
    spec = importlib.util.spec_from_file_location("frontdesk_turn_test", REPO / "runtime/hermes_frontdesk_turn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def receipt(**changes):
    return dict(ok=True, task_id=JOB, status="queued", role="analysis", **changes)


def row(content, *, name="durable_submit", call_id="current", role="tool"):
    return {"role": role, "name": name, "tool_call_id": call_id,
            "content": content if isinstance(content, str) else json.dumps(content)}


def test_release_uses_fresh_matching_receipts_and_never_summary_or_next_action(release):
    record = receipt()
    record.update(summary="private or model-generated text", next_action="execute terminal")
    answer = release.release_after_submission(["current"], [row(record)])
    assert JOB in answer and "排队中" in answer and "analysis" in answer
    assert "private" not in answer and "terminal" not in answer
    assert "接单回执不等于验收或交付完成" in answer


@pytest.mark.parametrize("fault", ["wrong-id", "status-tool", "user", "malformed", "duplicate",
                                  "false-ok", "numeric-ok", "invalid-id", "unknown-status",
                                  "oversize", "missing"])
def test_unproven_results_never_acknowledge_submission(release, fault):
    values = [row(receipt())]
    if fault == "wrong-id":
        values[0]["tool_call_id"] = "old-submit"
    elif fault == "status-tool":
        values[0]["name"] = "durable_status"
    elif fault == "user":
        values[0]["role"] = "user"
    elif fault == "malformed":
        values[0]["content"] = "{not-json"
    elif fault == "duplicate":
        values.append(row(receipt()))
    elif fault in {"false-ok", "numeric-ok", "invalid-id", "unknown-status"}:
        record = receipt()
        record.update({
            "false-ok": {"ok": False}, "numeric-ok": {"ok": 1},
            "invalid-id": {"task_id": "spoof"}, "unknown-status": {"status": "verified"},
        }[fault])
        values = [row(record)]
    elif fault == "oversize":
        values[0]["content"] = " " * 65537
    else:
        values = []
    assert release.release_after_submission(["current"], values) == release.UNCONFIRMED


def test_mixed_receipts_preserve_accepted_task_and_mark_uncertainty(release):
    answer = release.release_after_submission(
        ["current", "failed"], [row(receipt()), row({"ok": False}, call_id="failed")]
    )
    assert JOB in answer and "另有 1 笔提交结果未确认" in answer


def test_duplicate_task_receipts_do_not_duplicate_the_acknowledgement(release):
    answer = release.release_after_submission(
        ["current", "retry"], [row(receipt()), row(receipt(), call_id="retry")]
    )
    assert answer.count(JOB) == 1


def test_no_submission_and_disabled_worker_do_not_change_turns(release, monkeypatch):
    assert release.release_after_submission([], [row(receipt())]) is None
    monkeypatch.setitem(sys.modules, "superagent_frontdesk",
                        SimpleNamespace(frontdesk_enabled=lambda **kwargs: False))
    assert release.release_after_submission(["current"], [row(receipt())]) is None


def test_policy_failure_is_not_treated_as_disabled_worker(release, monkeypatch):
    def broken(**kwargs):
        raise ValueError("private configuration detail")
    monkeypatch.setitem(sys.modules, "superagent_frontdesk", SimpleNamespace(frontdesk_enabled=broken))
    with pytest.raises(ValueError):
        release.release_after_submission(["current"], [row(receipt())])

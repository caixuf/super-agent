"""HFC integration gate checks bounded doctor evidence without forwarding it."""

from __future__ import annotations

import json

from tools.hfc_acceptance import LOCK, evaluate_report

PINNED_COMMIT = json.loads(LOCK.read_text(encoding="utf-8"))["commit"]


def _evaluate(report, *, require_turn=True):
    return evaluate_report(report, installed_commit=PINNED_COMMIT, require_turn=require_turn)


def _report():
    return {
        "package_version": "5.0.13",
        "diagnostics": {
            "status": "ok",
            "hermes": {"compatibility": "full"},
            "install_state": {"status": "installed"},
            "runtime_import": {"status": "ok"},
            "runtime": {
                "sidecar_status": "healthy",
                "readiness": {"status": "ready", "generation_match": True},
                "integrity": {"mode": "safe"},
                "timing": {"started_samples": 1, "first_answer_samples": 1,
                           "completed_samples": 1},
            },
            "findings": [{"code": "route_fallback", "severity": "info"}],
        },
    }


def test_full_gate_requires_runtime_and_turn_evidence():
    result = _evaluate(_report())
    assert result["status"] == "ready"
    assert result["blockers"] == []
    assert result["notices"] == ["route_fallback"]
    assert "button_callback_resumes_same_turn" in result["manual_client_acceptance_required"]


def test_missing_turn_sample_blocks_default_gate_but_not_infra_gate():
    report = _report()
    report["diagnostics"]["runtime"]["timing"]["completed_samples"] = 0

    full = _evaluate(report)
    infrastructure = _evaluate(report, require_turn=False)

    assert full["status"] == "blocked"
    assert "turn_completed_observed" in full["blockers"]
    assert infrastructure["status"] == "ready"


def test_unpinned_hfc_version_fails_closed():
    report = _report()
    report["package_version"] = "5.0.14"
    result = _evaluate(report, require_turn=False)
    assert result["status"] == "blocked"
    assert "pinned_version" in result["blockers"]


def test_unpinned_source_commit_fails_closed():
    result = evaluate_report(_report(), installed_commit="f" * 40)
    assert result["status"] == "blocked"
    assert "pinned_cli_source_commit" in result["blockers"]

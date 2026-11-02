"""HFC integration gate checks bounded doctor evidence without forwarding it."""

from __future__ import annotations

import json

import importlib.util
from pathlib import Path
_spec = importlib.util.spec_from_file_location("hfc_acceptance", str(Path(__file__).resolve().parents[1] / "tools" / "hfc_acceptance.py"))
hfc_acceptance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hfc_acceptance)
LOCK = hfc_acceptance.LOCK
evaluate_report = hfc_acceptance.evaluate_report

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


def test_full_gate_requires_runtime_and_turn_evidence(tmp_path, monkeypatch):
    # A future reviewed release can pass; the current pin remains quarantined.
    lock = json.loads(LOCK.read_text())
    lock["known_delivery_blockers"] = []
    reviewed = tmp_path / "lock.json"
    reviewed.write_text(json.dumps(lock))
    monkeypatch.setattr(hfc_acceptance, "LOCK", reviewed)
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


def test_known_delivery_loss_blocks_even_healthy_runtime_and_completed_samples():
    result = _evaluate(_report())
    assert result["status"] == "blocked"
    assert result["blockers"] == ["no_known_delivery_loss"]
    assert len(result["known_delivery_blockers"]) == 3
    assert result["interaction_gate_passed"] is False


def test_infrastructure_ready_is_not_interaction_gate_pass():
    result = _evaluate(_report(), require_turn=False)
    assert result["status"] == "ready"
    assert result["known_delivery_blockers"]
    assert result["interaction_gate_passed"] is False


def test_absent_delivery_review_fails_closed(tmp_path, monkeypatch):
    lock = json.loads(LOCK.read_text())
    lock.pop("known_delivery_blockers")
    unchecked = tmp_path / "lock.json"
    unchecked.write_text(json.dumps(lock))
    monkeypatch.setattr(hfc_acceptance, "LOCK", unchecked)
    result = _evaluate(_report())
    assert "no_known_delivery_loss" in result["blockers"]
    assert result["known_delivery_blockers"] == ["delivery_safety_review_missing"]

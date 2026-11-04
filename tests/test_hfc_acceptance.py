"""HFC integration gate certifies an identity, not a wish.

Two identities are legitimate: a git checkout of the pinned upstream commit, and
a built wheel that carries the reviewed delivery repair. The lock certifies the
wheel, so a wheel-shaped lock is checked against its recorded sha256 and an
upstream-shaped lock against its commit. A wheel has no commit; asking it for one
can only ever produce a permanent red, which teaches people to skip the gate.
"""

from __future__ import annotations

import json

import importlib.util
from pathlib import Path
_spec = importlib.util.spec_from_file_location("hfc_acceptance", str(Path(__file__).resolve().parents[1] / "tools" / "hfc_acceptance.py"))
hfc_acceptance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hfc_acceptance)
LOCK = hfc_acceptance.LOCK
evaluate_report = hfc_acceptance.evaluate_report

LOCK_DATA = json.loads(LOCK.read_text(encoding="utf-8"))
PINNED_COMMIT = LOCK_DATA["commit"]
RECORDED = LOCK_DATA["deployed_build"]
ACCEPTED = hfc_acceptance.accepted_identity(LOCK_DATA)
PINNED_UPSTREAM = {"kind": "upstream_pin", "commit": PINNED_COMMIT}
FOREIGN_WHEEL = {"kind": "deployed_wheel", "wheel_sha256": "0" * 64,
                 "version": RECORDED["version"]}


_UNSET = object()


def _evaluate(report, *, identity=_UNSET, require_turn=True):
    supplied = ACCEPTED if identity is _UNSET else identity
    return evaluate_report(report, installed_identity=supplied, require_turn=require_turn)


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


def _reviewed_lock(tmp_path, monkeypatch, mutate):
    lock = json.loads(LOCK.read_text())
    mutate(lock)
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(lock))
    monkeypatch.setattr(hfc_acceptance, "LOCK", path)
    return lock


def test_full_gate_requires_runtime_and_turn_evidence(tmp_path, monkeypatch):
    # A future reviewed release can pass; every infrastructure signal is required.
    def clear(lock):
        lock["known_delivery_blockers"] = []

    _reviewed_lock(tmp_path, monkeypatch, clear)
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


def test_foreign_artifact_fails_closed_in_either_shape():
    # A different wheel, and a source checkout of the quarantined commit, are both
    # not the certified artifact.
    wheel = _evaluate(_report(), identity=FOREIGN_WHEEL, require_turn=False)
    source = _evaluate(_report(), identity=PINNED_UPSTREAM, require_turn=False)

    assert wheel["blockers"] == ["recorded_build_identity"]
    assert source["blockers"] == ["recorded_build_identity"]


def test_missing_identity_fails_closed():
    result = _evaluate(_report(), identity={}, require_turn=False)
    assert result["reported_identity"] is None
    assert result["checks"]["identity_resolvable"] is False
    assert set(result["blockers"]) >= {"identity_resolvable", "recorded_build_identity"}


def test_upstream_identity_is_still_certifiable(tmp_path, monkeypatch):
    # Retiring the wheel record must not strand the gate: the pin route stays open.
    def repin(lock):
        lock["known_delivery_blockers"] = []
        lock["identity"]["accepted"] = "upstream_pin"

    _reviewed_lock(tmp_path, monkeypatch, repin)
    good = _evaluate(_report(), identity=PINNED_UPSTREAM)
    wrong = _evaluate(_report(), identity={"kind": "upstream_pin", "commit": "f" * 40})

    assert good["status"] == "ready"
    assert good["checks"]["pinned_cli_source_commit"] is True
    assert wrong["blockers"] == ["pinned_cli_source_commit"]


def test_repaired_build_clears_the_upstream_delivery_blockers():
    result = _evaluate(_report())
    assert result["checks"]["recorded_build_identity"] is True
    assert result["checks"]["no_known_delivery_loss"] is True
    assert result["status"] == "ready"
    assert len(result["known_delivery_blockers"]) == 3


def test_repair_evidence_on_a_foreign_build_does_not_count():
    # Same arguments, wrong artifact: the recorded repair is not transferable.
    result = _evaluate(_report(), identity=FOREIGN_WHEEL)
    assert result["checks"]["no_known_delivery_loss"] is False
    assert set(result["blockers"]) == {"recorded_build_identity", "no_known_delivery_loss"}


def test_build_without_repair_evidence_stays_blocked(tmp_path, monkeypatch):
    def strip(lock):
        lock["deployed_build"].pop("blockers_resolved_on_this_build")

    _reviewed_lock(tmp_path, monkeypatch, strip)
    result = _evaluate(_report())
    assert result["status"] == "blocked"
    assert result["blockers"] == ["no_known_delivery_loss"]


def test_infrastructure_ready_is_not_interaction_gate_pass():
    result = _evaluate(_report(), require_turn=False)
    assert result["status"] == "ready"
    assert result["known_delivery_blockers"]
    assert result["interaction_gate_passed"] is False


def test_absent_delivery_review_fails_closed(tmp_path, monkeypatch):
    def drop(lock):
        lock.pop("known_delivery_blockers")

    _reviewed_lock(tmp_path, monkeypatch, drop)
    result = _evaluate(_report())
    assert "no_known_delivery_loss" in result["blockers"]
    assert result["known_delivery_blockers"] == ["delivery_safety_review_missing"]

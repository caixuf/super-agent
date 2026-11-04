from __future__ import annotations

import json
import subprocess
import time
import uuid

import pytest

from agentcore import colleague_containment as containment


def test_verify_systemd_containment_inactive(monkeypatch):
    unit = "agentcore-task-test-1.service"

    def fake_run(args, **kwargs):
        if "is-active" in args:
            return subprocess.CompletedProcess(args, 0, stdout="inactive\n", stderr="")
        if "show" in args and "--property=ControlGroup" in args:
            return subprocess.CompletedProcess(args, 0, stdout="/user.slice/user-1000.slice/fake.service\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = containment.verify_systemd_containment(unit)
    assert res["ok"] is False
    assert res["reason"] == "cgroup_unavailable"

    # A stopped-looking unit is not bound to this attempt.

    tok = containment.new_token()
    monkeypatch.setattr(containment, "_tagged", lambda t, exc: [])
    evidence = containment.sweep_and_verify(tok, systemd_unit=unit)
    assert evidence["quiescent"] is False
    assert evidence["proof_scope"] == "containment_unproven"
    assert containment.evidence_id(evidence) is None


def test_verify_systemd_containment_active_rejected(monkeypatch):
    unit = "agentcore-task-test-active.service"

    def fake_run(args, **kwargs):
        if "is-active" in args:
            return subprocess.CompletedProcess(args, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = containment.verify_systemd_containment(unit)
    assert res["ok"] is False
    assert res["reason"] == "unit_still_active"

    tok = containment.new_token()
    monkeypatch.setattr(containment, "_tagged", lambda t, exc: [])
    evidence = containment.sweep_and_verify(tok, systemd_unit=unit)
    assert evidence["quiescent"] is False
    assert containment.evidence_id(evidence) is None


def test_verify_docker_containment_exited_or_removed(monkeypatch):
    tok = containment.new_token()
    container = "agentcore-runner-test"

    # Case 1: Removed container (docker inspect returns code 1)
    def fake_run_removed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Error: No such object")

    monkeypatch.setattr(subprocess, "run", fake_run_removed)
    res = containment.verify_docker_containment(container, attempt_token=tok)
    assert res["ok"] is False
    assert res["reason"] == "docker_inspect_failed_or_missing"

    evidence = containment.sweep_and_verify(tok, docker_container=container)
    assert evidence["quiescent"] is False
    assert evidence["proof_scope"] == "containment_unproven"
    assert containment.evidence_id(evidence) is None

    # Case 2: Exited container matching attempt_token
    state = json.dumps({"Running": False, "Status": "exited", "ExitCode": 0})
    labels = json.dumps({"agentcore.attempt_token": tok})
    envs = json.dumps([f"AGENTCORE_ATTEMPT_TOKEN={tok}"])
    inspect_out = f"{state}:::{labels}:::{envs}\n"

    def fake_run_exited(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=inspect_out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_exited)
    res2 = containment.verify_docker_containment(container, attempt_token=tok)
    assert res2["ok"] is False
    assert res2["reason"] == "attempt_container_binding_unproven"


def test_verify_docker_containment_running_rejected(monkeypatch):
    tok = containment.new_token()
    container = "agentcore-runner-running"
    state = json.dumps({"Running": True, "Status": "running"})
    labels = json.dumps({"agentcore.attempt_token": tok})
    envs = json.dumps([f"AGENTCORE_ATTEMPT_TOKEN={tok}"])
    inspect_out = f"{state}:::{labels}:::{envs}\n"

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=inspect_out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = containment.verify_docker_containment(container, attempt_token=tok)
    assert res["ok"] is False
    assert res["reason"] == "container_still_running"

    evidence = containment.sweep_and_verify(tok, docker_container=container)
    assert evidence["quiescent"] is False
    assert containment.evidence_id(evidence) is None



def test_query_failure_or_unknown_unit_never_proves_quiescence(monkeypatch):
    monkeypatch.setattr(containment, "current_boot_id", lambda: "current")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw:
                        subprocess.CompletedProcess(args, 1, stdout="", stderr="daemon unavailable"))
    result = containment.verify_systemd_containment("not-present.service")
    assert result["ok"] is False
    assert result["reason"] == "unit_state_unknown"


def test_docker_error_and_untrusted_runner_boolean_do_not_prove_stop(monkeypatch):
    token = containment.new_token()
    monkeypatch.setattr(subprocess, "run", lambda args, **kw:
                        subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied"))
    assert containment.verify_docker_containment("unknown", attempt_token=token)["ok"] is False
    monkeypatch.setattr(containment, "_tagged", lambda token, exclude: [])
    assert containment.evidence_id(containment.sweep_and_verify(token, runner_contained=True)) is None


def test_container_token_requires_exact_match(monkeypatch):
    token = containment.new_token()
    state = json.dumps({"Running": False, "Status": "exited"})
    inspect = f'{state}:::{{}}:::{json.dumps(["AGENTCORE_ATTEMPT_TOKEN=" + token + "extra"])}'
    monkeypatch.setattr(subprocess, "run", lambda args, **kw:
                        subprocess.CompletedProcess(args, 0, stdout=inspect, stderr=""))
    result = containment.verify_docker_containment("other", attempt_token=token)
    assert result["reason"] == "attempt_token_mismatch"

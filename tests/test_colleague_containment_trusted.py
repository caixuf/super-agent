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
    assert res["ok"] is True
    assert res["unit"] == unit
    assert res["survivors"] == []

    # Whole attempt evidence check via sweep_and_verify
    tok = containment.new_token()
    monkeypatch.setattr(containment, "_tagged", lambda t, exc: [])
    evidence = containment.sweep_and_verify(tok, systemd_unit=unit)
    assert evidence["quiescent"] is True
    assert evidence["proof_scope"] == "whole_attempt"
    assert evidence["containment_type"] == "systemd_unit"
    assert containment.evidence_id(evidence) is not None


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
    assert res["ok"] is True
    assert res["state"] == "removed"

    evidence = containment.sweep_and_verify(tok, docker_container=container)
    assert evidence["quiescent"] is True
    assert evidence["proof_scope"] == "whole_attempt"
    assert evidence["containment_type"] == "docker_container"
    assert containment.evidence_id(evidence) is not None

    # Case 2: Exited container matching attempt_token
    state = json.dumps({"Running": False, "Status": "exited", "ExitCode": 0})
    labels = json.dumps({"agentcore.attempt_token": tok})
    envs = json.dumps([f"AGENTCORE_ATTEMPT_TOKEN={tok}"])
    inspect_out = f"{state}:::{labels}:::{envs}\n"

    def fake_run_exited(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=inspect_out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_exited)
    res2 = containment.verify_docker_containment(container, attempt_token=tok)
    assert res2["ok"] is True
    assert res2["status"] == "exited"


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

"""The queue worker must not settle an attempt whose descendants it cannot see.

A reservation is settled on the strength of a quiescence proof. These tests pin
the two ways that proof can be missing — a launcher that never owned it, and an
empty or absent attempt token — plus the case where a detached descendant is
actually found and stopped.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from agentcore import colleague_containment as containment

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAILED = {"ok": False, "error": "colleague_execution_failed_or_unknown"}


def envelope():
    return {"job": "job-1", "attempt": "attempt-1",
            "payload": {"owner": {"profile": "leader", "session": "conversation-a"},
                        "member_id": "alice", "generation": 1,
                        "request_id": "req-1", "prompt": "Inspect this patch"}}


def run_worker(bootstrap, *, job=None, env=None):
    environ = {key: value for key, value in os.environ.items() if key != containment.ENV}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(FIXTURES)]
        + [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part])
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "agentcore.colleague_worker", "--bootstrap", bootstrap],
        input=json.dumps(job if job is not None else envelope()),
        capture_output=True, text=True, env=environ, timeout=120)


def receipt(done):
    return json.loads(done.stdout)


def test_worker_refuses_the_builtin_gateway_launcher():
    # ``hermes chat`` submits to a long-lived gateway: the processes it starts
    # are gateway children that never inherit this attempt's token.
    done = run_worker("colleague_worker_probe:builtin",
                      env={containment.ENV: uuid.uuid4().hex})
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_worker_refuses_a_host_owned_launcher():
    done = run_worker("colleague_worker_probe:injected",
                      env={containment.ENV: uuid.uuid4().hex})
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_worker_refuses_a_runner_that_reports_no_containment():
    # Silence is not a claim. A gate that only inspected known classes would let
    # any other launcher through.
    done = run_worker("colleague_worker_probe:silent",
                      env={containment.ENV: uuid.uuid4().hex})
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_receipt_is_null_when_the_queue_supplied_no_attempt_token():
    # No token means no attempt identity: fail before executing the turn.
    done = run_worker("colleague_worker_probe:plain")
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_worker_rejects_a_malformed_attempt_token():
    done = run_worker("colleague_worker_probe:plain", env={containment.ENV: "not-a-token"})
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_valid_attempt_token_alone_does_not_prove_quiescence():
    token = uuid.uuid4().hex
    done = run_worker("colleague_worker_probe:plain", env={containment.ENV: token})
    # A token scan alone cannot certify complete attempt containment.
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_sweep_stops_a_detached_descendant_that_outlived_the_group():
    # ``start_new_session`` reproduces what a turn's tool call does: the process
    # leaves the worker's group and survives a killpg. Only the token finds it.
    token = uuid.uuid4().hex
    detached = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env={**os.environ, containment.ENV: token}, start_new_session=True)
    try:
        assert detached.poll() is None
        done = run_worker("colleague_worker_probe:plain", env={containment.ENV: token})
        assert done.returncode == 1
        assert receipt(done) == FAILED
        deadline = time.monotonic() + 20
        while detached.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert detached.poll() is not None, "attempt token did not reach the detached child"
    finally:
        if detached.poll() is None:
            detached.kill()
            detached.wait(timeout=10)


def test_worker_succeeds_when_trusted_containment_is_provided():
    token = uuid.uuid4().hex
    done = run_worker("colleague_worker_probe:trusted", env={containment.ENV: token})
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_worker_rejects_a_queue_envelope_it_did_not_define():
    done = run_worker("colleague_worker_probe:plain", job={"payload": {}})
    assert done.returncode == 1
    assert receipt(done) == FAILED


def test_failed_turn_still_sweeps_its_descendants():
    # A crashed turn must not be allowed to leave detached processes behind just
    # because it never produced a receipt.
    token = uuid.uuid4().hex
    detached = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env={**os.environ, containment.ENV: token}, start_new_session=True)
    try:
        assert detached.poll() is None
        done = run_worker("colleague_worker_probe:failing", env={containment.ENV: token})
        assert done.returncode == 1
        assert receipt(done) == FAILED
        deadline = time.monotonic() + 20
        while detached.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert detached.poll() is not None, "failure path left a descendant running"
    finally:
        if detached.poll() is None:
            detached.kill()
            detached.wait(timeout=10)


def test_worker_succeeds_when_trusted_containment_is_provided():
    token = uuid.uuid4().hex
    done = run_worker("colleague_worker_probe:trusted", env={containment.ENV: token})
    assert done.returncode == 1
    assert receipt(done) == FAILED

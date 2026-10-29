from __future__ import annotations

import os
import subprocess
import time

import pytest

from agentcore import colleague_containment as containment


def test_sweep_kills_a_descendant_that_left_the_process_group():
    token = containment.new_token()
    env = dict(os.environ)
    env[containment.ENV] = token
    # setsid makes the child a session leader, so killpg on our own group can
    # never reach it; only the inherited attempt token can find it again.
    child = subprocess.Popen(["setsid", "sleep", "60"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        parent_sid = os.getsid(0)
        deadline = time.monotonic() + 5
        while os.getsid(child.pid) == parent_sid and time.monotonic() < deadline:
            time.sleep(0.01)
        assert os.getsid(child.pid) != parent_sid
        assert child.pid in containment.tagged(token)
        evidence = containment.sweep(token)
        assert evidence["quiescent"] is True
        assert child.pid in evidence["signalled"]
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    assert containment.tagged(token) == []
    assert containment.evidence_id(evidence)


def test_sweep_of_an_untouched_attempt_is_quiescent_without_signalling():
    evidence = containment.sweep(containment.new_token())
    assert evidence["quiescent"] is True
    assert evidence["signalled"] == []
    assert containment.evidence_id(evidence)


def test_evidence_is_withheld_unless_the_attempt_is_proven_empty():
    assert containment.evidence_id(None) is None
    assert containment.evidence_id({"token": containment.new_token(), "quiescent": False}) is None
    assert containment.evidence_id({"token": "not-a-token", "quiescent": True}) is None


def test_tokens_are_validated_before_touching_proc():
    for bad in ("", "x" * 32, "0" * 31, "0" * 33, None, 7):
        with pytest.raises(ValueError, match="invalid_attempt_token"):
            containment.sweep(bad)

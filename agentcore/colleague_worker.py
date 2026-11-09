"""Pool runner for a host-configured colleague execution service.

The administrator supplies a trusted ``module:factory`` on the worker command
line. The factory returns ``(runner, authorize_assignment)``. Neither executable
nor bootstrap comes from the queued task. This module does not send messages.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys

from agentcore import colleague_containment as containment
from agentcore.colleague_dispatch import execute_payload


def _require_attempt_containment(runner):
    """Refuse executions whose whole attempt this worker cannot prove is gone.

    Every runner must say what it can prove, and only a runner that executes the
    turn inside this process can answer for the processes that turn starts.

    A ``hermes chat`` turn runs inside a long-lived gateway: the tools it calls
    start processes owned by that gateway, not by this worker, so neither the
    pool's process group nor an attempt-token sweep can see them. The built-in
    launcher therefore reports ``gateway_delegated``, and a host-injected
    launcher reports ``host_owned`` — both refuse here.

    A runner that reports nothing is refused too: silence is not a claim, and a
    gate that only inspects known classes lets any other launcher through.
    """
    kind = getattr(runner, "attempt_containment", None)
    if kind != "runner_and_descendants":
        reason = kind if isinstance(kind, str) and kind else "unreported"
        raise RuntimeError("colleague_turn_containment_unprovable;" + reason)


def _attempt_token():
    """The queue's token for this attempt, or ``None`` when absent or malformed."""
    token = os.environ.get(containment.ENV)
    if not token:
        return None
    try:
        return containment.checked_token(token)
    except ValueError:
        return None


def _trusted_containment_available(runner, *,
                                  expected_job: str | None = None,
                                  expected_attempt: str | None = None,
                                  expected_nonce: str | None = None,
                                  expected_token: str | None = None) -> bool:
    """Pre-execution containment gate.

    Must fail closed (return False) unless a real attempt-bound execution backend
    adapter reports a binding created by the trusted host for exactly this
    job/attempt/nonce.

    Expected identity values come from the host/pool, never from the payload or
    the proof itself.

    Self-declared booleans, gateway-delegated runners, and post-execution
    quiescence/stop proofs must NEVER open this pre-execution gate.
    """
    if getattr(runner, "attempt_containment", None) != "runner_and_descendants":
        return False
    adapter = getattr(runner, "execution_backend_adapter", None)
    if adapter is None or not hasattr(adapter, "verify_attempt_binding") or not callable(adapter.verify_attempt_binding):
        return False
    if not (expected_job and expected_attempt and expected_nonce and expected_token):
        return False
    try:
        containment.checked_token(expected_token)
        binding = adapter.verify_attempt_binding(
            expected_job=expected_job,
            expected_attempt=expected_attempt,
            expected_nonce=expected_nonce,
            expected_token=expected_token,
        )
        if not isinstance(binding, dict) or binding.get("ok") is not True:
            return False
        if binding.get("proof_scope") == "subreaper_tree" or "quiescent" in binding:
            return False
        if binding.get("proof_type") != "attempt_pre_execution_binding":
            return False
        if binding.get("backend") != "bwrap_subreaper":
            return False
        return True
    except Exception:
        return False


def _attempt_evidence(runner=None, *,
                      expected_job: str | None = None,
                      expected_attempt: str | None = None,
                      expected_nonce: str | None = None,
                      expected_token: str | None = None,
                      expected_supervisor_pid: int | None = None):
    """Second proof, from the queue's token and trusted boundary, that nothing of this job survives."""
    token = expected_token or _attempt_token()
    if token is None:
        return None
    unit = os.environ.get("AGENTCORE_CONTAINMENT_UNIT") or getattr(runner, "containment_unit", None)
    container = os.environ.get("AGENTCORE_CONTAINMENT_CONTAINER") or getattr(runner, "containment_container", None)
    runner_contained = bool(getattr(runner, "trusted_containment", False))

    supervisor_proof = None
    adapter = getattr(runner, "execution_backend_adapter", None)
    if hasattr(runner, "trusted_containment_evidence") and callable(runner.trusted_containment_evidence):
        supervisor_proof = runner.trusted_containment_evidence()
    elif adapter is not None and hasattr(adapter, "trusted_containment_evidence") and callable(adapter.trusted_containment_evidence):
        supervisor_proof = adapter.trusted_containment_evidence()
    elif os.environ.get("AGENTCORE_SUPERVISOR_PROOF_PATH"):
        try:
            supervisor_proof = json.loads(Path(os.environ["AGENTCORE_SUPERVISOR_PROOF_PATH"]).read_text(encoding="utf-8"))
        except Exception:
            supervisor_proof = None

    expected_job = expected_job or os.environ.get("SUPERAGENT_JOB_ID")
    expected_attempt = expected_attempt or os.environ.get("SUPERAGENT_ATTEMPT_ID")
    expected_nonce = expected_nonce or os.environ.get("SUPERAGENT_SUPERVISOR_NONCE") or os.environ.get("AGENTCORE_SUPERVISOR_NONCE")
    if expected_supervisor_pid is None:
        spid_str = os.environ.get("SUPERAGENT_SUPERVISOR_PID") or os.environ.get("AGENTCORE_SUPERVISOR_PID")
        if spid_str and spid_str.isdigit():
            expected_supervisor_pid = int(spid_str)

    evidence = containment.sweep_and_verify(
        token,
        systemd_unit=unit,
        docker_container=container,
        runner_contained=runner_contained,
        supervisor_proof=supervisor_proof,
        expected_job=expected_job,
        expected_attempt=expected_attempt,
        expected_nonce=expected_nonce,
        expected_supervisor_pid=expected_supervisor_pid,
    )
    if containment.evidence_id(evidence) is None:
        raise RuntimeError("attempt_containment_unproven")
    return evidence


def _discard_attempt():
    """Best-effort cleanup after a failure: a crashed turn may have left descendants.

    Never raises, so it cannot mask the failure that brought us here, and it
    makes no claim — the receipt stays generic.
    """
    token = _attempt_token()
    if token is None:
        return
    try:
        containment.sweep(token)
    except Exception:
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", required=True)
    args = parser.parse_args()
    try:
        module, symbol = args.bootstrap.split(":", 1)
        if not module or not symbol or not symbol.isidentifier():
            raise ValueError("invalid_bootstrap")
        runner, authorize = getattr(importlib.import_module(module), symbol)()
        _require_attempt_containment(runner)
        job = json.load(sys.stdin)
        if not isinstance(job, dict) or set(job) != {"job", "attempt", "payload"}:
            raise ValueError("invalid_queue_envelope")
        payload = job.get("payload")
        if isinstance(payload, dict) and any(k in payload for k in containment.FORBIDDEN_PAYLOAD_BINDING_KEYS):
            raise RuntimeError("payload_forbidden_binding_keys")
        # A token or a runner assertion cannot certify whole-attempt containment.
        # Until an attempt-bound host adapter exists, refuse before side effects.
        token = _attempt_token()
        expected_job = job["job"]
        expected_attempt = job["attempt"]
        expected_nonce = os.environ.get("SUPERAGENT_SUPERVISOR_NONCE") or os.environ.get("AGENTCORE_SUPERVISOR_NONCE")
        if token is None or not _trusted_containment_available(
            runner,
            expected_job=expected_job,
            expected_attempt=expected_attempt,
            expected_nonce=expected_nonce,
            expected_token=token,
        ):
            raise RuntimeError("attempt_containment_unavailable")
        result = execute_payload(job["payload"], runner=runner, authorize_assignment=authorize)
        evidence = _attempt_evidence(
            runner,
            expected_job=expected_job,
            expected_attempt=expected_attempt,
            expected_nonce=expected_nonce,
            expected_token=token,
        )
        print(json.dumps({"ok": True, "result": result,
                          "quiescence_evidence_id": containment.evidence_id(evidence)},
                         ensure_ascii=False))
        return 0
    except Exception:
        _discard_attempt()
        # Provider failures or user input may contain secrets. Do not copy them
        # into the public receipt or claim that an uncertain task was undone.
        print(json.dumps({"ok": False, "error": "colleague_execution_failed_or_unknown"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

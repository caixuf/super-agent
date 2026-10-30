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


def _trusted_containment_available(runner):
    """Check if trusted whole-attempt containment (cgroup, container, or provider) is available."""
    if os.environ.get("AGENTCORE_CONTAINMENT_UNIT"):
        return True
    if os.environ.get("AGENTCORE_CONTAINMENT_CONTAINER"):
        return True
    if getattr(runner, "trusted_containment", False):
        return True
    return False


def _attempt_evidence(runner=None):
    """Second proof, from the queue's token and trusted boundary, that nothing of this job survives."""
    token = _attempt_token()
    if token is None:
        return None
    unit = os.environ.get("AGENTCORE_CONTAINMENT_UNIT") or getattr(runner, "containment_unit", None)
    container = os.environ.get("AGENTCORE_CONTAINMENT_CONTAINER") or getattr(runner, "containment_container", None)
    runner_contained = bool(getattr(runner, "trusted_containment", False))
    evidence = containment.sweep_and_verify(token, systemd_unit=unit, docker_container=container,
                                            runner_contained=runner_contained)
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
        # A token scan alone cannot certify whole-attempt containment. If a token
        # is present, require trusted containment (cgroup, container, or provider)
        # before execution to prevent side effects before proving quiescence.
        if _attempt_token() is not None and not _trusted_containment_available(runner):
            raise RuntimeError("attempt_containment_unavailable")
        result = execute_payload(job["payload"], runner=runner, authorize_assignment=authorize)
        evidence = _attempt_evidence(runner)
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

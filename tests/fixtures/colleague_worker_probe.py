"""Host-supplied worker bootstraps for the containment gate tests.

Each factory returns ``(runner, authorize_assignment)`` exactly as a real
deployment does. The gate is about who can prove an attempt is gone, so the
doubles report the same ``attempt_containment`` values the real launchers do.
"""
from agentcore.colleague_hermes_runner import HermesColleagueRunner


def _permit(owner, member_id, generation):
    return True


def _hermes(kind):
    # The property only reads ``_builtin_launcher``; constructing a full runner
    # would require a registry, supervisor and provisioner the gate never sees.
    runner = object.__new__(HermesColleagueRunner)
    runner._builtin_launcher = kind == "gateway_delegated"
    return runner


def builtin():
    """The built-in launcher: it only drives ``hermes chat`` from outside."""
    return _hermes("gateway_delegated"), _permit


def injected():
    """A host launcher: the host, not this worker, owns the proof."""
    return _hermes("host_owned"), _permit


class InProcess:
    """A runner that executes the turn here, so it can answer for every descendant."""

    attempt_containment = "runner_and_descendants"

    def run(self, *, member_id, generation, request_id, prompt, owner_profile, owner_session):
        return {"request_id": request_id,
                "requested_by": {"profile": owner_profile, "session": owner_session},
                "executed_by": {"member_id": member_id, "generation": generation},
                "task_result_status": "output_unparsed"}


def plain():
    return InProcess(), _permit


class Trusted(InProcess):
    trusted_containment = True


def trusted():
    return Trusted(), _permit


class Silent:
    """A runner that says nothing about containment; the gate must not assume."""

    def run(self, **kwargs):
        raise AssertionError("refused before execution")


def silent():
    return Silent(), _permit


class Failing:
    """A contained runner whose turn raises, so the worker takes its failure path."""

    attempt_containment = "runner_and_descendants"

    def run(self, **kwargs):
        raise RuntimeError("provider_failed")


def failing():
    return Failing(), _permit

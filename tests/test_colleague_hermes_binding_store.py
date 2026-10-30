import hashlib
import pytest

from agentcore.colleague_registry import Actor, Registry, Supervisor
from agentcore.colleague_hermes_runner import (
    HermesColleagueRunner, HermesSessionBindingStore, HermesSessionCreationPending,
    ProcessResult,
)


def QUIESCED(code, stdout, stderr, *, timed_out=False, token="0" * 32):
    return ProcessResult(code, stdout, stderr, timed_out=timed_out, quiescent=True,
                         containment={"token": token, "signalled": [], "survivors": [],
                                      "quiescent": True, "proof_scope": "whole_attempt"})


class Ready:
    def status(self, member_id, generation):
        return {"ready": True}


@pytest.fixture
def context(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite", profile_id="team", primary_id="leader")
    leader, host = Actor("team", "leader"), Supervisor("team")
    registry.define_role(actor=leader, role_id="review", label="Reviewer",
                         duty="Review supplied work", boundaries=["Do not alter files"],
                         request_id="role")
    registry.hire(actor=leader, member_id="reviewer", role_id="review",
                  display_name="Reviewer", request_id="hire")
    executable = tmp_path / "hermes"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    state = tmp_path / "binding-state"
    state.mkdir(mode=0o700)
    return registry, host, executable, state


class LostResponseCreator:
    def __init__(self):
        self.responses = {}
        self.requests = []
        self.lose_once = True

    def create(self, profile, request_id):
        self.requests.append((profile, request_id))
        sid = self.responses.setdefault(
            (profile, request_id),
            "local-" + hashlib.sha256((profile + request_id).encode()).hexdigest())
        if self.lose_once:
            self.lose_once = False
            raise TimeoutError("response lost after native create")
        return sid


def test_pending_binding_recovers_same_create_request_after_restart(context):
    registry, host, _, state = context
    creator = LostResponseCreator()
    profiles = {("reviewer", 1): "reviewer_profile"}
    first = HermesSessionBindingStore(registry=registry, supervisor=host, profiles=profiles,
                                      creator=creator, state_dir=state)
    with pytest.raises(HermesSessionCreationPending, match="same binding"):
        first.resolve("reviewer", 1, "main", "owner-a")

    restarted = HermesSessionBindingStore(registry=registry, supervisor=host, profiles=profiles,
                                          creator=creator, state_dir=state)
    owner_a = restarted.resolve("reviewer", 1, "main", "owner-a")
    owner_a_again = restarted.resolve("reviewer", 1, "main", "owner-a")
    owner_b = restarted.resolve("reviewer", 1, "main", "owner-b")
    assert owner_a == owner_a_again
    assert owner_a.hermes_session_id != owner_b.hermes_session_id
    assert len(creator.requests) == 3
    assert creator.requests[0] == creator.requests[1]
    assert creator.requests[0][1] != creator.requests[2][1]


def test_binding_checks_registry_generation_before_native_creation(context):
    registry, host, _, state = context
    creator = LostResponseCreator()
    store = HermesSessionBindingStore(registry=registry, supervisor=host,
        profiles={("reviewer", 1): "reviewer_profile"}, creator=creator, state_dir=state)
    with pytest.raises(ValueError, match="stale_member_generation"):
        store.resolve("reviewer", 2, "main", "owner-a")
    assert creator.requests == []


def test_runner_can_use_persistent_binding_store(context):
    registry, host, executable, state = context
    creator = type("Creator", (), {
        "create": lambda self, profile, request_id:
            "local-" + hashlib.sha256((profile + request_id).encode()).hexdigest()
    })()
    store = HermesSessionBindingStore(registry=registry, supervisor=host,
        profiles={("reviewer", 1): "reviewer_profile"}, creator=creator, state_dir=state)
    calls = []
    def launcher(argv, *, input, timeout):
        calls.append((argv, input))
        return QUIESCED(0, "Hermes output", "")
    runner = HermesColleagueRunner(registry=registry, supervisor=host, provisioner=Ready(),
        bindings=store, executable=executable, state_dir=state, launcher=launcher)

    result = runner.run(member_id="reviewer", generation=1, request_id="req-1",
                        prompt="Review", owner_profile="main", owner_session="owner-a")
    argv = calls[0][0]
    assert argv[argv.index("--profile") + 1] == "reviewer_profile"
    assert argv[argv.index("--resume") + 1] == result["executed_by"]["hermes_session_id"]
    assert result["requested_by"] == {"profile": "main", "session": "owner-a"}


def test_reservation_tracks_session_create_across_deactivation(context):
    import threading
    from agentcore.colleague_hermes_runner import ProcessResult

    registry, host, executable, state = context
    entered, release = threading.Event(), threading.Event()

    class BlockingCreator:
        def create(self, profile, request_id):
            entered.set()
            assert release.wait(5)
            return "local-" + hashlib.sha256((profile + request_id).encode()).hexdigest()

    store = HermesSessionBindingStore(registry=registry, supervisor=host,
        profiles={("reviewer", 1): "reviewer_profile"}, creator=BlockingCreator(), state_dir=state)
    runner = HermesColleagueRunner(registry=registry, supervisor=host, provisioner=Ready(),
        bindings=store, executable=executable, state_dir=state,
        launcher=lambda argv, *, input, timeout: QUIESCED(0, "ok", ""))
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(
        runner.run(member_id="reviewer", generation=1, request_id="race",
                   prompt="Review", owner_profile="main", owner_session="owner-race")))
    worker.start()
    assert entered.wait(5), "reservation must precede native session creation"

    retired = registry.request_deactivation(actor=Actor("team", "leader"),
        member_id="reviewer", request_id="retire-race")
    assert retired["status"] == "draining"
    with pytest.raises(ValueError, match="active_work_remaining"):
        registry.finalize_deactivation(supervisor=host, member_id="reviewer",
                                       generation=1, external_idle=True)

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert outcomes and outcomes[0]["execution_state"] == "hermes_process_succeeded"
    assert registry.finalize_deactivation(supervisor=host, member_id="reviewer",
                                          generation=1, external_idle=True)["status"] == "inactive"


def test_deactivation_before_reservation_prevents_session_creation(context):
    registry, host, executable, state = context
    creator = LostResponseCreator()
    store = HermesSessionBindingStore(registry=registry, supervisor=host,
        profiles={("reviewer", 1): "reviewer_profile"}, creator=creator, state_dir=state)
    runner = HermesColleagueRunner(registry=registry, supervisor=host, provisioner=Ready(),
        bindings=store, executable=executable, state_dir=state,
        launcher=lambda argv, *, input, timeout: QUIESCED(0, "ok", ""))
    registry.request_deactivation(actor=Actor("team", "leader"),
        member_id="reviewer", request_id="retire-before")
    with pytest.raises(PermissionError, match="member_not_accepting_work"):
        runner.run(member_id="reviewer", generation=1, request_id="too-late",
                   prompt="Review", owner_profile="main", owner_session="owner-late")
    assert creator.requests == []
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 0


def test_unknown_session_create_keeps_reservation_until_host_recovery(context):
    registry, host, executable, state = context
    creator = LostResponseCreator()
    launched = []
    store = HermesSessionBindingStore(registry=registry, supervisor=host,
        profiles={("reviewer", 1): "reviewer_profile"}, creator=creator, state_dir=state)
    runner = HermesColleagueRunner(registry=registry, supervisor=host, provisioner=Ready(),
        bindings=store, executable=executable, state_dir=state,
        launcher=lambda argv, *, input, timeout: launched.append(argv))
    with pytest.raises(HermesSessionCreationPending):
        runner.run(member_id="reviewer", generation=1, request_id="create-lost",
                   prompt="Review", owner_profile="main", owner_session="owner-lost")
    status = registry.work_status(supervisor=host, member_id="reviewer", generation=1)
    assert status["tracked_open"] == 1
    assert launched == []

    result = runner.reconcile_uncertain(member_id="reviewer", generation=1,
        request_id="create-lost", owner_profile="main", owner_session="owner-lost",
        process_quiesced=True, side_effects_reviewed=True)
    assert result["resolved"] is True
    assert result["retry_permitted"] is False
    status = registry.work_status(supervisor=host, member_id="reviewer", generation=1)
    assert status["tracked_open"] == 0
    assert len(creator.requests) == 2
    assert creator.requests[0] == creator.requests[1]
    assert launched == []

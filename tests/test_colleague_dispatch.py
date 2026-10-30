import json
import os
import sys
from pathlib import Path

import pytest

from agentcore.colleague_dispatch import ColleagueDispatch, execute_payload
from agentcore.pool import Pool
from agentcore.worker import Worker


OWNER = {"profile": "leader", "session": "conversation-a"}


def permit(owner, member, generation):
    return owner == OWNER and member == "alice" and generation == 1


def request(**changes):
    value = dict(owner=OWNER, member_id="alice", generation=1,
                 request_id="request-1", prompt="Review the project")
    value.update(changes)
    return value


def test_admission_deduplicates_and_prevents_cross_session_inspection(tmp_path):
    pool = Pool(tmp_path / "pool")
    dispatch = ColleagueDispatch(pool, authorize_assignment=permit, dedicated=True)
    first = dispatch.submit(**request())
    assert dispatch.submit(**request()) == first
    with pytest.raises(ValueError, match="dedupe key reused"):
        dispatch.submit(**request(prompt="Different work"))
    dispatch.authorize_assignment = lambda owner, member, generation: (
        owner.get("profile") == "leader" and owner.get("session") == "conversation-a"
        and member == "alice" and generation == 1)
    with pytest.raises(ValueError, match="dedupe key reused"):
        dispatch.submit(**request(owner={**OWNER, "turn": "another-turn"}))
    with pytest.raises(PermissionError):
        dispatch.inspect(owner={"profile": "leader", "session": "other"}, job_id=first["job_id"])
    with pool.db() as db:
        row = db.execute("SELECT replay_safe,max_tries,origin FROM jobs WHERE id=?",
                         (first["job_id"],)).fetchone()
    assert tuple(row) == (0, 1, None)


def test_execution_rechecks_authorization():
    class NeverRun:
        def run(self, **kwargs):
            raise AssertionError("unauthorized execution")
    with pytest.raises(PermissionError, match="assignment_not_authorized"):
        execute_payload(request(), runner=NeverRun(), authorize_assignment=lambda *args: False)


def test_execution_rejects_result_attributed_to_another_member():
    class WrongMember:
        def run(self, **kwargs):
            return {"request_id": kwargs["request_id"],
                    "requested_by": {"profile": "leader", "session": "conversation-a"},
                    "executed_by": {"member_id": "bob", "generation": 1}}
    with pytest.raises(ValueError, match="colleague_result_identity_mismatch"):
        execute_payload(request(), runner=WrongMember(), authorize_assignment=permit)


def test_interrupted_assignment_is_not_replayed(tmp_path):
    pool = Pool(tmp_path / "pool")
    dispatch = ColleagueDispatch(pool, authorize_assignment=permit, dedicated=True)
    job = dispatch.submit(**request())["job_id"]
    row, fd = pool.claim()
    os.close(fd)  # Simulate a dead worker before the claim is settled.
    with pool.db() as db:
        db.execute("UPDATE jobs SET heartbeat=0 WHERE id=?", (job,))
    assert pool.claim(lease=1) is None
    observed = dispatch.inspect(owner=OWNER, job_id=job)
    assert observed["state"] == "failed"
    assert observed["result"]["reason"] == "interrupted; replay unavailable"


@pytest.mark.parametrize("backend", ["stdlib", "flowcoro"])
def test_worker_executes_queue_and_keeps_request_owner(tmp_path, monkeypatch, backend):
    if backend == "flowcoro":
        pytest.importorskip("flowcoro_py")
    root = Path(__file__).resolve().parents[1]
    marker = tmp_path / "runner-was-executed"
    monkeypatch.setenv("COLLEAGUE_TEST_MARKER", str(marker))
    (tmp_path / "host_fixture.py").write_text('''
import os
from pathlib import Path
class Runner:
    attempt_containment = "runner_and_descendants"
    def run(self, **kwargs):
        Path(os.environ["COLLEAGUE_TEST_MARKER"]).write_text("executed")
        return {"execution_state": "fixture", "request_id": kwargs["request_id"],
                "executed_by": {"member_id": kwargs["member_id"], "generation": kwargs["generation"]},
                "requested_by": {"profile": kwargs["owner_profile"], "session": kwargs["owner_session"]},
                "text": kwargs["prompt"]}
def bootstrap():
    def authorize(owner, member, generation):
        return owner == {"profile": "leader", "session": "conversation-a"} and member == "alice" and generation == 1
    return Runner(), authorize
''')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + ":" + str(root))
    pool = Pool(tmp_path / "pool")
    dispatch = ColleagueDispatch(pool, authorize_assignment=permit, dedicated=True)
    job = dispatch.submit(**request())["job_id"]
    command = [sys.executable, "-m", "agentcore.colleague_worker", "--bootstrap", "host_fixture:bootstrap"]
    assert Worker(pool, command, workers=1, backend=backend).run(drain=True)["attempts_finished"] == 1
    observed = dispatch.inspect(owner=OWNER, job_id=job)
    # The self-reported runner contract and empty token scan cannot prove the
    # whole attempt stopped. Fail closed until a real containment source exists.
    assert observed["state"] == "failed"
    assert observed["queue_state"] == "failed"
    assert observed["result"]["runner_error"] == "colleague_execution_failed_or_unknown"
    assert not marker.exists(), "uncontained work must be rejected before execution"
    assert pool.origin(job) is None


def test_worker_rechecks_permissions_after_enqueue(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    marker = tmp_path / "executed"
    (tmp_path / "host_revoked.py").write_text('''
import os
from pathlib import Path
class Runner:
    def run(self, **kwargs):
        Path(os.environ["COLLEAGUE_MARKER"]).write_text("ran")
        return {}
def bootstrap():
    return Runner(), lambda owner, member, generation: False
''')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + ":" + str(root))
    monkeypatch.setenv("COLLEAGUE_MARKER", str(marker))
    pool = Pool(tmp_path / "pool")
    dispatch = ColleagueDispatch(pool, authorize_assignment=permit, dedicated=True)
    job = dispatch.submit(**request())["job_id"]
    command = [sys.executable, "-m", "agentcore.colleague_worker", "--bootstrap", "host_revoked:bootstrap"]
    Worker(pool, command, workers=1).run(drain=True)
    assert dispatch.inspect(owner=OWNER, job_id=job)["state"] == "failed"
    assert not marker.exists()


def test_pool_worker_refuses_hermes_without_attempt_containment(tmp_path, monkeypatch):
    from agentcore.colleague_registry import Actor, Registry

    root = Path(__file__).resolve().parents[1]
    registry = Registry(tmp_path / "registry.sqlite", profile_id="team", primary_id="leader")
    actor = Actor("team", "leader")
    registry.define_role(actor=actor, role_id="review", label="Reviewer", duty="Review",
                         boundaries=["Respect authorization"], request_id="role")
    registry.hire(actor=actor, member_id="alice", role_id="review", display_name="Alice",
                  request_id="hire")
    (tmp_path / "runner-state").mkdir(mode=0o700)
    (tmp_path / "host_real_runner.py").write_text('''
import os, sys
from pathlib import Path
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_hermes_runner import HermesColleagueRunner, ProfileBinding, ProcessResult
def QUIESCED(code, stdout, stderr):
    return ProcessResult(code, stdout, stderr, quiescent=True,
        containment={"token": "0" * 32, "signalled": [], "survivors": [], "quiescent": True, "proof_scope": "whole_attempt"})
class Ready:
    def status(self, member_id, generation):
        return {"ready": True}
def bootstrap():
    root = Path(os.environ["COLLEAGUE_TEST_ROOT"])
    registry = Registry(root / "registry.sqlite", profile_id="team", primary_id="leader")
    binding = ProfileBinding("alice", 1, "leader", "conversation-a", "alice_profile", "local-" + "a" * 64)
    def launch(argv, *, input, timeout):
        assert argv[1:3] == ["--profile", "alice_profile"]
        assert "Review the project" in input
        return QUIESCED(0, "fixture model output", "")
    runner = HermesColleagueRunner(registry=registry, supervisor=Supervisor("team"),
        provisioner=Ready(), bindings={("alice", 1, "leader", "conversation-a"): binding},
        executable=sys.executable, state_dir=root / "runner-state", launcher=launch)
    return runner, lambda owner, member, generation: owner == {"profile": "leader", "session": "conversation-a"} and member == "alice" and generation == 1
''')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + ":" + str(root))
    monkeypatch.setenv("COLLEAGUE_TEST_ROOT", str(tmp_path))
    pool = Pool(tmp_path / "pool")
    dispatch = ColleagueDispatch(pool, authorize_assignment=permit, dedicated=True)
    job = dispatch.submit(**request())["job_id"]
    command = [sys.executable, "-m", "agentcore.colleague_worker", "--bootstrap", "host_real_runner:bootstrap"]
    Worker(pool, command, workers=1).run(drain=True)
    observed = dispatch.inspect(owner=OWNER, job_id=job)
    assert observed["state"] == "failed"
    assert observed["result"]["runner_error"] == "colleague_execution_failed_or_unknown"
    from agentcore.colleague_registry import Supervisor
    assert registry.work_status(supervisor=Supervisor("team"), member_id="alice", generation=1)["tracked_open"] == 0


def test_pool_worker_refuses_a_runner_whose_turn_runs_in_the_gateway(tmp_path, monkeypatch):
    """A real turn may leave the process group; the worker must still end it.

    ``setsid`` in the fixture is the escape: the pool's killpg cannot reach it,
    so the attempt token is what proves (and forces) quiescence.
    """
    from agentcore.colleague_registry import Actor, Registry, Supervisor

    root = Path(__file__).resolve().parents[1]
    registry = Registry(tmp_path / "registry.sqlite", profile_id="team", primary_id="leader")
    actor = Actor("team", "leader")
    registry.define_role(actor=actor, role_id="review", label="Reviewer", duty="Review",
                         boundaries=["Respect authorization"], request_id="role")
    registry.hire(actor=actor, member_id="alice", role_id="review", display_name="Alice",
                  request_id="hire")
    (tmp_path / "runner-state").mkdir(mode=0o700)
    fake = tmp_path / "fake_hermes"
    fake.write_text(
        "#!/bin/sh\n"
        "setsid sleep 120 >/dev/null 2>&1 &\n"
        'echo $! > "$ESCAPEE_PIDFILE"\n'
        "echo 'turn complete'\n")
    fake.chmod(0o755)
    pidfile = tmp_path / "escapee.pid"
    (tmp_path / "host_builtin_launcher.py").write_text('''
import os
from pathlib import Path
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_hermes_runner import HermesColleagueRunner, ProfileBinding
class Ready:
    def status(self, member_id, generation):
        return {"ready": True}
def bootstrap():
    root = Path(os.environ["COLLEAGUE_TEST_ROOT"])
    registry = Registry(root / "registry.sqlite", profile_id="team", primary_id="leader")
    binding = ProfileBinding("alice", 1, "leader", "conversation-a", "alice_profile", "local-" + "a" * 64)
    runner = HermesColleagueRunner(registry=registry, supervisor=Supervisor("team"),
        provisioner=Ready(), bindings={("alice", 1, "leader", "conversation-a"): binding},
        executable=Path(os.environ["COLLEAGUE_FAKE_HERMES"]), state_dir=root / "runner-state")
    return runner, lambda owner, member, generation: owner == {"profile": "leader", "session": "conversation-a"} and member == "alice" and generation == 1
''')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + ":" + str(root))
    monkeypatch.setenv("COLLEAGUE_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("COLLEAGUE_FAKE_HERMES", str(fake))
    monkeypatch.setenv("ESCAPEE_PIDFILE", str(pidfile))
    pool = Pool(tmp_path / "pool")
    dispatch = ColleagueDispatch(pool, authorize_assignment=permit, dedicated=True)
    job = dispatch.submit(**request())["job_id"]
    command = [sys.executable, "-m", "agentcore.colleague_worker",
               "--bootstrap", "host_builtin_launcher:bootstrap"]
    Worker(pool, command, workers=1).run(drain=True)

    observed = dispatch.inspect(owner=OWNER, job_id=job)
    # The built-in launcher runs a turn inside the shared gateway, where the
    # attempt token never reaches the processes the turn starts. Its sweep can
    # only prove that the client exited, so the worker must refuse the runner
    # instead of reading an empty sweep as containment.
    assert observed["state"] == "failed"
    assert observed["queue_state"] == "failed"
    assert observed["result"] is None or observed["result"].get("ok") is not True
    assert not pidfile.exists(), "the refused runner must not have started a turn"
    assert registry.work_status(supervisor=Supervisor("team"), member_id="alice",
                                generation=1)["tracked_open"] == 0


def test_digital_signing_and_verification_on_colleague_result(tmp_path):
    from agentcore.colleague_dispatch import compute_result_signature, verify_result_signature

    valid_result = {
        "request_id": "req-sign-01",
        "requested_by": {"profile": "leader", "session": "session-1"},
        "executed_by": {"member_id": "alice", "generation": 1},
        "text": "Analysis complete with no defect.",
    }
    key = "secret-attribution-token-xyz"

    sig = compute_result_signature(valid_result, key)
    assert isinstance(sig, str) and len(sig) == 64
    valid_result["signature"] = sig

    assert verify_result_signature(valid_result, key) is True
    # Tampered signature
    assert verify_result_signature(valid_result, key, signature="bad" + sig[3:]) is False
    # Tampered text
    tampered = dict(valid_result, text="Malicious modification")
    assert verify_result_signature(tampered, key) is False
    # Wrong key
    assert verify_result_signature(valid_result, "other-key") is False

    # Integration with execute_payload
    class SignedRunner:
        def run(self, **kwargs):
            return {
                "request_id": kwargs["request_id"],
                "requested_by": {"profile": kwargs["owner_profile"], "session": kwargs["owner_session"]},
                "executed_by": {"member_id": kwargs["member_id"], "generation": kwargs["generation"]},
                "text": "Signed response text",
            }

    req = request(request_id="req-sign-02", prompt="Work to sign")
    signed_res = execute_payload(req, runner=SignedRunner(), authorize_assignment=permit, signing_key=key)
    assert "signature" in signed_res
    assert verify_result_signature(signed_res, key) is True

    # Hermes runner style output with cli_stdout_unparsed
    hermes_res = {
        "request_id": "req-sign-03",
        "requested_by": {"profile": "leader", "session": "session-1"},
        "executed_by": {"member_id": "alice", "generation": 1},
        "cli_stdout_unparsed": "Standard unparsed CLI output stream",
    }
    hermes_sig = compute_result_signature(hermes_res, key)
    hermes_res["signature"] = hermes_sig
    assert verify_result_signature(hermes_res, key) is True

    # ColleagueDispatch.inspect signature enforcement
    pool = Pool(tmp_path / "sign_pool")
    dispatch = ColleagueDispatch(pool, authorize_assignment=permit, dedicated=True, signing_key=key)
    sub = dispatch.submit(**request(request_id="req-sign-04"))
    job_id = sub["job_id"]

    # Manually finish pool job with unsigned vs signed results
    row, fd = pool.claim()
    os.close(fd)
    pool.finish(job_id, row["attempt"], True, {"unsigned": "receipt"})
    insp_unsigned = dispatch.inspect(owner=OWNER, job_id=job_id)
    assert insp_unsigned["signature_valid"] is False


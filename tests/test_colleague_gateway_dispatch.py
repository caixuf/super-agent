import hashlib
import pytest

from agentcore.colleague_registry import Actor, Registry, Supervisor
from agentcore.colleague_hermes_runner import ProfileBinding
from agentcore.colleague_gateway_dispatch import GatewayColleagueDispatcher


class Ready:
    def status(self, member, generation):
        return {"ready": True}


class Bindings:
    def __init__(self):
        self.hermes_profile = "review_profile"
        self.session_override = None
    def resolve(self, member, generation, owner_profile, owner_session, *, allow_draining=False):
        sid = self.session_override or "local-" + hashlib.sha256((owner_profile + owner_session).encode()).hexdigest()
        return ProfileBinding(member, generation, owner_profile, owner_session, self.hermes_profile, sid)


class FakeGateway:
    hermes_profile = "review_profile"
    gateway_profile_id = "/tmp/isolated-hermes-profile"
    principal_id = "host:review_profile"
    def __init__(self):
        self.calls = []
        self.admissions = {}
        self.status = "queued"
        self.drop_after_accept = False
        self.wrong_ref = False
        self.wrong_result_session = False
        self.generation = 4
    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "session.resume":
            return {"stored_session_id": params["session_id"], "execution_generation": 4}
        if method == "prompt.submit":
            key = (params["session_id"], params["input_id"])
            prior = self.admissions.get(key)
            if prior and prior["text"] != params["text"]:
                raise ValueError("admission_conflict")
            if prior is None:
                prior = {"admission_id": "adm-" + params["input_id"][-12:], "text": params["text"],
                         "execution_generation": self.generation if self.status != "queued" else None, "status": self.status}
                self.admissions[key] = prior
            if self.drop_after_accept:
                self.drop_after_accept = False
                raise TimeoutError("lost response")
            return {"admission_id":prior["admission_id"],"execution_generation":prior["execution_generation"],
                    "status":prior["status"],"ref":{"profile_id":"wrong" if self.wrong_ref else self.gateway_profile_id,
                    "session_id":params["session_id"]}}
        if method == "prompt.receipt":
            prior = next(v for (sid, _), v in self.admissions.items()
                         if sid == params["session_id"] and v["admission_id"] == params["admission_id"])
            return {"admission_id": prior["admission_id"],
                    "execution_generation": self.generation if self.status != "queued" else None,
                    "ref":{"profile_id":"wrong" if self.wrong_ref else self.gateway_profile_id,
                    "session_id":params["session_id"]}, "status": self.status, "outcome": "completed" if self.status == "terminal" else None,
                    "result": {"session_id":"local-"+"0"*64 if self.wrong_result_session else params["session_id"],"completed":True,"failed":False,
                               "interrupted":False,"final_response":getattr(self,"response_text","reviewed"),"messages":["SECRET_HISTORY"],
                               "last_reasoning":"SECRET_REASONING","usage":{"secret":"SECRET_USAGE"}}
                    if self.status == "terminal" else None}
        raise AssertionError(method)


@pytest.fixture
def context(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite", profile_id="team", primary_id="leader")
    leader, supervisor = Actor("team", "leader"), Supervisor("team")
    registry.define_role(actor=leader, role_id="review", label="Reviewer", duty="Review work",
                         boundaries=["No file edits"], request_id="role")
    registry.hire(actor=leader, member_id="reviewer", role_id="review",
                  display_name="Reviewer", request_id="hire")
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    gateway = FakeGateway()
    dispatcher = GatewayColleagueDispatcher(registry=registry, supervisor=supervisor,
        provisioner=Ready(), bindings=Bindings(), connection_for_profile=lambda profile: gateway,
        all_work_quiescent=None, state_dir=state)
    return registry, supervisor, gateway, state, dispatcher


def _submit(dispatcher, owner=("main", "user-a"), request="req-1", prompt="review this"):
    return dispatcher.submit(owner={"profile":owner[0], "session":owner[1]}, member_id="reviewer",
        generation=1, request_id=request, prompt=prompt)


def test_gateway_contract_reuses_input_id_and_admission_after_response_loss(context):
    registry, supervisor, gateway, state, dispatcher = context
    gateway.drop_after_accept = True
    lost = _submit(dispatcher)
    assert lost["state"] == "unknown" and lost["retry_input_id"].startswith("colleague-")
    assert lost["member"]["display_name"] == "Reviewer"
    recovered = _submit(dispatcher)
    assert recovered["state"] == "queued"
    calls = [p for method,p in gateway.calls if method == "prompt.submit"]
    assert len(calls) == 2
    assert calls[0]["input_id"] == calls[1]["input_id"]
    assert calls[0]["text"] == calls[1]["text"]
    assert "unattended" not in calls[0] and "finite" not in calls[0]
    assert recovered["admission_id"] == next(iter(gateway.admissions.values()))["admission_id"]
    assert [m for m,_ in gateway.calls].count("session.resume") == 2
    restarted = GatewayColleagueDispatcher(registry=registry, supervisor=supervisor, provisioner=Ready(),
        bindings=Bindings(), connection_for_profile=lambda profile: gateway, all_work_quiescent=None, state_dir=state)
    again = _submit(restarted)
    assert again["admission_id"] == recovered["admission_id"]


def test_request_conflict_and_owner_binding_isolation(context):
    registry, supervisor, gateway, state, dispatcher = context
    first = _submit(dispatcher)
    with pytest.raises(ValueError, match="request_id_conflict"):
        _submit(dispatcher, prompt="different payload")
    assert dispatcher.inspect(owner={"profile":"other", "session":"user-a"}, request_id="req-1") is None
    gateway.status = "terminal"
    pending = dispatcher.inspect(owner={"profile":"main", "session":"user-a"}, request_id="req-1")
    assert pending["state"] == "terminal"
    assert pending["reservation"] == "awaiting_quiescence_evidence"
    assert pending["member"] == {"member_id":"reviewer", "generation":1,
                                  "display_name":"Reviewer", "role_id":"review", "role_label":"Reviewer"}
    import hashlib
    assert pending["result"] == {"final_response":"reviewed","response_offset":0,
                                 "response_revision":hashlib.sha256(b"reviewed").hexdigest(),
                                 "response_next_offset":None,"response_truncated":False,
                                 "completed":True,"failed":False,"interrupted":False}
    assert "SECRET" not in repr(pending) and "messages" not in pending["result"]
    assert registry.work_status(supervisor=supervisor, member_id="reviewer", generation=1)["tracked_open"] == 1
    with pytest.raises(ValueError, match="member_has_exclusive_work"):
        _submit(dispatcher, owner=("main","user-b"), request="blocked")


def test_only_independent_quiescence_evidence_settles_and_two_users_get_separate_sessions(context):
    registry, supervisor, gateway, state, dispatcher = context
    first = _submit(dispatcher, request="a")
    gateway.status = "terminal"
    dispatcher.all_work_quiescent = lambda member,generation,profile,session,admission: {
        "all_owned_processes_terminal":True,"evidence_id":"host-check-1","member_id":member,
        "generation":generation,"hermes_profile":profile,"hermes_session_id":session,"admission_id":admission}
    done = dispatcher.inspect(owner={"profile":"main","session":"user-a"}, request_id="a")
    assert done["reservation"] == "closed"
    import sqlite3
    with sqlite3.connect(dispatcher.db_path) as db:
        evidence = db.execute("SELECT quiescence_evidence_id FROM tasks WHERE request_id='a'").fetchone()[0]
    assert evidence == "host-check-1"
    second = _submit(dispatcher, owner=("main","user-b"), request="b")
    submits = [p for method,p in gateway.calls if method == "prompt.submit"]
    assert len(submits) == 2
    assert submits[0]["session_id"] != submits[1]["session_id"]
    assert first["admission_id"] != second["admission_id"]


def test_receipt_states_are_not_collapsed_and_generation_is_checked(context):
    registry, supervisor, gateway, state, dispatcher = context
    submitted = _submit(dispatcher)
    for status in ("queued", "started", "terminal"):
        gateway.status = status
        receipt = dispatcher.inspect(owner={"profile":"main","session":"user-a"}, request_id="req-1")
        assert receipt["state"] == status
        assert receipt["execution_generation"] == (None if status == "queued" else 4)
    # Owner-bound association remains available after dispatcher reconstruction.
    restarted = GatewayColleagueDispatcher(registry=registry, supervisor=supervisor, provisioner=Ready(),
        bindings=Bindings(), connection_for_profile=lambda profile: gateway, all_work_quiescent=None, state_dir=state)
    final = restarted.inspect(owner={"profile":"main","session":"user-a"}, request_id="req-1")
    assert final["state"] == "terminal"
    assert final["result_visibility"] == "verified"
    assert "SECRET" not in repr(final)


def test_receipt_ref_and_execution_generation_are_fenced(context):
    registry, supervisor, gateway, state, dispatcher = context
    _submit(dispatcher)
    gateway.status = "started"
    assert dispatcher.inspect(owner={"profile":"main","session":"user-a"},request_id="req-1")["state"] == "started"
    gateway.generation = 5
    mismatch = dispatcher.inspect(owner={"profile":"main","session":"user-a"},request_id="req-1")
    assert mismatch["state"] == "unknown"
    gateway.generation = 4; gateway.wrong_ref = True
    mismatch = dispatcher.inspect(owner={"profile":"main","session":"user-a"},request_id="req-1")
    assert mismatch["state"] == "unknown"


def test_result_for_rotated_or_unproven_session_is_withheld(context):
    registry, supervisor, gateway, state, dispatcher = context
    _submit(dispatcher); gateway.status = "started"
    dispatcher.inspect(owner={"profile":"main","session":"user-a"},request_id="req-1")
    gateway.status = "terminal"; gateway.wrong_result_session = True
    result = dispatcher.inspect(owner={"profile":"main","session":"user-a"},request_id="req-1")
    assert result["state"] == "terminal"
    assert result["result"] is None
    assert result["result_visibility"] == "withheld_unverified_session_identity"


def test_long_public_reply_reports_truncation_and_omits_history():
    sid = "local-" + "a" * 64
    result, visibility = GatewayColleagueDispatcher._public_result(
        {"result": {"session_id": sid, "final_response": "x" * 12001,
                    "messages": [{"content": "private history"}], "completed": True}}, sid)
    assert visibility == "verified"
    assert len(result["final_response"]) == 12000
    assert result["response_truncated"] is True
    assert "messages" not in result


def test_long_reply_pages_are_session_bound_and_lossless(context):
    _, _, gateway, _, dispatcher = context
    _submit(dispatcher)
    gateway.status = "terminal"
    gateway.response_text = "甲" * 12000 + "乙" * 12000 + "末尾"
    owner = {"profile":"main", "session":"user-a"}
    first = dispatcher.inspect(owner=owner, request_id="req-1")["result"]
    second = dispatcher.inspect(owner=owner, request_id="req-1",
                                response_offset=first["response_next_offset"], response_revision=first["response_revision"])["result"]
    last = dispatcher.inspect(owner=owner, request_id="req-1",
                              response_offset=second["response_next_offset"], response_revision=second["response_revision"])["result"]
    assert first["final_response"] + second["final_response"] + last["final_response"] == gateway.response_text
    assert last["response_next_offset"] is None and last["response_truncated"] is False
    assert dispatcher.inspect(owner={"profile":"main", "session":"other"}, request_id="req-1",
                              response_offset=12000, response_revision=first["response_revision"]) is None
    with pytest.raises(ValueError, match="invalid_response_offset"):
        dispatcher.inspect(owner=owner, request_id="req-1", response_offset=True)
    with pytest.raises(ValueError, match="response_offset_out_of_range"):
        dispatcher.inspect(owner=owner, request_id="req-1", response_offset=len(gateway.response_text)+1,
                           response_revision=first["response_revision"])
    with pytest.raises(ValueError, match="response_revision_required"):
        dispatcher.inspect(owner=owner, request_id="req-1", response_offset=12000)
    gateway.response_text += "已更改"
    with pytest.raises(ValueError, match="response_revision_changed"):
        dispatcher.inspect(owner=owner, request_id="req-1", response_offset=12000,
                           response_revision=first["response_revision"])
    gateway.wrong_result_session = True
    assert dispatcher.inspect(owner=owner, request_id="req-1", response_offset=12000,
                              response_revision=first["response_revision"])["result"] is None


@pytest.mark.parametrize("drift", ["session", "hermes_profile", "gateway_profile", "principal"])
def test_persisted_binding_drift_blocks_prompt_resubmit(context, drift):
    registry, supervisor, gateway, state, dispatcher = context
    gateway.drop_after_accept = True
    assert _submit(dispatcher)["state"] == "unknown"
    alternate = FakeGateway()
    alternate.hermes_profile = "review_profile_v2" if drift == "hermes_profile" else "review_profile"
    alternate.gateway_profile_id = "/tmp/other-authority" if drift == "gateway_profile" else gateway.gateway_profile_id
    alternate.principal_id = "other-principal" if drift == "principal" else gateway.principal_id
    if drift == "session":
        dispatcher.bindings.session_override = "local-" + "a"*64
    elif drift == "hermes_profile":
        dispatcher.bindings.hermes_profile = "review_profile_v2"
        dispatcher.connection_for_profile = lambda profile: alternate
    else:
        dispatcher.connection_for_profile = lambda profile: alternate
    with pytest.raises(PermissionError, match="gateway_task_binding_changed"):
        _submit(dispatcher)
    assert len([1 for method,_ in gateway.calls if method == "prompt.submit"]) == 1
    assert len([1 for method,_ in alternate.calls if method == "prompt.submit"]) == 0


def test_quiescence_evidence_intent_recovers_settlement_after_crash(context, monkeypatch):
    registry, supervisor, gateway, state, dispatcher = context
    _submit(dispatcher); gateway.status = "terminal"
    dispatcher.all_work_quiescent = lambda member,generation,profile,session,admission: {
        "all_owned_processes_terminal":True,"evidence_id":"durable-proof","member_id":member,
        "generation":generation,"hermes_profile":profile,"hermes_session_id":session,"admission_id":admission}
    original = registry.settle_work
    calls = {"n":0}
    def crash_before_commit(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated process crash before registry settle")
        return original(**kwargs)
    monkeypatch.setattr(registry, "settle_work", crash_before_commit)
    with pytest.raises(OSError, match="simulated process crash"):
        dispatcher.inspect(owner={"profile":"main","session":"user-a"},request_id="req-1")
    import sqlite3
    with sqlite3.connect(dispatcher.db_path) as db:
        row = db.execute("SELECT quiescence_evidence_id,settlement_state FROM tasks WHERE request_id='req-1'").fetchone()
    assert row == ("durable-proof","pending")
    restarted = GatewayColleagueDispatcher(registry=registry, supervisor=supervisor, provisioner=Ready(),
        bindings=Bindings(), connection_for_profile=lambda profile: gateway, all_work_quiescent=None, state_dir=state)
    done = restarted.inspect(owner={"profile":"main","session":"user-a"},request_id="req-1")
    assert done["reservation"] == "closed"
    assert registry.work_status(supervisor=supervisor,member_id="reviewer",generation=1)["tracked_open"] == 0
    with sqlite3.connect(restarted.db_path) as db:
        row = db.execute("SELECT quiescence_evidence_id,settlement_state FROM tasks WHERE request_id='req-1'").fetchone()
    assert row == ("durable-proof","settled")

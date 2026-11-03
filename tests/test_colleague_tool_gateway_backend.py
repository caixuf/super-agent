import os
from pathlib import Path
import pytest
from agentcore.colleague_tool_gateway_backend import ToolEnabledGatewayProfileBackend
from agentcore.colleague_provisioner import InstanceSpec
from plugins.colleague_team.host_runtime import make_quiescence_verifier


class MockConnection:
    def __init__(self, profile, home, pong=True, active_sessions=None, receipt_status="terminal", resume_snap=None):
        self.hermes_profile = profile
        self.gateway_profile_id = str(home)
        self.pong = pong
        self.active_sessions = active_sessions or []
        self.receipt_status = receipt_status
        self.resume_snap = resume_snap or {"running": False, "prompts": [], "pending": []}
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "ping":
            return {"pong": self.pong}
        elif method == "session.active_list":
            return {"sessions": self.active_sessions}
        elif method == "prompt.submit":
            return {"admission_id": "admission-1", "status": "terminal", "outcome": "completed",
                    "ref": {"profile_id": self.gateway_profile_id, "session_id": params.get("session_id")}}
        elif method == "prompt.receipt":
            return {"admission_id": params.get("admission_id", "admission-1"),
                    "status": self.receipt_status, "outcome": "completed",
                    "ref": {"profile_id": self.gateway_profile_id, "session_id": params.get("session_id")},
                    "result": {"final_response": "done", "session_id": params.get("session_id")}}
        elif method == "session.resume":
            sid = params.get("session_id", "local-" + "0" * 64)
            snap = dict(self.resume_snap)
            snap["stored_session_id"] = sid
            snap["execution_generation"] = 1
            return snap
        return {}

    def close(self):
        pass


class MockHost:
    def __init__(self, profiles, connections):
        self.configured_profiles = frozenset(profiles)
        self.connections = connections

    def connection_for_profile(self, profile):
        return self.connections[profile]


@pytest.fixture
def test_env(tmp_path):
    home = tmp_path / "cases"
    home.mkdir(mode=0o700)
    (home / "memories").mkdir(mode=0o700)
    workspace = home / "workspace"
    workspace.mkdir(mode=0o700)
    (home / "config.yaml").write_text("model:\n  default: flash\n  provider: litellm\nproviders:\n  litellm:\n    base_url: http://127.0.0.1:4000/v1\n    api_key: secret\n")
    return home, workspace


def test_tool_backend_enables_tools_and_validates_isolation(test_env):
    home, workspace = test_env
    conn = MockConnection("cases", home)
    host = MockHost(["cases"], {"cases": conn})

    backend = ToolEnabledGatewayProfileBackend(
        gateway_host=host,
        member_profiles={("cases", 1): "cases"},
        profile_homes={"cases": home},
        model_options={"cases": {"model": "flash", "provider": "custom", "base_url": "http://127.0.0.1:4000/v1", "api_key": "secret"}},
        profile_workdirs={"cases": workspace},
    )

    options = backend.session_options("cases")
    assert options["safe_mode"] is False
    assert options["model"] == "flash"

    spec = InstanceSpec("caixuf", "cases", 1, "cases", "Cases", "DUTY", ())
    assert backend.inspect(spec) == {"state": "ready", "isolated": True, "private_memory": True, "no_channels": True}
    assert backend.idle(spec) is True


def test_quiescence_verifier_attestation_and_rejection(test_env):
    home, workspace = test_env
    conn = MockConnection("cases", home, receipt_status="terminal")
    host = MockHost(["cases"], {"cases": conn})

    verifier = make_quiescence_verifier(host, {"cases": workspace})

    # Terminal + not running -> True
    evidence = verifier("cases", 1, "cases", "session-1", "admission-1")
    assert evidence["all_owned_processes_terminal"] is True
    assert evidence["evidence_id"].startswith("quiescence-admission-1-1-")
    assert evidence["member_id"] == "cases"
    assert evidence["admission_id"] == "admission-1"

    # If receipt status is still started -> False
    conn.receipt_status = "started"
    not_done = verifier("cases", 1, "cases", "session-1", "admission-1")
    assert not_done["all_owned_processes_terminal"] is False

    # If session is still running -> False
    conn.receipt_status = "terminal"
    conn.resume_snap = {"running": True, "prompts": ["queued"]}
    still_running = verifier("cases", 1, "cases", "session-1", "admission-1")
    assert still_running["all_owned_processes_terminal"] is False


def test_install_tool_dispatcher_boots_and_settles(test_env, tmp_path, monkeypatch):
    from agentcore.colleague_registry import Registry, Supervisor, Actor
    from plugins.colleague_team.host_runtime import install_tool_dispatcher

    home, workspace = test_env
    state_dir = tmp_path / "dispatch_state"
    state_dir.mkdir(mode=0o700)
    db_path = tmp_path / "registry.sqlite"
    registry = Registry(str(db_path), profile_id="caixuf", primary_id="caixuf", primary_name="caixuf")
    supervisor = Supervisor("caixuf")

    # Define role and hire member
    actor = Actor("caixuf", "caixuf")
    registry.define_role(actor=actor, role_id="cases", label="Cases", duty="Write tests", boundaries=["tests only"], request_id="r1")
    registry.hire(actor=actor, member_id="cases", role_id="cases", display_name="Cases", request_id="h1")

    conn = MockConnection("cases", home, receipt_status="terminal")
    def mock_connect_profile(profile, phome):
        return conn, str(phome)

    class MockStore:
        def __init__(self, connection):
            self.connection = connection
        def call(self, method, **params):
            if method == "session.create":
                return {"stored_session_id": "local-" + "0" * 64}
            return {}

    monkeypatch.setattr("plugins.colleague_team.host_runtime.connect_profile", mock_connect_profile)
    monkeypatch.setattr("plugins.colleague_team.host_runtime.session_store", lambda c: MockStore(c))

    entries = [{"member_id": "cases", "generation": 1, "profile": "cases", "home": str(home), "workdir": str(workspace)}]
    bootstrap = install_tool_dispatcher(
        service_profile_name="caixuf",
        registry=registry,
        supervisor=supervisor,
        entries=entries,
        state_dir=str(state_dir),
    )
    try:
        ready = bootstrap.provisioner.provision("cases", 1)
        assert ready["ready"] is True

        # Submit task
        owner = {"profile": "caixuf", "session": "main-1"}
        sub = bootstrap.dispatcher.submit(owner=owner, member_id="cases", generation=1, request_id="req-1", prompt="generate test")
        assert sub["state"] == "terminal"
        # Inspect to trigger quiescence settlement
        ins = bootstrap.dispatcher.inspect(owner=owner, request_id="req-1")
        assert ins["state"] == "terminal"
        assert ins["reservation"] == "closed"
        assert ins["result"]["final_response"] == "done"
    finally:
        bootstrap.close()


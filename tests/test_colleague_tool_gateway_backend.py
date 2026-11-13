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


def test_tool_backend_reports_availability_without_claiming_isolation(test_env):
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
    assert backend.inspect(spec) == {"state": "available", "memory_directory": "owner_only",
                                     "no_channels": True, "execution_isolation": "unverified"}
    assert backend.idle(spec) is False
    with pytest.raises(RuntimeError, match="authoritative_quiescence_required"):
        backend.park(spec)


def test_quiescence_verifier_refuses_terminal_without_attempt_containment(test_env):
    home, workspace = test_env
    conn = MockConnection("cases", home, receipt_status="terminal")
    host = MockHost(["cases"], {"cases": conn})
    verifier = make_quiescence_verifier(host, {"cases": workspace})

    # A terminal receipt, idle session and empty workspace do not prove that
    # detached or inaccessible descendants are gone.
    evidence = verifier("cases", 1, "cases", "session-1", "admission-1")
    assert evidence["all_owned_processes_terminal"] is False
    assert evidence["reason"] == "attempt_bound_containment_unavailable"
    assert "evidence_id" not in evidence


def test_install_tool_dispatcher_exposes_live_native_profile(test_env, tmp_path, monkeypatch):
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
    monkeypatch.setattr("plugins.colleague_team.host_runtime.profile_is_unserved", lambda *_: False)

    entries = [{"member_id": "cases", "generation": 1, "profile": "cases", "home": str(home),
                "workdir": str(workspace), "model_policy": "profile"}]
    bootstrap = install_tool_dispatcher(
        service_profile_name="caixuf",
        registry=registry,
        supervisor=supervisor,
        entries=entries,
        state_dir=str(state_dir),
    )
    try:
        status = bootstrap.provisioner.provision("cases", 1)
        assert bootstrap.provisioner.backend.session_options("cases") == {"safe_mode": False}
        assert status["ready"] is True
        assert status["execution_isolation"] == "unverified"
    finally:
        bootstrap.close()



def test_install_tool_dispatcher_repairs_group_readable_workdir(test_env, tmp_path, monkeypatch):
    from agentcore.colleague_registry import Registry, Supervisor, Actor
    from plugins.colleague_team.host_runtime import install_tool_dispatcher

    home, workspace = test_env
    workspace.chmod(0o775)
    state_dir = tmp_path / "dispatch_state_repair"
    state_dir.mkdir(mode=0o700)
    registry = Registry(str(tmp_path / "registry_repair.sqlite"), profile_id="caixuf",
                        primary_id="caixuf", primary_name="caixuf")
    supervisor = Supervisor("caixuf")
    actor = Actor("caixuf", "caixuf")
    registry.define_role(actor=actor, role_id="cases", label="Cases", duty="Write tests",
                         boundaries=["tests only"], request_id="r1")
    registry.hire(actor=actor, member_id="cases", role_id="cases", display_name="Cases",
                  request_id="h1")

    conn = MockConnection("cases", home, receipt_status="terminal")
    monkeypatch.setattr("plugins.colleague_team.host_runtime.connect_profile",
                        lambda profile, phome: (conn, str(phome)))
    monkeypatch.setattr("plugins.colleague_team.host_runtime.session_store",
                        lambda c: type("Store", (), {"call": lambda self, method, **params: {}})())
    monkeypatch.setattr("plugins.colleague_team.host_runtime.profile_is_unserved", lambda *_: False)

    entries = [{"member_id": "cases", "generation": 1, "profile": "cases", "home": str(home),
                "workdir": str(workspace), "model_policy": "profile"}]
    bootstrap = install_tool_dispatcher(
        service_profile_name="caixuf", registry=registry, supervisor=supervisor,
        entries=entries, state_dir=str(state_dir),
    )
    try:
        assert workspace.stat().st_mode & 0o777 == 0o700
    finally:
        bootstrap.close()


def test_profile_park_requires_host_idle_authority_and_uses_host_lifecycle(test_env):
    home, workspace = test_env
    conn = MockConnection("cases", home)
    host = MockHost(["cases"], {"cases": conn})
    spec = InstanceSpec("caixuf", "cases", 1, "cases", "Cases", "DUTY", ())
    stopped = []
    backend = ToolEnabledGatewayProfileBackend(
        gateway_host=host, member_profiles={("cases", 1): "cases"}, profile_homes={"cases": home},
        model_options={"cases": {"model": "flash", "provider": "custom", "base_url": "http://127.0.0.1:4000/v1", "api_key": "secret"}},
        profile_workdirs={"cases": workspace}, idle_authority=lambda _: True,
        stop_profile=lambda name, path: stopped.append((name, path)),
        parked_profile=lambda name, path: bool(stopped))
    backend.park(spec)
    assert stopped == [("cases", home)]
    assert backend.inspect(spec) == {"state": "parked"}


def test_native_profile_policy_does_not_require_or_override_credentials(test_env):
    home, workspace = test_env
    (home / "config.yaml").write_text("model:\n  default: native-model\n  provider: native\n")
    conn = MockConnection("cases", home)
    backend = ToolEnabledGatewayProfileBackend(
        gateway_host=MockHost(["cases"], {"cases": conn}),
        member_profiles={("cases", 1): "cases"}, profile_homes={"cases": home},
        model_options={"cases": {}}, profile_workdirs={"cases": workspace})
    assert backend.session_options("cases") == {"safe_mode": False}
    assert backend.inspect(InstanceSpec("caixuf", "cases", 1, "cases", "Cases", "DUTY", ()))["state"] == "available"

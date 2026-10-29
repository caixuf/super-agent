from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from agentcore.colleague_gateway_service import _dispatcher
from agentcore.colleague_gateway_bootstrap import ColleagueGatewayBootstrap
from agentcore.colleague_registry import Actor, Registry, Supervisor


from agentcore.colleague_gateway_host import HermesGatewayHost as _EngineGatewayHost

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "colleague_host_runtime", _ROOT / "plugins" / "colleague_team" / "host_runtime.py")
assert _spec and _spec.loader
_host_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_host_runtime)


def HermesGatewayHost(profiles):
    """Engine host wired to the real host-side Gateway connector."""
    return _EngineGatewayHost(profiles, connector=_host_runtime.connect_profile)


class OfflineBackend:
    def __init__(self):
        self.ensure_calls = 0

    def ensure(self, spec):
        self.ensure_calls += 1

    def inspect(self, spec):
        return {"state": "absent"}

    def idle(self, spec):
        return False

    def park(self, spec):
        raise RuntimeError("not_started")


def _inputs(tmp_path):
    registry = Registry(tmp_path / "registry.db", profile_id="team", primary_id="leader")
    actor = Actor("team", "leader")
    supervisor = Supervisor("team")
    registry.define_role(actor=actor, role_id="review", label="Reviewer", duty="Review",
                         boundaries=["Follow host policy"], request_id="role")
    registry.hire(actor=actor, member_id="alice", role_id="review",
                  display_name="Alice", request_id="hire")
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    home = tmp_path / "alice-home"
    home.mkdir(mode=0o700)
    os.chmod(home, 0o700)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    host = HermesGatewayHost({"alice_profile": (home, "process-peer")})
    return {
        "registry": registry,
        "supervisor": supervisor,
        "backend": OfflineBackend(),
        "member_profiles": {("alice", 1): "alice_profile"},
        "profile_workdirs": {"alice_profile": workdir},
        "gateway_host": host,
        "session_store_factory": _host_runtime.session_store,
        "state_dir": state,
        "owns_gateway_host": True,
    }


def test_bootstrap_registers_real_contract_without_fabricating_readiness(tmp_path):
    config = _inputs(tmp_path)
    bootstrap = ColleagueGatewayBootstrap(
        service_profile_name="leader", **config)
    try:
        assert _dispatcher("leader") is bootstrap.dispatcher
        assert bootstrap.bindings.profiles == config["member_profiles"]
        status = bootstrap.provisioner.status("alice", 1)
        assert status["ready"] is False
        assert status["runtime_state"] == "absent"
        assert config["backend"].ensure_calls == 0
        assert not config["gateway_host"]._connections
    finally:
        bootstrap.close()
    assert _dispatcher("leader") is None
    assert config["gateway_host"]._closed is True


def test_bootstrap_failure_does_not_leave_service_registered(tmp_path, monkeypatch):
    import agentcore.colleague_gateway_service as service

    config = _inputs(tmp_path)
    sentinel = object()
    monkeypatch.setitem(service._SERVICES, "leader", sentinel)
    try:
        with pytest.raises(RuntimeError, match="already_installed"):
            ColleagueGatewayBootstrap(service_profile_name="leader", **config)
        assert service._SERVICES["leader"] is sentinel
        assert config["gateway_host"]._closed is True
    finally:
        service._SERVICES.pop("leader", None)


def test_invalid_member_profile_is_rejected_before_registration(tmp_path):
    config = _inputs(tmp_path)
    config["member_profiles"] = {("alice", 1): "unconfigured"}
    with pytest.raises(ValueError, match="member_gateway_profile_not_configured"):
        ColleagueGatewayBootstrap(service_profile_name="leader", **config)
    assert _dispatcher("leader") is None
    assert config["gateway_host"]._closed is True

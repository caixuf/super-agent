import asyncio
import importlib.util
import os
from pathlib import Path
from contextlib import asynccontextmanager
from contextvars import ContextVar
import sys
from types import ModuleType

import pytest

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


def install_hermes_stubs(monkeypatch, *, endpoint_profile_id=None, state="ready", instances=None):
    home_var = ContextVar("test_hermes_home", default=None)
    constants = ModuleType("hermes_constants")
    constants.set_hermes_home_override = lambda value: home_var.set(str(value))
    constants.reset_hermes_home_override = home_var.reset
    client_module = ModuleType("hermes_cli.gateway_client")
    seen = []

    @asynccontextmanager
    async def connect_gateway(*, endpoint, subject=None):
        seen.append((home_var.get(), endpoint.profile_id, subject, endpoint.instance_id))
        yield object()

    client_module.connect_gateway = connect_gateway
    connection_module = ModuleType("hermes_cli.gateway_connection")

    class Connection:
        def __init__(self, connect):
            self.connect_factory = connect
            self.closed = False

        def _dial(self):
            async def enter_and_exit():
                async with self.connect_factory():
                    pass
            asyncio.run(enter_and_exit())

        def start(self):
            self._dial()

        def reconnect(self):
            self._dial()

        def call(self, method, **params):
            return method, params

        def close(self):
            self.closed = True

    connection_module.GatewayConnection = Connection
    runtime_module = ModuleType("hermes_cli.gateway_runtime")

    class Endpoint:
        def __init__(self, profile_id, instance_id):
            self.profile_id = profile_id
            self.instance_id = instance_id

    remaining = list(instances or [])
    last = [None]

    def discover(home):
        if remaining:
            last[0] = remaining.pop(0)
        profile_id, instance_id, current_state = last[0] or (endpoint_profile_id or str(home), "instance-1", state)
        return type("Discovery", (), {
            "endpoint": Endpoint(profile_id, instance_id) if current_state == "ready" else None,
            "state": current_state,
        })()

    runtime_module.discover_gateway_endpoint = discover
    runtime_module.gateway_profile_home = lambda home: home
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", client_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_connection", connection_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_runtime", runtime_module)
    return seen


def test_host_pins_each_profile_home_and_principal_and_closes(monkeypatch, tmp_path):
    seen = install_hermes_stubs(monkeypatch)
    homes = [tmp_path / "one", tmp_path / "two"]
    for home in homes:
        home.mkdir()
    host = HermesGatewayHost({"alice": (homes[0], "process-peer"),
                              "bob": (homes[1], "process-peer")})
    a = host.connection_for_profile("alice")
    b = host.connection_for_profile("bob")
    assert a is host.connection_for_profile("alice")
    assert a is not b
    assert (a.hermes_profile, a.gateway_profile_id, a.principal_id) == (
        "alice", str(homes[0]), f"uid:{os.getuid()}")
    assert (b.hermes_profile, b.gateway_profile_id, b.principal_id) == (
        "bob", str(homes[1]), f"uid:{os.getuid()}")
    assert seen == [(str(homes[0]), str(homes[0]), None, "instance-1"),
                    (str(homes[1]), str(homes[1]), None, "instance-1")]
    host.close()
    assert a._connection.closed and b._connection.closed
    with pytest.raises(RuntimeError, match="gateway_host_closed"):
        host.connection_for_profile("alice")


def test_rejects_unknown_profile_and_mismatched_gateway_identity(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    host = HermesGatewayHost({"alice": (home, "process-peer")})
    with pytest.raises(PermissionError, match="unconfigured_hermes_profile"):
        host.connection_for_profile("bob")
    host.close()
    install_hermes_stubs(monkeypatch, endpoint_profile_id="/other/profile")
    host = HermesGatewayHost({"alice": (home, "process-peer")})
    with pytest.raises(PermissionError, match="gateway_profile_identity_mismatch"):
        host.connection_for_profile("alice")
    host.close()


def test_requires_ready_endpoint_and_valid_trusted_principal(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="invalid_trusted_profile"):
        HermesGatewayHost({"alice": (home, "bad principal")})
    install_hermes_stubs(monkeypatch, state="inaccessible")
    host = HermesGatewayHost({"alice": (home, "process-peer")})
    with pytest.raises(ConnectionError, match="configured_profile_gateway_not_ready"):
        host.connection_for_profile("alice")
    host.close()


def test_redial_rediscovers_instance_but_keeps_profile_identity(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seen = install_hermes_stubs(monkeypatch, instances=[
        (str(home), "instance-1", "ready"),
        (str(home), "instance-2", "ready"),
    ])
    host = HermesGatewayHost({"alice": (home, "process-peer")})
    bound = host.connection_for_profile("alice")
    bound._connection.reconnect()
    assert [item[3] for item in seen] == ["instance-1", "instance-2"]
    assert all(item[1] == str(home) for item in seen)
    assert bound.gateway_profile_id == str(home)
    host.close()


def test_redial_refuses_profile_drift_before_gateway_connect(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seen = install_hermes_stubs(monkeypatch, instances=[
        (str(home), "instance-1", "ready"),
        ("/another/profile", "instance-2", "ready"),
    ])
    host = HermesGatewayHost({"alice": (home, "process-peer")})
    bound = host.connection_for_profile("alice")
    with pytest.raises(PermissionError, match="gateway_profile_identity_mismatch"):
        bound._connection.reconnect()
    assert len(seen) == 1
    host.close()


def test_host_without_a_host_connector_refuses_instead_of_importing_the_host(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    host = _EngineGatewayHost({"alice": (home, "process-peer")})
    try:
        with pytest.raises(RuntimeError, match="gateway_connector_required"):
            host.connection_for_profile("alice")
    finally:
        host.close()

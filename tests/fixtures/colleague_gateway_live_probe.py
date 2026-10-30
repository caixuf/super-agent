"""Executed by pytest only when the installed Hermes host is explicitly selected."""
import asyncio
import importlib.util
import os
import time
from pathlib import Path

import pytest

source = Path(os.environ['HERMES_SOURCE']) / 'tests/gateway/test_unified_gateway_native_live.py'
spec = importlib.util.spec_from_file_location('upstream_colleague_gateway_probe', source)
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)
peer = upstream.peer
harness = upstream.harness


def _host_module():
    """Load the host-side adapter module (real Hermes imports live in there)."""
    spec = importlib.util.spec_from_file_location(
        "colleague_host_runtime",
        Path(__file__).resolve().parents[2] / "plugins" / "colleague_team" / "host_runtime.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bound_host(profiles):
    """Engine host wired to the real host-side Gateway connector."""
    from agentcore.colleague_gateway_host import HermesGatewayHost
    return HermesGatewayHost(profiles, connector=_host_module().connect_profile)


@pytest.fixture(autouse=True)
def isolated_child_environment(harness):
    # The upstream fixture otherwise drops this variable from the child env.
    harness['env']['HERMES_DISABLE_LAZY_INSTALLS'] = '1'


def test_adapter_against_real_gateway(harness, tmp_path):
    from agentcore.colleague_registry import Registry, Actor, Supervisor
    from agentcore.colleague_hermes_runner import ProfileBinding
    from agentcore.colleague_gateway_dispatch import GatewayColleagueDispatcher

    home, env = harness['home'], harness['env']
    registry = Registry(tmp_path / 'team.db', profile_id='team', primary_id='leader')
    actor, supervisor = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=actor, role_id='review', label='Reviewer', duty='Review',
                         boundaries=['Respect permissions'], request_id='role')
    registry.hire(actor=actor, member_id='alice', role_id='review', display_name='Alice', request_id='hire')
    state = tmp_path / 'adapter'
    state.mkdir(mode=0o700)

    async def check(desc):
        async with upstream.websocket(home, desc) as ws:
            made = await upstream.ok(ws, 'session.create', request_id='adapter-session',
                                     source='cli', cwd=str(home), toolsets=[])
            sid = made['session_id']
            loop = asyncio.get_running_loop()

            class Connection:
                hermes_profile = 'alice_profile'
                gateway_profile_id = desc['served_profiles'][0]['profile_id']
                principal_id = 'fixture-principal'

                def call(self, method, **params):
                    return asyncio.run_coroutine_threadsafe(upstream.ok(ws, method, **params), loop).result(30)

            class BindingStore:
                def resolve(self, member, generation, profile, session, **kwargs):
                    return ProfileBinding(member, generation, profile, session, 'alice_profile', sid)

            class Ready:
                def status(self, member, generation):
                    return {'ready': True}

            dispatch = GatewayColleagueDispatcher(
                registry=registry, supervisor=supervisor, provisioner=Ready(),
                bindings=BindingStore(), connection_for_profile=lambda profile: Connection(), state_dir=state)
            owner = {'profile': 'leader', 'session': 'user-one'}
            submit = dict(owner=owner, member_id='alice', generation=1,
                          request_id='task-one', prompt='COLLEAGUE_TEST')
            first = await asyncio.to_thread(dispatch.submit, **submit)
            assert first['state'] in ('queued', 'started', 'terminal'), first
            again = await asyncio.to_thread(dispatch.submit, **submit)
            assert first['admission_id'] == again['admission_id']
            end = time.monotonic() + 30
            while time.monotonic() < end:
                receipt = await asyncio.to_thread(dispatch.inspect, owner=owner, request_id='task-one')
                if receipt['state'] == 'terminal':
                    assert receipt['result']['completed'] is True, receipt
                    assert 'NATIVE_ACK_' in receipt['result']['final_response'], receipt
                    assert receipt['reservation'] == 'awaiting_quiescence_evidence'
                    assert 'messages' not in receipt['result'] and 'last_reasoning' not in receipt['result']
                    return
                await asyncio.sleep(.1)
            raise AssertionError(receipt)

    with upstream.daemon(home, env, 'adapter-live.log') as (proc, desc, _):
        asyncio.run(check(desc))
        assert upstream.planned_stop(home, proc, desc) == 0

def test_bootstrap_binding_dispatch_query_end_to_end(harness, tmp_path):
    """Only the test backend readiness is synthetic; all Gateway calls are live loopback."""
    from agentcore.colleague_gateway_bootstrap import ColleagueGatewayBootstrap
    from agentcore.colleague_registry import Registry, Actor, Supervisor

    home, env = harness["home"], harness["env"]
    registry = Registry(tmp_path / "bootstrap-team.db", profile_id="bootstrap-team",
                        primary_id="leader")
    actor, supervisor = Actor("bootstrap-team", "leader"), Supervisor("bootstrap-team")
    registry.define_role(actor=actor, role_id="review", label="Reviewer", duty="Review the assigned question",
                         boundaries=["Use the provided session only"], request_id="bootstrap-role")
    registry.hire(actor=actor, member_id="alice", role_id="review",
                  display_name="Alice", request_id="bootstrap-hire")
    state = tmp_path / "bootstrap-state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)

    class TestOnlyReadyBackend:
        """Explicit test fixture; does not assert a production isolation boundary."""
        def ensure(self, spec):
            return None

        def inspect(self, spec):
            return {"state": "ready", "isolated": True,
                    "private_memory": True, "no_channels": True}

        def idle(self, spec):
            return False

        def park(self, spec):
            raise RuntimeError("test_backend_does_not_park")

    def check(desc):
        host = _bound_host({"alice_profile": (home, "process-peer")})
        bootstrap = None
        try:
            bootstrap = ColleagueGatewayBootstrap(
                service_profile_name="leader",
                registry=registry,
                supervisor=supervisor,
                backend=TestOnlyReadyBackend(),
                member_profiles={("alice", 1): "alice_profile"},
                profile_workdirs={"alice_profile": home},
                gateway_host=host,
                session_store_factory=_host_module().session_store,
                state_dir=state,
                owns_gateway_host=True,
            )
            # Attach on the host lifecycle thread before worker dispatch.
            host.connection_for_profile("alice_profile")
            provisioned = bootstrap.provisioner.provision("alice", 1)
            assert provisioned["ready"] is True  # Test-only injected attestation.

            owner = {"profile": "leader", "session": "bootstrap-owner-session"}
            submit = dict(owner=owner, member_id="alice", generation=1,
                          request_id="bootstrap-live-task", prompt="COLLEAGUE_TEST")

            async def dispatch_and_query():
                first = await asyncio.to_thread(bootstrap.dispatcher.submit, **submit)
                assert first["state"] in ("queued", "started", "terminal"), first
                binding = bootstrap.bindings.resolve(
                    "alice", 1, owner["profile"], owner["session"])
                assert binding.hermes_profile == "alice_profile"
                assert binding.hermes_session_id.startswith("local-")

                replay = await asyncio.to_thread(bootstrap.dispatcher.submit, **submit)
                assert replay["admission_id"] == first["admission_id"]
                end = time.monotonic() + 30
                while time.monotonic() < end:
                    status = await asyncio.to_thread(
                        bootstrap.dispatcher.inspect, owner=owner,
                        request_id="bootstrap-live-task")
                    if status["state"] == "terminal":
                        assert status["result"]["completed"] is True, status
                        assert "NATIVE_ACK_" in status["result"]["final_response"], status
                        assert status["reservation"] == "awaiting_quiescence_evidence"
                        return
                    await asyncio.sleep(.1)
                raise AssertionError(status)

            asyncio.run(dispatch_and_query())
        finally:
            if bootstrap is not None:
                bootstrap.close()
            else:
                host.close()

    with upstream.daemon(home, env, "bootstrap-live.log") as (proc, desc, _):
        check(desc)
        assert upstream.planned_stop(home, proc, desc) == 0


def test_official_profile_bound_connection(harness):

    home, env = harness['home'], harness['env']
    host = _bound_host({'probe': (home, 'process-peer')})
    try:
        with upstream.daemon(home, env, 'profile-bound-host.log') as (proc, desc, _):
            profile_id = desc['served_profiles'][0]['profile_id']
            first_instance = desc['instance_id']
            connection = host.connection_for_profile('probe')
            assert connection.hermes_profile == 'probe'
            assert connection.gateway_profile_id == profile_id
            assert connection.principal_id == f'uid:{os.getuid()}'
            made = connection.call('session.create', request_id='profile-bound-session',
                                   source='cli', cwd=str(home), row_cwd='')
            sid = made.get('stored_session_id') or made.get('session_id')
            assert isinstance(sid, str) and sid.startswith('local-')
            assert connection.call('session.resume', session_id=sid).get('stored_session_id') == sid
            assert upstream.planned_stop(home, proc, desc) == 0

        with upstream.daemon(home, env, 'profile-bound-host-restarted.log') as (proc, desc, _):
            assert desc['instance_id'] != first_instance
            resumed = connection.call('session.resume', session_id=sid)
            assert resumed.get('stored_session_id') == sid
            assert connection._connection.epoch >= 2
            assert upstream.planned_stop(home, proc, desc) == 0
    finally:
        host.close()


def test_dispatch_through_official_host_connection(harness, tmp_path):
    from agentcore.colleague_gateway_dispatch import GatewayColleagueDispatcher
    from agentcore.colleague_hermes_runner import ProfileBinding
    from agentcore.colleague_registry import Actor, Registry, Supervisor

    home, env = harness['home'], harness['env']
    registry = Registry(tmp_path / 'official-team.db', profile_id='team', primary_id='leader')
    actor = Actor('team', 'leader')
    registry.define_role(actor=actor, role_id='review', label='Reviewer', duty='Review',
                         boundaries=['Respect permissions'], request_id='role')
    registry.hire(actor=actor, member_id='alice', role_id='review', display_name='Alice', request_id='hire')
    state = tmp_path / 'official-adapter'
    state.mkdir(mode=0o700)

    with upstream.daemon(home, env, 'official-dispatch.log') as (proc, desc, _):
        with _bound_host({'alice_profile': (home, 'process-peer')}) as host:
            connection = host.connection_for_profile('alice_profile')
            made = connection.call('session.create', request_id='official-dispatch-session',
                                   source='cli', cwd=str(home), row_cwd='')
            sid = made.get('stored_session_id') or made['session_id']

            class BindingStore:
                def resolve(self, member, generation, profile, session, **kwargs):
                    return ProfileBinding(member, generation, profile, session, 'alice_profile', sid)

            class Ready:
                def status(self, member, generation):
                    return {'ready': True}

            dispatch = GatewayColleagueDispatcher(
                registry=registry, supervisor=Supervisor('team'), provisioner=Ready(),
                bindings=BindingStore(), connection_for_profile=host.connection_for_profile, state_dir=state)
            owner = {'profile': 'leader', 'session': 'user-one'}
            request = dict(owner=owner, member_id='alice', generation=1,
                           request_id='task-one', prompt='COLLEAGUE_TEST')
            first = dispatch.submit(**request)
            assert first['state'] in ('queued', 'started', 'terminal'), first
            assert dispatch.submit(**request)['admission_id'] == first['admission_id']
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                receipt = dispatch.inspect(owner=owner, request_id='task-one')
                if receipt['state'] == 'terminal':
                    assert receipt['result']['completed'] is True, receipt
                    assert 'NATIVE_ACK_' in receipt['result']['final_response'], receipt
                    assert receipt['reservation'] == 'awaiting_quiescence_evidence'
                    break
                time.sleep(.1)
            else:
                raise AssertionError(receipt)
        assert upstream.planned_stop(home, proc, desc) == 0

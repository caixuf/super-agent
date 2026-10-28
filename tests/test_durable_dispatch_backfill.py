"""Completion backfill: persisted route, restart replay, duplicate suppression.

These cases drive the real SQLite ledger. No gateway, network or active session is
involved: a fake transport records what would have been injected, and every restart is
modelled by dropping the Python objects and reopening the queue. One case runs the real
Hermes host API to prove the persisted route reaches its public injection surface.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types

import pytest

from agentcore.completion_backfill import drain, inspect, parked_jobs, routable
from agentcore.ops_alerts import DeliveryReceipt
from agentcore.pool import Pool


REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins' / 'durable_dispatch' / '__init__.py'
spec = importlib.util.spec_from_file_location('durable_dispatch_backfill_module', PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

ORIGIN = {'profile': 'test-profile', 'session': 'session-one',
          'route': 'agent:test-profile:feishu:dm:chat-1', 'channel': 'feishu',
          'chat': 'chat-1', 'thread': 'thread-1'}


class Transport:
    def __init__(self, *, response=None, raises=None):
        self.calls = []
        self.response = response
        self.raises = raises
        self.sent = threading.Event()

    def deliver(self, address, message, *, idempotency_key):
        self.calls.append({'address': address, 'message': message,
                           'idempotency_key': idempotency_key})
        self.sent.set()
        if self.raises is not None:
            raise self.raises
        if self.response is not None:
            return self.response
        return DeliveryReceipt(True, idempotency_key, 'receipt-' + idempotency_key[:10])


class Context:
    profile_name = 'test-profile'

    def __init__(self, settings, *, inject=None):
        self.settings, self.tools, self.prompt_sections = settings, {}, {}
        self.unload_callbacks = []
        self.injected = []
        self._inject = inject

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_system_prompt_section(self, id, content, **kwargs):
        self.prompt_sections[id] = {'content': content, **kwargs}

    def on_unload(self, callback):
        self.unload_callbacks.append(callback)

    def inject_message(self, text, role='user', *, session_key=None, origin=None):
        self.injected.append({'text': text, 'session_key': session_key})
        return self._inject(text, session_key) if self._inject else True


def _sources(tmp_path):
    directory = tmp_path / 'sources'
    directory.mkdir(exist_ok=True)
    source = directory / 'evidence.log'
    source.write_text('line one\nline two\n')
    return directory, source


def _args(tmp_path):
    _directory, source = _sources(tmp_path)
    return {'goal': 'Explain the failure', 'request_key': 'request-1',
            'sources': [{'path': str(source)}]}


def _service(tmp_path, *, route=ORIGIN, profile='test-profile', delivery=None):
    directory, _source = _sources(tmp_path)
    return plugin.DispatchService(
        str(tmp_path / 'queue'), profile, [str(directory)],
        delivery=delivery, route_provider=lambda _session_id: dict(route))


def _finish(pool, job, *, result=None, ok=True):
    row, fd = pool.claim()
    assert row is not None and row['id'] == job
    try:
        assert pool.finish(job, row['attempt'], ok, result or {'summary': 'analysis done'})
    finally:
        os.close(fd)


def _outbox_count(pool):
    with pool.db() as db:
        return db.execute('SELECT count(*) FROM outbox').fetchone()[0]


def test_route_is_persisted_from_the_submitting_turn(tmp_path):
    service = _service(tmp_path)
    result = service.submit('session-one', _args(tmp_path))
    assert service.pool.origin(result['task_id']) == ORIGIN
    # The ownership proof stays a one-way hash in the payload, never the route.
    with service.pool.db() as db:
        payload = json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',
                                        (result['task_id'],)).fetchone()['payload'])
    assert 'session-one' not in json.dumps(payload)
    assert set(payload) == {'goal', 'sources', '_dispatch_owner',
                            '_colleague_role', '_colleague_name', '_colleague_label'}


def test_route_without_a_bound_submitting_turn_is_not_invented(tmp_path):
    service = _service(tmp_path, route={})
    result = service.submit('session-one', _args(tmp_path))
    assert service.pool.origin(result['task_id']) == {
        'profile': 'test-profile', 'session': 'session-one'}


def test_bound_route_must_match_the_dispatcher_session(tmp_path, monkeypatch):
    values = {'HERMES_SESSION_ID': 'another-session',
              'HERMES_SESSION_KEY': 'agent:test-profile:feishu:dm:other',
              'HERMES_SESSION_PLATFORM': 'feishu',
              'HERMES_SESSION_CHAT_ID': 'other',
              'HERMES_SESSION_THREAD_ID': 't'}
    gateway = types.ModuleType('gateway')
    gateway.__path__ = []
    session_context = types.ModuleType('gateway.session_context')
    session_context.get_session_env = lambda key, default='': values.get(key, default)
    monkeypatch.setitem(sys.modules, 'gateway', gateway)
    monkeypatch.setitem(sys.modules, 'gateway.session_context', session_context)

    assert plugin._bound_route('session-one') == {}
    values['HERMES_SESSION_ID'] = 'session-one'
    assert plugin._bound_route('session-one') == {
        'route': 'agent:test-profile:feishu:dm:other', 'channel': 'feishu',
        'chat': 'other', 'thread': 't'}


def test_restart_resumes_the_pending_completion_exactly_once(tmp_path):
    service = _service(tmp_path)
    job = service.submit('session-one', _args(tmp_path))['task_id']
    _finish(service.pool, job)
    del service

    # A fresh process-equivalent: new Pool, new transport, nothing inherited.
    pool = Pool(str(tmp_path / 'queue'))
    transport = Transport()
    assert drain(pool, transport, profile='test-profile')['delivered'] == 1
    assert [call['address'] for call in transport.calls] == [ORIGIN]
    assert transport.calls[0]['idempotency_key'] == job
    record = pool.delivery_record(job)
    assert record['acknowledged'] == 1
    assert record['receipt_id'].startswith('receipt-')

    # Another restart after delivery must not replay it.
    later = Transport()
    assert drain(Pool(str(tmp_path / 'queue')), later, profile='test-profile')['delivered'] == 0
    assert later.calls == []


def test_reconnect_retries_an_unknown_delivery_without_marking_it_delivered(tmp_path):
    service = _service(tmp_path)
    job = service.submit('session-one', _args(tmp_path))['task_id']
    _finish(service.pool, job)
    del service

    failing = Transport(raises=RuntimeError('adapter disconnected'))
    first = drain(Pool(str(tmp_path / 'queue')), failing, profile='test-profile', lease=60)
    assert first['outcomes'][0]['status'] == 'delivery_unknown'
    assert failing.calls and Pool(str(tmp_path / 'queue')).delivery_record(job)['acknowledged'] == 0
    # Expire the lease deterministically. A 10 ms lease can expire during the
    # successful ACK itself on a real fsync-backed filesystem.
    with Pool(str(tmp_path / 'queue')).db() as db:
        db.execute('UPDATE outbox SET lease_until=0 WHERE job=?', (job,))

    recovered = Transport()
    second = drain(Pool(str(tmp_path / 'queue')), recovered, profile='test-profile',
                   lease=60)
    assert second['delivered'] == 1
    assert len(recovered.calls) == 1
    assert Pool(str(tmp_path / 'queue')).delivery_record(job)['acknowledged'] == 1


def test_duplicate_completion_records_one_receipt_and_sends_once(tmp_path):
    service = _service(tmp_path)
    job = service.submit('session-one', _args(tmp_path))['task_id']
    _finish(service.pool, job)
    assert _outbox_count(service.pool) == 1

    transport = Transport()
    assert drain(service.pool, transport, profile='test-profile')['delivered'] == 1
    assert drain(service.pool, transport, profile='test-profile')['delivered'] == 0
    assert len(transport.calls) == 1
    assert _outbox_count(service.pool) == 1


def test_legacy_receipt_without_route_is_parked_not_acked(tmp_path):
    pool = Pool(str(tmp_path / 'queue'))
    legacy = pool.submit('legacy', {'goal': 'old'}, replay_safe=True,
                         origin={'profile': 'test-profile', 'session': 'old-session'})
    _finish(pool, legacy)
    routed = pool.submit('routed', {'goal': 'new'}, replay_safe=True, origin=ORIGIN)
    _finish(pool, routed)

    transport = Transport()
    result = drain(pool, transport, profile='test-profile')
    assert result['delivered'] == 1
    assert result['parked'] == [legacy]
    assert [call['idempotency_key'] for call in transport.calls] == [routed]
    assert pool.delivery_record(legacy)['acknowledged'] == 0
    assert pool.delivery_record(routed)['acknowledged'] == 1
    assert parked_jobs(pool, profile='test-profile') == [legacy]


def test_completion_addressed_to_another_profile_is_never_delivered(tmp_path):
    pool = Pool(str(tmp_path / 'queue'))
    job = pool.submit('foreign', {'goal': 'g'}, replay_safe=True,
                      origin=dict(ORIGIN, profile='another-profile'))
    _finish(pool, job)
    transport = Transport()
    result = drain(pool, transport, profile='test-profile')
    assert transport.calls == []
    assert result['parked'] == [job]
    assert pool.delivery_record(job)['acknowledged'] == 0
    assert routable(ORIGIN, profile='test-profile')
    assert not routable(dict(ORIGIN, profile='other'), profile='test-profile')


def test_plugin_transport_refuses_unaddressable_and_unaccepted_sends(tmp_path):
    calls = []

    def inj(text, session_key):
        calls.append((text, session_key))
        return True

    transport = plugin.InjectionTransport(inj, 'test-profile')
    assert transport.deliver({'profile': 'test-profile', 'session': 's'}, {}, idempotency_key='j') is None
    assert transport.deliver(dict(ORIGIN, profile='other'), {}, idempotency_key='j') is None
    assert calls == []
    rejected = plugin.InjectionTransport(lambda *a, **k: False, 'test-profile')
    assert rejected.deliver(ORIGIN, {'task_id': 'j', 'status': 'completed',
                                     'summary': 'x', 'truncated': False}, idempotency_key='j') is False
    receipt = transport.deliver(ORIGIN, {'task_id': 'j', 'status': 'completed',
                                         'summary': 'x', 'truncated': False}, idempotency_key='j')
    assert receipt['accepted'] is True and receipt['event_id'] == 'j'
    assert isinstance(receipt['receipt_id'], str) and receipt['receipt_id']
    assert calls == [('[后台任务 j] 已完成\nx', ORIGIN['route'])]


def test_delivery_disabled_registers_no_backfill(tmp_path):
    directory, _source = _sources(tmp_path)
    context = Context({'enabled': True, 'queue_root': str(tmp_path / 'queue'),
                       'allowed_roots': [str(directory)]})
    before = {thread.name for thread in threading.enumerate()}
    plugin.register(context)
    assert set(context.tools) == {'durable_submit', 'durable_status', 'durable_cancel'}
    assert context.unload_callbacks == []
    assert not ({thread.name for thread in threading.enumerate()} - before
                & {'durable-dispatch-backfill'})


def test_enabled_backfill_delivers_after_restart_and_stops_on_unload(tmp_path):
    service = _service(tmp_path)
    job = service.submit('session-one', _args(tmp_path))['task_id']
    _finish(service.pool, job)
    del service

    directory, _source = _sources(tmp_path)
    context = Context({'enabled': True, 'queue_root': str(tmp_path / 'queue'),
                       'allowed_roots': [str(directory)],
                       'deliver_completions': True, 'delivery_interval': 1,
                       'engine_root': str(REPO)})
    plugin.register(context)
    deadline = time.monotonic() + 5
    while not context.injected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert context.injected, 'backfill thread did not deliver a pending completion'
    assert context.injected[0]['session_key'] == ORIGIN['route']
    assert job in context.injected[0]['text']
    assert Pool(str(tmp_path / 'queue')).delivery_record(job)['acknowledged'] == 1

    for callback in context.unload_callbacks:
        callback()
    assert not any(thread.name == 'durable-dispatch-backfill' and thread.is_alive()
                   for thread in threading.enumerate())


def test_engine_fallback_loads_backfill_without_polluting_imports(tmp_path, monkeypatch):
    original = plugin.importlib.import_module
    original_pool = sys.modules.get('agentcore.pool')

    def unavailable(name, *args, **kwargs):
        if name in ('agentcore.pool', 'agentcore.completion_backfill'):
            error = ModuleNotFoundError('engine unavailable')
            error.name = name
            raise error
        return original(name, *args, **kwargs)

    monkeypatch.setattr(plugin.importlib, 'import_module', unavailable)
    before = list(sys.path)
    pool_type, backfill = plugin._engine(str(REPO))
    assert pool_type.__name__ == 'Pool'
    assert pool_type.__module__ == 'agentcore.pool'
    assert callable(backfill.drain)
    assert sys.path == before
    # The pre-existing modules are restored rather than replaced by the transient ones.
    assert sys.modules.get('agentcore.pool') is original_pool
    pool = pool_type(str(tmp_path / 'queue'))
    job = pool.submit('fallback', {'goal': 'g'}, replay_safe=True, origin=ORIGIN)
    _finish(pool, job)
    transport = plugin.InjectionTransport(lambda text, session_key: True, 'test-profile')
    assert backfill.drain(pool, transport, profile='test-profile')['delivered'] == 1


def test_inspect_marks_which_pending_receipts_are_deliverable(tmp_path):
    pool = Pool(str(tmp_path / 'queue'))
    legacy = pool.submit('legacy', {'goal': 'old'}, replay_safe=True)
    _finish(pool, legacy)
    routed = pool.submit('routed', {'goal': 'new'}, replay_safe=True, origin=ORIGIN)
    _finish(pool, routed)
    rows = inspect(pool, profile='test-profile')
    assert [(row['task_id'], row['deliverable']) for row in rows] == [
        (legacy, False), (routed, True)]


def test_real_host_public_injection_receives_the_persisted_route(tmp_path):
    """Run the plugin inside the real Hermes interpreter with its public injector API."""
    host = Path(os.environ.get('HERMES_SOURCE', Path.home() / '.hermes' / 'hermes-agent'))
    runtime = host / 'venv' / 'bin' / 'python' if (host / 'venv' / 'bin' / 'python').is_file() else host / '.venv' / 'bin' / 'python'
    if not (host / 'hermes_cli' / 'plugins.py').is_file():
        pytest.skip('Hermes host source unavailable')
    if not runtime.is_file():
        pytest.skip('Hermes host interpreter unavailable')
    # The plugin gates delivery on the profile name the host derives from HERMES_HOME,
    # and a temp home resolves to 'custom' — derive the same value instead of assuming
    # the host's own profile name, which only matches when HERMES_HOME is unset.
    probe = subprocess.run(
        [str(runtime), '-c',
         'import sys;sys.path.insert(0,sys.argv[1]);'
         'from hermes_cli.profiles import get_active_profile_name;'
         'print(get_active_profile_name())', str(host)],
        env=dict(os.environ, HERMES_HOME=str(tmp_path / 'home')),
        capture_output=True, text=True, timeout=40)
    if probe.returncode != 0:
        pytest.skip('Hermes host cannot report its profile name: %s' % probe.stderr[-200:])
    profile = probe.stdout.strip().splitlines()[-1]
    origin = dict(ORIGIN, profile=profile)
    pool = Pool(str(tmp_path / 'queue'))
    job = pool.submit('host', {'goal': 'g'}, replay_safe=True, origin=origin)
    _finish(pool, job)
    code = r'''
import importlib.util, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from gateway.session_context import set_session_vars
from hermes_cli import config as config_mod
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugins_state import save_plugin_setting

home = Path(sys.argv[4]); home.mkdir(parents=True, exist_ok=True)
config_mod.save_config({"plugins": {"entries": {"durable-dispatch": {
    "allow_gateway_injection": True}}}}, merge_existing=True)
for key, value in {'enabled': True, 'engine_root': sys.argv[2],
                   'queue_root': sys.argv[3], 'allowed_roots': [str(home)],
                   'deliver_completions': True, 'delivery_interval': 1}.items():
    save_plugin_setting('durable-dispatch', (key,), value)
spec = importlib.util.spec_from_file_location('host_backfill_plugin', sys.argv[5])
plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
# The real bound-session API, exercised with the same keys production binds.
set_session_vars(platform='feishu', chat_id='chat-1', chat_type='dm',
                 thread_id='thread-1', session_key=sys.argv[6],
                 session_id='session-one', profile='default')
assert plugin._bound_route('session-one') == {
    'route': sys.argv[6], 'channel': 'feishu', 'chat': 'chat-1', 'thread': 'thread-1'}
assert plugin._bound_route('another-session') == {}
manifest = PluginManifest(name='durable-dispatch', version='0.2.0', description='test',
                          author='test', path=Path(sys.argv[5]).parent)
manager = PluginManager(scope_key=str(home))
seen = []
manager.set_gateway_message_injector(object(), lambda **kw: seen.append(kw) or True)
context = PluginContext(manifest, manager)
plugin.register(context)
deadline = time.time() + 10
while not seen and time.time() < deadline:
    time.sleep(0.05)
assert seen, 'public injection was not called'
assert seen[0]['session_key'] == sys.argv[6], seen[0]
assert sys.argv[7] in seen[0]['content']
row = None
while time.time() < deadline:
    with sqlite3.connect(str(Path(sys.argv[3]) / 'pool.db')) as db:
        row = db.execute('SELECT acknowledged, receipt_id FROM outbox WHERE job=?',
                         (sys.argv[7],)).fetchone()
    if row and row[0] == 1:
        break
    time.sleep(0.05)
assert row and row[0] == 1 and row[1], row
print('public injection delivery passed')
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'))
    env.pop('PYTHONPATH', None)
    result = subprocess.run(
        [str(runtime), '-c', code, str(host), str(REPO), str(tmp_path / 'queue'),
         str(tmp_path / 'home'), str(PLUGIN), origin['route'], job],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert 'public injection delivery passed' in result.stdout

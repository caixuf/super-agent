import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('host_direct_transport', ROOT/'runtime/hermes_completion_transport.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def host(monkeypatch):
    calls = []
    class Adapter:
        DURABLE_DELIVERY_UUID = True
        async def send(self, chat, text, **kw):
            calls.append(('text', chat, text, kw))
            return NS(success=True, message_id='text-receipt')
        async def send_document(self, chat, path, **kw):
            calls.append(('file', chat, path, kw))
            return NS(success=True, message_id='file-receipt')
        async def _send_reply_card(self, *, chat_id, content, reply_to, metadata, finalize):
            calls.append(('card', chat_id, content, finalize))
            return NS(success=True, message_id='card-receipt')
    adapter = Adapter()
    target = NS(platform='feishu', chat_id='chat', metadata={'thread_id':'thread'}, live=None, adapter=adapter)
    ctx = NS(plugin_id='durable-dispatch', profile_name='owner', _manager=NS(home_path='/profile/owner'))
    def resolve(runner, **kw):
        assert kw == dict(session_key='stored-route', route=None, plugin_home='/profile/owner', plugin_id='durable-dispatch')
        return target, None
    pm = NS(resolve=resolve, _fit=lambda adapter, chat, text:text)
    monkeypatch.setitem(sys.modules, 'gateway', NS(plugin_messages=pm))
    monkeypatch.setitem(sys.modules, 'gateway.platforms.base', NS(BasePlatformAdapter=type('Base', (), {'send_document':None})))
    address = dict(profile='owner', route='stored-route', channel='feishu', chat='chat', thread='thread')
    return bridge.HostSender(ctx), target, address, calls


def test_posts_text_and_native_file_without_injecting_a_turn(host):
    sender, target, address, calls = host
    for item in [dict(kind='text',text='done'), dict(kind='file',path='/staged/report.md',name='report.md')]:
        result = asyncio.run(sender.send(object(),address,dict(item,delivery_uuid='stable-key')))
        assert result['ok'] and result['message_id'].endswith('-receipt')
    assert [c[0] for c in calls] == ['text','file']
    assert all(c[3]['metadata']['_delivery_uuid'] == 'stable-key' for c in calls)
    assert all(c[3]['metadata']['_interim_send'] for c in calls)


@pytest.mark.parametrize('field,value', [('channel','other'),('chat','other'),('thread','other')])
def test_mismatched_origin_never_sends_to_the_resolved_chat(host,field,value):
    sender, target, address, calls = host
    result = asyncio.run(sender.send(object(),dict(address,**{field:value}),dict(kind='text',text='x')))
    assert result['not_sent'] and not calls


def test_thread_added_by_route_resolution_is_not_authorized(host):
    sender, target, address, calls = host
    del address['thread']
    result = asyncio.run(sender.send(object(), address, dict(kind='text', text='x', delivery_uuid='stable')))
    assert result == {'ok': False, 'not_sent': True, 'error': 'stored_origin_mismatch'}
    assert not calls
    target.metadata = {}
    result = asyncio.run(sender.send(object(), address, dict(kind='text', text='x', delivery_uuid='stable')))
    assert result['ok'] is True


def test_local_viewer_is_not_acknowledged_as_platform_delivery(host):
    sender, target, address, calls = host
    target.live = object()
    result = asyncio.run(sender.send(object(),address,dict(kind='text',text='x')))
    assert result['not_sent'] and not calls


@pytest.mark.parametrize('field,value', [('channel','other'), ('chat','other'), ('thread','other')])
def test_card_patch_refuses_mismatched_stored_origin(host, field, value, monkeypatch):
    sender, target, address, calls = host
    async def edit(*args, **kwargs):
        calls.append(('edit', args))
        return NS(success=True)
    target.adapter.edit_message = edit
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
    try:
        result = sender.patch_card(dict(address, **{field: value}), 'message', {'schema': '2.0'})
        assert result['not_sent'] and result['error'] == 'stored_origin_mismatch'
        assert not calls
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(3)
        loop.close()


def test_card_patch_refuses_thread_absent_from_stored_origin(host, monkeypatch):
    sender, target, address, calls = host
    del address['thread']
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
    try:
        result = sender.patch_card(address, 'message', {'schema': '2.0'})
        assert result == {'ok': False, 'not_sent': True, 'error': 'stored_origin_mismatch'}
        assert not calls
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(3)
        loop.close()


def test_card_patch_refuses_to_block_gateway_loop(host, monkeypatch):
    sender, target, address, calls = host
    async def check():
        loop = asyncio.get_running_loop()
        monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
        monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
        result = sender.patch_card(address, 'message', {'schema': '2.0'})
        assert result == {'ok': False, 'not_sent': True, 'error': 'blocking_on_gateway_loop'}
        assert not calls
    asyncio.run(check())


@pytest.mark.parametrize('case', ['success', 'wrong_chat', 'wrong_profile', 'missing_uuid', 'send_exception'])
def test_initial_card_create_uses_host_route_and_fences_uncertain_result(host, monkeypatch, case):
    sender, target, address, calls = host
    adapter = target.adapter
    adapter._client, adapter._app_id = object(), 'app'
    async def raw(**kwargs):
        calls.append(kwargs)
        if case == 'send_exception':
            raise TimeoutError('outcome unknown')
        return NS(success=True, message_id='new-message')
    adapter._send_raw_message = raw
    adapter._response_succeeded = lambda response: response.success
    adapter._extract_response_field = lambda response, name: getattr(response, name)
    if case == 'wrong_chat': address['chat'] = 'other'
    if case == 'wrong_profile': address['profile'] = 'other'
    if case == 'missing_uuid': adapter.DURABLE_DELIVERY_UUID = False
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
    try:
        result = sender.create_card(address, {'schema': '2.0'}, 'a' * 64)
        if case == 'success':
            assert result == {'ok': True, 'message_id': 'new-message'}
            assert calls[0]['metadata']['_delivery_uuid'] == 'a' * 32
            assert calls[0]['metadata']['thread_id'] == 'thread'
            assert calls[0]['msg_type'] == 'interactive'
        elif case == 'send_exception':
            assert result == {'ok': False, 'error': 'card_create_outcome_unknown'}
        else:
            assert result['not_sent'] is True and not calls
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(3)
        loop.close()


def test_completion_sender_refuses_own_event_loop(host, monkeypatch):
    sender, target, address, calls = host
    async def check():
        loop = asyncio.get_running_loop()
        monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
        monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
        result = sender(address, {'kind': 'text', 'text': 'x'}, idempotency_key='key')
        assert result == {'ok': False, 'not_sent': True, 'error': 'blocking_on_gateway_loop'}
        assert not calls
    asyncio.run(check())


def test_completion_sender_closed_loop_is_definitely_unsent(host, monkeypatch):
    sender, target, address, calls = host
    loop = asyncio.new_event_loop()
    loop.close()
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
    result = sender(address, {'kind': 'text', 'text': 'x'}, idempotency_key='key')
    assert result == {'ok': False, 'not_sent': True, 'error': 'gateway_unavailable'}
    assert not calls


def test_completion_sender_schedule_failure_closes_coroutine(host, monkeypatch):
    sender, target, address, calls = host
    loop = asyncio.new_event_loop()
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
    captured = []
    def reject(coroutine, target_loop):
        captured.append(coroutine)
        raise RuntimeError('loop closed during scheduling')
    monkeypatch.setattr(bridge.asyncio, 'run_coroutine_threadsafe', reject)
    try:
        result = sender(address, {'kind': 'text', 'text': 'x'}, idempotency_key='key')
        assert result == {'ok': False, 'not_sent': True, 'error': 'gateway_unavailable'}
        assert captured[0].cr_frame is None
        assert not calls
    finally:
        loop.close()


def test_card_patch_timeout_is_not_claimed_unsent(host, monkeypatch):
    sender, target, address, calls = host
    loop = asyncio.new_event_loop()
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
    class Pending:
        def result(self, timeout):
            raise TimeoutError('may have reached the platform')
    def pending(coroutine, target_loop):
        coroutine.close()
        return Pending()
    monkeypatch.setattr(bridge.asyncio, 'run_coroutine_threadsafe', pending)
    try:
        result = sender.patch_card(address, 'message', {'schema': '2.0'})
        assert result == {'ok': False, 'error': 'card_patch_outcome_unknown', 'error_type': 'TimeoutError'}
        assert 'not_sent' not in result
    finally:
        loop.close()


@pytest.mark.parametrize('mismatch', [None, 'chat', 'sender', 'message', 'type', 'deleted'])
def test_card_patch_checks_platform_ownership_and_never_uses_generic_edit(host, mismatch, monkeypatch):
    sender, target, address, calls = host
    item = NS(message_id='message', chat_id='chat', msg_type='interactive',
              deleted=False, sender=NS(id='app'))
    if mismatch == 'chat': item.chat_id = 'other'
    if mismatch == 'sender': item.sender.id = 'other'
    if mismatch == 'message': item.message_id = 'other'
    if mismatch == 'type': item.msg_type = 'text'
    if mismatch == 'deleted': item.deleted = True
    def get(request):
        calls.append(('get', request))
        return NS(ok=True, data=NS(items=[item]))
    def patch(request):
        calls.append(('patch', request))
        return NS(ok=True)
    adapter = target.adapter
    adapter._app_id = 'app'
    adapter._client = NS(im=NS(v1=NS(message=NS(get=get, patch=patch))))
    adapter._build_get_message_request = lambda message_id: ('get', message_id)
    adapter._build_patch_message_body = lambda content: content
    adapter._build_patch_message_request = lambda **kwargs: kwargs
    adapter._response_succeeded = lambda response: response.ok
    async def run_blocking(fn, request):
        return fn(request)
    adapter._run_blocking = run_blocking
    async def forbidden_edit(*args, **kwargs):
        raise AssertionError('generic edit may send a duplicate')
    adapter.edit_message = forbidden_edit
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS())
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_messages', NS(_gateway_loop=lambda: (object(), loop)))
    try:
        result = sender.patch_card(address, 'message', {'schema': '2.0'})
        assert result['ok'] is (mismatch is None)
        assert [call[0] for call in calls] == (['get', 'patch'] if mismatch is None else ['get'])
        if mismatch is not None:
            assert result['not_sent'] and result['error'] == 'card_ownership_unverified'
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(3)
        loop.close()


def test_unpatched_adapter_cannot_retry_with_random_message_ids(host):
    sender, target, address, calls = host
    target.adapter.DURABLE_DELIVERY_UUID = False
    result = asyncio.run(sender.send(object(),address,dict(kind='text',text='x')))
    assert result['error'] == 'stable_delivery_uuid_patch_required' and not calls


def test_bridge_requires_explicit_direct_delivery_opt_in():
    ctx = NS(get_config=lambda key,default=None:default)
    with pytest.raises(ValueError,match='administrator opt-in'):
        bridge.create_transport(ctx, None)


def test_unconfirmed_card_finalization_is_visible_without_leaking_exception(caplog):
    class Card:
        def finalize(self, *args, **kwargs):
            raise RuntimeError('private content')
    bridge._finalize_card(Card(), 'task', {'status': 'completed'})
    assert 'task-card finalization failed' in caplog.text
    assert 'private content' not in caplog.text


def test_missing_card_binding_does_not_flood_warning_log(caplog):
    class Card:
        def finalize(self, *args, **kwargs):
            return {'ok': False, 'error': 'task_not_found'}
    bridge._finalize_card(Card(), 'task', {'status': 'completed'})
    assert 'task-card finalization not confirmed' not in caplog.text


def test_uuid_patch_is_idempotent_and_rejects_upstream_drift():
    spec = importlib.util.spec_from_file_location('uuid_patch',ROOT/'patches/patch_feishu_delivery_uuid.py')
    patch = importlib.util.module_from_spec(spec);spec.loader.exec_module(patch)
    source = ('class Adapter:\n    async def _send_raw_message(self, metadata):\n'
              '        first = dict(uuid_value=str(uuid.uuid4()),)\n'
              '        second = dict(uuid_value=str(uuid.uuid4()),)\n')
    updated = patch.transform(source)
    assert updated.count(patch.NEW) == 2
    assert patch.transform(updated) == updated
    with pytest.raises(ValueError,match='drifted'):
        patch.transform(source.replace('uuid_value=', 'different='))


def test_card_report_uses_host_card_renderer(host):
    sender, target, address, calls = host
    result = asyncio.run(sender.send(object(), address,
        dict(kind='text', text='done', card=True, delivery_uuid='stable-key')))
    assert result['ok'] and result['message_id'] == 'card-receipt'
    assert calls[0][0] == 'card' and calls[0][3] is True


def test_card_report_refuses_when_host_card_seam_missing(host, monkeypatch):
    sender, target, address, calls = host
    monkeypatch.setattr(target.adapter, '_send_reply_card', None)
    result = asyncio.run(sender.send(object(), address,
        dict(kind='text', text='done', card=True, delivery_uuid='stable-key')))
    assert result['ok'] is False and result['error'] == 'card_adapter_unavailable'
    assert calls == []


def test_deliver_finalizes_card_even_when_send_is_deferred(tmp_path):
    finalized = []
    class DummyBridge:
        def finalize(self, task_id, **kwargs):
            finalized.append((task_id, kwargs))
            return {'ok': True, 'already_finalized': True}

    class DummyService:
        profile = 'test-profile'
        class DummyPool:
            root = tmp_path / 'queue'
            def outbox_receipt(self, task_id):
                return None
            def status(self, task_id):
                return {'status': 'completed', 'result': {'summary': 'done'}}
            def record_card_finalization_state(self, task_id, state, error):
                pass
        pool = DummyPool()

    ctx = NS(
        get_config=lambda k, default=None: True if k == 'direct_delivery_enabled' else (str(ROOT) if k == 'engine_root' else str(tmp_path / 'cards')),
        plugin_id='durable-dispatch',
        profile_name='test-profile',
    )
    transport = bridge.create_transport(ctx, DummyService())
    transport.card_bridge = DummyBridge()
    # Mock super().deliver to return deferred (e.g. host refused or offline)
    transport.__class__.__bases__[0].deliver = lambda self, addr, msg, idempotency_key: {'deferred': True, 'accepted': False}

    address = dict(profile='test-profile', route='stored-route', channel='feishu', chat='chat')
    message = dict(task_id='task-123', status='completed', summary='result text')
    res = transport.deliver(address, message, idempotency_key='task-123')
    assert res['deferred'] is True
    # Card was finalized despite text delivery deferral!
    assert len(finalized) == 1
    assert finalized[0][0] == 'task-123'
    assert finalized[0][1]['status'] == 'completed'


def test_reconcile_card_falls_back_to_pool_status_when_outbox_missing(tmp_path):
    finalized = []
    class DummyBridge:
        def finalize(self, task_id, **kwargs):
            finalized.append((task_id, kwargs))
            return {'ok': True, 'already_finalized': True}

    class DummyService:
        profile = 'test-profile'
        class DummyPool:
            root = tmp_path / 'queue'
            def outbox_receipt(self, task_id):
                return None
            def status(self, task_id):
                return {'status': 'completed', 'result': {'summary': 'recovered from status'}}
            def record_card_finalization_state(self, task_id, state, error):
                pass
        pool = DummyPool()

    ctx = NS(
        get_config=lambda k, default=None: True if k == 'direct_delivery_enabled' else (str(ROOT) if k == 'engine_root' else str(tmp_path / 'cards')),
        plugin_id='durable-dispatch',
        profile_name='test-profile',
    )
    transport = bridge.create_transport(ctx, DummyService())
    transport.card_bridge = DummyBridge()

    res = transport.reconcile_card('task-456')
    assert res['terminal'] is True
    assert len(finalized) == 1
    assert finalized[0][0] == 'task-456'
    assert finalized[0][1]['status'] == 'completed'
    assert 'recovered from status' in finalized[0][1]['summary']

@pytest.mark.parametrize('field', ['chat', 'channel'])
def test_incomplete_origin_is_not_a_wildcard_and_is_not_sent(host, field):
    sender, target, address, calls = host
    incomplete = dict(address)
    incomplete.pop(field)
    result = asyncio.run(sender.send(object(), incomplete, dict(kind='text', text='x', delivery_uuid='stable')))
    assert result == {'ok': False, 'not_sent': True, 'error': 'stored_origin_mismatch'}
    assert not calls


def test_resolve_cannot_add_a_chat_the_origin_never_stored(host):
    sender, target, address, calls = host
    incomplete = dict(address)
    del incomplete['chat']
    assert target.chat_id == 'chat'
    result = asyncio.run(sender.send(object(), incomplete, dict(kind='text', text='x', delivery_uuid='stable')))
    assert result == {'ok': False, 'not_sent': True, 'error': 'stored_origin_mismatch'}
    assert not calls


def test_unresolvable_stored_route_is_not_sent(host, monkeypatch):
    sender, target, address, calls = host
    def resolve(runner, **kw):
        assert kw == dict(session_key='stored-route', route=None, plugin_home='/profile/owner', plugin_id='durable-dispatch')
        return None, {'error': 'no_conversation'}
    monkeypatch.setattr(sys.modules['gateway'].plugin_messages, 'resolve', resolve)
    result = asyncio.run(sender.send(object(), address, dict(kind='text', text='x', delivery_uuid='stable')))
    assert result == {'ok': False, 'not_sent': True, 'error': 'no_conversation'}
    assert not calls


def test_pre_send_card_failure_returns_not_sent(host, monkeypatch):
    from types import SimpleNamespace
    sender, target, address, calls = host
    async def fail_card(**kw):
        return SimpleNamespace(success=False, message_id=None, raw_response=None, error='missing card session ownership')
    monkeypatch.setattr(target.adapter, '_send_reply_card', fail_card)
    result = asyncio.run(sender.send(object(), address,
        dict(kind='text', text='done', card=True, delivery_uuid='stable-key')))
    assert result == {'ok': False, 'not_sent': True, 'error': 'missing card session ownership'}


def test_stage_workspace_deliverable_copies_into_attempt_dir(tmp_path):
    ws = tmp_path / 'demo_workspace' / 'docs'
    ws.mkdir(parents=True)
    report = ws / 'demo_feasibility_test.md'
    report.write_text('# Feasibility\nOK\n', encoding='utf-8')
    attempt = 'a' * 32
    attempt_root = tmp_path / 'attempts'
    attempt_dir = attempt_root / attempt
    attempt_dir.mkdir(parents=True)

    class Ctx:
        plugin_id = 'durable-dispatch'
        def get_config(self, key, default=None):
            if key == 'direct_delivery_enabled':
                return True
            if key == 'engine_root':
                return str(ROOT)
            if key == 'allowed_roots':
                return [str(tmp_path / 'demo_workspace')]
            return default

    class Pool:
        root = tmp_path
        def status(self, tid):
            return {'attempt': attempt}

    class Service:
        profile = 'demo'
        pool = Pool()

    transport = bridge.create_transport(Ctx(), Service())
    prepared = transport._prepare_message({
        'task_id': 't1',
        'status': 'completed',
        'summary': f'completed\n{report}',
        'artifacts': [],
    })
    staged = str(attempt_dir / 'demo_feasibility_test.md')
    assert staged in prepared['artifacts']
    assert Path(staged).is_file()


def test_finalize_card_prefers_created_at_over_stale_progress_elapsed(monkeypatch):
    transport_mod = importlib.util.spec_from_file_location(
        'hermes_completion_transport_test',
        ROOT / 'runtime' / 'hermes_completion_transport.py',
    )
    mod = importlib.util.module_from_spec(transport_mod)
    transport_mod.loader.exec_module(mod)

    captured = {}

    class Bridge:
        def get_card(self, task_id):
            return {
                'task_id': task_id,
                'finalized': 0,
                'elapsed': 0.7,
                'created_at': 1000.0,
            }

        def finalize(self, task_id, **kwargs):
            captured.update(kwargs)
            return {'ok': True, 'patch_result': {'ok': True}}

    monkeypatch.setattr(mod.time, 'time', lambda: 1125.4)
    mod._finalize_card(Bridge(), 't-elapsed', {'status': 'completed', 'summary': 'done'})
    assert captured['elapsed'] == 125.4


def test_deliver_dedupes_completion_card_when_lifecycle_card_confirmed(tmp_path):
    super_calls = []

    class DummyBridge:
        def get_card(self, task_id):
            return {'task_id': task_id, 'finalized': 0, 'created_at': 1000.0}

        def finalize(self, task_id, **kwargs):
            return {'ok': True, 'patch_result': {'ok': True, 'message_id': 'm-1'}}

    class DummyService:
        profile = 'test-profile'

        class DummyPool:
            root = tmp_path / 'queue'

            def status(self, task_id):
                return {'status': 'completed', 'result': {'summary': 'done'}}

            def record_card_finalization_state(self, task_id, state, error):
                pass

        pool = DummyPool()

    ctx = NS(
        get_config=lambda k, default=None: (
            True if k in ('direct_delivery_enabled', 'task_lifecycle_cards_enabled')
            else (str(ROOT) if k == 'engine_root' else str(tmp_path / 'cards'))
        ),
        plugin_id='durable-dispatch',
        profile_name='test-profile',
    )
    transport = bridge.create_transport(ctx, DummyService())
    transport.card_bridge = DummyBridge()
    transport.__class__.__bases__[0].deliver = lambda self, addr, msg, idempotency_key: super_calls.append(idempotency_key) or {'accepted': True}

    address = dict(profile='test-profile', route='stored-route', channel='feishu', chat='chat')
    res = transport.deliver(address, dict(task_id='t-dedupe', status='completed', summary='done'), idempotency_key='t-dedupe')
    assert res == {'accepted': True, 'deduped_by_lifecycle_card': True, 'task_id': 't-dedupe'}
    assert super_calls == []



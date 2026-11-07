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

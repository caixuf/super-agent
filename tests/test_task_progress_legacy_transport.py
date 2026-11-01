"""Legacy-host transport: real route boundaries, fake network only."""
import asyncio
import contextlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    '_legacy_transport_test', ROOT / 'plugins/task_progress/legacy_messages.py')
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def host():
    source = SimpleNamespace(platform=SimpleNamespace(value='feishu'), chat_id='existing-chat')
    adapter = SimpleNamespace(
        is_connected=True, MAX_MESSAGE_LENGTH=4096, format_message=lambda text: text,
        truncate_message=lambda text, limit: [text],
        send=AsyncMock(return_value=SimpleNamespace(success=True, message_id='card-1')),
        edit_message=AsyncMock(), handle_message=Mock(side_effect=AssertionError('no model injection')))
    runner = SimpleNamespace(
        _running=True, _draining=False, _served_profile_for_home=lambda home: 'default',
        session_store=SimpleNamespace(lookup_by_session_key=Mock(
            return_value=SimpleNamespace(origin=source))),
        _restored_source=lambda entry: entry.origin,
        _canonicalize=lambda source: SimpleNamespace(runtime_profile='default'),
        _plugin_injection_authorized=lambda *args, **kwargs: True,
        _delivery_adapter_for=lambda source: adapter,
        _thread_metadata_for_source=lambda source: {'thread_id': 'existing-thread'},
        _plugin_injection_scope=lambda source: contextlib.nullcontext())
    return runner, adapter


def resolve(runner):
    return transport.resolve(runner, session_key='exact-session', route=None,
                             plugin_home=Path('/profile'), plugin_id='task-progress')


def test_existing_session_posts_one_card_without_model_turn():
    runner, adapter = host()
    target, error = resolve(runner)
    assert error is None
    result = asyncio.run(transport.post(target, 'nonempty progress', plugin_id='task-progress'))
    assert result == {'ok': True, 'message_id': 'card-1'}
    adapter.send.assert_awaited_once_with('existing-chat', 'nonempty progress', metadata={
        'thread_id': 'existing-thread', 'expect_edits': True, 'hermes_plugin_id': 'task-progress'})
    adapter.handle_message.assert_not_called()
    runner.session_store.lookup_by_session_key.assert_called_with('exact-session')


@pytest.mark.parametrize('failure', ['profile', 'unauthorized', 'missing_session', 'disconnected', 'draining'])
def test_route_failures_never_send(failure):
    runner, adapter = host()
    if failure == 'profile':
        runner._canonicalize = lambda source: SimpleNamespace(runtime_profile='other')
    elif failure == 'unauthorized':
        runner._plugin_injection_authorized = lambda *args, **kwargs: False
    elif failure == 'missing_session':
        runner.session_store.lookup_by_session_key.return_value = None
    elif failure == 'disconnected':
        adapter.is_connected = False
    else:
        runner._draining = True
    target, error = resolve(runner)
    assert target is None and error
    adapter.send.assert_not_called()


def test_post_rechecks_authorization_and_rejects_empty_content():
    runner, adapter = host()
    target, _ = resolve(runner)
    assert asyncio.run(transport.post(target, '  ', plugin_id='task-progress'))['error'] == 'empty_message'
    runner._plugin_injection_authorized = lambda *args, **kwargs: False
    assert asyncio.run(transport.post(target, 'progress', plugin_id='task-progress'))['error'] == 'unauthorized'
    adapter.send.assert_not_called()


def test_unknown_host_api_fails_closed():
    runner, adapter = host()
    del runner._canonicalize
    assert resolve(runner)[1]['error'] == 'host_transport_unsupported'
    adapter.send.assert_not_called()

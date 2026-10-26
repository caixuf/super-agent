import asyncio
import importlib.util
from pathlib import Path
import sys
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
    adapter = Adapter()
    target = NS(platform='feishu', chat_id='chat', metadata={'thread_id':'thread'}, live=None, adapter=adapter)
    ctx = NS(plugin_id='durable-dispatch', _manager=NS(home_path='/profile/owner'))
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


def test_local_viewer_is_not_acknowledged_as_platform_delivery(host):
    sender, target, address, calls = host
    target.live = object()
    result = asyncio.run(sender.send(object(),address,dict(kind='text',text='x')))
    assert result['not_sent'] and not calls


def test_unpatched_adapter_cannot_retry_with_random_message_ids(host):
    sender, target, address, calls = host
    target.adapter.DURABLE_DELIVERY_UUID = False
    result = asyncio.run(sender.send(object(),address,dict(kind='text',text='x')))
    assert result['error'] == 'stable_delivery_uuid_patch_required' and not calls


def test_bridge_requires_explicit_direct_delivery_opt_in():
    ctx = NS(get_config=lambda key,default=None:default)
    with pytest.raises(ValueError,match='administrator opt-in'):
        bridge.create_transport(ctx, None)


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

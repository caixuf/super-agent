"""Offline contract checks against an explicitly supplied real host adapter.

Run with the host interpreter and HERMES_SOURCE set. All client calls below are
replaced with in-memory fakes; no platform connection or message is made.
These observations do not prove server-side rendering or mention behavior.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


HOST = os.environ.get('HERMES_SOURCE')


class RealAdapterContract(unittest.TestCase):
    @unittest.skipUnless(HOST, 'set HERMES_SOURCE and use the real host interpreter')
    def test_offline_send_contract(self):
        with tempfile.TemporaryDirectory(prefix='literal-host-check-') as home:
            env = os.environ.copy()
            env['HERMES_HOME'] = home
            env['HERMES_SOURCE'] = str(Path(HOST).resolve())
            env['PYTHONPATH'] = env['HERMES_SOURCE']
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker'],
                                    env=env, cwd=home, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            self.assertIn('offline-adapter-checks-passed', result.stdout)


def _worker():
    sys.path.insert(0, os.environ['HERMES_SOURCE'])
    from plugins.platforms.feishu import adapter as platform
    from gateway.plugin_messages import _fit

    def response(*, ok=True, code=0):
        return types.SimpleNamespace(success=lambda: ok, code=code,
                                     data=types.SimpleNamespace(message_id='fake-message'))

    async def run():
        instance = object.__new__(platform.FeishuAdapter)
        instance._client = object()
        instance._card_replies = False
        sent = []

        async def fake_send(**kwargs):
            sent.append(kwargs)
            return response()

        instance._feishu_send_with_retry = fake_send
        mention = '<at user_id="all">everyone</at>'
        got = await instance.send('chat', 'example ' + mention, metadata={'_delivery_uuid': 'a' * 32})
        assert got.success and got.message_id == 'fake-message'
        assert sent[0]['msg_type'] == 'text' and mention in json.loads(sent[0]['payload'])['text']
        # The remote server's interpretation is outside this offline test.
        sent.clear()
        await instance.send('chat', '**bold**', metadata={'_delivery_uuid': 'a' * 32})
        assert sent[0]['msg_type'] == 'post'
        sent.clear()
        await instance.send('chat', 'x' * (instance.MAX_MESSAGE_LENGTH * 2 + 1),
                            metadata={'_delivery_uuid': 'b' * 32})
        assert len(sent) >= 2 and all(item['metadata']['_delivery_uuid'] == 'b' * 32 for item in sent)
        # The host currently measures characters, not encoded bytes. A message
        # below the character cap can still exceed the documented byte budget.
        emoji = '😀' * instance.MAX_MESSAGE_LENGTH
        assert _fit(instance, 'chat', emoji) == emoji
        assert len(json.dumps({'text': emoji}, ensure_ascii=False).encode('utf-8')) > 30 * 1024

        # Restore the actual retry helper, but replace only the raw I/O call.
        del instance._feishu_send_with_retry
        calls = []
        async def reply_fallback(**kwargs):
            calls.append(kwargs)
            return response(ok=False, code=next(iter(platform._FEISHU_REPLY_FALLBACK_CODES))) if len(calls) == 1 else response()
        instance._send_raw_message = reply_fallback
        metadata = {'_delivery_uuid': 'c' * 32}
        result = await instance._feishu_send_with_retry(chat_id='chat', msg_type='text', payload='{}',
                                                         reply_to='old-message', metadata=metadata)
        assert result.success() and [item['reply_to'] for item in calls] == ['old-message', None]
        assert all(item['metadata']['_delivery_uuid'] == 'c' * 32 for item in calls)
        calls.clear()
        result = await instance._feishu_send_with_retry(chat_id='chat', msg_type='text', payload='{}',
                          reply_to='old-message', metadata={**metadata, 'thread_id': 'thread'})
        assert not result.success() and len(calls) == 1

        # Exceptions invoke actual retry logic with the same operation UUID.
        calls.clear()
        async def raise_then_succeed(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError('may have been sent')
            return response()
        async def no_wait(*args, **kwargs):
            return None
        instance._send_raw_message = raise_then_succeed
        with patch.object(platform.asyncio, 'sleep', no_wait):
            result = await instance._feishu_send_with_retry(chat_id='chat', msg_type='text', payload='{}',
                                                            reply_to=None, metadata=metadata)
        assert result.success() and len(calls) == 2
        assert all(item['metadata']['_delivery_uuid'] == 'c' * 32 for item in calls)

    asyncio.run(run())
    print('offline-adapter-checks-passed')


if __name__ == '__main__':
    if sys.argv[1:] == ['--worker']:
        _worker()
    else:
        unittest.main()

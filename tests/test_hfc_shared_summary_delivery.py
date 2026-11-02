"""Opt-in actual sidecar HTTP lifecycle: shared final UI must keep one owner."""
import asyncio
import json

import pytest

# Same private HOME/state/fake transport as the delivery-loss regression suite.
from test_hfc_delivery_contract import hfc, _Platform, _client, _event, _post

SAMPLE = '''## 检查结论

**完成**，代码 `a|b` 和链接 [报告](https://example.test/report) 保留。

| 项目 | 结果 |
|---|---|
| a\\|b | **通过** |

````markdown
```cpp
| not | a table |
|---|---|
```
````
'''


class TrackedPlatform(_Platform):
    def __init__(self):
        super().__init__()
        self.update_targets = []

    async def update_card_message(self, message_id, card):
        await super().update_card_message(message_id, card)
        self.update_targets.append(message_id)


def test_shared_final_changes_body_not_delivery_owner(hfc, tmp_path):
    server, _ = hfc

    async def run():
        platform = TrackedPlatform()
        client = await _client(server, tmp_path, platform, card_config={
            'final_renderer': 'shared', '_presentation_mode': 'task',
            'show_reasoning': True, 'timeline_expanded': False,
            'footer_fields': ['duration', 'model'],
        })
        try:
            await _post(client, _event('message.started', 0))
            await _post(client, _event('answer.delta', 1, SAMPLE))
            terminal = _event('message.completed', 2, SAMPLE)
            terminal['data'].update(duration=3.0, model='test-model')
            reply = await _post(client, terminal)
            assert reply.get('delivery') == {'outcome': 'delivered'}
            assert len(platform.sent) == 1
            assert set(platform.update_targets) == {'om_probe_1'}
            final = platform.updated[-1]
            assert final['schema'] == '2.0'
            assert final['header']['template'] == 'green'
            assert final['config']['update_multi'] is True
            body = final['body']['elements']
            tables = [e for e in body if e.get('tag') == 'table']
            assert len(tables) == 1
            assert tables[0]['rows'] == [{'c0': 'a|b', 'c1': '**通过**'}]
            rendered = '\n'.join(e.get('content', '') for e in body if e.get('tag') == 'markdown')
            assert '```cpp' in rendered  # no task plain_text code-selector rewrite
            assert '[报告](https://example.test/report)' in rendered
            assert any(e.get('element_id') == 'footer' for e in body)
            session = next(iter(client.app[server.SESSIONS_KEY].values()))
            assert session.answer_text == SAMPLE.rstrip()  # HFC terminal normalizer strips the tail
            counts = len(platform.sent), len(platform.updated)
            replay = await _post(client, _event('message.completed', 3, SAMPLE))
            assert replay.get('delivery') == {'outcome': 'delivered'}
            assert counts == (len(platform.sent), len(platform.updated))
        finally:
            await client.close()

    asyncio.run(run())

"""Tests for single-card handoff (frontdesk live card -> TaskCardBridge lifecycle)."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentcore.pool import normalize_origin
from agentcore.task_card_bridge import TaskCardBridge
from plugins.durable_dispatch import DispatchService


def test_normalize_origin_accepts_card_message_id():
    raw = {
        'profile': 'caixuf',
        'session': 's1',
        'route': 'agent:main:feishu:dm:oc_1',
        'channel': 'feishu',
        'chat': 'oc_1',
        'card_message_id': 'om_live_001',
    }
    encoded = normalize_origin(raw)
    decoded = json.loads(encoded)
    assert decoded['card_message_id'] == 'om_live_001'


def test_drain_cards_reuses_claimed_card_without_creating_second_card(tmp_path: Path):
    patches = []

    def fake_sender(address, message_id, card_payload):
        patches.append((message_id, card_payload))
        return {'ok': True, 'message_id': message_id}

    cards_dir = tmp_path / 'cards'
    bridge = TaskCardBridge(cards_dir, profile='caixuf', sender=fake_sender)
    creator = MagicMock(side_effect=AssertionError('creator must not be called when card_message_id is claimed'))

    transport = MagicMock()
    transport.card_bridge = bridge
    transport.card_creator = creator
    transport.reconcile_card = MagicMock(return_value={'terminal': False})

    service = DispatchService(
        str(tmp_path / 'queue'),
        'caixuf',
        [str(tmp_path)],
        role='ops',
        route_provider=lambda _sid: {
            'route': 'agent:main:feishu:dm:oc_1',
            'channel': 'feishu',
            'chat': 'oc_1',
        },
        require_delivery_route=True,
        task_cards_enabled=True,
    )
    origin = {
        'profile': 'caixuf',
        'session': 's1',
        'route': 'agent:main:feishu:dm:oc_1',
        'channel': 'feishu',
        'chat': 'oc_1',
        'card_message_id': 'om_live_001',
    }
    src = tmp_path / 'sample.txt'
    src.write_text('hello\n', encoding='utf-8')
    res = service._submit_internal(
        's1',
        'check status',
        'req-single-card-1',
        sources=[{'path': str(src)}],
        origin=origin,
        max_goal_len=4000,
        is_scheduled=False,
    )
    assert res.get('task_id')
    task_id = res['task_id']

    outcomes = service.drain_cards(transport)
    assert len(outcomes) == 1
    assert outcomes[0]['state'] == 'sent'
    creator.assert_not_called()

    card = bridge.get_card(task_id)
    assert card is not None
    assert card['message_id'] == 'om_live_001'
    assert card['create_state'] == 'sent'
    assert len(patches) == 1
    assert patches[0][0] == 'om_live_001'

    # Finalize on the same card
    fin = bridge.finalize(task_id, status='completed', summary='all good', elapsed=1.2)
    assert fin['ok'] is True
    assert fin['patch_result']['ok'] is True
    assert patches[-1][0] == 'om_live_001'

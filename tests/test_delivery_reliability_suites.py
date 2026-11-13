"""5-suite reliability & delivery regression tests for Super Agent."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from agentcore.direct_completion import DirectCompletionTransport
from agentcore.task_card_bridge import TaskCardBridge
from plugins.durable_dispatch import DispatchService


# ---------------------------------------------------------------------------
# Suite 1: Card Invariants (artifact dedup + structured_report v1.1 delta)
# ---------------------------------------------------------------------------

def test_suite1_artifact_dedup_skip_text_card(tmp_path: Path):
    """When lifecycle card is confirmed and artifacts exist, skip_text_card sends only artifacts."""
    sent_items = []

    def fake_sender(address, item, *, idempotency_key):
        sent_items.append(item)
        return {'ok': True, 'message_id': f'msg_{len(sent_items)}'}

    attempt_dir = tmp_path / 'attempts'
    own_attempt = attempt_dir / ('a' * 32)
    own_attempt.mkdir(parents=True)
    report = own_attempt / '任务执行报告.md'
    report.write_text('# Report\n\nAll good.\n', encoding='utf-8')

    transport = DirectCompletionTransport(
        tmp_path / 'delivery',
        profile='caixuf',
        sender=fake_sender,
        render=lambda m: m.get('summary', ''),
        attempt_root=attempt_dir,
        attempt_provider=lambda _job: 'a' * 32,
        card=True,
    )
    res = transport.deliver(
        {'profile': 'caixuf', 'route': 'agent:main:feishu:dm:oc_1', 'channel': 'feishu', 'chat': 'oc_1'},
        {
            'task_id': 't-art-1',
            'status': 'completed',
            'summary': 'done',
            'artifacts': [str(report)],
            'skip_text_card': True,
        },
        idempotency_key='t-art-1',
    )
    assert res['accepted'] is True
    assert len(sent_items) == 1
    assert sent_items[0]['kind'] != 'text'
    assert sent_items[0]['name'] == '任务执行报告.md'


def test_suite1_structured_report_incremental_delta(tmp_path: Path):
    """structured_report v1.1 apply_report_delta updates the same card in-place to terminal."""
    patches = []

    def fake_sender(address, message_id, card_payload):
        patches.append((message_id, card_payload))
        return {'ok': True, 'message_id': message_id}

    bridge = TaskCardBridge(tmp_path / 'cards', profile='caixuf', sender=fake_sender)
    origin = {'route': 'agent:main:feishu:dm:oc_1', 'channel': 'feishu', 'chat': 'oc_1'}
    bound = bridge.bind_task('t-delta-1', origin, 'check delta', 'ops', 'Ops', message_id='om_live_d1')
    assert bound['ok'] is True

    mid = bridge.apply_report_delta(
        't-delta-1',
        {'conclusion': 'checking logs...', 'progress': 0.5},
        elapsed=2.0,
    )
    assert mid['ok'] is True
    assert mid['patch_result']['ok'] is True

    fin = bridge.apply_report_delta(
        't-delta-1',
        {'conclusion': 'all checks passed'},
        elapsed=4.5,
        terminal_status='completed',
    )
    assert fin['ok'] is True
    assert fin['patch_result']['ok'] is True
    card = bridge.get_card('t-delta-1')
    assert card['finalized'] == 1
    assert card['status'] == 'completed'
    assert all(mid_id == 'om_live_d1' for mid_id, _ in patches)


# ---------------------------------------------------------------------------
# Suite 2: Concurrency & Recovery (reconcile_card after fast finish)
# ---------------------------------------------------------------------------

def test_suite2_bind_reused_card_and_reconcile_terminal(tmp_path: Path):
    """If a task finishes before drain_cards runs, bind + reconcile finalizes the claimed card."""
    patches = []

    def fake_sender(address, message_id, card_payload):
        patches.append(message_id)
        return {'ok': True, 'message_id': message_id}

    bridge = TaskCardBridge(tmp_path / 'cards', profile='caixuf', sender=fake_sender)
    origin = {'route': 'agent:main:feishu:dm:oc_1', 'channel': 'feishu', 'chat': 'oc_1'}
    bridge.bind_task('t-fast-1', origin, 'fast task', 'ops', 'Ops', message_id='om_fast_1')
    fin = bridge.finalize('t-fast-1', status='completed', summary='fast done', elapsed=0.5)
    assert fin['ok'] is True
    assert patches == ['om_fast_1']
    # Idempotent second finalize does not emit another patch
    fin2 = bridge.finalize('t-fast-1', status='completed', summary='fast done', elapsed=0.5)
    assert fin2['ok'] is True
    assert fin2.get('already_finalized') is True
    assert patches == ['om_fast_1']


# ---------------------------------------------------------------------------
# Suite 3: Routing & Boundary (502 / timeout fast-fail)
# ---------------------------------------------------------------------------

def test_suite3_frontdesk_router_fast_fail_on_error(tmp_path: Path):
    """FrontdeskRouter degrades cleanly within route_timeout when decider returns 502/error."""
    import time
    from plugins.local_model_grounding.client import LocalModelClient
    from plugins.local_model_grounding.router import FrontdeskRouter

    spec = tmp_path / 'roles_spec.json'
    spec.write_text(json.dumps({
        'version': 1,
        'roles': {
            'ops': {'display_name': 'IMFL编译与运维专家', 'keywords': ['发版']},
            'triage': {'display_name': 'IMFL分诊归因专家', 'keywords': ['归因']},
        },
    }), encoding='utf-8')
    client = LocalModelClient(base_url='http://127.0.0.1:1', timeout=0.2)
    router = FrontdeskRouter(client, roles_spec_path=str(spec), timeout=0.2, min_confidence=0.45)
    t0 = time.monotonic()
    decision = router.route('看下最近一次imfl平台测试的任务号')
    elapsed = time.monotonic() - t0
    assert decision is None
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Suite 4: Config Drift & Latency SLO
# ---------------------------------------------------------------------------

def test_suite4_live_profile_config_invariants():
    """Verify caixuf profile keeps required single-card / low-latency settings."""
    cfg_path = Path('/home/caixuf/.hermes/profiles/caixuf/config.yaml')
    if cfg_path.is_file():
        cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8'))
        display = cfg.get('display') or {}
        feishu = (display.get('platforms') or {}).get('feishu') or {}
        assert display.get('busy_input_mode') == 'steer', 'caixuf busy_input_mode drifted'
        assert feishu.get('cleanup_progress') is False, 'caixuf cleanup_progress drifted'
        assert feishu.get('interim_assistant_messages') is False, 'caixuf interim_assistant_messages drifted'
        assert feishu.get('tool_progress') is False, 'caixuf tool_progress drifted'
        lmg = ((cfg.get('plugins') or {}).get('entries') or {}).get('local-model-grounding', {}).get('settings', {})
        if 'route_timeout' in lmg:
            assert float(lmg['route_timeout']) <= 2.0, f'caixuf route_timeout too high: {lmg["route_timeout"]}'


# ---------------------------------------------------------------------------
# Suite 5: Fault Injection & Idempotency
# ---------------------------------------------------------------------------

def test_suite5_failed_task_finalizes_card_and_duplicate_submit_is_idempotent(tmp_path: Path):
    """Failed task transitions card to failed; duplicate request_key does not create a second job."""
    patches = []

    def fake_sender(address, message_id, card_payload):
        patches.append((message_id, card_payload))
        return {'ok': True, 'message_id': message_id}

    bridge = TaskCardBridge(tmp_path / 'cards', profile='caixuf', sender=fake_sender)
    src = tmp_path / 'sample.txt'
    src.write_text('x\n', encoding='utf-8')
    service = DispatchService(
        str(tmp_path / 'queue'),
        'caixuf',
        [str(tmp_path)],
        role='ops',
        route_provider=lambda _sid: {'route': 'agent:main:feishu:dm:oc_1', 'channel': 'feishu', 'chat': 'oc_1'},
        require_delivery_route=True,
        task_cards_enabled=True,
    )
    origin = {
        'profile': 'caixuf', 'session': 's1',
        'route': 'agent:main:feishu:dm:oc_1', 'channel': 'feishu', 'chat': 'oc_1',
        'card_message_id': 'om_fault_1',
    }
    r1 = service._submit_internal('s1', 'run op', 'idem-key-1', sources=[{'path': str(src)}],
                                  origin=origin, max_goal_len=4000, is_scheduled=False)
    r2 = service._submit_internal('s1', 'run op', 'idem-key-1', sources=[{'path': str(src)}],
                                  origin=origin, max_goal_len=4000, is_scheduled=False)
    assert r1['task_id'] == r2['task_id']

    transport = MagicMock()
    transport.card_bridge = bridge
    transport.card_creator = MagicMock(side_effect=AssertionError('must not create second card'))
    transport.reconcile_card = MagicMock(return_value={'terminal': False})
    service.drain_cards(transport)

    fin = bridge.finalize(r1['task_id'], status='failed', summary='worker crashed', elapsed=3.1)
    assert fin['ok'] is True
    card = bridge.get_card(r1['task_id'])
    assert card['status'] == 'failed'
    assert card['finalized'] == 1

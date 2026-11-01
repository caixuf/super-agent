"""Independent acceptance guards: synthetic inputs, temporary state, no live services."""
import json

import pytest

from agentcore.memory import memory_autonomy, memory_store, progressive, runtime_bridge


def process(root, message, quote=None, *, turn='t', **identity):
    fields = dict(profile='p', user_id='u', agent_id='a', session_id='s',
                  turn_id=turn, user_message=message, assistant_reply='完成')
    fields.update(identity)
    event = runtime_bridge.build_turn_event(**fields)
    queue = runtime_bridge.TurnEventQueue(root / 'events.db')
    queue.enqueue(event)
    quote = message if quote is None else quote

    def model(messages, timeout_seconds):
        return json.dumps({'facts': [{'statement': quote, 'user_quote': quote}]},
                          ensure_ascii=False)

    pipeline = memory_autonomy.AutonomousMemoryPipeline(queue=queue, model=model)
    return pipeline.process(event), event


def statuses():
    conn = memory_store._connect()
    try:
        return [dict(row) for row in conn.execute(
            'SELECT fact_id,status,statement FROM working_fact')]
    finally:
        conn.close()


@pytest.mark.parametrize('message,quote', [
    ('我还没决定以后使用英文回复。', '以后使用英文回复'),
    ('以后可能采用 3% 门限，但还没确定。', '采用 3% 门限'),
    ('以后可能用英文回复，我还没想好。', '以后可能用英文回复，我还没想好。'),
    ('有人建议以后删除备份，我不同意。', '以后删除备份'),
    ('假设以后默认删除备份，但这不是我的决定。', '以后默认删除备份'),
    ('以后不能把“默认删除备份”当成我的决定。', '默认删除备份'),
])
def test_quote_cannot_remove_uncertainty_or_negation(tmp_path, configured, message, quote):
    with configured(tmp_path):
        outcome, _ = process(tmp_path, message, quote)
        assert outcome.get('written', 0) == 0, outcome
        assert not any(row['status'] == 'active' for row in statuses())


@pytest.mark.parametrize('message', [
    '以后默认采用 api_key=synthetic0123456789abcdefghij',
    '以后默认采用 access_token: synthetic0123456789abcdefghij',
    '以后记住密码是 synthetic-only-not-a-secret-123456',
    '以后默认使用 Bearer synthetic-token-not-real-0123456789abcdef',
])
def test_synthetic_secrets_do_not_reach_fact_store_frozen_or_logs(
        tmp_path, configured, caplog, message):
    with configured(tmp_path):
        outcome, _ = process(tmp_path, message)
        assert outcome.get('written', 0) == 0, outcome
        assert statuses() == []
        assert message not in caplog.text
        for path in (tmp_path / 'memory_autonomy_frozen').glob('*.json'):
            assert message not in path.read_text()
        for path in (tmp_path / 'memory' / 'log').glob('*.md'):
            assert message not in path.read_text()


@pytest.mark.parametrize('message,quote', [
    ('是否改用英文？以后默认中文回复。', '以后默认中文回复。'),
    ('决定以后默认使用 https://example.test/api?mode=fast。',
     '决定以后默认使用 https://example.test/api?mode=fast。'),
    ('以后默认用中文回复。', '以后默认用中文回复。'),
])
def test_explicit_independent_decision_is_not_lost(tmp_path, configured, message, quote):
    with configured(tmp_path):
        outcome, _ = process(tmp_path, message, quote)
        assert outcome.get('ok') is True and outcome.get('written') == 1, outcome


@pytest.mark.parametrize('foreign', [dict(agent_id='other'), dict(profile='other')])
def test_actual_progressive_entrypoints_reject_foreign_identity(
        tmp_path, configured, foreign):
    with configured(tmp_path):
        outcome, _ = process(tmp_path, '以后回复我时保持简洁。')
        assert outcome.get('written') == 1, outcome
        fact_id = next(row['fact_id'] for row in statuses() if row['status'] == 'active')
        scope = dict(user_id='u', agent_id='a', profile='p')
        own = progressive.get_observations([fact_id], **scope)
        assert own['count'] == 1
        scope.update(foreign)
        assert progressive.search('以后回复我时保持简洁。', **scope)['hits'] == []
        assert progressive.timeline([fact_id], **scope)['hits'] == []
        assert progressive.get_observations([fact_id], **scope)['results'] == []
        prefetch = runtime_bridge.prefetch_context('以后回复我时保持简洁。', **scope)
        assert fact_id not in json.dumps(prefetch)


def test_failed_final_recall_cannot_publish_new_fact(tmp_path, configured, monkeypatch):
    with configured(tmp_path):
        monkeypatch.setattr(memory_store, 'recall_facts',
                            lambda *args, **kwargs: {'ok': False, 'results': []})
        outcome, _ = process(tmp_path, '决定以后默认回复使用中文。')
        assert outcome.get('ok') is False, outcome
        assert any(row.get('reason') == 'recall_verification_failed'
                   for row in outcome.get('results', [])), outcome
        assert not any(row['status'] == 'active' for row in statuses()), statuses()
        for path in (tmp_path / 'memory' / 'log').glob('*.md'):
            assert '决定以后默认回复使用中文。' not in path.read_text()


def test_failed_final_recall_preserves_superseded_predecessor(
        tmp_path, configured, monkeypatch):
    from agentcore.memory import memory_conflict
    with configured(tmp_path):
        old = memory_store.write_fact(
            '决定以后默认使用英文回复。', user_id='u', agent_id='a',
            subject='回复', tags={'profile': 'p'})
        memory_store.flush_background(timeout=30)
        monkeypatch.setattr(memory_conflict, 'resolve', lambda *args, **kwargs: {
            'action': 'write', 'relation': 'supersede',
            'superseded': [old['fact_id']], 'reason': 'synthetic_supersede', 'judged': []})
        monkeypatch.setattr(memory_store, 'recall_facts',
                            lambda *args, **kwargs: {'ok': False, 'results': []})
        outcome, _ = process(tmp_path, '决定以后默认回复使用中文。')
        assert outcome.get('ok') is False, outcome
        assert any(row.get('reason') == 'recall_verification_failed'
                   for row in outcome.get('results', [])), outcome
        active = [row['fact_id'] for row in statuses() if row['status'] == 'active']
        assert active == [old['fact_id']], statuses()


@pytest.mark.parametrize('unrelated_count', [0, 9])
def test_unresolved_opposite_preference_keeps_older_fact(
        tmp_path, configured, monkeypatch, unrelated_count):
    from agentcore.memory import memory_conflict
    with configured(tmp_path):
        old = memory_store.write_fact(
            '以后回答保持简洁。', user_id='u', agent_id='a',
            subject='以后回答保持简洁。', tags={'profile': 'p'})
        for index in range(unrelated_count):
            memory_store.write_fact(
                '默认项目制品编号为编号%d。' % index, user_id='u', agent_id='a',
                subject='独立制品%d' % index, tags={'profile': 'p'})
        memory_store.flush_background(timeout=30)
        judged = []

        def judge(new, previous):
            judged.append(previous['fact_id'])
            return {'relation': 'defer', 'reason': 'synthetic_judge_unavailable'}

        monkeypatch.setattr(memory_conflict, 'judge', judge)
        outcome, _ = process(tmp_path, '今后回答尽可能详细。')
        assert outcome.get('written', 0) == 0, outcome
        active = [row for row in statuses() if row['status'] == 'active']
        assert old['fact_id'] in [row['fact_id'] for row in active]
        assert old['fact_id'] in judged, (outcome, judged)
        assert not any(row['statement'] == '今后回答尽可能详细。' for row in active)



def test_semantic_duplicate_terminates_without_retry_or_new_active_fact(
        tmp_path, configured, monkeypatch):
    from agentcore.memory import memory_conflict
    with configured(tmp_path):
        old = memory_store.write_fact(
            '以后回复保持简洁。', user_id='u', agent_id='a',
            subject='回复偏好', tags={'profile': 'p'})
        memory_store.flush_background(timeout=30)
        monkeypatch.setattr(memory_conflict, 'judge', lambda new, previous: {
            'relation': 'duplicate', 'reason': 'synthetic_same_preference'})
        outcome, _ = process(tmp_path, '今后回答保持简短。')
        assert outcome.get('ok') is True and outcome.get('written') == 0, outcome
        assert not outcome.get('retryable'), outcome
        active = [row['fact_id'] for row in statuses() if row['status'] == 'active']
        assert active == [old['fact_id']]


def test_team_legacy_recall_does_not_expose_other_agents_auto_memory(
        tmp_path, configured):
    with configured(tmp_path):
        outcome, _ = process(tmp_path, '以后回复我时保持简洁。')
        assert outcome.get('written') == 1, outcome
        auto_id = next(row['fact_id'] for row in statuses() if row['status'] == 'active')
        manual = memory_store.write_fact(
            '团队默认制品编号为 SHARED_SYNTHETIC_701。',
            user_id='u', agent_id='a', subject='SHARED_SYNTHETIC_701')
        memory_store.flush_background(timeout=30)
        own = memory_store.recall_facts(
            '以后回复我时保持简洁。', user_id='u', agent_id='a',
            profile='p', shared_legacy=True)
        assert auto_id in [row['fact_id'] for row in own.get('results', [])]
        foreign = dict(user_id='u', agent_id='other', profile='other', shared_legacy=True)
        recalled = memory_store.recall_facts('以后回复我时保持简洁。', **foreign)
        assert auto_id not in [row['fact_id'] for row in recalled.get('results', [])]
        recalled = memory_store.recall_facts('SHARED_SYNTHETIC_701', **foreign)
        assert manual['fact_id'] in [row['fact_id'] for row in recalled.get('results', [])]


def test_active_snapshot_drift_during_judgment_does_not_publish_stale_verdict(
        tmp_path, configured, monkeypatch):
    from agentcore.memory import memory_conflict
    with configured(tmp_path):
        old = memory_store.write_fact(
            '以后回答保持简洁。', user_id='u', agent_id='a',
            subject='回答风格', tags={'profile': 'p'})
        memory_store.flush_background(timeout=30)
        concurrent = []

        def judge(new, previous):
            if not concurrent:
                concurrent.append(memory_store.write_fact(
                    '以后回答仅使用英文。', user_id='u', agent_id='a',
                    subject='回答语言', tags={'profile': 'p'}))
            return {'relation': 'supersede', 'reason': 'synthetic_stale_verdict'}

        monkeypatch.setattr(memory_conflict, 'judge', judge)
        outcome, _ = process(tmp_path, '今后回答尽可能详细。')
        assert concurrent, outcome
        assert outcome.get('ok') is False and outcome.get('written') == 0, outcome
        assert outcome.get('retryable') is True, outcome
        active = [row for row in statuses() if row['status'] == 'active']
        assert old['fact_id'] in [row['fact_id'] for row in active]
        assert not any(row['statement'] == '今后回答尽可能详细。' for row in active)

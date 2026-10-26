import json

import pytest

from agentcore.memory import memory_trigger, memory_store
from agentcore.memory.runtime_service import run_and_flush, scoped_runner


@pytest.mark.parametrize('response,error', [
    ({'content': 'not json'}, 'model_invalid_json'),
    ({'content': '{"facts":false}'}, 'model_invalid_facts'),
    ({'content': '{"facts":[{}]}'}, 'model_invalid_fact_entry'),
    ({'content': '{"facts":[]}', 'raw': {'choices': [{'finish_reason': 'length'}]}}, 'model_output_truncated'),
])
def test_model_errors_are_retryable_failures_not_empty_success(tmp_path, configured, response, error):
    with configured(tmp_path, llm_fn=lambda *a, **kw: response):
        result = memory_trigger.run('决定以后保留绿色标签', '已记录')
        assert result['ok'] is False and result['retryable'] is True
        assert result['reason'] == 'extract_failed' and result['error'] == error


def test_empty_facts_is_valid_success(tmp_path, configured):
    with configured(tmp_path, llm_fn=lambda *a, **kw: {'content': '{"facts":[]}'}):
        result = memory_trigger.run('决定以后保留绿色标签', '已记录')
        assert result['ok'] is True and result['reason'] == 'no_fact_extracted'


def test_transport_error_is_not_silently_acknowledged(tmp_path, configured):
    def broken(*a, **kw):
        raise TimeoutError('private provider detail')
    with configured(tmp_path, llm_fn=broken):
        result = memory_trigger.run('决定以后保留绿色标签', '已记录')
        assert result['ok'] is False and result['error'] == 'model_call_failed'
        assert 'private' not in json.dumps(result)


def test_partial_write_failure_remains_retryable(tmp_path, configured, monkeypatch):
    response = {'content': json.dumps({'facts': [{'statement': '以后保留绿色标签', 'subject': '标签'}]})}
    with configured(tmp_path, llm_fn=lambda *a, **kw: response):
        monkeypatch.setattr(memory_store, 'write_fact', lambda *a, **kw: {'ok': False, 'error': 'unavailable'})
        result = memory_trigger.run('决定以后保留绿色标签', '已记录')
        assert result['ok'] is False and result['reason'] == 'write_failed'


def test_event_not_done_until_index_work_joins(monkeypatch):
    monkeypatch.setattr(memory_trigger, 'run', lambda *a, **kw: {'ok': True, 'written': 1})
    monkeypatch.setattr(memory_store, 'flush_background', lambda **kw: 1)
    assert run_and_flush('decision', 'ok') == {
        'ok': False, 'retryable': True, 'reason': 'index_flush_incomplete'}


@pytest.mark.parametrize('quote', [None, '助手自己决定', '', '保留红色'])
def test_autonomous_write_requires_verbatim_user_evidence(tmp_path, configured, quote):
    response = {'content': json.dumps({'facts': [{'statement': '以后保留绿色标签',
                                                  'subject': '标签', 'user_quote': quote}]})}
    with configured(tmp_path, llm_fn=lambda *a, **kw: response):
        result = memory_trigger.run('决定以后保留绿色标签', '我决定保留红色标签', require_user_evidence=True)
        assert result['ok'] is False and result['error'] == 'model_unverified_user_evidence'


def test_user_grounded_fact_is_written(tmp_path, configured):
    response = {'content': json.dumps({'facts': [{'statement': '以后保留绿色标签',
        'subject': '标签', 'user_quote': '决定以后保留绿色标签'}]})}
    with configured(tmp_path, llm_fn=lambda *a, **kw: response):
        result = memory_trigger.run('决定以后保留绿色标签', '已记录', require_user_evidence=True)
        assert result['ok'] is True and result['written'] == 1
        assert result['facts'][0]['statement'] == '决定以后保留绿色标签'


def test_priority_statement_is_considered_but_question_is_not():
    assert memory_trigger.should_consider('完成个人工作站，十个任务首先完成，之后再优化')['consider']
    assert not memory_trigger.should_consider('先做完十个任务再优化吗？')['consider']


@pytest.mark.parametrize('field,value', [('profile', 'other'), ('user_id', 'another'),
                                       ('agent_id', 'foreign'), ('status', 'failed')])
def test_runtime_rejects_foreign_events_before_extraction(monkeypatch, field, value):
    event = dict(profile='p', user_id='u', agent_id='a', status='completed', idempotency_key='turn')
    event[field] = value
    def forbidden(*a, **kw):
        pytest.fail('foreign event must not reach extraction')
    monkeypatch.setattr(memory_trigger, 'run', forbidden)
    result = scoped_runner(profile='p', user_id='u', agent_id='a')(event)
    assert result == {'ok': False, 'retryable': False, 'reason': 'event_principal_mismatch'}

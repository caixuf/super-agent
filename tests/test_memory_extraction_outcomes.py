import json

import pytest

from agentcore.memory import memory_trigger, memory_store
from agentcore.memory.runtime_service import run_and_flush, scoped_runner,worker_observation


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


def test_automatic_quote_deduplication_does_not_depend_on_model_subject(tmp_path, configured):
    subjects = iter(['配色偏好', '标签颜色约定'])
    def model(*a, **kw):
        return {'content': json.dumps({'facts': [{'statement': 'model paraphrase is ignored',
            'subject': next(subjects), 'user_quote': '决定以后保留绿色标签'}]})}
    with configured(tmp_path, llm_fn=model):
        first = memory_trigger.run('决定以后保留绿色标签', '已记录', require_user_evidence=True)
        again = memory_trigger.run('决定以后保留绿色标签', '已记录', require_user_evidence=True)
        assert first['ok'] and first['written'] == 1
        assert again['ok'] and again['written'] == 0
        assert again['results'][0]['fact_id'] == first['results'][0]['fact_id']


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


def test_assistant_rule_cannot_promote_a_question_suffix_to_personal_preference(tmp_path,configured):
    def forbidden(*args,**kwargs):pytest.fail('a question alone must not start automatic extraction')
    with configured(tmp_path,llm_fn=forbidden):
        result=memory_trigger.run('运行结束 exit 0 算成功吗？请直接回答，不调用工具。',
                                  '必须检查真实产物。',require_user_evidence=True)
        assert result['ok'] and result['written']==0


def test_user_only_screening_keeps_explicit_future_and_readable_document_preferences():
    assert memory_trigger.should_consider('以后默认用中文回复。')['consider']
    assert memory_trigger.should_consider('生成的文档要讲人话，可读性高。')['consider']
    assert memory_trigger.should_consider('已经确定，并发上限固定为7个。')['consider']


@pytest.mark.parametrize('message,quote',[
    ('以后默认英文回复好吗？请直接回答。','以后默认英文回复'),
    ('以后默认英文回复好吗？','以后默认英文回复好吗？'),
    ('有人建议“以后删除备份”，我不同意，不能当成我的决定。','有人建议“以后删除备份”，我不同意，不能当成我的决定。'),
    ('有人建议删除备份，我不同意，不能当成我的决定。','我不同意'),
])
def test_verbatim_quote_cannot_drop_question_or_rejection_qualifications(message,quote):
    assert memory_trigger.unconfirmed_quote_context(message,quote)


def test_independent_declaration_after_question_is_still_eligible():
    assert not memory_trigger.unconfirmed_quote_context('要用什么颜色？我喜欢蓝色。','我喜欢蓝色。')
    quote='决定使用 https://example.test/api?mode=fast。'
    assert not memory_trigger.unconfirmed_quote_context(quote,quote)


@pytest.mark.parametrize('quote',[
    '请直接回答，不调用工具。','这次不要改文件。','不要运行仿真，只分析日志。',
    'Only answer, do not use tools.','本轮按规则不要改代码。',
])
def test_turn_constraints_are_not_long_term_rules(quote):
    assert memory_trigger.turn_scoped_constraint(quote)


@pytest.mark.parametrize('quote',[
    '以后默认不调用工具。','我决定不修改生产文件。','主会话职责是不运行长任务。',
    '门限不要低于3%。','生成的文档要讲人话，可读性高。',
    '我不喜欢冗长的回复。',
])
def test_confirmed_durable_rules_and_non_capability_facts_remain_eligible(quote):
    assert not memory_trigger.turn_scoped_constraint(quote)


def test_model_claim_cannot_override_temporary_scope(tmp_path,configured):
    quote='这次不要改文件。'
    response={'content':json.dumps({'facts':[dict(statement='以后不改文件',subject='长期偏好',user_quote=quote)]})}
    with configured(tmp_path,llm_fn=lambda *a,**kw:response):
        result=memory_trigger.run(quote,'以后都不改。',require_user_evidence=True)
        assert result['ok'] and result['written']==0 and result['reason']=='turn_scoped_instruction'


def test_reserved_probe_sessions_skip_before_model_without_skipping_other_principals(monkeypatch):
    monkeypatch.setattr(memory_trigger,'run',lambda *a,**kw:pytest.fail('probe cannot write user facts'))
    runner=scoped_runner(profile='p',user_id='u',agent_id='a',excluded_session_prefixes=['private-probe-'])
    event=dict(profile='p',user_id='u',agent_id='a',status='completed',session_id='private-probe-1',turn_id='t')
    assert runner(event)['reason']=='excluded_session'
    event['user_id']='foreign'
    assert runner(event)['reason']=='event_principal_mismatch'


def test_worker_observation_correlates_outcome_without_exposing_raw_input():
    value=worker_observation(dict(processed=True,state='done',idempotency_key='private-input-key',
        result=dict(reason='excluded_session',written=0,facts=['private extracted sentence'])))
    assert value['reason']=='excluded_session' and value['written']==0 and len(value['event_sha256'])==64
    assert 'private' not in json.dumps(value)

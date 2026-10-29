import pytest

from agentcore.document_intent import advise_delivery, delivery_context


@pytest.mark.parametrize('message,mode,kind', [
    ('介绍一下服务的架构', 'artifact', 'architecture'),
    ('Explain our service architecture', 'artifact', 'architecture'),
    ('讲清楚组件关系和模块边界', 'artifact', 'architecture'),
    ('Show the data flow between components', 'artifact', 'architecture'),
    ('讲解任务恢复机制', 'artifact', 'explanation'),
    ('Explain how retry recovery works', 'artifact', 'explanation'),
    ('Walk me through the request lifecycle', 'artifact', 'explanation'),
    ('制定一个实施计划，说明依赖和验收', 'artifact', 'plan'),
    ('Make a plan for the next three weeks', 'artifact', 'plan'),
    ('帮我安排未来几周的工作', 'artifact', 'plan'),
    ('列一个 rollout plan with milestones', 'artifact', 'plan'),
    ('比较两个设计方案的优缺点', 'artifact', 'comparison'),
    ('Compare retry approaches and their tradeoffs', 'artifact', 'comparison'),
    ('架构用文字就行', 'inline', 'document'),
    ('只用文字解释一下系统架构，不要生成附件', 'inline', 'document'),
    ('Explain architecture in one sentence', 'inline', 'document'),
    ('这个计划完成了吗', 'inline', 'document'),
    ('What is the current port?', 'inline', 'document'),
    ('服务端口是多少', 'inline', 'document'),
    ('汇报下<business-domain>新需求用例进度，昨天跟进的八个', 'artifact', 'plan'),
    ('汇报进度', 'artifact', 'plan'),
    ('汇报下这八条需求的情况', 'artifact', 'plan'),
    ('汇报一下服务端口是多少', 'inline', 'document'),
    ('现在进度如何', 'inline', 'document'),
    ('这个词是什么意思', 'inline', 'explanation'),
    ('什么是幂等', 'inline', 'explanation'),
    ('What is idempotency?', 'inline', 'explanation'),
    ('这一行为什么这样写', 'inline', 'explanation'),
    ('Explain this arrow', 'inline', 'explanation'),
    ('帮我把方案做成图文', 'artifact', 'document'),
    ('不要只给文字，要解释清楚', 'artifact', 'document'),
    ('Generate a PDF of the proposal', 'defer', 'document'),
    ('Create a text-only PDF', 'defer', 'document'),
    ('讲解一下这个模块', 'defer', 'document'),
    ('给我建议', 'defer', 'document'),
    ('这里有一个小表格', 'defer', 'document'),
    ('```text\n画一个架构图\n```\n只检查拼写', 'defer', 'document'),
])
def test_delivery_examples(message, mode, kind):
    decision = advise_delivery(message)
    assert (decision['mode'], decision['kind']) == (mode, kind)
    assert decision['reason_code']


@pytest.mark.parametrize('structure,kind', [
    ({'roles': ['caller', 'worker'], 'relationships': [('caller', 'worker')]}, 'architecture'),
    ({'phases': 3, 'roles': 4}, 'plan'),
    ({'alternatives': 3, 'criteria': 4}, 'comparison'),
    ({'steps': 4, 'reusable': True}, 'explanation'),
])
def test_planned_structure_handles_unlisted_intent(structure, kind):
    result = advise_delivery('Help me understand this', planned_structure=structure)
    assert result['mode'] == 'artifact'
    assert result['kind'] == kind


def test_user_preference_beats_complex_structure():
    decision = advise_delivery('文字即可', planned_structure={'roles': 8, 'relationships': 10})
    assert decision['mode'] == 'inline'


def test_local_followup_does_not_regenerate_whole_document():
    decision = advise_delivery('这一步为什么要等待？')
    assert decision['avoid_full_regeneration']
    assert decision['mode'] == 'inline'


def test_regeneration_request_can_update_existing_artifact():
    decision = advise_delivery('把这一步补进完整文档，重新生成')
    assert decision['mode'] == 'artifact'


def test_advice_does_not_echo_private_request_or_history():
    decision = advise_delivery('Explain our service architecture PRIVATE_MARKER')
    context = delivery_context(decision)
    assert 'PRIVATE_MARKER' not in context
    assert 'illustrated-doc-rendering' in context
    assert 'first use' in context
    assert len(context) < 1600


def test_unknown_intent_is_model_judgment_not_veto():
    result = advise_delivery('讲解一下这个')
    assert result['requires_model_judgment']
    assert 'decide whether' in delivery_context(result)


def test_progress_report_advice_requires_plain_language_document():
    result = advise_delivery('汇报下<business-domain>新需求用例进度，昨天跟进的八个')
    assert result['mode'] == 'artifact'
    context = delivery_context(result)
    assert 'progress report' in context
    assert 'md_deliver' in context
    assert 'appendix' in context
    assert 'first use' in context

"""Local integration only: no platform transport or persistent model chat."""
from agentcore.colleague_conversation import ConversationRouter
from agentcore.colleague_readonly_dialogue import ReadOnlyDialogue
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_registry import Actor, Registry, Supervisor


def test_hr_hires_developer_and_followup_selects_verified_actual_worker(tmp_path):
    registry = Registry(tmp_path / 'registry.sqlite', profile_id='team', primary_id='leader')
    leader, supervisor = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=leader, role_id='people', label='人事', duty='招聘',
                         boundaries=['遵守宿主规则'], request_id='people-role')
    registry.hire(actor=leader, member_id='hr', role_id='people', display_name='小人', request_id='hr-hire')
    registry.grant_management(actor=leader, member_id='hr', enabled=True, request_id='hr-grant')
    registry.define_role(actor=Actor('team', 'hr'), role_id='developer', label='开发', duty='只读分析',
                         boundaries=['只读'], request_id='dev-role')
    registry.hire(actor=Actor('team', 'hr'), member_id='dev', role_id='developer',
                  display_name='小程', request_id='dev-hire')
    # Test-only host observation, not proof that a production worker exists.
    registry.observe_runtime(supervisor=supervisor, member_id='dev', generation=1, state='running')
    source_root = tmp_path / 'source'
    source_root.mkdir()
    source = source_root / 'example.py'
    source.write_text('def example():\n    return 42\n')
    runtime_dir = tmp_path / 'runtime'
    runtime_dir.mkdir(mode=0o700)
    owner = {'profile': 'leader', 'session': 'conversation-1'}
    calls = []

    def model(messages, **kwargs):
        calls.append(messages)
        return {'conclusion': '返回 42', 'claims': [{'statement': '返回 42', 'evidence': [
            {'source_id': 'source_1', 'line': 2, 'quote': 'return 42'}]}], 'limitations': []}

    runtime = ReadOnlyColleagueRuntime(registry, supervisor, state_dir=runtime_dir,
        policy={'allowed_roots': [str(source_root)]}, model=model, authorize_owner=lambda candidate: candidate == owner)
    # Real host must persist this mapping atomically with its inbound request;
    # this test uses a local dictionary, so it is not a production adapter.
    requests = {}

    def verify(candidate, sequence, member, generation, work):
        request_id = requests.get((candidate['session'], sequence))
        if not request_id:
            return False
        record = runtime.inspect_assignment(owner=candidate, request_id=request_id)
        return bool(record and record['checked_result'] and record['task_id'] == work
                    and record['member_id'] == member and record['generation'] == generation)

    router = ConversationRouter(tmp_path / 'conversation.sqlite', registry, supervisor,
        owner_profile='leader', authorize=lambda candidate, member: candidate == owner and member['member_id'] == 'dev',
        verify_assignment=verify)
    chosen = router.select(owner, 1, target={'name': '小程'})
    assert chosen['state'] == 'selected' and chosen['member_id'] == 'dev'
    result = runtime.run(owner=owner, request_id='request-1', member_id=chosen['member_id'],
                         generation=chosen['generation'], goal='解释这个函数', sources=[{'path': str(source)}])
    requests[('conversation-1', 1)] = 'request-1'
    assert result['state'] == 'completed'
    presented = ReadOnlyDialogue(runtime, owner=owner).present('request-1')
    assert presented['member_id'] == 'dev' and presented['sender'] == 'primary_relay'
    assert '小程' in presented['text'] and '主 Bot 转交' in presented['text']
    router.remember(owner, 1, member_id='dev', generation=1, work_id=result['task_id'])
    followup = router.select(owner, 2)
    assert followup['member_id'] == 'dev'
    assert followup['conversation_key'] == chosen['conversation_key']
    second = runtime.run(owner=owner, request_id='request-2', member_id=followup['member_id'],
                         generation=followup['generation'], goal='再解释返回值', sources=[{'path': str(source)}])
    assert second['state'] == 'completed' and len(calls) == 2
    # The current runner receives no conversation history; routing alone does
    # not claim independent persistent chat or automatic source inheritance.

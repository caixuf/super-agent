import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentcore.colleague_dialogue_history import ColleagueDialogueHistory
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_registry import Actor, Registry, Supervisor

OWNER_A = {'profile': 'leader', 'session': 'user-a'}
OWNER_B = {'profile': 'leader', 'session': 'user-b'}
# Test-only host assertions; production must consult authenticated durable
# ingress and current source/range authorization, never just return True.
INGRESS = lambda *args: True
SOURCES = lambda task: True


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / 'registry.db', profile_id='team', primary_id='leader')
    lead, supervisor = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=lead, role_id='developer', label='Developer', duty='Read',
                         boundaries=['Read only'], request_id='role')
    for member in ('alice', 'bob'):
        registry.hire(actor=lead, member_id=member, role_id='developer', display_name=member,
                      request_id='hire-' + member)
    sources = tmp_path / 'sources'
    sources.mkdir(mode=0o700)
    (sources / 'sample.py').write_text('def hello():\n    return 42\n')
    root, histories = tmp_path / 'runtime', tmp_path / 'histories'
    root.mkdir(mode=0o700)
    histories.mkdir(mode=0o700)
    def model(messages, **kwargs):
        return {'conclusion': 'The function returns 42.',
                'claims': [{'statement': 'Returns 42.', 'evidence': [
                    {'source_id': 'source_1', 'line': 2, 'quote': 'return 42'}]}],
                'limitations': []}
    runtime = ReadOnlyColleagueRuntime(registry, supervisor, state_dir=root,
        policy={'allowed_roots': [str(sources)]}, model=model,
        authorize_owner=lambda owner: owner in (OWNER_A, OWNER_B))
    def history(owner=OWNER_A, member='alice', generation=1, ingress=INGRESS):
        return ColleagueDialogueHistory(runtime, root=histories, owner=owner,
              member_id=member, generation=generation, authorize_ingress=ingress)
    def execute(request, goal, sequence, *, owner=OWNER_A, member='alice', admit=True):
        if admit:
            history(owner, member).admit(request, sequence=sequence)
        return runtime.run(owner=owner, request_id=request, member_id=member, generation=1,
                           goal=goal, conversation_sequence=sequence,
                           sources=[{'path': str(sources / 'sample.py')}])
    return runtime, execute, history


def test_persists_once_and_cannot_invent_question_or_answer(env):
    _, execute, history = env
    run = execute('one', '解释这个函数', 1)
    db = history()
    assert db.record_completed('one') == {'task_id': run['task_id'], 'created': True}
    assert db.record_completed('one') == {'task_id': run['task_id'], 'created': False}
    db.admit('next', sequence=2)
    assert db.context('next')['turns'] == []
    context = history().context('next', authorize_prior=SOURCES)
    assert context['turns'] == [{'user_goal': '解释这个函数', 'colleague_summary': 'The function returns 42.'}]
    assert context['trust'] == 'untrusted_prior_conversation'
    with pytest.raises(TypeError):
        db.record_completed('one', goal='伪造', answer='伪造')


def test_separate_owner_and_member_cannot_see_or_record_other_tasks(env):
    _, execute, history = env
    execute('a', 'alice 的对话', 1)
    execute('b', 'bob 的对话', 1, member='bob')
    execute('c', '另一个会话', 1, owner=OWNER_B)
    alice, bob, other_user = history(), history(member='bob'), history(owner=OWNER_B)
    alice.record_completed('a'); bob.record_completed('b'); other_user.record_completed('c')
    for db in (alice, bob, other_user):
        db.admit('next', sequence=2)
    assert [x['user_goal'] for x in alice.context('next', authorize_prior=SOURCES)['turns']] == ['alice 的对话']
    assert [x['user_goal'] for x in bob.context('next', authorize_prior=SOURCES)['turns']] == ['bob 的对话']
    assert [x['user_goal'] for x in other_user.context('next', authorize_prior=SOURCES)['turns']] == ['另一个会话']
    with pytest.raises(PermissionError, match='task_member_mismatch'):
        bob.record_completed('a')
    with pytest.raises(ValueError, match='completed_result_required'):
        other_user.record_completed('a')
    with pytest.raises(ValueError, match='stale_member_generation'):
        history(generation=2)
    with pytest.raises(PermissionError):
        history(owner={'profile': 'leader', 'session': 'unauthorized'})


def test_atomic_concurrent_recording_is_idempotent(env):
    _, execute, history = env
    execute('one', '一次回答', 1)
    db = history()
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: db.record_completed('one'), range(24)))
    assert sum(result['created'] for result in results) == 1
    db.admit('next', sequence=2)
    assert len(db.context('next', authorize_prior=SOURCES)['turns']) == 1


def test_budget_is_utf8_bytes_and_keeps_a_consecutive_suffix(env):
    _, execute, history = env
    db = history()
    for number, goal in enumerate(('小问题', '非常非常非常长的问题' * 8, '短'), 1):
        request = 'request-' + str(number)
        execute(request, goal, number)
        db.record_completed(request)
    db.admit('next', sequence=4)
    last = db.context('next', max_turns=1, authorize_prior=SOURCES)['turns']
    budget = len(json.dumps(last, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8'))
    bounded = db.context('next', max_turns=3, byte_budget=budget, authorize_prior=SOURCES)
    assert bounded['turns'] == last and bounded['encoded_turns_bytes'] == budget
    assert db.context('next', max_turns=3, byte_budget=2, authorize_prior=SOURCES)['turns'] == []
    for kwargs in ({'byte_budget': True}, {'byte_budget': 8001}, {'max_turns': 11}):
        with pytest.raises(ValueError, match='invalid_history_budget'):
            db.context('next', **kwargs)


def test_incomplete_legacy_or_conflicting_result_is_not_admitted(env):
    runtime, execute, history = env
    db = history()
    with pytest.raises(ValueError, match='completed_result_required'):
        db.record_completed('missing')
    run = execute('one', 'the real question', 1)
    path = runtime.root / run['task_id'] / 'state.json'
    state = json.loads(path.read_text())
    state.pop('request_goal')
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='original_goal_unavailable'):
        db.record_completed('one')
    state['request_goal'] = 'the real question'
    path.write_text(json.dumps(state))
    db.record_completed('one')
    state['result']['summary'] = 'changed'
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='history_replay_conflict'):
        db.record_completed('one')
    db.admit('next', sequence=2)
    assert db.context('next', authorize_prior=SOURCES)['turns'][0]['colleague_summary'] == 'The function returns 42.'


def test_crashed_writer_cannot_leave_partial_or_forged_turn(env):
    _, execute, history = env
    run = execute('one', '真实问题', 1)
    db = history()
    code = '''
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE turns SET content=?,state='completed' WHERE scope=? AND task=?", (sys.argv[4], sys.argv[2], sys.argv[3]))
os._exit(71)
'''
    forged = json.dumps({'user_goal': '伪造', 'colleague_summary': '伪造'})
    process = subprocess.run([sys.executable, '-c', code, str(db.db_path), db._scope(), run['task_id'], forged], timeout=10)
    assert process.returncode == 71
    db.admit('next', sequence=2)
    assert db.context('next', authorize_prior=SOURCES)['state'] == 'waiting'
    assert db.record_completed('one')['created'] is True
    assert db.context('next', authorize_prior=SOURCES)['turns'][0]['user_goal'] == '真实问题'


def test_revoked_or_unavailable_source_authorization_stops_history(env):
    _, execute, history = env
    db = history()
    a, b = execute('a', '旧资料', 1), execute('b', '新资料', 2)
    db.record_completed('a'); db.record_completed('b'); db.admit('next', sequence=3)
    assert db.context('next')['turns'] == []
    assert db.context('next', authorize_prior=lambda task: task != b['task_id'])['turns'] == []
    assert [t['user_goal'] for t in db.context('next', authorize_prior=lambda task: task != a['task_id'])['turns']] == ['新资料']
    def broken(_):
        raise RuntimeError('policy unavailable')
    assert db.context('next', authorize_prior=broken)['turns'] == []
    with pytest.raises(ValueError, match='invalid_history_authorizer'):
        db.context('next', authorize_prior=True)


def test_out_of_order_completion_waits_and_never_reads_future_turn(env):
    _, execute, history = env
    db = history()
    db.admit('one', sequence=1); db.admit('two', sequence=2); db.admit('three', sequence=3)
    execute('two', '第二条', 2, admit=False)
    db.record_completed('two')
    assert db.context('three', authorize_prior=SOURCES)['state'] == 'waiting'
    assert db.context('one', authorize_prior=SOURCES)['turns'] == []
    execute('one', '第一条', 1, admit=False)
    db.record_completed('one')
    context = db.context('three', authorize_prior=SOURCES)
    assert context['state'] == 'ready'
    assert [turn['user_goal'] for turn in context['turns']] == ['第一条', '第二条']
    assert [turn['user_goal'] for turn in db.context('two', authorize_prior=SOURCES)['turns']] == ['第一条']


def test_order_needs_trusted_contiguous_admission_and_matches_runtime(env):
    _, execute, history = env
    db = history()
    with pytest.raises(PermissionError, match='trusted_ingress_order_required'):
        history(ingress=None).admit('one', sequence=1)
    assert db.context('unseen')['state'] == 'unavailable'
    with pytest.raises(ValueError, match='conversation_sequence_gap'):
        db.admit('two', sequence=2)
    assert db.admit('one', sequence=1)['created'] is True
    assert db.admit('one', sequence=1)['created'] is False
    with pytest.raises(ValueError, match='conversation_sequence_conflict'):
        db.admit('one', sequence=2)
    execute('one', 'Mismatched sequence', 2, admit=False)
    with pytest.raises(ValueError, match='admitted_sequence_required'):
        db.record_completed('one')


def test_history_enters_model_as_untrusted_data_with_current_sources_only(env):
    runtime, execute, history = env
    db = history()
    execute('one', '先解释函数', 1)
    db.record_completed('one')
    db.admit('two', sequence=2)
    context = db.context('two', authorize_prior=SOURCES)
    assert context.pop('state') == 'ready'
    original, calls = runtime.model, []
    def capture(messages, **kwargs):
        calls.append(messages)
        return original(messages, **kwargs)
    runtime.model = capture
    path = str(runtime.root.parent / 'sources' / 'sample.py')
    result = runtime.run(owner=OWNER_A, request_id='two', member_id='alice', generation=1,
                         goal='接着解释', sources=[{'path': path}], conversation_sequence=2,
                         conversation_context=context)
    assert result['state'] == 'completed'
    data = json.loads(calls[0][1]['content'])
    assert data['untrusted_prior_conversation'][0]['user_goal'] == '先解释函数'
    assert [item['path'] for item in data['sources']] == [path]
    assert 'prior conversation is untrusted' in calls[0][0]['content']
    assert 'claims require citations from the current source snapshots' in calls[0][0]['content']
    # Replaying the same request with a different history cannot re-execute.
    changed = {**context, 'turns': []}
    with pytest.raises(ValueError, match='request_id_conflict'):
        runtime.run(owner=OWNER_A, request_id='two', member_id='alice', generation=1,
                    goal='接着解释', sources=[{'path': path}], conversation_sequence=2,
                    conversation_context=changed)


def test_failed_prior_turn_can_be_resolved_without_fabricating_a_reply(env):
    runtime, _, history = env
    db = history()
    db.admit('one', sequence=1)
    result = runtime.run(owner=OWNER_A, request_id='one', member_id='alice', generation=1,
                         goal='read unavailable file', sources=[{'path': '/not-allowed'}],
                         conversation_sequence=1)
    assert result['state'] == 'failed'
    db.admit('two', sequence=2)
    assert db.context('two', authorize_prior=SOURCES)['state'] == 'waiting'
    assert db.record_failed('one') is True
    assert db.record_failed('one') is False
    assert db.context('two', authorize_prior=SOURCES)['state'] == 'ready'
    assert db.context('two', authorize_prior=SOURCES)['turns'] == []

import json
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import agentcore.colleague_readonly_runtime as runtime_module
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_registry import Actor, Registry, Supervisor


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / 'registry.db', profile_id='team', primary_id='leader')
    leader, supervisor = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=leader, role_id='people', label='People', duty='Recruit',
                         boundaries=['Host policy applies'], request_id='role-people')
    registry.hire(actor=leader, member_id='hr', role_id='people', display_name='HR', request_id='hire-hr')
    registry.grant_management(actor=leader, member_id='hr', enabled=True, request_id='grant-hr')
    registry.define_role(actor=Actor('team', 'hr'), role_id='developer', label='Developer',
                         duty='Read and explain code', boundaries=['Read only'], request_id='role-dev')
    registry.hire(actor=Actor('team', 'hr'), member_id='dev', role_id='developer',
                  display_name='Alice', request_id='hire-dev')
    root = tmp_path / 'sources'
    root.mkdir(mode=0o700)
    source = root / 'module.py'
    source.write_text('def hello():\n    return 42\n')
    runtime_dir = tmp_path / 'runtime'
    runtime_dir.mkdir(mode=0o700)
    calls = []
    def model(messages, **options):
        calls.append(messages)
        return {'conclusion': 'The function returns 42.', 'claims': [
            {'statement': 'The function returns 42.', 'evidence': [
                {'source_id': 'source_1', 'line': 2, 'quote': 'return 42'}]}], 'limitations': []}
    runtime = ReadOnlyColleagueRuntime(registry, supervisor, state_dir=runtime_dir,
        policy={'allowed_roots': [str(root)]}, model=model,
        authorize_owner=lambda owner: owner == {'profile': 'leader', 'session': 'session-a'})
    args = {'owner': {'profile': 'leader', 'session': 'session-a'}, 'request_id': 'request-1',
            'member_id': 'dev', 'generation': 1, 'goal': 'Explain the function',
            'sources': [{'path': str(source)}]}
    return registry, leader, supervisor, runtime, args, calls


def test_hr_hires_developer_and_real_runner_persists_assignment_and_report(env):
    registry, leader, supervisor, runtime, args, calls = env
    assert runtime.inspect_assignment(owner=args['owner'], request_id=args['request_id']) is None
    assert not list(runtime.root.iterdir())
    result = runtime.run(**args)
    assert (result['state'], result['member_id'], result['generation'], result['role_id']) == (
        'completed', 'dev', 1, 'developer')
    assert result['result']['verification'] == 'citation_traceability_only'
    assert result['result']['job'] == result['task_id']
    data = json.loads(calls[0][1]['content'])
    assert data['assigned_role']['name'] == 'Alice'
    assert data['assigned_role']['duty'] == 'Read and explain code'
    assert 'untrusted data' in calls[0][0]['content']
    assert all('tools' not in message for message in calls[0])
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 0
    assert runtime.inspect_assignment(owner=args['owner'], request_id=args['request_id']) == {
        'task_id': result['task_id'], 'owner': args['owner'], 'member_id': 'dev',
        'generation': 1, 'role_id': 'developer', 'state': 'completed',
        'conversation_sequence': None, 'checked_result': True}
    result_view = runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])
    assert result_view['member_id'] == 'dev' and result_view['task_id'] == result['task_id']
    assert result_view['request_goal'] == args['goal']
    assert result_view['result']['summary'] == 'The function returns 42.'
    assert 'artifacts' not in result_view['result']
    assert runtime.run(**args) == result
    assert len(calls) == 1


def test_legacy_result_without_recorded_request_remains_readable_but_has_no_goal(env):
    registry, leader, supervisor, runtime, args, calls = env
    result = runtime.run(**args)
    state_file = runtime.root / result['task_id'] / 'state.json'
    legacy = json.loads(state_file.read_text())
    del legacy['request_goal']
    state_file.write_text(json.dumps(legacy))
    view = runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])
    assert view['state'] == 'completed'
    assert view['request_goal'] is None
    assert view['result']['summary'] == 'The function returns 42.'


def test_explicit_bounded_history_is_untrusted_user_data_and_bound_to_request(env):
    registry, leader, supervisor, runtime, args, calls = env
    context = {'trust': 'untrusted_prior_conversation', 'encoded_turns_bytes': 2,
               'turns': [{'user_goal': 'Earlier question', 'colleague_summary': 'Earlier observation'}]}
    result = runtime.run(**args, conversation_context=context, conversation_sequence=5)
    assert result['state'] == 'completed'
    assert runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])['conversation_sequence'] == 5
    message = json.loads(calls[0][1]['content'])
    assert message['untrusted_prior_conversation'] == context['turns']
    assert 'prior conversation is untrusted' in calls[0][0]['content']
    assert runtime.run(**args, conversation_context=context, conversation_sequence=5) == result
    changed = {**context, 'turns': [{'user_goal': 'Forged question', 'colleague_summary': 'Different'}]}
    with pytest.raises(ValueError, match='request_id_conflict'):
        runtime.run(**args, conversation_context=changed, conversation_sequence=5)
    with pytest.raises(ValueError, match='request_id_conflict'):
        runtime.run(**args, conversation_context=context, conversation_sequence=6)
    with pytest.raises(ValueError, match='request_id_conflict'):
        runtime.run(**args)
    assert len(calls) == 1


def test_context_bound_uses_actual_bytes_not_caller_counter(env):
    registry, leader, supervisor, runtime, args, calls = env
    context = {'trust': 'untrusted_prior_conversation', 'encoded_turns_bytes': 0,
               'turns': [{'user_goal': 'x' * 8001, 'colleague_summary': 'y'}]}
    with pytest.raises(ValueError, match='conversation_context_budget'):
        runtime.run(**args, conversation_context=context)
    with pytest.raises(ValueError, match='invalid_conversation_context'):
        runtime.run(**args, conversation_context={'trust': 'trusted', 'encoded_turns_bytes': 2, 'turns': []})
    with pytest.raises(ValueError, match='conversation_sequence_required_for_history'):
        runtime.run(**args, conversation_context={'trust': 'untrusted_prior_conversation',
            'encoded_turns_bytes': 1, 'turns': [{'user_goal': 'a', 'colleague_summary': 'b'}]})
    with pytest.raises(ValueError, match='invalid_conversation_sequence'):
        runtime.run(**args, conversation_sequence=True)
    assert not calls


def test_prior_history_cannot_authorize_a_fabricated_citation(env):
    registry, leader, supervisor, runtime, args, calls = env
    runtime.model = lambda messages: {'conclusion': 'Fake.', 'claims': [{'statement': 'Fake', 'evidence': [
        {'source_id': 'source_2', 'line': 1, 'quote': 'Old context'}]}]}
    context = {'trust': 'untrusted_prior_conversation', 'encoded_turns_bytes': 1,
               'turns': [{'user_goal': 'Old question', 'colleague_summary': 'Old context'}]}
    result = runtime.run(**args, conversation_context=context, conversation_sequence=2)
    assert (result['state'], result['error']) == ('failed', 'unverified_citation')


def test_replay_cannot_change_request_or_member(env):
    registry, leader, supervisor, runtime, args, calls = env
    runtime.run(**args)
    for change in ({'goal': 'Different task'}, {'member_id': 'hr'}):
        with pytest.raises(ValueError, match='request_id_conflict'):
            runtime.run(**{**args, **change})
    assert len(calls) == 1


def test_owner_authority_and_primary_protection_are_checked_before_execution(env):
    registry, leader, supervisor, runtime, args, calls = env
    with pytest.raises(PermissionError, match='owner_not_authorized'):
        runtime.run(**{**args, 'owner': {'profile': 'leader', 'session': 'session-b'}})
    with pytest.raises(PermissionError, match='primary_not_a_colleague_worker'):
        runtime.run(**{**args, 'member_id': 'leader'})
    with pytest.raises(PermissionError, match='owner_not_authorized'):
        runtime.inspect_assignment(owner={'profile': 'leader', 'session': 'session-b'}, request_id='request-1')
    assert not calls


def test_retirement_fences_new_work_while_existing_job_finishes(env):
    registry, leader, supervisor, runtime, args, calls = env
    entered, release = threading.Event(), threading.Event()
    base_model = runtime.model
    def model(messages, **options):
        entered.set()
        assert release.wait(10)
        return base_model(messages, **options)
    runtime.model = model
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run, **args)
        assert entered.wait(5)
        assert runtime.inspect_assignment(owner=args['owner'], request_id=args['request_id'])['state'] == 'running'
        assert not runtime.inspect_assignment(owner=args['owner'], request_id=args['request_id'])['checked_result']
        assert runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])['result'] is None
        registry.request_deactivation(actor=leader, member_id='dev', request_id='remove-dev')
        with pytest.raises(ValueError, match='member_not_accepting_work'):
            runtime.run(**{**args, 'request_id': 'request-2'})
        with pytest.raises(ValueError, match='active_work_remaining'):
            registry.finalize_deactivation(supervisor=supervisor, member_id='dev', generation=1, external_idle=True)
        release.set()
        assert future.result(timeout=10)['state'] == 'completed'
    assert registry.finalize_deactivation(supervisor=supervisor, member_id='dev', generation=1,
                                          external_idle=True)['status'] == 'inactive'
    assert runtime.run(**args)['state'] == 'completed'


def test_crash_stays_unknown_and_blocks_retirement_until_host_recovers(env):
    registry, leader, supervisor, runtime, args, calls = env
    def crash(*_args, **_kwargs):
        raise SystemExit('simulated process interruption')
    runtime.model = crash
    with pytest.raises(SystemExit):
        runtime.run(**args)
    result = runtime.run(**args)
    assert result['state'] == 'unknown'
    assert not runtime.inspect_assignment(owner=args['owner'], request_id=args['request_id'])['checked_result']
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 1
    registry.request_deactivation(actor=leader, member_id='dev', request_id='remove-dev')
    with pytest.raises(ValueError, match='active_work_remaining'):
        registry.finalize_deactivation(supervisor=supervisor, member_id='dev', generation=1, external_idle=True)
    with pytest.raises(PermissionError, match='authoritative_stop_confirmation_required'):
        runtime.recover_uncertain(owner=args['owner'], request_id=args['request_id'], confirmed_stopped=False)
    recovered = runtime.recover_uncertain(owner=args['owner'], request_id=args['request_id'], confirmed_stopped=True)
    assert recovered['state'] == 'failed'
    assert recovered['error'] == 'interrupted_execution_outcome_unknown'
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 0


def test_disallowed_source_fails_without_calling_model_and_releases_reservation(env, tmp_path):
    registry, leader, supervisor, runtime, args, calls = env
    outside = tmp_path / 'outside.py'
    outside.write_text('return 42\n')
    result = runtime.run(**{**args, 'sources': [{'path': str(outside)}]})
    assert (result['state'], result['error']) == ('failed', 'source_outside_roots')
    assert not calls
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 0


def test_simultaneous_duplicate_request_is_not_executed_twice(env):
    registry, leader, supervisor, runtime, args, calls = env
    entered, release = threading.Event(), threading.Event()
    base_model = runtime.model
    def model(messages, **options):
        entered.set()
        assert release.wait(10)
        return base_model(messages, **options)
    runtime.model = model
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run, **args)
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match='task_in_progress'):
            runtime.run(**args)
        release.set()
        assert future.result(timeout=10)['state'] == 'completed'
    assert len(calls) == 1


def test_abrupt_process_exit_releases_lock_but_preserves_uncertain_reservation(env):
    registry, leader, supervisor, runtime, args, calls = env
    code = '''
import json, os, sys
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
registry = Registry(sys.argv[1], profile_id='team', primary_id='leader')
def killed_model(*args, **kwargs):
    os._exit(77)
runtime = ReadOnlyColleagueRuntime(registry, Supervisor('team'), state_dir=sys.argv[2],
    policy=json.loads(sys.argv[3]), model=killed_model, authorize_owner=lambda owner: True)
runtime.run(**json.loads(sys.argv[4]))
'''
    proc = subprocess.run([sys.executable, '-c', code, str(registry.path), str(runtime.root),
                           json.dumps(runtime.policy), json.dumps(args)],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=15)
    assert proc.returncode == 77, proc.stderr
    assert runtime.run(**args)['state'] == 'unknown'
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 1
    result = runtime.recover_uncertain(owner=args['owner'], request_id=args['request_id'], confirmed_stopped=True)
    assert result['state'] == 'failed'
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 0


def test_prepared_assignment_does_not_claim_execution(env):
    registry, leader, supervisor, runtime, args, calls = env
    registry.request_deactivation(actor=leader, member_id='dev', request_id='retire-before-run')
    with pytest.raises(ValueError, match='member_not_accepting_work'):
        runtime.run(**args)
    assignment = runtime.inspect_assignment(owner=args['owner'], request_id=args['request_id'])
    assert assignment['state'] == 'prepared'
    assert not assignment['checked_result']
    assert runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])['result'] is None
    assert not calls


def test_terminal_state_is_not_reexecuted_if_reservation_settlement_fails(env, monkeypatch):
    registry, leader, supervisor, runtime, args, calls = env
    settle = registry.settle_work
    times = []
    def fail_once(**kwargs):
        times.append(True)
        if len(times) == 1:
            raise OSError('simulated storage interruption')
        return settle(**kwargs)
    monkeypatch.setattr(registry, 'settle_work', fail_once)
    with pytest.raises(OSError):
        runtime.run(**args)
    assert runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])['state'] == 'completed'
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 1
    assert runtime.run(**args)['state'] == 'completed'
    assert len(calls) == 1
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 0


def test_crash_before_running_state_reuses_existing_reservation_without_losing_work(env, monkeypatch):
    registry, leader, supervisor, runtime, args, calls = env
    write = runtime_module._atomic
    def fail_before_running(path, value):
        if value['state'] == 'running':
            raise OSError('simulated write interruption')
        return write(path, value)
    monkeypatch.setattr(runtime_module, '_atomic', fail_before_running)
    with pytest.raises(OSError):
        runtime.run(**args)
    assert runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])['state'] == 'prepared'
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 1
    assert not calls
    monkeypatch.setattr(runtime_module, '_atomic', write)
    assert runtime.run(**args)['state'] == 'completed'
    assert len(calls) == 1
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 0


def test_forked_child_does_not_keep_crashed_parent_task_lock(env):
    registry, leader, supervisor, runtime, args, calls = env
    code = '''
import json, os, sys, time
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
registry = Registry(sys.argv[1], profile_id='team', primary_id='leader')
def fork_and_crash(*args, **kwargs):
    child = os.fork()
    if child == 0:
        os.close(1)
        os.close(2)
        time.sleep(30)
        os._exit(0)
    print(child, flush=True)
    os._exit(77)
runtime = ReadOnlyColleagueRuntime(registry, Supervisor('team'), state_dir=sys.argv[2],
    policy=json.loads(sys.argv[3]), model=fork_and_crash, authorize_owner=lambda owner: True)
runtime.run(**json.loads(sys.argv[4]))
'''
    proc = subprocess.run([sys.executable, '-c', code, str(registry.path), str(runtime.root),
                           json.dumps(runtime.policy), json.dumps(args)],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=15)
    assert proc.returncode == 77, proc.stderr
    child = int(proc.stdout.strip())
    try:
        # The forked child is alive, but the parent's task lock is not stuck.
        os.kill(child, 0)
        assert runtime.run(**args)['state'] == 'unknown'
        assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 1
    finally:
        os.kill(child, signal.SIGTERM)


def test_forked_model_child_cannot_publish_without_the_parent_lock(env):
    registry, leader, supervisor, runtime, args, calls = env
    code = '''
import json, os, sys
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
registry = Registry(sys.argv[1], profile_id='team', primary_id='leader')
def fork_model(*args, **kwargs):
    child = os.fork()
    if child:
        _, status = os.waitpid(child, 0)
        print(os.waitstatus_to_exitcode(status), flush=True)
        os._exit(77)
    return {'conclusion':'42', 'claims':[{'statement':'42','evidence':[
        {'source_id':'source_1','line':2,'quote':'return 42'}]}]}
runtime = ReadOnlyColleagueRuntime(registry, Supervisor('team'), state_dir=sys.argv[2],
    policy=json.loads(sys.argv[3]), model=fork_model, authorize_owner=lambda owner: True)
try:
    runtime.run(**json.loads(sys.argv[4]))
except RuntimeError as error:
    os._exit(42 if str(error) == 'forked_execution_forbidden' else 43)
'''
    proc = subprocess.run([sys.executable, '-c', code, str(registry.path), str(runtime.root),
                           json.dumps(runtime.policy), json.dumps(args)],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=15)
    assert proc.returncode == 77, proc.stderr
    assert proc.stdout.strip() == '42'
    assert runtime.run(**args)['state'] == 'unknown'
    assert registry.work_status(supervisor=supervisor, member_id='dev', generation=1)['tracked_open'] == 1
    assert runtime.inspect_result(owner=args['owner'], request_id=args['request_id'])['result'] is None

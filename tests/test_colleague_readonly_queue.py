import threading
import time
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentcore.colleague_readonly_queue import ReadOnlyQueue
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_registry import Actor, Registry, Supervisor
from agentcore.completion_backfill import inspect as inspect_backfill
from agentcore.completion_delivery import deliver_once
from agentcore.pool import Pool


@pytest.fixture
def env(tmp_path):
    reg = Registry(tmp_path / 'team.db', profile_id='team', primary_id='leader')
    leader, host = Actor('team', 'leader'), Supervisor('team')
    reg.define_role(actor=leader, role_id='dev', label='Developer', duty='Review code',
                    boundaries=['Read only'], request_id='role')
    reg.hire(actor=leader, member_id='alice', role_id='dev', display_name='Alice', request_id='hire')
    source_root = tmp_path / 'sources'
    source_root.mkdir(mode=0o700)
    source = source_root / 'example.py'
    source.write_text('def answer():\n    return 42\n')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    calls = []
    def model(messages, **options):
        calls.append(messages)
        return {'conclusion': 'Returns 42.', 'claims': [{'statement': 'Returns 42.', 'evidence': [
            {'source_id': 'source_1', 'line': 2, 'quote': 'return 42'}]}]}
    runtime = ReadOnlyColleagueRuntime(reg, host, state_dir=state,
        policy={'allowed_roots': [str(source_root)]}, model=model,
        authorize_owner=lambda owner: owner['profile'] == 'leader')
    pool = Pool(tmp_path / 'queue')
    queue = ReadOnlyQueue(pool, runtime, authorize_assignment=lambda *args: True, dedicated=True)
    owner = {'profile': 'leader', 'session': 'a'}
    args = {'owner': owner, 'origin': {**owner, 'route': 'test-route'}, 'request_id': 'request-1',
            'member_id': 'alice', 'generation': 1, 'goal': 'Explain it', 'sources': [{'path': str(source)}]}
    return queue, runtime, reg, leader, host, args, calls


def test_submit_returns_before_model_and_outbox_never_sends_automatically(env):
    queue, runtime, reg, leader, host, args, calls = env
    result = queue.submit(**args)
    assert result['state'] == 'queued' and not calls
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 2
    assert queue.pool.origin(result['job_id']) is None
    assert queue.run_once()['job_state'] == 'completed'
    assert len(calls) == 1
    view = queue.inspect_completion(owner=args['owner'], job_id=result['job_id'])
    assert view['origin'] == args['origin']
    assert view['presentation']['member_id'] == 'alice'
    assert view['presentation']['task_id'] == result['task_id']
    assert not view['acknowledged'] and len(queue.pool.receipts()) == 1
    assert inspect_backfill(queue.pool)[0]['deliverable'] is False
    class NeverSend:
        def deliver(self, *args, **kwargs):
            pytest.fail('generic sender must never receive an internal receipt')
    assert deliver_once(queue.pool, NeverSend())['status'] == 'unowned'
    # Execution is settled, but delivery is still open until a real receipt.
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 1
    assert queue.submit(**args)['job_id'] == result['job_id']
    assert queue.submit(**args)['state'] == 'completed'
    assert len(calls) == 1


def test_same_request_conflicts_on_changed_member_goal_or_route(env):
    queue, runtime, reg, leader, host, args, calls = env
    queue.submit(**args)
    with pytest.raises(ValueError, match='request_id_conflict'):
        queue.submit(**{**args, 'goal': 'Different'})
    with pytest.raises(ValueError, match='request_id_conflict'):
        queue.submit(**{**args, 'origin': {**args['origin'], 'route': 'different'}})
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 2
    assert not calls


def test_retirement_waits_for_reserved_execution_and_delivery(env):
    queue, runtime, reg, leader, host, args, calls = env
    queue.submit(**args)
    reg.request_deactivation(actor=leader, member_id='alice', request_id='retire')
    with pytest.raises(ValueError, match='active_work_remaining'):
        reg.finalize_deactivation(supervisor=host, member_id='alice', generation=1, external_idle=True)
    with pytest.raises(ValueError, match='member_not_accepting_work'):
        queue.submit(**{**args, 'request_id': 'new'})
    assert queue.run_once()['job_state'] == 'completed'
    with pytest.raises(ValueError, match='active_work_remaining'):
        reg.finalize_deactivation(supervisor=host, member_id='alice', generation=1, external_idle=True)


def test_owner_and_route_are_bound_and_other_sessions_cannot_read(env):
    queue, runtime, reg, leader, host, args, calls = env
    with pytest.raises(PermissionError, match='authenticated_route_required'):
        queue.submit(**{**args, 'origin': {**args['origin'], 'session': 'other'}})
    job = queue.submit(**args)['job_id']
    queue.run_once()
    with pytest.raises(PermissionError, match='queue_owner_mismatch'):
        queue.inspect_completion(owner={'profile': 'leader', 'session': 'other'}, job_id=job)


def test_duplicate_concurrent_admission_runs_model_once(env):
    queue, runtime, reg, leader, host, args, calls = env
    def submit_or_busy(_):
        try:
            return queue.submit(**args)
        except RuntimeError as error:
            assert str(error) == 'submission_busy'
            return None
    with ThreadPoolExecutor(max_workers=4) as ex:
        jobs = list(ex.map(submit_or_busy, range(4)))
    job_ids = {j['job_id'] for j in jobs if j is not None}
    job_ids.add(queue.submit(**args)['job_id'])
    assert len(job_ids) == 1
    assert queue.run_once()['job_state'] == 'completed'
    assert queue.run_once()['state'] == 'empty'
    assert len(calls) == 1


def test_model_crash_is_not_automatically_executed_twice(env):
    queue, runtime, reg, leader, host, args, calls = env
    runtime.model = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit('simulated crash'))
    job = queue.submit(**args)['job_id']
    with pytest.raises(SystemExit):
        queue.run_once(lease=0.01)
    time.sleep(0.03)
    runtime.model = lambda *args, **kwargs: pytest.fail('model must not run twice')
    # Pool can retry the safe lookup, but runtime reports unknown without a call.
    assert queue.run_once(lease=0.01)['job_state'] == 'queued'
    assert queue.run_once(lease=0.01)['job_state'] == 'failed'
    assert queue.inspect_completion(owner=args['owner'], job_id=job)['text'] is None
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 2


def test_retirement_between_reservations_rolls_back_only_new_work(env, monkeypatch):
    queue, runtime, reg, leader, host, args, calls = env
    first_reserved, continue_submit = threading.Event(), threading.Event()
    reserve = reg.reserve_work
    def paused_reserve(**kwargs):
        result = reserve(**kwargs)
        if not kwargs['work_id'].startswith('delivery:'):
            first_reserved.set()
            assert continue_submit.wait(10)
        return result
    monkeypatch.setattr(reg, 'reserve_work', paused_reserve)
    with ThreadPoolExecutor(max_workers=2) as ex:
        first = ex.submit(queue.submit, **args)
        assert first_reserved.wait(5)
        second = ex.submit(queue.submit, **args)
        with pytest.raises(RuntimeError, match='submission_busy'):
            second.result(timeout=2)
        reg.request_deactivation(actor=leader, member_id='alice', request_id='retire-race')
        continue_submit.set()
        with pytest.raises(ValueError, match='member_not_accepting_work'):
            first.result(timeout=10)
        with pytest.raises(ValueError, match='admission_closed'):
            queue.submit(**args)
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0
    assert queue.run_once()['state'] == 'empty'
    assert reg.finalize_deactivation(supervisor=host, member_id='alice', generation=1,
                                     external_idle=True)['status'] == 'inactive'


def test_crash_between_reservation_and_enqueue_can_be_safely_reconciled(env, monkeypatch):
    queue, runtime, reg, leader, host, args, calls = env
    def crash(*args, **kwargs):
        raise SystemExit('crash before enqueue')
    monkeypatch.setattr(queue.pool, 'submit', crash)
    with pytest.raises(SystemExit):
        queue.submit(**args)
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 2
    with pytest.raises(PermissionError, match='authoritative_external_idle_required'):
        queue.reconcile_orphan(owner=args['owner'], request_id=args['request_id'], member_id='alice',
                               generation=1, confirmed_no_external_work=False)
    with pytest.raises(ValueError, match='orphan_not_proven'):
        queue.reconcile_orphan(owner=args['owner'], request_id=args['request_id'], member_id='leader',
                               generation=1, confirmed_no_external_work=True)
    result = queue.reconcile_orphan(owner=args['owner'], request_id=args['request_id'], member_id='alice',
                                    generation=1, confirmed_no_external_work=True)
    assert result['settled_reservations'] == 2
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0
    with pytest.raises(ValueError, match='admission_closed'):
        queue.submit(**args)


def test_orphan_reconciliation_cannot_release_an_enqueued_task(env):
    queue, runtime, reg, leader, host, args, calls = env
    queue.submit(**args)
    with pytest.raises(ValueError, match='orphan_not_proven'):
        queue.reconcile_orphan(owner=args['owner'], request_id=args['request_id'], member_id='alice',
                               generation=1, confirmed_no_external_work=True)
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 2


def test_real_process_crash_before_enqueue_is_reconciled_after_lock_release(env):
    queue, runtime, reg, leader, host, args, calls = env
    code = '''
import json, os, sys
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_readonly_queue import ReadOnlyQueue
from agentcore.pool import Pool
reg = Registry(sys.argv[1], profile_id='team', primary_id='leader')
runtime = ReadOnlyColleagueRuntime(reg, Supervisor('team'), state_dir=sys.argv[2],
    policy=json.loads(sys.argv[3]), model=lambda *args, **kwargs: None, authorize_owner=lambda owner: True)
pool = Pool(sys.argv[4])
queue = ReadOnlyQueue(pool, runtime, authorize_assignment=lambda *args: True, dedicated=True)
pool.submit = lambda *args, **kwargs: os._exit(77)
queue.submit(**json.loads(sys.argv[5]))
'''
    proc = subprocess.run([sys.executable, '-c', code, str(reg.path), str(runtime.root),
                           json.dumps(runtime.policy), str(queue.pool.root), json.dumps(args)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 77, proc.stderr
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 2
    result = queue.reconcile_orphan(owner=args['owner'], request_id=args['request_id'], member_id='alice',
                                    generation=1, confirmed_no_external_work=True)
    assert result['settled_reservations'] == 2
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0

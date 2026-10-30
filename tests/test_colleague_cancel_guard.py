"""Root cancellation must not escape the proven workflow/role boundary."""
import os
import json
import multiprocessing
import threading

import pytest

from agentcore.colleague_cancel_guard import BindingError, CancelGuard
from agentcore.colleague_workflow_store import WorkflowStore
from agentcore.pool import Pool


@pytest.fixture
def setup_guard(tmp_path):
    store = WorkflowStore(tmp_path / 'ledger')
    root = Pool(tmp_path / 'root')
    pools = {'reader': Pool(tmp_path / 'reader'), 'reviewer': Pool(tmp_path / 'reviewer')}
    owner = {'profile': 'profile-a', 'session': 'session-a'}
    origin = {**owner, 'route': 'route-a', 'channel': 'test', 'chat': 'chat-a'}
    stages = [{'role': role, 'goal': 'read evidence',
               'sources': [{'path': '/safe/evidence.txt', 'start_line': 1, 'end_line': 2}]}
              for role in pools]
    workflow = store.create(owner, 'request-a', stages, origin)
    root_job = root.submit('workflow-root:' + workflow, {'workflow_id': workflow}, max_tries=1, origin=origin)
    guard = CancelGuard(store, root, pools, fence_timeout=3)
    return store, root, pools, owner, origin, workflow, root_job, guard


def prepare_and_submit(fixture, *, bind=True, payload=None, origin=None):
    store, root, pools, owner, _, workflow, root_job, guard = fixture
    with store.submission_guard(owner, workflow):
        stage = store.prepare_next(owner, workflow)['stage']
        if payload is None:
            payload = {'_workflow_id': workflow, '_workflow_stage': stage['ordinal']}
        job = pools[stage['role']].submit(stage['child_key'], payload, origin=origin)
        if bind:
            store.bind_child(owner, workflow, stage['ordinal'], stage['child_key'], job)
    return stage, job


def _submit_in_process(store_root, pool_root, owner, workflow, entered, release, results):
    try:
        store, pool = WorkflowStore(store_root), Pool(pool_root)
        with store.submission_guard(owner, workflow):
            stage = store.prepare_next(owner, workflow)['stage']
            entered.set()
            if not release.wait(8):
                raise RuntimeError('test release timeout')
            child = pool.submit(stage['child_key'], {'_workflow_id': workflow, '_workflow_stage': stage['ordinal']})
            store.bind_child(owner, workflow, stage['ordinal'], stage['child_key'], child)
        results.put(('ok', child))
    except BaseException as exc:
        results.put(('error', type(exc).__name__))


def test_active_root_does_not_cancel_anything(setup_guard):
    fixture = setup_guard
    stage, child = prepare_and_submit(fixture)
    store, root, pools, owner, _, workflow, root_job, guard = fixture
    assert guard.reconcile(owner, workflow, root_job)['status'] == 'not_cancelling'
    assert pools[stage['role']].status(child)['cancel'] == 0


def test_root_cancel_reconciles_bound_child_after_worker_and_restart(setup_guard):
    fixture = setup_guard
    stage, child = prepare_and_submit(fixture)
    store, root, pools, owner, _, workflow, root_job, guard = fixture
    unrelated = pools[stage['role']].submit('unrelated', {'goal': 'other'})
    root.cancel(root_job)
    assert guard.reconcile(owner, workflow, root_job)['status'] == 'child_pending'
    assert store.inspect(owner, workflow)['state'] == 'cancelling'
    assert pools[stage['role']].status(child)['cancel'] == 1
    assert pools[stage['role']].status(unrelated)['cancel'] == 0
    # A worker claims the queued cancellation and records its terminal receipt.
    claim = pools[stage['role']].claim()
    if claim:
        assert claim[0]['id'] == unrelated
        os.close(claim[1])
    restarted = CancelGuard(WorkflowStore(store.root), Pool(root.root),
                            {name: Pool(pool.root) for name, pool in pools.items()})
    outcome = restarted.reconcile(owner, workflow, root_job)
    assert outcome['status'] == 'cancelled' and outcome['child_terminal'] == 'cancelled'
    assert restarted.store.inspect(owner, workflow)['state'] == 'cancelled'


@pytest.mark.parametrize('terminal', ['failed', 'cancelled'])
def test_terminal_root_failure_or_cancel_fans_out(setup_guard, terminal):
    fixture = setup_guard
    stage, child = prepare_and_submit(fixture)
    store, root, pools, owner, _, workflow, root_job, guard = fixture
    row, fd = root.claim()
    try:
        if terminal == 'cancelled':
            root.cancel(root_job)
        root.finish(root_job, row['attempt'], False, {'reason': 'timeout'})
    finally:
        os.close(fd)
    assert root.status(root_job)['status'] == terminal
    assert guard.reconcile(owner, workflow, root_job)['status'] == 'child_pending'
    assert pools[stage['role']].status(child)['cancel'] == 1


def test_attempt_timeout_scheduled_for_retry_does_not_cancel_child(tmp_path):
    store = WorkflowStore(tmp_path / 'ledger')
    root, child_pool = Pool(tmp_path / 'root'), Pool(tmp_path / 'reader')
    owner = {'profile': 'p', 'session': 's'}
    origin = {**owner, 'route': 'r'}
    stages = [{'role': 'reader', 'goal': 'read', 'sources': [{'path': '/safe/a'}]},
              {'role': 'reader', 'goal': 'review', 'sources': [{'path': '/safe/a'}]}]
    workflow = store.create(owner, 'retry', stages, origin)
    root_job = root.submit('workflow-root:' + workflow, {'workflow_id': workflow},
                           replay_safe=True, max_tries=2, origin=origin)
    with store.submission_guard(owner, workflow):
        stage = store.prepare_next(owner, workflow)['stage']
        child = child_pool.submit(stage['child_key'], {'_workflow_id': workflow, '_workflow_stage': 0})
        store.bind_child(owner, workflow, 0, stage['child_key'], child)
    row, fd = root.claim()
    try:
        root.finish(root_job, row['attempt'], False, {'reason': 'timeout'})
    finally:
        os.close(fd)
    assert root.status(root_job)['status'] == 'queued'
    assert CancelGuard(store, root, {'reader': child_pool}).reconcile(owner, workflow, root_job)['status'] == 'not_cancelling'
    assert child_pool.status(child)['cancel'] == 0


def test_unbound_absence_is_settled_only_under_shared_submission_lock(setup_guard):
    store, root, pools, owner, _, workflow, root_job, guard = setup_guard
    with store.submission_guard(owner, workflow):
        stage = store.prepare_next(owner, workflow)['stage']
    root.cancel(root_job)
    assert guard.reconcile(owner, workflow, root_job) == {
        'status': 'cancelled', 'workflow_state': 'cancelled', 'child': 'fenced_absent'}
    assert store.inspect(owner, workflow)['stages'][0]['state'] == 'cancelled'
    with pools[stage['role']].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_crash_after_submit_before_bind_recovers_by_stable_key(setup_guard):
    fixture = setup_guard
    stage, child = prepare_and_submit(fixture, bind=False)
    store, root, pools, owner, _, workflow, root_job, guard = fixture
    root.cancel(root_job)
    assert guard.reconcile(owner, workflow, root_job)['status'] == 'child_pending'
    assert store.inspect(owner, workflow)['stages'][0]['child_id'] == child
    assert pools[stage['role']].status(child)['cancel'] == 1


def test_submit_and_cancel_race_waits_for_submitter_fence(setup_guard):
    store, root, pools, owner, _, workflow, root_job, guard = setup_guard
    entered, release, reconciled = threading.Event(), threading.Event(), threading.Event()
    saved, errors = {}, []

    def submitter():
        try:
            with store.submission_guard(owner, workflow):
                stage = store.prepare_next(owner, workflow)['stage']
                entered.set()
                assert release.wait(5)
                child = pools[stage['role']].submit(stage['child_key'],
                    {'_workflow_id': workflow, '_workflow_stage': stage['ordinal']})
                store.bind_child(owner, workflow, stage['ordinal'], stage['child_key'], child)
                saved.update(stage=stage, child=child)
        except BaseException as exc:
            errors.append(exc)

    def canceller():
        try:
            saved['outcome'] = guard.reconcile(owner, workflow, root_job)
        except BaseException as exc:
            errors.append(exc)
        finally:
            reconciled.set()

    worker = threading.Thread(target=submitter); worker.start()
    assert entered.wait(5)
    root.cancel(root_job)
    watcher = threading.Thread(target=canceller); watcher.start()
    assert not reconciled.wait(.1)
    release.set(); worker.join(5); watcher.join(5)
    assert not worker.is_alive() and not watcher.is_alive() and not errors
    assert saved['outcome']['status'] == 'child_pending'
    assert pools[saved['stage']['role']].status(saved['child'])['cancel'] == 1


def test_submission_fence_works_across_processes(setup_guard):
    store, root, pools, owner, _, workflow, root_job, guard = setup_guard
    ctx = multiprocessing.get_context('fork')
    entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_submit_in_process,
                          args=(str(store.root), str(pools['reader'].root), owner, workflow,
                                entered, release, results))
    process.start()
    try:
        assert entered.wait(8)
        root.cancel(root_job)
        seen, done = [], threading.Event()

        def reconcile():
            try:
                seen.append(guard.reconcile(owner, workflow, root_job))
            finally:
                done.set()

        thread = threading.Thread(target=reconcile); thread.start()
        assert not done.wait(.1)
        release.set(); process.join(8); thread.join(8)
        assert process.exitcode == 0 and not thread.is_alive()
        status, child = results.get(timeout=2)
        assert status == 'ok' and seen[0]['status'] == 'child_pending'
        assert pools['reader'].status(child)['cancel'] == 1
    finally:
        release.set()
        if process.is_alive():
            process.terminate(); process.join(5)


def test_busy_submission_fence_is_bounded_and_never_proves_absence(setup_guard):
    store, root, pools, owner, _, workflow, root_job, _ = setup_guard
    entered, release = threading.Event(), threading.Event()

    def hold_fence():
        with store.submission_guard(owner, workflow):
            store.prepare_next(owner, workflow)
            entered.set()
            assert release.wait(5)

    worker = threading.Thread(target=hold_fence); worker.start()
    try:
        assert entered.wait(5)
        root.cancel(root_job)
        guard = CancelGuard(store, root, pools, fence_timeout=.05)
        assert guard.reconcile(owner, workflow, root_job)['status'] == 'submission_fence_busy'
        assert store.inspect(owner, workflow)['state'] == 'active'
    finally:
        release.set(); worker.join(5)
    assert not worker.is_alive()
    assert guard.reconcile(owner, workflow, root_job)['status'] == 'cancelled'


def test_wrong_child_binding_is_never_cancelled(setup_guard):
    fixture = setup_guard
    stage, unrelated = prepare_and_submit(fixture, payload={
        '_workflow_id': '0' * 32, '_workflow_stage': 0})
    store, root, pools, owner, _, workflow, root_job, guard = fixture
    root.cancel(root_job)
    with pytest.raises(BindingError):
        guard.reconcile(owner, workflow, root_job)
    assert pools[stage['role']].status(unrelated)['cancel'] == 0
    assert store.inspect(owner, workflow)['state'] == 'cancelling'


def test_foreign_addressed_child_is_never_cancelled(setup_guard):
    store, root, pools, owner, origin, workflow, root_job, guard = setup_guard
    stage, child = prepare_and_submit(setup_guard, origin=origin)
    root.cancel(root_job)
    with pytest.raises(BindingError):
        guard.reconcile(owner, workflow, root_job)
    assert pools[stage['role']].status(child)['cancel'] == 0


def test_root_binding_and_owner_are_required(setup_guard):
    fixture = setup_guard
    stage, child = prepare_and_submit(fixture)
    store, root, pools, owner, origin, workflow, root_job, guard = fixture
    wrong_root = root.submit('wrong', {'workflow_id': workflow}, origin=origin)
    root.cancel(wrong_root)
    with pytest.raises(BindingError):
        guard.reconcile(owner, workflow, wrong_root)
    with pytest.raises(PermissionError):
        guard.reconcile({'profile': 'profile-a', 'session': 'other'}, workflow, root_job)
    assert pools[stage['role']].status(child)['cancel'] == 0


def test_changed_root_origin_cannot_cancel_child(setup_guard):
    stage, child = prepare_and_submit(setup_guard)
    store, root, pools, owner, origin, workflow, root_job, guard = setup_guard
    with root.db() as db:
        db.execute('UPDATE jobs SET origin=? WHERE id=?',
                   (json.dumps({**origin, 'chat': 'different'}), root_job))
    root.cancel(root_job)
    with pytest.raises(BindingError):
        guard.reconcile(owner, workflow, root_job)
    assert pools[stage['role']].status(child)['cancel'] == 0


def test_bound_missing_child_stays_unsettled(setup_guard):
    store, root, pools, owner, _, workflow, root_job, guard = setup_guard
    with store.submission_guard(owner, workflow):
        stage = store.prepare_next(owner, workflow)['stage']
        store.bind_child(owner, workflow, stage['ordinal'], stage['child_key'], 'a' * 32)
    root.cancel(root_job)
    assert guard.reconcile(owner, workflow, root_job)['status'] == 'child_unavailable'
    assert store.inspect(owner, workflow)['state'] == 'cancelling'


def test_scan_finds_root_cancellation_after_guard_restart(setup_guard):
    fixture = setup_guard
    stage, child = prepare_and_submit(fixture)
    store, root, pools, owner, _, workflow, root_job, guard = fixture
    root.cancel(root_job)
    restarted = CancelGuard(WorkflowStore(store.root), Pool(root.root),
                            {name: Pool(pool.root) for name, pool in pools.items()})
    result = restarted.scan_once(limit=1)
    assert result['outcomes'][0]['root_job'] == root_job
    assert result['outcomes'][0]['status'] == 'child_pending'
    assert pools[stage['role']].status(child)['cancel'] == 1
    assert restarted.scan_once(after=result['next_after'], limit=1)['next_after'] == ''


def test_role_pools_cannot_alias_each_other_or_root(setup_guard):
    store, root, pools, _, _, _, _, _ = setup_guard
    with pytest.raises(ValueError):
        CancelGuard(store, root, {'reader': root})
    with pytest.raises(ValueError):
        CancelGuard(store, root, {'reader': pools['reader'], 'reviewer': pools['reader']})


def test_supervised_loop_reports_outcome_and_stops(setup_guard):
    store, root, pools, owner, _, workflow, root_job, guard = setup_guard
    root.cancel(root_job)
    stop = threading.Event()
    seen = []

    def observe(outcome):
        seen.append(outcome)
        stop.set()

    guard.run(stop, observer=observe, interval=.1)
    assert len(seen) == 1 and seen[0]['status'] == 'workflow_terminal'
    assert seen[0]['workflow_state'] == 'cancelled'

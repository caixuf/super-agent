import concurrent.futures
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from agentcore.colleague_workflow_store import WorkflowConflict, WorkflowStore
from agentcore.pool import Pool


OWNER = {'profile': 'profile-a', 'session': 'session-a'}
ORIGIN = {'profile': 'profile-a', 'session': 'session-a', 'route': 'route-a', 'channel': 'test'}
OTHER = {'profile': 'profile-a', 'session': 'session-b'}
CHILD1, CHILD2 = '1' * 32, '2' * 32


def stages():
    return [{'role': role, 'goal': 'Analyze authorized data',
             'sources': [{'path': '/approved/source.txt', 'start_line': 1, 'end_line': 20}]}
            for role in ('evidence', 'review')]


def evidence(attempt='a', summary='Untrusted summary'):
    return {'attempt_id': attempt * 32, 'report_sha256': 'b' * 64,
            'source_manifest_sha256': 'c' * 64, 'summary': summary,
            'verification': 'citation_traceability_only'}


def create(store):
    return store.create(OWNER, 'request-1', stages(), ORIGIN)


def bind(store, wid, child):
    intent = store.prepare_next(OWNER, wid)
    stage = intent['stage']
    store.bind_child(OWNER, wid, stage['ordinal'], stage['child_key'], child)
    return stage


def test_create_is_owner_bound_idempotent_and_conflicts_on_changed_input(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    assert create(store) == wid
    changed = stages()
    changed[1]['goal'] = 'Different goal'
    with pytest.raises(WorkflowConflict):
        store.create(OWNER, 'request-1', changed, ORIGIN)
    with pytest.raises(WorkflowConflict):
        store.create(OWNER, 'request-1', stages(), {**ORIGIN, 'route': 'other-route'})
    for operation in (lambda: store.inspect(OTHER, wid), lambda: store.prepare_next(OTHER, wid),
                      lambda: store.cancel(OTHER, wid), lambda: store.inspect_final(OTHER, wid)):
        with pytest.raises(PermissionError, match='unavailable'):
            operation()
    assert store.create(OTHER, 'request-1', stages(), {**ORIGIN, 'session': 'session-b'}) != wid


@pytest.mark.parametrize('mutate', [
    lambda stages: stages[0].update(role='../../run'),
    lambda stages: stages[0]['sources'][0].update(path='/approved/../secret'),
    lambda stages: stages[0]['sources'][0].update(end_line=10000),
    lambda stages: stages[0].update(extra='injected'),
    lambda stages: stages[0].update(sources=[]),
])
def test_stages_are_bounded_but_not_claimed_as_authorized(tmp_path, mutate):
    store = WorkflowStore(tmp_path)
    items = stages()
    mutate(items)
    with pytest.raises(ValueError):
        store.create(OWNER, 'key', items, ORIGIN)


def test_intent_and_evidence_recover_after_restart_with_same_child_key(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    first = store.prepare_next(OWNER, wid)
    assert first['stage']['state'] == 'prepared'
    reopened = WorkflowStore(tmp_path)
    assert reopened.prepare_next(OWNER, wid) == first
    reopened.bind_child(OWNER, wid, 0, first['stage']['child_key'], CHILD1)
    again = WorkflowStore(tmp_path)
    assert again.prepare_next(OWNER, wid)['stage']['child_id'] == CHILD1
    assert again.finish(OWNER, wid, 0, CHILD1, evidence()) == 'active'
    second = WorkflowStore(tmp_path).prepare_next(OWNER, wid)
    assert second['stage']['ordinal'] == 1
    assert second['previous_evidence'] == evidence()
    assert second['stage']['child_key'] != first['stage']['child_key']
    assert again.finish(OWNER, wid, 0, CHILD1, evidence()) == 'active'
    with pytest.raises(WorkflowConflict, match='immutable'):
        again.finish(OWNER, wid, 0, CHILD1, evidence(summary='Changed'))


def test_many_concurrent_preparers_get_one_persisted_intent(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: store.prepare_next(OWNER, wid), range(24)))
    assert len({r['stage']['child_key'] for r in results}) == 1
    assert all(r['stage']['state'] == 'prepared' for r in results)
    first = results[0]['stage']
    store.bind_child(OWNER, wid, 0, first['child_key'], CHILD1)
    assert store.bind_child(OWNER, wid, 0, first['child_key'], CHILD1)['state'] == 'active'
    with pytest.raises(WorkflowConflict):
        store.bind_child(OWNER, wid, 0, first['child_key'], CHILD2)


def _acquire_guard(root, wid, ready, acquired):
    store = WorkflowStore(root)
    ready.set()
    with store.submission_guard(OWNER, wid):
        acquired.set()


def test_submission_guard_fences_other_process(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    ctx = multiprocessing.get_context('fork')
    ready, acquired = ctx.Event(), ctx.Event()
    with store.submission_guard(OWNER, wid):
        process = ctx.Process(target=_acquire_guard, args=(str(tmp_path), wid, ready, acquired))
        process.start()
        assert ready.wait(3)
        assert not acquired.wait(.2)
    assert acquired.wait(3)
    process.join(3)
    assert process.exitcode == 0


def test_submission_guard_busy_times_out_without_advancing_workflow(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    with store.submission_guard(OWNER, wid):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            def contender():
                with store.submission_guard(OWNER, wid, timeout=.1):
                    pytest.fail('contender unexpectedly acquired held lock')
            future = executor.submit(contender)
            with pytest.raises(TimeoutError):
                future.result(timeout=2)
        assert store.inspect(OWNER, wid)['stages'][0]['state'] == 'queued'


def test_opening_second_store_preserves_existing_sqlite_write_lock(tmp_path):
    store = WorkflowStore(tmp_path)
    create(store)
    probe = '''import sqlite3, sys
db=sqlite3.connect(sys.argv[1],timeout=.1)
try:
    db.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as e:
    sys.exit(0 if "locked" in str(e).lower() else 2)
else:
    db.rollback()
    sys.exit(1)
finally:
    db.close()
'''
    connection = sqlite3.connect(store.path, timeout=1, isolation_level=None)
    try:
        connection.execute('BEGIN IMMEDIATE')
        first = subprocess.run([sys.executable, '-c', probe, str(store.path)], timeout=3)
        assert first.returncode == 0
        WorkflowStore(tmp_path)
        second = subprocess.run([sys.executable, '-c', probe, str(store.path)], timeout=3)
        assert second.returncode == 0, 'second store initialization released the first SQLite write lock'
    finally:
        connection.rollback()
        connection.close()


def test_cancel_unbound_intent_requires_fenced_absence_or_real_child(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    first = store.prepare_next(OWNER, wid)['stage']
    pending = store.cancel(OWNER, wid)
    assert pending['state'] == 'cancelling' and pending['pending_child']['child_key'] == first['child_key']
    assert store.prepare_next(OWNER, wid) is None
    with pytest.raises(WorkflowConflict, match='not fenced'):
        store.settle_cancel(OWNER, wid, 0)
    bound = store.bind_child(OWNER, wid, 0, first['child_key'], CHILD1)
    assert bound['cancel_required']
    with pytest.raises(WorkflowConflict):
        store.settle_cancel(OWNER, wid, 0, child_id=CHILD2)
    assert store.settle_cancel(OWNER, wid, 0, child_id=CHILD1) == 'cancelled'
    assert [s['state'] for s in store.inspect(OWNER, wid)['stages']] == ['cancelled', 'cancelled']


def test_cancel_and_completion_race_cannot_release_next_stage(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    bind(store, wid, CHILD1)
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait()
        return store.finish(OWNER, wid, 0, CHILD1, evidence())

    def cancel():
        barrier.wait()
        return store.cancel(OWNER, wid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1, f2 = executor.submit(finish), executor.submit(cancel)
        f1.result(); f2.result()
    result = store.inspect(OWNER, wid)
    assert result['state'] == 'cancelled'
    assert result['stages'][1]['state'] == 'cancelled'
    assert store.prepare_next(OWNER, wid) is None


def test_final_outbox_waits_for_internal_acks_and_reconciles_unknown_send(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    bind(store, wid, CHILD1)
    store.finish(OWNER, wid, 0, CHILD1, evidence())
    bind(store, wid, CHILD2)
    store.finish(OWNER, wid, 1, CHILD2, evidence('d'))
    assert store.inspect_final(OWNER, wid)['state'] == 'pending'
    assert store.claim_final(OWNER, wid)['state'] == 'waiting_for_child_ack'
    # A queue ACK may already exist when the coordinator crashes; this marker
    # can be completed after constructing a new store instance.
    store = WorkflowStore(tmp_path)
    assert [r['child_id'] for r in store.pending_child_acks(OWNER, wid)] == [CHILD1, CHILD2]
    assert store.mark_child_ack(OWNER, wid, 0, CHILD1, 'internal-one')
    assert not store.mark_child_ack(OWNER, wid, 0, CHILD1, 'internal-one')
    store.mark_child_ack(OWNER, wid, 1, CHILD2, 'internal-two')
    claim = store.claim_final(OWNER, wid, lease=1)
    assert claim['state'] == 'sending' and claim['payload']['origin'] == ORIGIN
    assert store.claim_final(OWNER, wid)['state'] == 'sending'
    real = time.time
    monkeypatch.setattr('agentcore.colleague_workflow_store.time.time', lambda: real() + 2)
    assert store.claim_final(OWNER, wid)['state'] == 'unknown'
    assert store.claim_final(OWNER, wid)['state'] == 'unknown'
    with pytest.raises(WorkflowConflict):
        store.ack_final(OWNER, wid, claim['token'], claim['delivery_key'], 'message-1')
    assert store.reconcile_final(OWNER, wid, claim['delivery_key'], 'message-1')
    assert not store.reconcile_final(OWNER, wid, claim['delivery_key'], 'message-1')
    assert store.inspect_final(OWNER, wid)['state'] == 'delivered'
    with pytest.raises(WorkflowConflict):
        store.reconcile_final(OWNER, wid, claim['delivery_key'], 'different-message')


def test_failed_child_closes_workflow_and_never_runs_downstream(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    bind(store, wid, CHILD1)
    assert store.fail(OWNER, wid, 0, CHILD1, 'unverified_citation') == 'failed'
    assert store.prepare_next(OWNER, wid) is None
    assert store.inspect_final(OWNER, wid)['payload']['status'] == 'failed'
    assert store.claim_final(OWNER, wid)['state'] == 'waiting_for_child_ack'


def test_real_queue_replay_and_internal_ack_after_coordinator_restart(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow')
    pool = Pool(str(tmp_path / 'queue'))
    wid = create(store)
    stage = store.prepare_next(OWNER, wid)['stage']
    payload = {'goal': stage['goal'], 'sources': stage['sources'],
               '_workflow_id': wid, '_workflow_stage': stage['ordinal']}
    first_job = pool.submit(stage['child_key'], payload, origin=None, replay_safe=True)
    # Coordinator dies after enqueue but before bind. The ledger reconstructs
    # the same intent and the destination queue returns the same real job.
    store = WorkflowStore(tmp_path / 'workflow')
    recovered = store.prepare_next(OWNER, wid)['stage']
    assert pool.submit(recovered['child_key'], payload, origin=None, replay_safe=True) == first_job
    store.bind_child(OWNER, wid, 0, recovered['child_key'], first_job)
    row, fd = pool.claim()
    try:
        assert pool.finish(first_job, row['attempt'], True, {'summary': 'finished'})
    finally:
        os.close(fd)
    proof = evidence()
    proof['attempt_id'] = row['attempt']
    store.finish(OWNER, wid, 0, first_job, proof)
    receipt = pool.claim_receipt()
    assert receipt['receipt']['job'] == first_job
    assert pool.acknowledge(first_job, receipt['token'], receipt_id='internal-queue-receipt')
    # A crash after queue ACK but before the ledger marker stays recoverable.
    store = WorkflowStore(tmp_path / 'workflow')
    assert [x['child_id'] for x in store.pending_child_acks(OWNER, wid)] == [first_job]
    queue_record = pool.delivery_record(first_job)
    assert queue_record['acknowledged'] and queue_record['receipt_id'] == 'internal-queue-receipt'
    store.mark_child_ack(OWNER, wid, 0, first_job, queue_record['receipt_id'])
    assert not store.pending_child_acks(OWNER, wid)


def test_final_receipt_requires_live_matching_fence_and_is_immutable(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    bind(store, wid, CHILD1)
    store.finish(OWNER, wid, 0, CHILD1, evidence())
    bind(store, wid, CHILD2)
    store.finish(OWNER, wid, 1, CHILD2, evidence('d'))
    store.mark_child_ack(OWNER, wid, 0, CHILD1, 'first')
    store.mark_child_ack(OWNER, wid, 1, CHILD2, 'second')
    claim = store.claim_final(OWNER, wid)
    with pytest.raises(WorkflowConflict):
        store.ack_final(OWNER, wid, 'wrong-token', claim['delivery_key'], 'receipt')
    with pytest.raises(WorkflowConflict):
        store.ack_final(OWNER, wid, claim['token'], 'wrong-key', 'receipt')
    with pytest.raises(ValueError):
        store.ack_final(OWNER, wid, claim['token'], claim['delivery_key'], '')
    assert store.ack_final(OWNER, wid, claim['token'], claim['delivery_key'], 'receipt')
    assert not store.ack_final(OWNER, wid, claim['token'], claim['delivery_key'], 'receipt')
    with pytest.raises(WorkflowConflict):
        store.ack_final(OWNER, wid, claim['token'], claim['delivery_key'], 'different')


def test_unbound_cancellation_is_terminal_only_after_fenced_absence(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = create(store)
    stage = store.prepare_next(OWNER, wid)['stage']
    store.cancel(OWNER, wid)
    assert store.inspect_final(OWNER, wid) is None
    with store.submission_guard(OWNER, wid):
        assert store.settle_cancel(OWNER, wid, stage['ordinal'], submission_fenced=True) == 'cancelled'
    assert store.claim_final(OWNER, wid)['state'] == 'sending'

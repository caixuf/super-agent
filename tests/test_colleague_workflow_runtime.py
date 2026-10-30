import json
import os
from pathlib import Path

import pytest

from agentcore.analysis_runner import run_analysis
from agentcore.colleague_workflow_runtime import WorkflowBindingError, WorkflowRuntime
from agentcore.colleague_workflow_store import WorkflowStore
from agentcore.pool import Pool


OWNER = {'profile': 'p', 'session': 's'}
ORIGIN = {'profile': 'p', 'session': 's', 'route': 'chat-route'}


def setup_workflow(tmp_path, authorize_first=None, authorize_second=None):
    directory = tmp_path / 'sources'
    directory.mkdir()
    source = directory / 'evidence.txt'
    source.write_text('before\nobserved value is seven\nafter\n')
    pools = {'evidence': Pool(tmp_path / 'pool-a'), 'review': Pool(tmp_path / 'pool-b')}
    store = WorkflowStore(tmp_path / 'ledger')
    sources = [{'path': str(source), 'start_line': 2, 'end_line': 2}]
    stages = [{'role': role, 'goal': 'Check the observation', 'sources': sources}
              for role in ('evidence', 'review')]
    wid = store.create(OWNER, 'request', stages, ORIGIN)
    allowed = lambda items: all(Path(x['path']).resolve().is_relative_to(directory.resolve()) for x in items)
    bindings = {
        'evidence': {'pool': pools['evidence'], 'authorize_sources': authorize_first or allowed, 'dedicated': True},
        'review': {'pool': pools['review'], 'authorize_sources': authorize_second or allowed, 'dedicated': True},
    }
    return store, WorkflowRuntime(store, bindings), pools, wid, directory, bindings


def finish_real_analysis(pool, source_root):
    claimed = pool.claim()
    assert claimed is not None
    row, fd = claimed
    payload = json.loads(row['payload'])
    seen = []

    def model(messages, **kwargs):
        seen.extend(messages)
        return {'content': json.dumps({'conclusion': 'The observed value was seven.',
            'claims': [{'statement': 'Value seven is recorded.', 'evidence': [
                {'source_id': 'source_1', 'line': 2, 'quote': 'value is seven'}]}],
            'limitations': ['No cause was established.']})}

    try:
        output = pool.root / 'attempts' / row['attempt']
        result = run_analysis({'job': row['id'], 'attempt': row['attempt'], 'payload': payload},
                              {'allowed_roots': [str(source_root)]}, model, output)
        assert pool.finish(row['id'], row['attempt'], True, result)
    finally:
        os.close(fd)
    return row['id'], output, seen


def test_two_private_pools_real_analysis_and_one_final_delivery_intent(tmp_path):
    store, runtime, pools, wid, sources, _ = setup_workflow(tmp_path)
    first = runtime.step(OWNER, wid)
    assert first['state'] == 'submitted' and first['stage'] == 0
    first_id, _, _ = finish_real_analysis(pools['evidence'], sources)
    assert first_id == first['child_id']
    assert runtime.step(OWNER, wid)['state'] == 'active'
    assert pools['evidence'].delivery_record(first_id)['receipt_id'].startswith('workflow-internal:')
    second = runtime.step(OWNER, wid)
    assert second['state'] == 'submitted' and second['stage'] == 1
    second_id, _, messages = finish_real_analysis(pools['review'], sources)
    data = json.loads(messages[1]['content'])
    assert '[Untrusted previous colleague summary' in data['goal']
    assert 'The observed value was seven.' in data['goal']
    assert data['sources'][0]['path'] == str(sources / 'evidence.txt')
    assert runtime.step(OWNER, wid)['state'] == 'completed'
    assert pools['review'].delivery_record(second_id)['acknowledged'] == 1
    assert not store.pending_child_acks(OWNER, wid)
    final = store.claim_final(OWNER, wid)
    assert final['state'] == 'sending' and final['payload']['origin'] == ORIGIN
    assert len(final['payload']['stages']) == 2
    assert runtime.step(OWNER, wid)['final_delivery'] == 'requires_host_transport'


@pytest.mark.parametrize('count', [3, 4])
def test_three_and_four_stage_workflows_progress_in_order(tmp_path, count):
    store, runtime, pools, _, sources, _ = setup_workflow(tmp_path)
    roles = ['evidence', 'review', 'evidence', 'review'][:count]
    src = [{'path': str(sources / 'evidence.txt'), 'start_line': 2, 'end_line': 2}]
    wid = store.create(OWNER, 'longer-request',
                       [{'role': role, 'goal': 'Check the observation', 'sources': src} for role in roles], ORIGIN)
    for index, role in enumerate(roles):
        submitted = runtime.step(OWNER, wid)
        assert submitted['state'] == 'submitted' and submitted['stage'] == index
        finish_real_analysis(pools[role], sources)
        result = runtime.step(OWNER, wid)
        assert result['state'] == ('completed' if index == count - 1 else 'active')
    assert [stage['state'] for stage in store.inspect(OWNER, wid)['stages']] == ['completed'] * count
    assert store.claim_final(OWNER, wid)['state'] == 'sending'


def test_submit_before_bind_crash_recovers_existing_child(tmp_path, monkeypatch):
    store, runtime, pools, wid, _, bindings = setup_workflow(tmp_path)
    def crash(*args, **kwargs):
        raise RuntimeError('simulated crash')
    monkeypatch.setattr(store, 'bind_child', crash)
    with pytest.raises(RuntimeError, match='simulated'):
        runtime.step(OWNER, wid)
    with pools['evidence'].db() as db:
        jobs = db.execute('SELECT id FROM jobs').fetchall()
    assert len(jobs) == 1
    store = WorkflowStore(tmp_path / 'ledger')
    runtime = WorkflowRuntime(store, bindings)
    result = runtime.step(OWNER, wid)
    assert result['state'] == 'child_pending' and result['child_id'] == jobs[0]['id']
    with pools['evidence'].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


@pytest.mark.parametrize('crash_point', ['before_queue_ack', 'after_queue_ack'])
def test_stage_completion_and_internal_ack_recover_after_crash(tmp_path, monkeypatch, crash_point):
    store, runtime, pools, wid, sources, bindings = setup_workflow(tmp_path)
    result = runtime.step(OWNER, wid)
    child = result['child_id']
    finish_real_analysis(pools['evidence'], sources)

    def crash(*args, **kwargs):
        raise RuntimeError('simulated crash')

    if crash_point == 'before_queue_ack':
        monkeypatch.setattr(pools['evidence'], 'acknowledge', crash)
    else:
        monkeypatch.setattr(store, 'mark_child_ack', crash)
    with pytest.raises(RuntimeError, match='simulated'):
        runtime.step(OWNER, wid)
    assert store.inspect(OWNER, wid)['stages'][0]['state'] == 'completed'
    if crash_point == 'before_queue_ack':
        monkeypatch.undo()
    store = WorkflowStore(tmp_path / 'ledger')
    runtime = WorkflowRuntime(store, bindings)
    resumed = runtime.step(OWNER, wid)
    assert resumed['state'] == 'submitted' and resumed['stage'] == 1
    assert pools['evidence'].delivery_record(child)['acknowledged'] == 1
    assert store.inspect(OWNER, wid)['stages'][0]['internal_acked']


def test_downstream_denied_source_fails_without_submitting_or_granting_upstream_access(tmp_path):
    store, runtime, pools, wid, sources, _ = setup_workflow(tmp_path, authorize_second=lambda _: False)
    runtime.step(OWNER, wid)
    finish_real_analysis(pools['evidence'], sources)
    runtime.step(OWNER, wid)
    result = runtime.step(OWNER, wid)
    assert result == {'state': 'failed', 'reason': 'source_denied'}
    with pools['review'].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    assert store.inspect(OWNER, wid)['stages'][1]['failure'] == 'source_denied'
    assert store.claim_final(OWNER, wid)['state'] == 'sending'


def test_revoked_policy_aborts_bound_child_and_survives_policy_restoration(tmp_path):
    allowed = [True]
    store, runtime, pools, wid, _, _ = setup_workflow(tmp_path, authorize_first=lambda _: allowed[0])
    child = runtime.step(OWNER, wid)['child_id']
    allowed[0] = False
    assert runtime.step(OWNER, wid)['state'] == 'child_abort_pending'
    assert pools['evidence'].status(child)['cancel'] == 1
    assert store.inspect(OWNER, wid)['stages'][0]['abort_reason'] == 'source_denied'
    allowed[0] = True
    assert pools['evidence'].claim() is None  # Processes queued cancellation to terminal.
    result = runtime.step(OWNER, wid)
    assert result['state'] == 'failed'
    assert store.inspect(OWNER, wid)['stages'][0]['failure'] == 'source_denied'
    assert pools['evidence'].delivery_record(child)['acknowledged'] == 1


def test_authorization_exception_before_submit_is_retryable_and_never_leaks_error(tmp_path):
    def unavailable(_):
        raise RuntimeError('sensitive exception detail')
    store, runtime, pools, wid, _, bindings = setup_workflow(tmp_path, authorize_first=unavailable)
    result = runtime.step(OWNER, wid)
    assert result == {'state': 'authorization_unavailable', 'stage': 0}
    assert 'sensitive' not in json.dumps(result)
    assert store.inspect(OWNER, wid)['stages'][0]['state'] == 'prepared'
    with pools['evidence'].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    bindings['evidence']['authorize_sources'] = lambda _: True
    assert WorkflowRuntime(store, bindings).step(OWNER, wid)['state'] == 'submitted'


def test_authorization_exception_after_submit_cancels_child_without_mislabeling_denial(tmp_path):
    available = [True]
    def authorize(_):
        if not available[0]:
            raise RuntimeError('private policy error')
        return True
    store, runtime, pools, wid, _, _ = setup_workflow(tmp_path, authorize_first=authorize)
    child = runtime.step(OWNER, wid)['child_id']
    available[0] = False
    result = runtime.step(OWNER, wid)
    assert result['state'] == 'child_abort_pending' and result['reason'] == 'authorization_unavailable'
    assert pools['evidence'].status(child)['cancel'] == 1
    assert store.inspect(OWNER, wid)['stages'][0]['abort_reason'] == 'authorization_unavailable'


def test_tampered_report_fails_closed_and_acknowledges_internal_receipt(tmp_path):
    store, runtime, pools, wid, sources, _ = setup_workflow(tmp_path)
    child = runtime.step(OWNER, wid)['child_id']
    _, output, _ = finish_real_analysis(pools['evidence'], sources)
    path = output / 'report.json'
    report = json.loads(path.read_text())
    report['claims'][0]['evidence'][0]['quote'] = 'invented'
    path.write_text(json.dumps(report))
    assert runtime.step(OWNER, wid)['state'] == 'failed'
    assert store.inspect(OWNER, wid)['stages'][0]['failure'] == 'invalid_evidence'
    assert pools['evidence'].delivery_record(child)['acknowledged'] == 1
    with pools['review'].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_forged_existing_child_is_neither_bound_nor_acknowledged(tmp_path):
    store, runtime, pools, wid, _, _ = setup_workflow(tmp_path)
    stage = store.prepare_next(OWNER, wid)['stage']
    fake = pools['evidence'].submit(stage['child_key'], {'goal': 'different'}, origin=None)
    with pytest.raises(WorkflowBindingError, match='binding'):
        runtime.step(OWNER, wid)
    assert store.inspect(OWNER, wid)['stages'][0]['child_id'] is None
    assert pools['evidence'].status(fake)['status'] == 'queued'


def test_cancellation_during_submit_requests_child_cancel_without_advancing(tmp_path, monkeypatch):
    store, runtime, pools, wid, _, _ = setup_workflow(tmp_path)
    original = pools['evidence'].submit
    def cancel_during_submit(*args, **kwargs):
        child = original(*args, **kwargs)
        store.cancel(OWNER, wid)
        return child
    monkeypatch.setattr(pools['evidence'], 'submit', cancel_during_submit)
    result = runtime.step(OWNER, wid)
    assert result['state'] == 'needs_cancel_guard'
    assert pools['evidence'].status(result['child_id'])['cancel'] == 1
    assert store.inspect(OWNER, wid)['state'] == 'cancelling'
    assert runtime.step(OWNER, wid)['state'] == 'needs_cancel_guard'
    with pools['review'].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_cancellation_during_result_verification_wins_before_next_stage(tmp_path, monkeypatch):
    import agentcore.colleague_workflow_runtime as module
    store, runtime, pools, wid, sources, _ = setup_workflow(tmp_path)
    runtime.step(OWNER, wid)
    finish_real_analysis(pools['evidence'], sources)
    real = module.verify_internal_result
    def verify_and_cancel(*args):
        result = real(*args)
        store.cancel(OWNER, wid)
        return result
    monkeypatch.setattr(module, 'verify_internal_result', verify_and_cancel)
    assert runtime.step(OWNER, wid)['state'] == 'cancelled'
    assert store.inspect(OWNER, wid)['stages'][1]['state'] == 'cancelled'
    with pools['review'].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_cancel_during_rebind_cancels_existing_unbound_child(tmp_path, monkeypatch):
    store, runtime, pools, wid, _, _ = setup_workflow(tmp_path)
    prepared = store.prepare_next(OWNER, wid)
    stage = prepared['stage']
    payload = runtime._payload(wid, stage, None)
    child_id = pools['evidence'].submit(stage['child_key'], payload, origin=None, replay_safe=True, max_tries=2)
    original = store.bind_child
    def cancel_then_bind(*args, **kwargs):
        store.cancel(OWNER, wid)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'bind_child', cancel_then_bind)
    assert runtime.step(OWNER, wid)['state'] == 'needs_cancel_guard'
    assert pools['evidence'].status(child_id)['cancel'] == 1
    assert store.inspect(OWNER, wid)['stages'][0]['child_id'] == child_id


def test_cancel_races_source_rejection_leaves_intent_for_guard(tmp_path, monkeypatch):
    store, runtime, pools, wid, _, _ = setup_workflow(tmp_path, authorize_first=lambda _: False)
    original = store.reject_unsubmitted
    def cancel_then_reject(*args, **kwargs):
        store.cancel(OWNER, wid)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'reject_unsubmitted', cancel_then_reject)
    assert runtime.step(OWNER, wid)['state'] == 'needs_cancel_guard'
    assert store.inspect(OWNER, wid)['state'] == 'cancelling'
    with pools['evidence'].db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_separate_cancel_guard_and_runtime_reconcile_internal_terminal_receipt(tmp_path):
    from agentcore.colleague_cancel_guard import CancelGuard
    store, runtime, pools, wid, _, _ = setup_workflow(tmp_path)
    root = Pool(tmp_path / 'root-queue')
    root_id = root.submit('workflow-root:' + wid, {'workflow_id': wid}, origin=ORIGIN)
    child_id = runtime.step(OWNER, wid)['child_id']
    guard = CancelGuard(store, root, pools)
    root.cancel(root_id)
    assert guard.reconcile(OWNER, wid, root_id)['status'] == 'child_pending'
    assert pools['evidence'].status(child_id)['cancel']
    assert pools['evidence'].claim() is None
    assert guard.reconcile(OWNER, wid, root_id)['status'] == 'cancelled'
    assert runtime.step(OWNER, wid)['state'] == 'cancelled'
    assert pools['evidence'].delivery_record(child_id)['acknowledged'] == 1
    assert not store.pending_child_acks(OWNER, wid)
    assert store.claim_final(OWNER, wid)['payload']['status'] == 'cancelled'

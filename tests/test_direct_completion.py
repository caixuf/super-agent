import os
from pathlib import Path
import threading

import pytest

from agentcore.completion_delivery import build_message
from agentcore.direct_completion import DirectCompletionTransport

ADDRESS = dict(profile='owner', session='session', route='route', channel='feishu', chat='chat-1')
ATTEMPT = 'a'*32


def make_transport(tmp_path, sender, card=False):
    directory = tmp_path/'attempts'/ATTEMPT
    directory.mkdir(parents=True, exist_ok=True)
    file = directory/'report.md'
    if not file.exists():
        file.write_text('verified report')
    transport = DirectCompletionTransport(tmp_path/'delivery', profile='owner', sender=sender,
        render=lambda m: m['summary'], attempt_root=tmp_path/'attempts', attempt_provider=lambda job: ATTEMPT,
        card=card)
    message = dict(task_id='job', status='completed', summary='done', artifacts=[str(file)])
    return transport, message, file


def test_real_worker_envelope_keeps_summary_and_artifacts():
    result = build_message(dict(job='j', status='completed', result=dict(ok=True, result=dict(
        summary='actual analysis', artifacts=['/safe/report.md'])))).render()
    assert result['summary'] == 'actual analysis'
    assert result['artifacts'] == ['/safe/report.md']


def test_queue_ack_retry_after_restart_reuses_all_platform_receipts(tmp_path):
    calls = []
    def sender(address, item, **kwargs):
        calls.append(item)
        return dict(ok=True, message_id='platform-'+str(len(calls)))
    transport, message, file = make_transport(tmp_path, sender)
    first = transport.deliver(ADDRESS, message, idempotency_key='job')
    assert first['accepted'] and len(calls) == 2
    file.unlink()  # Worker cleanup must not require resending acknowledged parts.
    restarted, _, _ = make_transport(tmp_path, sender)
    assert restarted.deliver(ADDRESS, message, idempotency_key='job') == first
    assert len(calls) == 2


def test_unknown_file_result_is_not_resent_after_restart(tmp_path):
    calls = []
    def sender(address, item, **kwargs):
        calls.append(item['kind'])
        if item['kind'] == 'file':
            raise TimeoutError('may already have reached platform')
        return dict(ok=True, message_id='summary')
    transport, message, _ = make_transport(tmp_path, sender)
    assert transport.deliver(ADDRESS, message, idempotency_key='job')['deferred']
    restarted, _, _ = make_transport(tmp_path, sender)
    assert restarted.deliver(ADDRESS, message, idempotency_key='job')['deferred']
    assert calls == ['text', 'file']


@pytest.mark.parametrize('response', [
    {'ok': True, 'not_sent': True, 'message_id': 'm'},
    {'ok': False, 'not_sent': True, 'message_id': 'm'},
    {'not_sent': True},
    {'ok': True, 'not_sent': 'yes', 'message_id': 'm'},
    {'ok': True, 'message_id': 'm' * 513},
])
def test_contradictory_sender_receipt_is_unknown_and_not_retried(tmp_path, response):
    calls = []
    def sender(*args, **kwargs):
        calls.append(1)
        return response
    transport, message, _ = make_transport(tmp_path, sender)
    message['artifacts'] = []
    assert transport.deliver(ADDRESS, message, idempotency_key='job')['deferred']
    assert transport.inspect()[0]['state'] == 'unknown'
    restarted, _, _ = make_transport(tmp_path, sender)
    assert restarted.deliver(ADDRESS, message, idempotency_key='job')['deferred']
    assert len(calls) == 1


def test_definite_presend_refusal_retries_only_unsent_part_with_staged_bytes(tmp_path):
    calls = []
    def sender(address, item, **kwargs):
        calls.append(item['kind'])
        if len(calls) == 2:
            return dict(ok=False, not_sent=True)
        if item['kind'] == 'file':
            assert Path(item['path']).read_text() == 'verified report'
        return dict(ok=True, message_id=str(len(calls)))
    transport, message, file = make_transport(tmp_path, sender)
    assert transport.deliver(ADDRESS, message, idempotency_key='job')['deferred']
    file.write_text('mutated source after partial delivery')
    assert transport.deliver(ADDRESS, message, idempotency_key='job')['accepted']
    assert calls == ['text', 'file', 'file']


def test_concurrent_snapshot_publication_exposes_only_complete_file(tmp_path, monkeypatch):
    transport, _, file = make_transport(tmp_path, lambda *a, **kw: None)
    barrier = threading.Barrier(2)
    original_link = os.link
    def synchronized_link(src, dst, **kwargs):
        barrier.wait(timeout=5)
        return original_link(src, dst, **kwargs)
    monkeypatch.setattr(os, 'link', synchronized_link)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(transport._stage, file, ATTEMPT) for _ in range(2)]
        snapshots = [future.result(timeout=10) for future in futures]
    assert snapshots[0] == snapshots[1]
    assert Path(snapshots[0]['path']).read_bytes() == b'verified report'
    assert not list(Path(snapshots[0]['path']).parent.glob('.snapshot-*'))


def test_failed_snapshot_publish_leaves_no_partial_final(tmp_path, monkeypatch):
    transport, _, file = make_transport(tmp_path, lambda *a, **kw: None)
    def fail(*args, **kwargs):
        raise OSError('simulated publish failure')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'link', fail)
        with pytest.raises(OSError, match='simulated publish failure'):
            transport._stage(file, ATTEMPT)
    staged = list((tmp_path / 'delivery' / 'files').rglob('*'))
    assert not any(path.is_file() for path in staged)
    result = transport._stage(file, ATTEMPT)
    assert Path(result['path']).read_bytes() == b'verified report'


@pytest.mark.parametrize('kind', ['foreign_attempt', 'symlink', 'attempt_symlink', 'oversize'])
def test_invalid_attachment_prevents_even_summary_delivery(tmp_path, kind):
    calls = []
    transport, message, file = make_transport(tmp_path, lambda *a, **kw: calls.append(a))
    if kind == 'foreign_attempt':
        other = tmp_path/'attempts'/('b'*32)
        other.mkdir(); (other/'private.md').write_text('other job')
        message['artifacts'] = [str(other/'private.md')]
    elif kind == 'symlink':
        file.unlink(); file.symlink_to('/etc/hostname')
    elif kind == 'attempt_symlink':
        file.unlink()
        file.parent.rmdir()
        other = tmp_path/'attempts'/('b'*32)
        other.mkdir(); (other/'report.md').write_text('other job')
        file.parent.symlink_to(other, target_is_directory=True)
    else:
        with file.open('wb') as out:
            out.truncate(16*1024*1024+1)
    with pytest.raises((ValueError, OSError)):
        transport.deliver(ADDRESS, message, idempotency_key='job')
    assert not calls


def test_two_consumers_cannot_send_the_same_inflight_operation(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def sender(address, item, **kwargs):
        calls.append(item['kind'])
        entered.set(); assert release.wait(3)
        return dict(ok=True, message_id='receipt-'+item['kind'])
    first, message, _ = make_transport(tmp_path, sender)
    second, _, _ = make_transport(tmp_path, sender)
    thread = threading.Thread(target=first.deliver, args=(ADDRESS, message), kwargs=dict(idempotency_key='job'))
    thread.start()
    try:
        assert entered.wait(3)
        assert second.deliver(ADDRESS, message, idempotency_key='job')['deferred']
    finally:
        release.set(); thread.join(3)
    assert calls == ['text', 'file']


def test_different_profile_and_mutated_plan_cannot_reuse_delivery(tmp_path):
    calls = []
    transport, message, _ = make_transport(tmp_path, lambda *a, **kw: calls.append(a) or dict(ok=True, message_id='m'))
    assert transport.deliver(dict(ADDRESS, profile='other'), message, idempotency_key='job') is None
    assert not calls
    assert transport.deliver(ADDRESS, message, idempotency_key='job')['accepted']
    with pytest.raises(ValueError, match='plan conflict'):
        transport.deliver(ADDRESS, dict(message, summary='different contents'), idempotency_key='job')
    assert len(calls) == 2


def test_unknown_delivery_does_not_block_other_jobs(tmp_path):
    from agentcore.pool import Pool
    from agentcore.completion_backfill import drain
    pool = Pool(tmp_path/'queue')
    jobs = []
    for summary in ['uncertain', 'ready']:
        job = pool.submit(summary, {'goal':summary}, origin=ADDRESS)
        claimed, fd = pool.claim()
        try:
            assert pool.finish(job, claimed['attempt'], True, {'summary':summary})
        finally:
            os.close(fd)
        jobs.append(job)
    sent = []
    def sender(address, item, **kwargs):
        sent.append(item['text'])
        return None if item['text'] == 'uncertain' else dict(ok=True, message_id='confirmed')
    transport = DirectCompletionTransport(tmp_path/'delivery', profile='owner', sender=sender,
        render=lambda m:m['summary'], attempt_root=pool.root/'attempts', attempt_provider=lambda j:pool.status(j)['attempt'])
    outcome = drain(pool, transport, profile='owner')
    assert outcome['delivered'] == 1 and sent == ['uncertain','ready']
    assert not pool.delivery_record(jobs[0])['acknowledged']
    assert pool.delivery_record(jobs[1])['acknowledged']
    drain(pool, transport, profile='owner')
    assert sent == ['uncertain','ready']


def test_card_opt_in_marks_only_the_summary_item(tmp_path):
    calls = []
    def sender(address, item, **kwargs):
        calls.append(item)
        return dict(ok=True, message_id='platform-'+str(len(calls)))
    transport, message, _ = make_transport(tmp_path, sender, card=True)
    assert transport.deliver(ADDRESS, message, idempotency_key='job')['accepted']
    assert calls[0].get('card') is True
    assert 'card' not in calls[1]


def test_default_plan_has_no_card_marker(tmp_path):
    calls = []
    def sender(address, item, **kwargs):
        calls.append(item)
        return dict(ok=True, message_id='platform-'+str(len(calls)))
    transport, message, _ = make_transport(tmp_path, sender)
    assert transport.deliver(ADDRESS, message, idempotency_key='job')['accepted']
    assert 'card' not in calls[0]


def test_delivery_plan_retry_normalizes_empty_vs_missing_artifacts(tmp_path):
    import json
    calls = []
    def sender(address, item, **kwargs):
        calls.append(item)
        return dict(ok=True, message_id='platform-1')
    transport, _, _ = make_transport(tmp_path, sender)
    msg_without_artifacts = dict(task_id='job', status='completed', summary='done')
    # First delivery with message missing artifacts
    first = transport.deliver(ADDRESS, msg_without_artifacts, idempotency_key='job')
    assert first['accepted']

    # Verify plan was saved
    identity = json.dumps([transport.profile, ADDRESS['route'], 'job'])
    with transport.db() as db:
        saved_req = db.execute('SELECT request FROM plans WHERE key=?', (identity,)).fetchone()[0]
    saved_data = json.loads(saved_req)
    assert saved_data['message'].get('artifacts') == []

    # Second delivery with explicit empty artifacts: []
    msg_with_empty_artifacts = dict(task_id='job', status='completed', summary='done', artifacts=[])
    retry = transport.deliver(ADDRESS, msg_with_empty_artifacts, idempotency_key='job')
    assert retry['accepted']
    assert retry == first


def test_presend_not_sent_releases_outbox_lease_for_healthy_consumer(tmp_path):
    from agentcore.pool import Pool
    from agentcore.completion_backfill import drain

    pool = Pool(tmp_path / 'queue')
    job = pool.submit('job-yield', {'goal': 'summarize'}, origin=ADDRESS)
    claimed, fd = pool.claim()
    try:
        assert pool.finish(job, claimed['attempt'], True, {'summary': 'full report'})
    finally:
        os.close(fd)

    unhealthy_calls = []
    healthy_calls = []

    def unhealthy_sender(address, item, **kwargs):
        unhealthy_calls.append(item['text'])
        return dict(ok=False, not_sent=True, error='card_adapter_unavailable')

    def healthy_sender(address, item, **kwargs):
        healthy_calls.append(item['text'])
        return dict(ok=True, message_id='msg-ok-1')

    unhealthy = DirectCompletionTransport(
        tmp_path / 'delivery',
        profile='owner',
        sender=unhealthy_sender,
        render=lambda m: m['summary'],
        attempt_root=pool.root / 'attempts',
        attempt_provider=lambda j: pool.status(j)['attempt'],
        card=True,
    )
    healthy = DirectCompletionTransport(
        tmp_path / 'delivery',
        profile='owner',
        sender=healthy_sender,
        render=lambda m: m['summary'],
        attempt_root=pool.root / 'attempts',
        attempt_provider=lambda j: pool.status(j)['attempt'],
        card=True,
    )

    first = drain(pool, unhealthy, profile='owner', lease=60)
    assert first['delivered'] == 0
    assert first['outcomes'][0]['status'] == 'delivery_deferred'
    assert first['outcomes'][0].get('not_sent') is True
    # Lease must be released immediately so the healthy consumer does not wait 60s.
    outbox = pool.outbox_record(job)
    assert outbox['acknowledged'] == 0
    assert outbox['lease_token'] is None
    assert outbox['lease_until'] is None

    second = drain(pool, healthy, profile='owner', lease=60)
    assert second['delivered'] == 1
    assert healthy_calls == ['full report']
    assert pool.delivery_record(job)['acknowledged'] == 1



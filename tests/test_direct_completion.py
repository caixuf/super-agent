import os
from pathlib import Path
import threading

import pytest

from agentcore.completion_delivery import build_message
from agentcore.direct_completion import DirectCompletionTransport

ADDRESS = dict(profile='owner', session='session', route='route')
ATTEMPT = 'a'*32


def make_transport(tmp_path, sender):
    directory = tmp_path/'attempts'/ATTEMPT
    directory.mkdir(parents=True, exist_ok=True)
    file = directory/'report.md'
    if not file.exists():
        file.write_text('verified report')
    transport = DirectCompletionTransport(tmp_path/'delivery', profile='owner', sender=sender,
        render=lambda m: m['summary'], attempt_root=tmp_path/'attempts', attempt_provider=lambda job: ATTEMPT)
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


@pytest.mark.parametrize('kind', ['foreign_attempt', 'symlink', 'oversize'])
def test_invalid_attachment_prevents_even_summary_delivery(tmp_path, kind):
    calls = []
    transport, message, file = make_transport(tmp_path, lambda *a, **kw: calls.append(a))
    if kind == 'foreign_attempt':
        other = tmp_path/'attempts'/('b'*32)
        other.mkdir(); (other/'private.md').write_text('other job')
        message['artifacts'] = [str(other/'private.md')]
    elif kind == 'symlink':
        file.unlink(); file.symlink_to('/etc/hostname')
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

"""完成回执投递：地址来自提交时归属，ACK 只认显式回执。

这些用例守的是「不许猜会话、不许假 ACK」两条线——它们出错时不会抛异常，
只会把某个人的报告发到另一个人的聊天窗口里。
"""
from __future__ import annotations

import os
import threading

import pytest

from agentcore.completion_delivery import (build_message, deliver_once, deliver_pending,
                                           inspect_pending)
from agentcore.ops_alerts import DeliveryReceipt
from agentcore.pool import Pool

ORIGIN = {'profile': 'prof-1', 'session': 'sess-1', 'route': 'route-1'}


class Transport:
    """Records what it was asked to deliver and answers with an explicit receipt."""

    _UNSET = object()

    def __init__(self, *, accepted=True, receipt_id='rc-1', event_id='echo', raise_exc=None,
                 response=_UNSET):
        self.calls, self.accepted, self.receipt_id = [], accepted, receipt_id
        self.event_id, self.raise_exc, self.response = event_id, raise_exc, response

    def deliver(self, address, message, *, idempotency_key):
        self.calls.append({'address': dict(address), 'message': dict(message),
                           'idempotency_key': idempotency_key})
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.response is not Transport._UNSET:
            return self.response
        event = idempotency_key if self.event_id == 'echo' else self.event_id
        return DeliveryReceipt(accepted=self.accepted, event_id=event, receipt_id=self.receipt_id)


def finished_job(pool, key='task-1', *, result=None, origin=ORIGIN, ok=True):
    job = pool.submit(key, {'goal': 'analyse'}, origin=origin)
    claimed, fd = pool.claim()
    try:
        assert pool.finish(claimed['id'], claimed['attempt'], ok,
                           {'summary': 'the failure is a timeout'} if result is None else result)
    finally:
        os.close(fd)
    return job


def test_delivers_to_the_persisted_origin_and_acknowledges(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool)
    outcome = deliver_once(pool, transport)
    assert outcome == {'status': 'delivered', 'task_id': job, 'receipt_id': 'rc-1'}
    assert transport.calls[0]['address'] == ORIGIN
    assert transport.calls[0]['idempotency_key'] == job
    assert transport.calls[0]['message']['summary'] == 'the failure is a timeout'
    record = pool.delivery_record(job)
    assert record['acknowledged'] == 1 and record['receipt_id'] == 'rc-1'
    assert record['delivered'] is not None
    # The queue is drained: a second pass must not deliver twice.
    assert deliver_once(pool, transport) == {'status': 'empty'}
    assert len(transport.calls) == 1


def test_activation_cutoff_holds_old_backlog_without_blocking_new_results(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    old = finished_job(pool, key='old')
    recent = finished_job(pool, key='recent')
    with pool.db() as db:
        db.execute('UPDATE jobs SET created=? WHERE id=?', (10, old))
        db.execute('UPDATE jobs SET created=? WHERE id=?', (30, recent))
    outcome = deliver_once(pool, transport, not_before=20)
    assert outcome['task_id'] == recent and outcome['status'] == 'delivered'
    assert not pool.delivery_record(old)['acknowledged']
    assert deliver_once(pool, transport, not_before=20)['status'] == 'empty'


def test_task_without_origin_is_refused_and_left_pending(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool, origin=None)
    outcome = deliver_once(pool, transport)
    assert outcome['status'] == 'unowned' and outcome['task_id'] == job
    assert transport.calls == []  # never guessed a conversation
    assert pool.delivery_record(job)['acknowledged'] == 0
    # Still visible for an operator, not silently dropped.
    assert [receipt['job'] for receipt in pool.receipts()] == [job]


def test_delivery_does_not_rewrite_task_state(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool, result={'summary': 'done'})
    before = pool.status(job)
    deliver_once(pool, transport)
    assert pool.status(job) == before


def test_rejected_delivery_is_not_acknowledged(tmp_path):
    pool = Pool(tmp_path)
    job = finished_job(pool)
    outcome = deliver_once(pool, Transport(accepted=False))
    assert outcome['status'] == 'delivery_rejected'
    assert pool.delivery_record(job)['acknowledged'] == 0


@pytest.mark.parametrize('transport', [
    Transport(receipt_id=''),
    Transport(event_id='some-other-task'),
    Transport(response={'accepted': True}),
    Transport(response=True),
    Transport(response=None),
    Transport(response=[]),
])
def test_unproven_acceptance_never_acknowledges(tmp_path, transport):
    pool = Pool(tmp_path)
    job = finished_job(pool)
    outcome = deliver_once(pool, transport)
    assert outcome['status'] == 'delivery_unknown'
    assert pool.delivery_record(job)['acknowledged'] == 0


def test_explicit_rejection_is_distinct_from_unknown(tmp_path):
    pool = Pool(tmp_path)
    job = finished_job(pool)
    outcome = deliver_once(pool, Transport(response={'accepted': True, 'event_id': job,
                                                    'receipt_id': 'rc', 'success': False}))
    assert outcome['status'] == 'delivery_rejected'
    assert pool.delivery_record(job)['acknowledged'] == 0


def test_transport_exception_stays_pending_and_is_reported_unknown(tmp_path):
    pool = Pool(tmp_path)
    job = finished_job(pool)
    outcome = deliver_once(pool, Transport(raise_exc=ConnectionResetError('boom')), lease=0.0001)
    assert outcome['status'] == 'delivery_unknown'
    assert outcome['error_type'] == 'ConnectionResetError'
    assert pool.delivery_record(job)['acknowledged'] == 0
    # The lease expires, the retry re-sends: at-least-once, and it is documented.
    retry = Transport()
    assert deliver_once(pool, retry)['status'] == 'delivered'
    assert len(retry.calls) == 1


def test_lost_ack_is_reported_and_retried(tmp_path, monkeypatch):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool)
    original = pool.acknowledge

    def explode(*args, **kwargs):
        raise OSError('database went away')

    monkeypatch.setattr(pool, 'acknowledge', explode)
    outcome = deliver_once(pool, transport, lease=0.0001)
    assert outcome['status'] == 'ack_unknown' and outcome['receipt_id'] == 'rc-1'
    monkeypatch.setattr(pool, 'acknowledge', original)
    # The send already happened; the retry must re-send. That duplicate is the
    # at-least-once cost, not a bug this module can hide.
    assert deliver_once(pool, Transport())['status'] == 'delivered'
    assert pool.delivery_record(job)['acknowledged'] == 1


def test_two_consumers_never_deliver_the_same_completion(tmp_path):
    pool = Pool(tmp_path)
    finished_job(pool)
    transports = [Transport(), Transport()]
    results, lock = [], threading.Lock()

    def run(transport):
        outcome = deliver_once(pool, transport)
        with lock:
            results.append(outcome['status'])

    threads = [threading.Thread(target=run, args=(transport,)) for transport in transports]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ['delivered', 'empty']
    assert sum(len(transport.calls) for transport in transports) == 1


def test_no_transport_configured_is_explicit(tmp_path):
    pool = Pool(tmp_path)
    finished_job(pool)
    assert deliver_once(pool, None) == {'status': 'unavailable', 'reason': 'no transport configured'}


def test_drain_stops_at_the_first_non_delivered_outcome(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    finished_job(pool, key='a')
    finished_job(pool, key='b')
    outcomes = deliver_pending(pool, transport)
    assert [outcome['status'] for outcome in outcomes] == ['delivered', 'delivered']
    # A blocked head must not spin: one attempt, then report.
    pending_pool, blocked = Pool(tmp_path / 'blocked'), Transport(accepted=False)
    finished_job(pending_pool, key='c')
    finished_job(pending_pool, key='d')
    assert [outcome['status'] for outcome in deliver_pending(pending_pool, blocked)] == \
        ['delivery_rejected']
    assert len(blocked.calls) == 1


def test_summary_is_bounded_and_marks_truncation():
    message = build_message({'job': 'a' * 32, 'status': 'completed',
                             'result': {'summary': 'x' * 5000}}, limit=100)
    assert message.truncated is True
    assert message.summary.startswith('x' * 100)
    assert 'truncated' in message.summary
    assert len(message.summary) <= 100 + 64
    plain = build_message({'job': 'b' * 32, 'status': 'failed', 'result': {'reason': 'runner failed'}})
    assert plain.summary == 'runner failed' and plain.truncated is False
    assert build_message({'job': 'c' * 32, 'status': 'completed', 'result': None}).summary == 'completed'


def test_failed_task_is_also_delivered(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = finished_job(pool, result={'reason': 'runner failed'}, ok=False)
    assert deliver_once(pool, transport)['status'] == 'delivered'
    assert transport.calls[0]['message']['status'] == 'failed'
    assert transport.calls[0]['message']['summary'] == 'runner failed'
    assert pool.status(job)['status'] == 'failed'


def test_malformed_receipt_is_not_acknowledged(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    with pool.db() as db:
        db.execute("INSERT INTO outbox(job,receipt) VALUES(?,?)", ('broken', '{"status":"completed"}'))
    assert deliver_once(pool, transport)['status'] == 'invalid_receipt'
    assert transport.calls == []


def test_receipt_id_must_be_meaningful_when_recorded(tmp_path):
    pool = Pool(tmp_path)
    job = finished_job(pool)
    claim = pool.claim_receipt()
    with pytest.raises(ValueError):
        pool.acknowledge(job, claim['token'], receipt_id='  ')


def test_dry_run_lists_addresses_without_claiming_or_acking(tmp_path):
    pool = Pool(tmp_path)
    owned = finished_job(pool, key='owned')
    unowned = finished_job(pool, key='unowned', origin=None)
    rows = inspect_pending(pool)
    assert [row['task_id'] for row in rows] == [owned, unowned]
    assert rows[0]['origin'] == ORIGIN and rows[0]['status'] == 'completed'
    assert rows[1]['origin'] is None
    # Nothing was claimed or acknowledged, so the real consumer still gets its turn.
    assert len(pool.receipts()) == 2
    assert deliver_once(pool, Transport())['status'] == 'delivered'


def test_colleague_identity_comes_from_persisted_job_payload(tmp_path):
    pool, transport = Pool(tmp_path), Transport()
    job = pool.submit('colleague-task', {'goal': 'analyse',
                                         '_colleague_role': 'evidence',
                                         '_colleague_name': '阿证',
                                         '_colleague_label': '证据分析'}, origin=ORIGIN)
    claimed, fd = pool.claim()
    try:
        pool.finish(claimed['id'], claimed['attempt'], True, {'summary': 'checked'})
    finally:
        os.close(fd)
    assert deliver_once(pool, transport)['status'] == 'delivered'
    message = transport.calls[0]['message']
    assert message['task_id'] == job
    assert message['colleague_role'] == 'evidence'
    assert message['colleague_name'] == '阿证'
    assert message['colleague_label'] == '证据分析'


def test_build_message_rejects_unbounded_colleague_labels():
    with pytest.raises(ValueError):
        build_message({'job': 'a' * 32, 'status': 'completed'},
                      colleague_role='x' * 65)
    with pytest.raises(ValueError):
        build_message({'job': 'a' * 32, 'status': 'completed'},
                      colleague_name='x' * 81)

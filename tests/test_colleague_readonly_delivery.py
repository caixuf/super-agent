import threading
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentcore.colleague_readonly_delivery import ReadOnlyDelivery
from test_colleague_readonly_queue import env


class Transport:
    def __init__(self):
        self.sends = []
        self.receipt = None

    def send_plaintext(self, *, origin, delivery_key, payload):
        self.sends.append((origin, delivery_key, payload))
        self.receipt = {'origin': origin, 'delivery_key': delivery_key, 'receipt_id': 'platform-123'}
        return self.receipt

    def lookup(self, *, origin, delivery_key):
        return self.receipt


def completed(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = queue.submit(**args)['job_id']
    assert queue.run_once()['job_state'] == 'completed'
    return job


def test_explicit_matching_receipt_acks_queue_and_settles_delivery_reservation(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    transport = Transport()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    result = sender.deliver(job)
    assert result['state'] == 'delivered'
    assert len(transport.sends) == 1
    origin, key, payload = transport.sends[0]
    assert origin == args['origin'] and payload['member_id'] == 'alice'
    assert payload['job_id'] == job and key == 'readonly-final:' + payload['task_id']
    assert queue.pool.delivery_record(job)['acknowledged'] == 1
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0
    assert sender.deliver(job)['state'] == 'delivered'
    assert len(transport.sends) == 1


def test_unknown_send_is_parked_and_only_matching_lookup_can_ack(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    class Unknown(Transport):
        def send_plaintext(self, *, origin, delivery_key, payload):
            self.sends.append((origin, delivery_key, payload))
            raise TimeoutError('outcome unknown')
    transport = Unknown()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'unknown'
    assert sender.deliver(job)['state'] == 'unknown' and len(transport.sends) == 1
    assert sender.reconcile(job)['state'] == 'unknown'
    origin, key, _ = transport.sends[0]
    transport.receipt = {'origin': {**origin, 'route': 'wrong'}, 'delivery_key': key, 'receipt_id': 'p'}
    assert sender.reconcile(job)['state'] == 'unknown'
    assert not queue.pool.delivery_record(job)['acknowledged']
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 1
    transport.receipt = {'origin': origin, 'delivery_key': key, 'receipt_id': 'p'}
    assert sender.reconcile(job)['state'] == 'delivered'
    assert queue.pool.delivery_record(job)['acknowledged'] == 1
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0
    assert len(transport.sends) == 1


def test_two_senders_cannot_send_same_outbox_receipt(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    entered, release = threading.Event(), threading.Event()
    class Slow(Transport):
        def send_plaintext(self, **kwargs):
            entered.set()
            assert release.wait(10)
            return super().send_plaintext(**kwargs)
    transport = Slow()
    first = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    second = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.deliver, job)
        assert entered.wait(5)
        assert second.deliver(job)['state'] == 'sending'
        release.set()
        assert future.result(timeout=10)['state'] == 'delivered'
    assert len(transport.sends) == 1


def test_valid_receipt_then_reservation_failure_is_reconciled_without_resend(env, monkeypatch):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    transport = Transport()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    actual, attempts = reg.settle_work, []
    def fail_once(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError('temporary failure')
        return actual(**kwargs)
    monkeypatch.setattr(reg, 'settle_work', fail_once)
    assert sender.deliver(job)['state'] == 'delivered_pending_settlement'
    assert queue.pool.delivery_record(job)['acknowledged'] == 1
    assert sender.deliver(job)['state'] == 'delivered'
    assert len(transport.sends) == 1
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0


def test_wrong_owner_and_existing_lease_cannot_send(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    transport = Transport()
    foreign = ReadOnlyDelivery(queue, owner={'profile': 'leader', 'session': 'other'}, transport=transport, literal_text_only=True, no_implicit_retries=True)
    with pytest.raises(PermissionError, match='queue_owner_mismatch'):
        foreign.deliver(job)
    claim = queue.pool.claim_receipt(lease=60)
    assert claim['receipt']['job'] == job
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'busy'
    assert not transport.sends


def test_unattested_transport_and_outside_ack_cannot_complete_delivery(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    transport = Transport()
    with pytest.raises(ValueError, match='host_verified_literal_text_transport_required'):
        ReadOnlyDelivery(queue, owner=args['owner'], transport=transport)
    with pytest.raises(ValueError, match='host_verified_no_implicit_retries_required'):
        ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True)
    claim = queue.pool.claim_receipt()
    assert queue.pool.acknowledge(job, claim['token'], receipt_id='untrusted-other-consumer')
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    with pytest.raises(ValueError, match='unclaimed_outbox_required'):
        sender.deliver(job)
    assert not transport.sends
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 1


def test_process_crash_after_send_is_unknown_until_platform_lookup(env, tmp_path):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    receipt_path = tmp_path / 'simulated-platform-receipt.json'
    code = '''
import json, os, sys
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_readonly_queue import ReadOnlyQueue
from agentcore.colleague_readonly_delivery import ReadOnlyDelivery
from agentcore.pool import Pool
reg = Registry(sys.argv[1], profile_id='team', primary_id='leader')
runtime = ReadOnlyColleagueRuntime(reg, Supervisor('team'), state_dir=sys.argv[2],
    policy=json.loads(sys.argv[3]), model=lambda *args, **kwargs: None, authorize_owner=lambda owner: True)
queue = ReadOnlyQueue(Pool(sys.argv[4]), runtime, authorize_assignment=lambda *args: True, dedicated=True)
class Transport:
    def send_plaintext(self, *, origin, delivery_key, payload):
        with open(sys.argv[7], 'w') as f:
            json.dump({'origin':origin, 'delivery_key':delivery_key, 'receipt_id':'platform-123'}, f)
            f.flush()
            os.fsync(f.fileno())
        os._exit(77)
    def lookup(self, **kwargs):
        return None
ReadOnlyDelivery(queue, owner=json.loads(sys.argv[5]), transport=Transport(),
                 literal_text_only=True, no_implicit_retries=True, lease=1).deliver(sys.argv[6])
'''
    proc = subprocess.run([sys.executable, '-c', code, str(reg.path), str(runtime.root),
                           json.dumps(runtime.policy), str(queue.pool.root), json.dumps(args['owner']),
                           job, str(receipt_path)], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 77, proc.stderr
    with queue.pool.db() as db:
        db.execute('UPDATE readonly_deliveries SET lease_until=0 WHERE job=?', (job,))
        db.execute('UPDATE outbox SET lease_until=0 WHERE job=?', (job,))
    transport = Transport()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'unknown'
    assert not transport.sends and not queue.pool.delivery_record(job)['acknowledged']
    transport.receipt = json.loads(receipt_path.read_text())
    assert sender.reconcile(job)['state'] == 'delivered'
    assert not transport.sends and queue.pool.delivery_record(job)['acknowledged']
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0


def test_lost_outbox_lease_requires_lookup_and_never_repeats_send(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    class LostLease(Transport):
        def send_plaintext(self, **kwargs):
            response = super().send_plaintext(**kwargs)
            with queue.pool.db() as db:
                db.execute("UPDATE outbox SET lease_token='other-consumer',lease_until=9999999999 WHERE job=?", (job,))
            return response
    transport = LostLease()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'unknown'
    assert sender.reconcile(job)['state'] == 'busy'
    assert not queue.pool.delivery_record(job)['acknowledged']
    with queue.pool.db() as db:
        db.execute('UPDATE outbox SET lease_until=0 WHERE job=?', (job,))
    assert sender.reconcile(job)['state'] == 'delivered'
    assert len(transport.sends) == 1
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0


def test_wrong_key_from_send_is_not_acknowledged(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = completed(env)
    class WrongKey(Transport):
        def send_plaintext(self, **kwargs):
            result = super().send_plaintext(**kwargs)
            return {**result, 'delivery_key': 'not-the-task'}
    transport = WrongKey()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport, literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'unknown'
    assert not queue.pool.delivery_record(job)['acknowledged']
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 1


def test_cancelled_before_start_sends_primary_status_and_settles_both_reservations(env):
    queue, runtime, reg, leader, host, args, calls = env
    job = queue.submit(**args)['job_id']
    queue.pool.cancel(job)
    assert queue.run_once()['state'] == 'empty'
    assert queue.pool.status(job)['status'] == 'cancelled'
    assert not calls
    transport = Transport()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport,
                              literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'delivered'
    assert len(transport.sends) == 1
    payload = transport.sends[0][2]
    assert payload['sender'] == 'primary_status' and '已取消' in payload['text']
    assert '只读分析结果' not in payload['text']
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0
    assert queue.pool.delivery_record(job)['acknowledged'] == 1


def test_failed_task_sends_fixed_primary_status_and_waits_for_receipt(env, tmp_path):
    queue, runtime, reg, leader, host, args, calls = env
    outside = tmp_path / 'outside.py'
    outside.write_text('secret synthetic data\n')
    job = queue.submit(**{**args, 'sources': [{'path': str(outside)}]})['job_id']
    for _ in range(3):
        queue.run_once()
    assert queue.pool.status(job)['status'] == 'failed' and not calls
    class Timeout(Transport):
        def send_plaintext(self, *, origin, delivery_key, payload):
            self.sends.append((origin, delivery_key, payload))
            raise TimeoutError()
    transport = Timeout()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport,
                              literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'unknown'
    origin, key, payload = transport.sends[0]
    assert payload['sender'] == 'primary_status' and '未完成' in payload['text']
    assert 'secret' not in payload['text'] and 'outside.py' not in payload['text']
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 1
    transport.receipt = {'origin': origin, 'delivery_key': key, 'receipt_id': 'status-123'}
    assert sender.reconcile(job)['state'] == 'delivered'
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0


def test_cancelled_after_crash_never_settles_unknown_execution(env):
    queue, runtime, reg, leader, host, args, calls = env
    runtime.model = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit('crash'))
    job = queue.submit(**args)['job_id']
    with pytest.raises(SystemExit):
        queue.run_once(lease=0.01)
    queue.pool.cancel(job)
    time.sleep(0.03)
    queue.run_once(lease=0.01)
    assert queue.pool.status(job)['status'] == 'cancelled'
    transport = Transport()
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport,
                              literal_text_only=True, no_implicit_retries=True)
    assert sender.deliver(job)['state'] == 'delivered_pending_execution_reconciliation'
    assert queue.pool.delivery_record(job)['acknowledged'] == 1
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 1
    assert len(transport.sends) == 1
    runtime.recover_uncertain(owner=args['owner'], request_id=args['request_id'], confirmed_stopped=True)
    assert sender.deliver(job)['state'] == 'delivered'
    assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0
    assert len(transport.sends) == 1

import concurrent.futures
import threading
import time

import pytest

from agentcore.colleague_workflow_delivery import WorkflowDelivery
from agentcore.colleague_workflow_store import WorkflowStore

OWNER = {'profile': 'p', 'session': 's'}
ORIGIN = {'profile': 'p', 'session': 's', 'route': 'chat', 'channel': 'test'}
STAGES = [{'role': role, 'goal': 'Read data', 'sources': [{'path': '/allowed/file'}]}
          for role in ('analyst', 'reviewer')]


class Transport:
    def __init__(self):
        self.sends = []
        self.lookups = []
        self.receipt = None

    def send(self, **kwargs):
        self.sends.append(kwargs)
        self.receipt = {'origin': kwargs['origin'], 'delivery_key': kwargs['delivery_key'],
                        'receipt_id': 'platform-message-1'}
        return self.receipt

    def lookup(self, **kwargs):
        self.lookups.append(kwargs)
        return self.receipt


def make(tmp_path, *, completed_children=False):
    store = WorkflowStore(tmp_path)
    wid = store.create(OWNER, 'request', STAGES, ORIGIN)
    if not completed_children:
        store.cancel(OWNER, wid)
    else:
        for i in range(2):
            stage = store.prepare_next(OWNER, wid)['stage']
            child = str(i + 1) * 32
            store.bind_child(OWNER, wid, i, stage['child_key'], child)
            store.finish(OWNER, wid, i, child, {'attempt_id': 'a' * 32,
                'report_sha256': 'b' * 64, 'source_manifest_sha256': 'c' * 64,
                'summary': 'Untrusted clue', 'verification': 'citation_traceability_only'})
    return store, wid


def test_waits_for_all_child_acks_and_sends_once_to_bound_origin(tmp_path):
    store, wid = make(tmp_path, completed_children=True)
    transport = Transport()
    delivery = WorkflowDelivery(store, OWNER, ORIGIN, transport)
    assert delivery.deliver(wid)['state'] == 'waiting_for_child_ack'
    store.mark_child_ack(OWNER, wid, 0, '1' * 32, 'internal-1')
    assert delivery.deliver(wid)['state'] == 'waiting_for_child_ack'
    assert not transport.sends
    store.mark_child_ack(OWNER, wid, 1, '2' * 32, 'internal-2')
    result = delivery.deliver(wid)
    assert result['state'] == 'delivered' and result['receipt_id'] == 'platform-message-1'
    assert transport.sends[0]['origin'] == ORIGIN
    assert transport.sends[0]['payload']['workflow_id'] == wid
    assert transport.sends[0]['delivery_key'] == result['delivery_key']
    reopened = WorkflowDelivery(WorkflowStore(tmp_path), OWNER, ORIGIN, transport)
    assert reopened.deliver(wid)['state'] == 'delivered'
    assert len(transport.sends) == 1


def test_owner_and_full_route_must_match_before_claim(tmp_path):
    store, wid = make(tmp_path)
    transport = Transport()
    wrong_route = WorkflowDelivery(store, OWNER, {**ORIGIN, 'route': 'other'}, transport)
    with pytest.raises(PermissionError):
        wrong_route.deliver(wid)
    other = {'profile': 'p', 'session': 'other'}
    wrong_owner = WorkflowDelivery(store, other, {**ORIGIN, **other}, transport)
    with pytest.raises(PermissionError):
        wrong_owner.deliver(wid)
    assert store.inspect_final(OWNER, wid)['state'] == 'pending'
    assert not transport.sends


def test_not_terminal_does_not_send(tmp_path):
    store = WorkflowStore(tmp_path)
    wid = store.create(OWNER, 'request', STAGES, ORIGIN)
    transport = Transport()
    assert WorkflowDelivery(store, OWNER, ORIGIN, transport).deliver(wid)['state'] == 'not_terminal'
    assert not transport.sends


def test_concurrent_claims_have_only_one_sender(tmp_path):
    store, wid = make(tmp_path)
    entered, release = threading.Event(), threading.Event()
    class BlockingTransport(Transport):
        def send(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().send(**kwargs)
    transport = BlockingTransport()
    one = WorkflowDelivery(store, OWNER, ORIGIN, transport)
    two = WorkflowDelivery(WorkflowStore(tmp_path), OWNER, ORIGIN, transport)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(one.deliver, wid)
        assert entered.wait(5)
        second = two.deliver(wid)
        assert second['state'] == 'sending' and 'token' not in second
        release.set()
        assert first.result(timeout=5)['state'] == 'delivered'
    assert len(transport.sends) == 1


@pytest.mark.parametrize('corrupt', [
    lambda r: None,
    lambda r: {**r, 'receipt_id': ''},
    lambda r: {**r, 'delivery_key': 'wrong'},
    lambda r: {**r, 'origin': {**r['origin'], 'route': 'wrong'}},
])
def test_unverified_send_receipt_goes_unknown_without_retry(tmp_path, corrupt):
    store, wid = make(tmp_path)
    class Corrupt(Transport):
        def send(self, **kwargs):
            return corrupt(super().send(**kwargs))
    transport = Corrupt()
    delivery = WorkflowDelivery(store, OWNER, ORIGIN, transport)
    assert delivery.deliver(wid)['state'] == 'unknown'
    assert delivery.deliver(wid)['state'] == 'unknown'
    assert len(transport.sends) == 1
    assert delivery.reconcile(wid)['state'] == 'delivered'
    assert len(transport.sends) == 1


def test_timeout_then_reconcile_requires_matching_platform_proof(tmp_path):
    store, wid = make(tmp_path)
    class Timeout(Transport):
        def send(self, **kwargs):
            super().send(**kwargs)
            raise TimeoutError('do not expose this secret')
    transport = Timeout()
    delivery = WorkflowDelivery(store, OWNER, ORIGIN, transport)
    assert delivery.deliver(wid)['state'] == 'unknown'
    original = transport.receipt
    transport.receipt = {**original, 'origin': {**ORIGIN, 'route': 'other'}}
    assert delivery.reconcile(wid)['state'] == 'unknown'
    transport.receipt = None
    assert delivery.reconcile(wid)['state'] == 'unknown'
    transport.receipt = original
    assert delivery.reconcile(wid)['state'] == 'delivered'
    assert delivery.reconcile(wid)['state'] == 'delivered'
    assert len(transport.sends) == 1


def test_crash_after_send_before_ack_never_blindly_resends(tmp_path, monkeypatch):
    store, wid = make(tmp_path)
    class Crash(Transport):
        def send(self, **kwargs):
            super().send(**kwargs)
            raise KeyboardInterrupt()
    transport = Crash()
    delivery = WorkflowDelivery(store, OWNER, ORIGIN, transport, lease=1)
    with pytest.raises(KeyboardInterrupt):
        delivery.deliver(wid)
    real_time = time.time
    monkeypatch.setattr('agentcore.colleague_workflow_store.time.time', lambda: real_time() + 2)
    reopened = WorkflowDelivery(WorkflowStore(tmp_path), OWNER, ORIGIN, transport)
    assert reopened.deliver(wid)['state'] == 'unknown'
    assert reopened.reconcile(wid)['state'] == 'delivered'
    assert len(transport.sends) == 1


def test_receipt_after_lease_expiry_requires_independent_lookup(tmp_path, monkeypatch):
    store, wid = make(tmp_path)
    real_time = time.time
    class Slow(Transport):
        def send(self, **kwargs):
            receipt = super().send(**kwargs)
            monkeypatch.setattr('agentcore.colleague_workflow_store.time.time', lambda: real_time() + 2)
            return receipt
    transport = Slow()
    delivery = WorkflowDelivery(store, OWNER, ORIGIN, transport, lease=1)
    assert delivery.deliver(wid)['state'] == 'unknown'
    assert delivery.reconcile(wid)['state'] == 'delivered'
    assert len(transport.sends) == 1

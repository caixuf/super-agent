"""Single-owner, receipt-backed delivery for the dedicated read-only queue.

The queue's own Pool outbox remains the durable terminal receipt. This adapter
claims it directly, sends at most once and acknowledges it in the same database
transaction as its delivery state. An expired/inconclusive send is parked as
unknown; only an independent, exact platform lookup can reconcile it. It never
uses the generic backfill and cannot manufacture evidence of platform receipt.

The host must supply an authenticated owner and a trusted transport, protect the
queue, and ensure this adapter is its only sender. Other processes with database
access can still bypass this adapter and ACK directly; no database-enforced
sender exclusivity is claimed. The transport must actually prove that a message
with the same fixed origin and key was accepted. The host must independently
verify that send_plaintext displays literal text without interpreting mentions,
links, markup or embedded commands, and that the adapter does not blindly retry
ambiguous sends internally. No current platform adapter, attachment
handling, scheduler or production wiring is here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import time
import uuid

from agentcore.colleague_readonly_queue import ReadOnlyQueue
from agentcore.colleague_dialogue import _name
from agentcore.pool import normalize_origin


class ReadOnlyDelivery:
    def __init__(self, queue, *, owner, transport, literal_text_only=False,
                 no_implicit_retries=False, lease=60):
        if not isinstance(queue, ReadOnlyQueue):
            raise ValueError('trusted_readonly_queue_required')
        if (isinstance(lease, bool) or not isinstance(lease, (int, float))
                or not 1 <= lease <= 3600):
            raise ValueError('invalid_delivery_lease')
        if literal_text_only is not True:
            raise ValueError('host_verified_literal_text_transport_required')
        if no_implicit_retries is not True:
            raise ValueError('host_verified_no_implicit_retries_required')
        if not callable(getattr(transport, 'send_plaintext', None)) or not callable(getattr(transport, 'lookup', None)):
            raise ValueError('receipt_capable_transport_required')
        self.queue, self.owner = queue, queue.runtime._owner(owner)
        self.transport, self.lease = transport, lease
        self.queue.pool.init_readonly_deliveries_schema()

    def _view(self, job):
        view = self.queue.inspect_completion(owner=self.owner, job_id=job)
        if view is None:
            return None
        if view['state'] in ('failed', 'cancelled'):
            # Fixed wording from trusted queue terminal state, never model text.
            # It is a primary status update, not a colleague's successful reply.
            row = self.queue.pool.job_outbox_record(job)
            payload = json.loads(row['payload'])
            receipt = json.loads(row['receipt'])
            if (row['status'] != view['state'] or receipt.get('job') != job
                    or receipt.get('status') != view['state']):
                raise ValueError('queue_terminal_binding_mismatch')
            owner, origin, member = self.queue._binding(payload['owner'], payload['origin'],
                                                         payload['member_id'], payload['generation'])
            _, task = self.queue.runtime._identity(owner, payload['request_id'])
            if owner != self.owner or task != payload['runtime_task_id']:
                raise PermissionError('queue_owner_mismatch')
            label = '已取消' if view['state'] == 'cancelled' else '未完成'
            presentation = {'task_id': task, 'delivery_key': 'readonly-final:' + task,
                            'sender': 'primary_status', 'member_id': member['member_id'],
                            'generation': member['generation'], 'role_id': member['role_id'],
                            'text': ('主 Bot 状态｜任务 ' + task + '｜' + label + '\n'
                                     '负责同事：' + _name(member['display_name']) + '（'
                                     + member['member_id'] + '）')}
            return {'job_id': job, 'state': view['state'], 'origin': origin,
                    'request_id': payload['request_id'], 'presentation': presentation}
        if view['state'] != 'completed':
            return view
        presentation = view['presentation']
        if (presentation.get('sender') != 'primary_relay'
                or not isinstance(presentation.get('delivery_key'), str)
                or not isinstance(presentation.get('text'), str)
                or not presentation['text']):
            raise ValueError('invalid_readonly_presentation')
        return view

    @staticmethod
    def _receipt(value, origin, key):
        try:
            valid = (isinstance(value, dict) and set(value) == {'origin', 'delivery_key', 'receipt_id'}
                     and value['delivery_key'] == key
                     and normalize_origin(value['origin']) == normalize_origin(origin)
                     and isinstance(value['receipt_id'], str) and value['receipt_id'].strip()
                     and len(value['receipt_id']) <= 512)
        except (TypeError, ValueError):
            valid = False
        return value['receipt_id'] if valid else None

    def _settle(self, view):
        presentation = view['presentation']
        try:
            self.queue.runtime.registry.settle_work(
                supervisor=self.queue.runtime.supervisor, member_id=presentation['member_id'],
                generation=presentation['generation'], work_id='delivery:' + presentation['task_id'])
        except Exception:
            return 'settlement_pending'
        if view['state'] == 'completed':
            return 'settled'
        try:
            assignment = self.queue.runtime.inspect_assignment(owner=self.owner, request_id=view['request_id'])
            if assignment is not None and assignment['state'] == 'running':
                # A crash or timeout can leave an external model call unresolved.
                # Never infer its termination from the queue terminal alone.
                return 'execution_unknown'
            if assignment is not None and (assignment['task_id'] != presentation['task_id']
                    or assignment['member_id'] != presentation['member_id']
                    or assignment['generation'] != presentation['generation']):
                return 'execution_unknown'
            self.queue.runtime.registry.settle_work(
                supervisor=self.queue.runtime.supervisor, member_id=presentation['member_id'],
                generation=presentation['generation'], work_id=presentation['task_id'])
        except Exception:
            return 'settlement_pending'
        return 'settled'

    def _outcome(self, view, receipt_id):
        settled = self._settle(view)
        status = {'settled': 'delivered', 'settlement_pending': 'delivered_pending_settlement',
                  'execution_unknown': 'delivered_pending_execution_reconciliation'}[settled]
        return {'state': status,
                'job_id': view['job_id'], 'receipt_id': receipt_id}

    def _claim(self, job, key):
        return self.queue.pool.claim_readonly_delivery(job, key, self.lease)

    def _unknown(self, job, token):
        self.queue.pool.mark_readonly_delivery_unknown(job, token)

    def _ack_send(self, job, key, token, receipt_id):
        return self.queue.pool.acknowledge_readonly_delivery(job, key, token, receipt_id)

    def deliver(self, job):
        """Attempt one send; duplicate calls never blindly send a second time."""
        view = self._view(job)
        if view is None:
            return {'state': 'not_found'}
        if view['state'] not in ('completed', 'failed', 'cancelled'):
            return {'state': view['state'], 'job_id': job}
        p = view['presentation']
        key, origin = p['delivery_key'], view['origin']
        claim = self._claim(job, key)
        if claim['state'] == 'delivered':
            return self._outcome(view, claim['receipt_id'])
        if claim['state'] != 'claimed':
            return {'state': claim['state'], 'job_id': job}
        token = claim['token']
        payload = {'job_id': job, 'task_id': p['task_id'], 'sender': p['sender'], 'member_id': p['member_id'],
                   'generation': p['generation'], 'role_id': p['role_id'], 'text': p['text']}
        try:
            receipt = self.transport.send_plaintext(origin=deepcopy(origin), delivery_key=key, payload=payload)
        except Exception:
            self._unknown(job, token)
            return {'state': 'unknown', 'job_id': job}
        receipt_id = self._receipt(receipt, origin, key)
        if receipt_id is None or not self._ack_send(job, key, token, receipt_id):
            self._unknown(job, token)
            return {'state': 'unknown', 'job_id': job}
        return self._outcome(view, receipt_id)

    def reconcile(self, job):
        """Resolve unknown only by a fresh, matching platform receipt lookup."""
        view = self._view(job)
        if view is None:
            return {'state': 'not_found'}
        if view['state'] not in ('completed', 'failed', 'cancelled'):
            return {'state': view['state'], 'job_id': job}
        p, origin = view['presentation'], view['origin']
        key = p['delivery_key']
        record = self.queue.pool.get_readonly_delivery(job, expire_sending=True)
        if record is None:
            return {'state': 'not_attempted', 'job_id': job}
        if record['delivery_key'] != key:
            raise ValueError('delivery_binding_mismatch')
        if record['state'] == 'delivered':
            return self._outcome(view, record['receipt_id'])
        if record['state'] != 'unknown':
            return {'state': record['state'], 'job_id': job}
        try:
            receipt = self.transport.lookup(origin=deepcopy(origin), delivery_key=key)
        except Exception:
            receipt = None
        receipt_id = self._receipt(receipt, origin, key)
        if receipt_id is None:
            return {'state': 'unknown', 'job_id': job}
        reconciled = self.queue.pool.reconcile_readonly_delivery(job, key, receipt_id)
        if reconciled['state'] != 'delivered':
            return {'state': reconciled['state'], 'job_id': job}
        return self._outcome(view, reconciled['receipt_id'])

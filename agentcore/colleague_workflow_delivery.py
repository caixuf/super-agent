"""One-shot, receipt-backed delivery for a completed colleague workflow.

This module neither discovers a recipient nor implements a platform client.
The host must construct it using the authenticated original owner and its fixed
route, provide a trusted platform transport, and make this outbox the *only*
external delivery owner. Model output, payload text and receipt fields are not
routing authority. All worker receipt acknowledgements are gated by the store.

Transport ``send(origin=..., delivery_key=..., payload=...)`` and
``lookup(origin=..., delivery_key=...)`` must return a platform-verified receipt
mapping ``{'origin': origin, 'delivery_key': key, 'receipt_id': message_id}``.
Lookup is read-only and must prove the existing message belongs to that exact
origin and key; it returns None when unavailable. These are trusted adapters,
not LLM-generated assertions. A platform that cannot correlate the key and
route cannot safely reconcile an ambiguous send through this interface.

A timeout, invalid receipt, process crash, or lost lease leaves the result
unknown and never triggers a resend. A durable platform receipt is necessary
for confirmation; remote delivery and local persistence cannot be atomic.
"""
from __future__ import annotations

from copy import deepcopy
import json

from agentcore.colleague_workflow_store import WorkflowConflict, WorkflowStore
from agentcore.pool import normalize_origin


class WorkflowDelivery:
    """Deliver only to a host-supplied owner-bound fixed origin.

    ``owner`` and ``origin`` must be obtained from trusted authenticated host
    context, never from user/model tool parameters. ``transport`` is an injected
    trusted adapter; its lookup must consult the platform, not cached user data.
    The adapter sends one logical message per key and must not re-route payloads
    or treat untrusted stage evidence as instructions. No attachments, platform
    rendering, scheduler or gateway wiring is supplied here.
    """

    def __init__(self, store, owner, origin, transport, *, lease=60):
        if not isinstance(store, WorkflowStore):
            raise ValueError('trusted workflow store required')
        if (not isinstance(owner, dict) or set(owner) != {'profile', 'session'}
                or any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in owner.values())):
            raise PermissionError('trusted owner required')
        canonical = normalize_origin(origin)
        if (canonical is None or origin.get('profile') != owner['profile']
                or origin.get('session') != owner['session'] or not origin.get('route')):
            raise PermissionError('fixed owner-bound recipient required')
        if not callable(getattr(transport, 'send', None)) or not callable(getattr(transport, 'lookup', None)):
            raise ValueError('receipt-capable trusted transport required')
        if isinstance(lease, bool) or not isinstance(lease, (int, float)) or not 1 <= lease <= 3600:
            raise ValueError('invalid delivery lease')
        self.store, self.owner = store, deepcopy(owner)
        self.origin, self.transport, self.lease = json.loads(canonical), transport, lease

    def _check_origin(self, value):
        try:
            return normalize_origin(value) == normalize_origin(self.origin)
        except (TypeError, ValueError):
            return False

    def _inspect(self, workflow):
        flow = self.store.inspect(self.owner, workflow)
        if not self._check_origin(flow['origin']):
            raise PermissionError('workflow recipient mismatch')
        final = self.store.inspect_final(self.owner, workflow)
        if final is not None and (final['payload'].get('workflow_id') != workflow
                                  or not self._check_origin(final['payload'].get('origin'))):
            raise PermissionError('final recipient mismatch')
        return final

    def _receipt(self, value, key):
        if (not isinstance(value, dict) or set(value) != {'origin', 'delivery_key', 'receipt_id'}
                or value['delivery_key'] != key or not self._check_origin(value['origin'])
                or not isinstance(value['receipt_id'], str) or not value['receipt_id'].strip()
                or len(value['receipt_id']) > 512):
            return None
        return value['receipt_id']

    def _unknown(self, workflow, token):
        try:
            self.store.mark_final_unknown(self.owner, workflow, token)
        except WorkflowConflict:
            # Another process may already have observed lease expiry. Never
            # infer that a message was absent or retry it in this case.
            pass
        state = self.store.inspect_final(self.owner, workflow)
        return {'state': state['state'], 'delivery_key': state['delivery_key']}

    def deliver(self, workflow):
        """Try once; only the holder of a fresh token invokes transport.send.

        Nonterminal, pending child ACK, in-flight, delivered and unknown states
        do not call the transport. Exceptions and malformed send receipts are
        treated as ambiguous, even if the adapter claims no send occurred.
        """
        self._inspect(workflow)
        claim = self.store.claim_final(self.owner, workflow, lease=self.lease)
        if claim['state'] != 'sending' or 'token' not in claim:
            return claim
        token, key, payload = claim['token'], claim['delivery_key'], claim['payload']
        if payload.get('workflow_id') != workflow or not self._check_origin(payload.get('origin')):
            self._unknown(workflow, token)
            raise PermissionError('claimed final recipient mismatch')
        try:
            receipt = self.transport.send(origin=deepcopy(self.origin), delivery_key=key,
                                          payload=deepcopy(payload))
        except Exception:
            return self._unknown(workflow, token)
        receipt_id = self._receipt(receipt, key)
        if receipt_id is None:
            return self._unknown(workflow, token)
        try:
            self.store.ack_final(self.owner, workflow, token, key, receipt_id)
        except WorkflowConflict:
            # Receipt arrived too late or another observer fenced the lease.
            # Reconciliation must independently query the platform.
            return self._unknown(workflow, token)
        return {'state': 'delivered', 'delivery_key': key, 'receipt_id': receipt_id}

    def reconcile(self, workflow):
        """Resolve only unknown sends via a fresh matching platform lookup.

        Lookup failure or absence is not evidence that sending is safe; no
        automatic retry or reset to pending is available through this method.
        """
        final = self._inspect(workflow)
        if final is None:
            return {'state': 'not_terminal'}
        if final['state'] != 'unknown':
            return {'state': final['state'], 'delivery_key': final['delivery_key']}
        key = final['delivery_key']
        try:
            receipt = self.transport.lookup(origin=deepcopy(self.origin), delivery_key=key)
        except Exception:
            receipt = None
        receipt_id = self._receipt(receipt, key)
        if receipt_id is None:
            return {'state': 'unknown', 'delivery_key': key}
        self.store.reconcile_final(self.owner, workflow, key, receipt_id)
        return {'state': 'delivered', 'delivery_key': key, 'receipt_id': receipt_id}

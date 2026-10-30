"""Reconcile cancelled root workflows with their private stage queues.

The host supplies an administrator-owned root Pool, a WorkflowStore and a fixed
role-to-Pool mapping. Model input never selects a queue or a filesystem path.
All stage submit/bind and terminal reconciliation must use the same
WorkflowStore.submission_guard; without that cooperation a missing child cannot
be proven absent and cancellation must not be settled.

Cancelling a Pool job is asynchronous: the flag is not a terminal receipt.
This guard returns pending until the worker observes the flag or the child has
otherwise become terminal. A root attempt timeout that is being retried is not
the root workflow's terminal failure; only an explicit cancel or a final failed
or cancelled root triggers fanout.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import math
import re
import threading

from agentcore.pool import Pool, normalize_origin


_JOB = re.compile(r'^[0-9a-f]{32}$')
_ROLE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
_ROOT_STOP = frozenset(('failed', 'cancelled'))
_CHILD_TERMINAL = frozenset(('completed', 'failed', 'cancelled'))


class BindingError(ValueError):
    """A ledger entry does not prove ownership of the queue record."""


class CancelGuard:
    """Trusted, idempotent reconciliation; no messaging or queue creation."""

    def __init__(self, store, root_pool, role_pools, *, fence_timeout=.25):
        if not isinstance(root_pool, Pool):
            raise ValueError('a trusted root Pool is required')
        if (not isinstance(role_pools, dict) or not 1 <= len(role_pools) <= 16
                or any(not isinstance(role, str) or not _ROLE.fullmatch(role)
                       or not isinstance(pool, Pool) for role, pool in role_pools.items())):
            raise ValueError('a bounded administrator role-to-Pool mapping is required')
        roots = [root_pool.root] + [pool.root for pool in role_pools.values()]
        if len(set(roots)) != len(roots):
            raise ValueError('root and role queues must be distinct')
        if not all(callable(getattr(store, name, None)) for name in
                   ('inspect', 'submission_guard', 'cancel', 'bind_child', 'settle_cancel')):
            raise ValueError('trusted workflow ledger is required')
        if (type(fence_timeout) not in (int, float) or not math.isfinite(fence_timeout)
                or not 0.01 <= fence_timeout <= 30):
            raise ValueError('invalid submission fence timeout')
        self.store, self.root_pool, self.role_pools = store, root_pool, dict(role_pools)
        self.fence_timeout = float(fence_timeout)

    def _root(self, root_job, workflow, flow):
        if not isinstance(root_job, str) or not _JOB.fullmatch(root_job):
            raise BindingError('invalid root job identity')
        with self.root_pool.db() as db:
            row = db.execute('SELECT id,dedupe,payload,origin,status,cancel FROM jobs WHERE id=?',
                             (root_job,)).fetchone()
        if row is None:
            raise BindingError('root job unavailable')
        try:
            payload, origin = json.loads(row['payload']), json.loads(row['origin'])
            expected = normalize_origin(flow['origin'])
            actual = normalize_origin(origin)
        except (ValueError, TypeError, KeyError):
            raise BindingError('invalid root ownership record') from None
        if (not isinstance(payload, dict) or payload.get('workflow_id') != workflow
                or row['dedupe'] != 'workflow-root:' + workflow or actual != expected):
            raise BindingError('root workflow binding mismatch')
        if row['status'] not in {'queued', 'running', 'completed', 'failed', 'cancelled'} or row['cancel'] not in (0, 1):
            raise BindingError('invalid root job state')
        return dict(row)

    @staticmethod
    def _child(pool, stage, workflow):
        with pool.db() as db:
            row = db.execute('SELECT id,payload,origin,status,cancel FROM jobs WHERE dedupe=?',
                             (stage['child_key'],)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row['payload'])
        except (ValueError, TypeError):
            raise BindingError('invalid child ownership record') from None
        if (not isinstance(payload, dict) or payload.get('_workflow_id') != workflow
                or type(payload.get('_workflow_stage')) is not int
                or payload['_workflow_stage'] != stage['ordinal'] or row['origin'] is not None
                or (stage.get('child_id') and stage['child_id'] != row['id'])):
            raise BindingError('child workflow binding mismatch')
        if row['status'] not in {'queued', 'running', 'completed', 'failed', 'cancelled'}:
            raise BindingError('invalid child job state')
        return dict(row)

    def reconcile(self, owner, workflow, root_job):
        """Reconcile one workflow; repeat after a crash until ``cancelled``.

        An active root with an active workflow does nothing. A workflow already
        cancelling also gets reconciled when cancellation originated at its own
        API. ``child_pending`` means a cancellation flag was requested, not that
        the running child or its external effects have stopped.
        """
        with ExitStack() as held:
            try:
                held.enter_context(self.store.submission_guard(owner, workflow, timeout=self.fence_timeout))
            except TimeoutError:
                return {'status': 'submission_fence_busy'}
            flow = self.store.inspect(owner, workflow)
            root = self._root(root_job, workflow, flow)
            if flow['state'] in _CHILD_TERMINAL:
                return {'status': 'workflow_terminal', 'workflow_state': flow['state']}
            if not root['cancel'] and root['status'] not in _ROOT_STOP and flow['state'] != 'cancelling':
                return {'status': 'not_cancelling', 'workflow_state': flow['state']}
            cancellation = self.store.cancel(owner, workflow)
            stage = cancellation.get('pending_child')
            if stage is None:
                return {'status': 'workflow_terminal', 'workflow_state': cancellation['state']}
            role = stage['role']
            if role not in self.role_pools:
                raise BindingError('workflow role is not administrator configured')
            pool = self.role_pools[role]
            child = self._child(pool, stage, workflow)
            if child is None:
                if stage.get('child_id'):
                    return {'status': 'child_unavailable', 'workflow_state': 'cancelling'}
                # The common submission lock fences every valid submitter. An
                # absent key in this exact role pool is now proof of no child.
                state = self.store.settle_cancel(owner, workflow, stage['ordinal'], submission_fenced=True)
                return {'status': 'cancelled', 'workflow_state': state, 'child': 'fenced_absent'}
            if not stage.get('child_id'):
                self.store.bind_child(owner, workflow, stage['ordinal'], stage['child_key'], child['id'])
            child_id = child['id']
            if child['status'] not in _CHILD_TERMINAL:
                pool.cancel(child_id)
                child = pool.status(child_id)
            if child is None or child['status'] not in _CHILD_TERMINAL:
                return {'status': 'child_pending', 'workflow_state': 'cancelling', 'child_id': child_id}
            state = self.store.settle_cancel(owner, workflow, stage['ordinal'], child_id=child_id)
            return {'status': 'cancelled', 'workflow_state': state,
                    'child_id': child_id, 'child_terminal': child['status']}

    def scan_once(self, *, after='', limit=128):
        """Walk a bounded root-queue page; repeat after restart from an empty cursor.

        Root origins identify the owner. Foreign or malformed root rows are
        reported without cancelling anything. When ``next_after`` is empty the
        sweep is complete; begin the next sweep at the start so newly inserted
        UUIDs below an earlier cursor are not missed.
        """
        if not isinstance(after, str) or (after and not _JOB.fullmatch(after)):
            raise ValueError('invalid scan cursor')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid scan limit')
        with self.root_pool.db() as db:
            rows = db.execute('SELECT id,payload,origin FROM jobs WHERE id>? ORDER BY id LIMIT ?',
                              (after, limit)).fetchall()
        outcomes = []
        for row in rows:
            try:
                payload, origin = json.loads(row['payload']), json.loads(row['origin'])
                if not isinstance(payload, dict) or not isinstance(origin, dict):
                    raise BindingError('invalid root ownership record')
                owner = {'profile': origin['profile'], 'session': origin['session']}
                workflow = payload['workflow_id']
                outcome = self.reconcile(owner, workflow, row['id'])
            except (BindingError, PermissionError, KeyError, TypeError, ValueError) as exc:
                outcome = {'status': 'invalid_binding', 'error_type': type(exc).__name__}
            outcomes.append({'root_job': row['id'], **outcome})
        return {'outcomes': outcomes, 'next_after': rows[-1]['id'] if len(rows) == limit else ''}

    def run(self, stop=None, *, observer, interval=2.0, limit=128):
        """Supervised polling loop; the host must observe every bounded outcome."""
        if type(interval) not in (int, float) or not 0.1 <= interval <= 60:
            raise ValueError('invalid scan interval')
        if not callable(observer):
            raise ValueError('host observer is required')
        stop = stop if stop is not None else threading.Event()
        cursor = ''
        while not stop.is_set():
            page = self.scan_once(after=cursor, limit=limit)
            for outcome in page['outcomes']:
                observer(outcome)
            cursor = page['next_after']
            stop.wait(interval)

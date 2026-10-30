"""Dedicated durable queue for a host-owned read-only colleague.

Submission never calls a model. A separately driven worker executes at most one
task per ``run_once`` and records its terminal receipt in the existing Pool
outbox. The queue is internal-only: jobs deliberately have no Pool origin, so
generic completion backfill cannot send them. This module never sends or ACKs a
receipt. A future single external-delivery owner must verify and acknowledge it
only after an unambiguous platform receipt; wiring any generic outbox sender to
this queue is unsupported.

Every admission reserves execution and delivery work before enqueue. A crash
between those SQLite databases can leave a reservation with no job; it remains
open and requires host reconciliation. Completed tasks keep their delivery
reservation open until a separately integrated, authoritative delivery settles
it. Therefore this module alone cannot complete retirement of a real colleague.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading

from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_readonly_dialogue import ReadOnlyDialogue
from agentcore.pool import Pool, normalize_origin

_fds = set()
_fds_lock = threading.Lock()


def _fork_prepare():
    _fds_lock.acquire()


def _fork_parent():
    _fds_lock.release()


def _fork_child():
    try:
        for fd in tuple(_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        _fds.clear()
    finally:
        _fds_lock.release()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_fork_prepare, after_in_parent=_fork_parent, after_in_child=_fork_child)


class ReadOnlyQueue:
    """Host-only admission/worker API; not a model-facing authentication boundary.

    ``authorize_assignment`` must validate the authenticated origin and member
    against the current host policy at both admission and execution. The host
    must exclusively own the private queue and configure exactly one eventual
    final-delivery owner. The bool ``dedicated`` is an administrator assertion,
    not proof that no other process has access.
    """

    def __init__(self, pool, runtime, *, authorize_assignment, dedicated=False):
        if not isinstance(pool, Pool) or not isinstance(runtime, ReadOnlyColleagueRuntime):
            raise ValueError('trusted_queue_and_runtime_required')
        if dedicated is not True or not callable(authorize_assignment):
            raise ValueError('dedicated_queue_and_authorizer_required')
        info = pool.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
            raise ValueError('private_queue_directory_required')
        self.pool, self.runtime, self.authorize_assignment = pool, runtime, authorize_assignment
        with self.pool.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS readonly_admissions '
                       '(task TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, member TEXT NOT NULL, '
                       'generation INTEGER NOT NULL, state TEXT NOT NULL)')

    @contextmanager
    def _submission_guard(self, task):
        # Serialize each request's reserve/enqueue/rollback across processes.
        # It is separate from the execution lock, so a worker can run after
        # enqueue without waiting for a submitting chat turn to unwind.
        with _fds_lock:
            fd = os.open(self.pool.root / 'locks' / ('readonly-submit-' + task),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            _fds.add(fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
                raise ValueError('invalid_submission_guard')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('submission_busy') from None
            yield
        finally:
            with _fds_lock:
                if fd in _fds:
                    _fds.remove(fd)
                    os.close(fd)

    def _binding(self, owner, origin, member_id, generation):
        owner = self.runtime._owner(owner)
        route = normalize_origin(origin)
        if route is None:
            raise PermissionError('authenticated_route_required')
        route = json.loads(route)
        if (route.get('profile') != owner['profile'] or route.get('session') != owner['session']
                or not route.get('route')):
            raise PermissionError('authenticated_route_required')
        member = self.runtime.registry.role_for_member(supervisor=self.runtime.supervisor,
                                                       member_id=member_id, generation=generation)
        if member_id == self.runtime.registry.primary_member(supervisor=self.runtime.supervisor):
            raise PermissionError('primary_not_a_colleague_worker')
        if self.authorize_assignment(deepcopy(owner), deepcopy(route), member_id, generation) is not True:
            raise PermissionError('assignment_not_authorized')
        return owner, route, member

    def submit(self, *, owner, origin, request_id, member_id, generation, goal, sources,
               conversation_context=None, conversation_sequence=None):
        """Reserve then enqueue and return immediately, without invoking the model."""
        owner, origin, member = self._binding(owner, origin, member_id, generation)
        _, task = self.runtime._identity(owner, request_id)
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 8000 or not isinstance(sources, list):
            raise ValueError('invalid_analysis_request')
        # Validate history/sequence without trusting the caller's byte counter.
        from agentcore.colleague_readonly_runtime import _conversation_context
        prior = _conversation_context(conversation_context)
        if prior is not None and prior['turns']:
            # Historical summaries can include sources whose access has since
            # been revoked. This queue has no source re-authorization bridge.
            raise ValueError('history_authorization_not_integrated')
        if conversation_sequence is not None and (type(conversation_sequence) is not int
                                                  or not 0 < conversation_sequence < 2**63):
            raise ValueError('invalid_conversation_sequence')
        if prior is not None and prior['turns'] and conversation_sequence is None:
            raise ValueError('conversation_sequence_required_for_history')
        payload = {'owner': owner, 'origin': origin, 'request_id': request_id,
                   'member_id': member_id, 'generation': generation, 'goal': goal,
                   'sources': deepcopy(sources), 'conversation_context': deepcopy(conversation_context),
                   'conversation_sequence': conversation_sequence, 'runtime_task_id': task}
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
            if len(encoded) > 131072:
                raise ValueError('analysis_request_budget')
        except (TypeError, RecursionError):
            raise ValueError('invalid_analysis_request') from None
        key = 'readonly:' + task
        fingerprint = hashlib.sha256(encoded).hexdigest()
        with self._submission_guard(task):
            with self.pool.db() as db:
                db.execute('BEGIN IMMEDIATE')
                admission = db.execute('SELECT fingerprint,member,generation,state FROM readonly_admissions '
                                       'WHERE task=?', (task,)).fetchone()
                if admission is None:
                    db.execute('INSERT INTO readonly_admissions VALUES (?,?,?,?,?)',
                               (task, fingerprint, member_id, generation, 'preparing'))
                elif (admission['fingerprint'] != fingerprint or admission['member'] != member_id
                      or admission['generation'] != generation):
                    raise ValueError('request_id_conflict')
                elif admission['state'] == 'aborted':
                    raise ValueError('admission_closed')
                existing = db.execute('SELECT id FROM jobs WHERE dedupe=?', (key,)).fetchone()
            if existing is None:
                if member['status'] != 'active':
                    raise ValueError('member_not_accepting_work')
                first = self.runtime.registry.reserve_work(supervisor=self.runtime.supervisor,
                    member_id=member_id, generation=generation, work_id=task)
                try:
                    self.runtime.registry.reserve_work(supervisor=self.runtime.supervisor,
                        member_id=member_id, generation=generation, work_id='delivery:' + task)
                except ValueError as error:
                    if str(error) == 'member_not_accepting_work' and first['created']:
                        # Under this same guard no same-task submitter can have
                        # enqueued between our first reserve and this failure.
                        self.runtime.registry.settle_work(supervisor=self.runtime.supervisor,
                            member_id=member_id, generation=generation, work_id=task)
                        with self.pool.db() as db:
                            db.execute("UPDATE readonly_admissions SET state='aborted' WHERE task=?", (task,))
                    raise
            # Pool validates exact payload on every replay. Internal origin=None
            # makes legacy backfill refuse to guess an external destination.
            job = self.pool.submit(key, payload, replay_safe=True, max_tries=3, origin=None)
            with self.pool.db() as db:
                db.execute("UPDATE readonly_admissions SET state='queued' WHERE task=?", (task,))
        return {'job_id': job, 'task_id': task, 'state': self.pool.status(job)['status']}

    def reconcile_orphan(self, *, owner, request_id, member_id, generation, confirmed_no_external_work):
        """Close a crashed pre-enqueue reservation after authoritative inspection.

        Host-only: all producers must use this queue, and the host must confirm
        no external model or delivery process exists. Refuses any queued job or
        any runtime state. The closed request cannot be reopened; submit a new
        request ID if a new task is desired.
        """
        owner, task = self.runtime._identity(owner, request_id)
        if confirmed_no_external_work is not True:
            raise PermissionError('authoritative_external_idle_required')
        self.runtime.registry.role_for_member(supervisor=self.runtime.supervisor,
                                              member_id=member_id, generation=generation)
        with self._submission_guard(task):
            with self.pool.db() as db:
                admission = db.execute('SELECT member,generation,state FROM readonly_admissions '
                                       'WHERE task=?', (task,)).fetchone()
                job = db.execute('SELECT id FROM jobs WHERE dedupe=?', ('readonly:' + task,)).fetchone()
            if (admission is None or admission['member'] != member_id or admission['generation'] != generation
                    or admission['state'] not in ('preparing', 'aborted') or job is not None
                    or self.runtime.inspect_assignment(owner=owner, request_id=request_id) is not None):
                raise ValueError('orphan_not_proven')
            closed = []
            for work_id in (task, 'delivery:' + task):
                try:
                    self.runtime.registry.settle_work(supervisor=self.runtime.supervisor,
                        member_id=member_id, generation=generation, work_id=work_id)
                except ValueError as error:
                    if str(error) != 'unknown_work':
                        raise
                else:
                    closed.append(work_id)
            with self.pool.db() as db:
                db.execute("UPDATE readonly_admissions SET state='aborted' WHERE task=?", (task,))
            return {'task_id': task, 'settled_reservations': len(closed)}

    def run_once(self, *, lease=120):
        """Run one queued task in a separate host worker, not in the caller turn."""
        with _fds_lock:
            claim = self.pool.claim(lease=lease)
            if claim is None:
                return {'state': 'empty'}
            row, fd = claim
            _fds.add(fd)
        worker_pid = os.getpid()
        try:
            try:
                payload = json.loads(row['payload'])
                owner, _, _ = self._binding(payload['owner'], payload['origin'], payload['member_id'],
                                             payload['generation'])
                _, expected = self.runtime._identity(owner, payload['request_id'])
                if expected != payload['runtime_task_id'] or self.pool.origin(row['id']) is not None:
                    raise ValueError('queue_task_binding_mismatch')
                result = self.runtime.run(owner=owner, request_id=payload['request_id'],
                    member_id=payload['member_id'], generation=payload['generation'],
                    goal=payload['goal'], sources=payload['sources'],
                    conversation_context=payload.get('conversation_context'),
                    conversation_sequence=payload.get('conversation_sequence'))
                ok = result['state'] == 'completed'
                public = {'runtime_task_id': expected, 'runtime_state': result['state']}
            except Exception:
                if os.getpid() != worker_pid:
                    raise RuntimeError('forked_worker_forbidden') from None
                # Preserve the execution reservation on an unclassified failure;
                # it is unsafe to guess whether the model started or completed.
                ok, public = False, {'reason': 'worker_failed_or_outcome_unknown'}
            if os.getpid() != worker_pid:
                raise RuntimeError('forked_worker_forbidden')
            committed = self.pool.finish(row['id'], row['attempt'], ok, public)
            return {'state': 'attempt_finished' if committed else 'stale_attempt', 'job_id': row['id'],
                    'job_state': self.pool.status(row['id'])['status']}
        finally:
            with _fds_lock:
                if fd in _fds:
                    _fds.remove(fd)
                    os.close(fd)

    def inspect_completion(self, *, owner, job_id):
        """Owner-bound presentation from the still-unacknowledged Pool outbox.

        No send or ACK is performed. The result is suitable only for a trusted
        outbox adapter; an unknown send must never be blindly retried.
        """
        owner = self.runtime._owner(owner)
        with self.pool.db() as db:
            row = db.execute('SELECT j.payload,j.origin,o.receipt,o.acknowledged FROM jobs j '
                             'JOIN outbox o ON o.job=j.id WHERE j.id=?', (job_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row['payload'])
        if payload.get('owner') != owner or row['origin'] is not None:
            raise PermissionError('queue_owner_mismatch')
        self._binding(owner, payload['origin'], payload['member_id'], payload['generation'])
        receipt = json.loads(row['receipt'])
        if receipt.get('job') != job_id or receipt.get('status') != 'completed':
            return {'job_id': job_id, 'state': receipt.get('status'), 'text': None,
                    'acknowledged': bool(row['acknowledged'])}
        if (not isinstance(receipt.get('result'), dict)
                or receipt['result'].get('runtime_task_id') != payload['runtime_task_id']
                or receipt['result'].get('runtime_state') != 'completed'):
            raise ValueError('queue_receipt_binding_mismatch')
        presentation = ReadOnlyDialogue(self.runtime, owner=owner).present(payload['request_id'])
        if (presentation.get('state') != 'completed'
                or presentation.get('task_id') != payload['runtime_task_id']
                or presentation.get('member_id') != payload['member_id']
                or presentation.get('generation') != payload['generation']):
            raise ValueError('queue_result_binding_mismatch')
        return {'job_id': job_id, 'state': 'completed', 'origin': deepcopy(payload['origin']),
                'acknowledged': bool(row['acknowledged']), 'presentation': presentation}

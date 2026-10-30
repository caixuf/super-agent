"""Durable ownership and handoff intents for a small sequence of colleagues.

This store coordinates metadata; it does not schedule a job, authorize a file,
verify a report, or send a message. The caller must derive owner and origin from
the authenticated host, validate each role and source against administrator
policy, submit each returned child key idempotently, and verify terminal child
receipts and evidence before calling ``finish``. A previous summary is untrusted
data, never a permission grant or proof that a claim is true.

Each SQLite transaction is local to this ledger. Submitting to a separate queue
is not atomic with this database: persist an intent with ``prepare_next`` before
submitting, use the returned stable ``child_key`` and exact stage payload, and
reconcile by that same key after interruption. A cancellation of a prepared or
running stage stays ``cancelling`` until the trusted coordinator observes the
child terminal, or fences all submitters and verifies that no child was created.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import time
import uuid

from agentcore.pool import normalize_origin

_HEX32 = re.compile(r'^[0-9a-f]{32}$')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_ROLE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
_REASON = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_TERMINAL = frozenset(('completed', 'failed', 'cancelled'))
_guard_fds = set()


def _close_forked_guards():
    # flock is associated with the inherited open file description. A child
    # holding the parent's descriptor would otherwise keep the parent lock
    # alive after the parent exits the context, and can deadlock its own lock.
    for fd in tuple(_guard_fds):
        try:
            os.close(fd)
        except OSError:
            pass  # The child has no usable inherited guard in either case.
    _guard_fds.clear()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_close_forked_guards)


class WorkflowConflict(ValueError):
    """The same identity was reused with different immutable content."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _owner(owner):
    if not isinstance(owner, dict) or set(owner) != {'profile', 'session'}:
        raise PermissionError('trusted workflow owner required')
    for value in owner.values():
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise PermissionError('trusted workflow owner required')
    return _hash(_json(owner))


def _stages(stages):
    if not isinstance(stages, list) or not 2 <= len(stages) <= 4:
        raise ValueError('workflow requires two to four stages')
    normalized = []
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {'role', 'goal', 'sources'}:
            raise ValueError('invalid stage fields')
        role, goal, sources = stage['role'], stage['goal'], stage['sources']
        if not isinstance(role, str) or not _ROLE.fullmatch(role):
            raise ValueError('invalid stage role')
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            raise ValueError('invalid stage goal')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
            raise ValueError('invalid stage sources')
        clean = []
        for source in sources:
            if not isinstance(source, dict) or set(source) - {'path', 'start_line', 'end_line'}:
                raise ValueError('invalid source fields')
            path = source.get('path')
            if (not isinstance(path, str) or len(path) > 4096 or '\x00' in path
                    or not PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts):
                raise ValueError('absolute source path required')
            start, end = source.get('start_line', 1), source.get('end_line')
            if (type(start) is not int or start < 1 or
                    ('end_line' in source and (type(end) is not int or end < start or end-start+1 > 400))):
                raise ValueError('source lines outside budget')
            item = {'path': path, 'start_line': start}
            if 'end_line' in source:
                item['end_line'] = end
            clean.append(item)
        normalized.append({'role': role, 'goal': goal, 'sources': clean})
    return normalized


def _evidence(value):
    expected = {'attempt_id', 'report_sha256', 'source_manifest_sha256', 'summary', 'verification'}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('invalid evidence fields')
    if not isinstance(value['attempt_id'], str) or not _HEX32.fullmatch(value['attempt_id']):
        raise ValueError('invalid evidence attempt')
    for key in ('report_sha256', 'source_manifest_sha256'):
        if not isinstance(value[key], str) or not _HEX64.fullmatch(value[key]):
            raise ValueError('invalid evidence digest')
    if not isinstance(value['summary'], str) or not value['summary'].strip() or len(value['summary']) > 4000:
        raise ValueError('invalid evidence summary')
    if (not isinstance(value['verification'], str) or not value['verification'].strip()
            or len(value['verification']) > 128):
        raise ValueError('invalid evidence verification')
    return _json(value)


class WorkflowStore:
    """Transactional local chain ledger; callers own external queue reconciliation."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise PermissionError('workflow store directory must be private')
        self.path = self.root / 'colleague-workflows.db'
        if self.path.is_symlink():
            raise ValueError('workflow database must not be a symlink')
        # Do not open/close this path with raw file descriptors. A raw close
        # can release other SQLite connections' process-wide POSIX locks.
        # The private directory protects a newly created DB until chmod runs.
        with self._db() as db:
            os.chmod(self.path, 0o600, follow_symlinks=False)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workflows (
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key_hash TEXT NOT NULL,
                  specification TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL,
                  UNIQUE(owner, request_key_hash));
                CREATE TABLE IF NOT EXISTS workflow_stages (
                  workflow TEXT NOT NULL REFERENCES workflows(id), ordinal INTEGER NOT NULL,
                  role TEXT NOT NULL, goal TEXT NOT NULL, sources TEXT NOT NULL,
                  child_key TEXT NOT NULL UNIQUE, child_id TEXT, state TEXT NOT NULL,
                  evidence TEXT, failure TEXT, cancellation_proof TEXT,
                  PRIMARY KEY(workflow, ordinal));
                CREATE TABLE IF NOT EXISTS workflow_final_outbox (
                  workflow TEXT PRIMARY KEY REFERENCES workflows(id),
                  delivery_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                  state TEXT NOT NULL, lease_token TEXT, lease_until REAL, receipt_id TEXT);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(workflow_stages)')}
            if 'internal_acked' not in columns:
                db.execute('ALTER TABLE workflow_stages ADD COLUMN internal_acked INTEGER NOT NULL DEFAULT 0')
            if 'internal_ack_receipt' not in columns:
                db.execute('ALTER TABLE workflow_stages ADD COLUMN internal_ack_receipt TEXT')
            if 'abort_reason' not in columns:
                db.execute('ALTER TABLE workflow_stages ADD COLUMN abort_reason TEXT')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _owned(db, owner, workflow):
        digest = _owner(owner)
        if not isinstance(workflow, str) or not _HEX32.fullmatch(workflow):
            raise PermissionError('workflow unavailable in this session')
        row = db.execute('SELECT * FROM workflows WHERE id=? AND owner=?',
                         (workflow, digest)).fetchone()
        if row is None:
            raise PermissionError('workflow unavailable in this session')
        return row

    @staticmethod
    def _row(db, workflow, ordinal):
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError('invalid stage number')
        row = db.execute('SELECT * FROM workflow_stages WHERE workflow=? AND ordinal=?',
                         (workflow, ordinal)).fetchone()
        if row is None:
            raise ValueError('unknown stage')
        return row

    @staticmethod
    def _view(row):
        return {'ordinal': row['ordinal'], 'role': row['role'], 'goal': row['goal'],
                'sources': json.loads(row['sources']), 'child_key': row['child_key'],
                'child_id': row['child_id'], 'state': row['state'],
                'evidence': json.loads(row['evidence']) if row['evidence'] else None,
                'failure': row['failure'], 'abort_reason': row['abort_reason'],
                'internal_acked': bool(row['internal_acked'])}

    @staticmethod
    def _set_state(db, workflow, state):
        """Create final delivery intent in the same transaction as terminal state."""
        db.execute('UPDATE workflows SET state=?,updated=? WHERE id=?', (state, time.time(), workflow))
        if state not in _TERMINAL:
            return
        flow = db.execute('SELECT origin FROM workflows WHERE id=?', (workflow,)).fetchone()
        stages = db.execute('SELECT * FROM workflow_stages WHERE workflow=? ORDER BY ordinal',
                            (workflow,)).fetchall()
        payload = {'workflow_id': workflow, 'status': state, 'origin': json.loads(flow['origin']),
                   'stages': [{'ordinal': row['ordinal'], 'role': row['role'], 'state': row['state'],
                               'child_id': row['child_id'], 'failure': row['failure'],
                               'evidence': json.loads(row['evidence']) if row['evidence'] else None}
                              for row in stages]}
        key = 'workflow-final:' + _hash(workflow)[:40]
        db.execute("INSERT OR IGNORE INTO workflow_final_outbox(workflow,delivery_key,payload,state) "
                   "VALUES (?,?,?,'pending')", (workflow, key, _json(payload)))

    def create(self, owner, request_key, stages, origin):
        """Create once; same owner/key/content returns the same id, changes conflict.

        ``owner`` and ``origin`` must come from the host, not model arguments. Origin
        must name the same profile/session and an addressable route. Stage sources
        are only structurally bounded here: the caller must reauthorize resolved
        paths and role policy before *every* external submission.
        """
        digest = _owner(owner)
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 128:
            raise ValueError('invalid request key')
        normalized = _stages(stages)
        canonical_origin = normalize_origin(origin)
        if (canonical_origin is None or origin.get('profile') != owner['profile']
                or origin.get('session') != owner['session'] or not origin.get('route')):
            raise PermissionError('addressable owner-bound origin required')
        spec, key = _json(normalized), _hash(request_key)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM workflows WHERE owner=? AND request_key_hash=?',
                                  (digest, key)).fetchone()
            if existing:
                if existing['specification'] != spec or existing['origin'] != canonical_origin:
                    raise WorkflowConflict('request key reused with different workflow')
                return existing['id']
            workflow, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?)',
                       (workflow, digest, key, spec, canonical_origin, 'active', now, now))
            for i, stage in enumerate(normalized):
                child_key = 'workflow:' + _hash(_json([workflow, i]))[:48]
                db.execute('INSERT INTO workflow_stages '
                           '(workflow,ordinal,role,goal,sources,child_key,state) VALUES (?,?,?,?,?,?,?)',
                           (workflow, i, stage['role'], stage['goal'], _json(stage['sources']), child_key, 'queued'))
            return workflow

    def inspect(self, owner, workflow):
        """Return this owner's persisted state; no ambient-session address lookup."""
        with self._db() as db:
            row = self._owned(db, owner, workflow)
            stages = db.execute('SELECT * FROM workflow_stages WHERE workflow=? ORDER BY ordinal',
                                (workflow,)).fetchall()
            return {'workflow_id': workflow, 'state': row['state'], 'origin': json.loads(row['origin']),
                    'stages': [self._view(stage) for stage in stages]}

    @contextmanager
    def submission_guard(self, owner, workflow, *, timeout=None):
        """Fence queue submission and cancellation reconciliation across processes.

        Every coordinator must hold this same guard while it rechecks state,
        prepares an intent, submits the external child and binds its receipt.
        A cancellation reconciler takes the guard before resolving an unbound
        child key or settling absence. It cannot fence code that ignores it.
        Never hold this lock while waiting for a child to finish. A finite
        timeout raises TimeoutError without acquiring a contested guard.
        """
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or not 0 <= timeout <= 3600):
            raise ValueError('invalid submission guard timeout')
        with self._db() as db:
            self._owned(db, owner, workflow)
        directory = self.root / 'submission-locks'
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (workflow + '.lock')
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        _guard_fds.add(fd)
        try:
            if timeout is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('submission guard busy') from None
                        time.sleep(min(remaining, .05))
            with self._db() as db:
                self._owned(db, owner, workflow)
            yield
        finally:
            if fd in _guard_fds:
                _guard_fds.remove(fd)
                os.close(fd)

    def prepare_next(self, owner, workflow):
        """Atomically persist/recover the current stage intent before queue submission.

        Returns the stable key and original stage specification with the preceding
        evidence, or ``None`` for terminal/cancelling workflows. A running stage is
        returned too: a coordinator must inspect its existing child, not resubmit
        with a different payload. Concurrent callers obtain the same intent/key.
        """
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow = self._owned(db, owner, workflow)
            if flow['state'] != 'active':
                return None
            row = db.execute("SELECT * FROM workflow_stages WHERE workflow=? AND state IN "
                             "('queued','prepared','running') ORDER BY ordinal LIMIT 1", (workflow,)).fetchone()
            if row is None:
                raise RuntimeError('active workflow without a current stage')
            if row['ordinal']:
                previous = self._row(db, workflow, row['ordinal'] - 1)
                if previous['state'] != 'completed' or not previous['evidence']:
                    raise RuntimeError('prior stage not completed')
                prior = json.loads(previous['evidence'])
            else:
                prior = None
            if row['state'] == 'queued':
                db.execute("UPDATE workflow_stages SET state='prepared' WHERE workflow=? AND ordinal=? AND state='queued'",
                           (workflow, row['ordinal']))
                db.execute('UPDATE workflows SET updated=? WHERE id=?', (time.time(), workflow))
                row = self._row(db, workflow, row['ordinal'])
            return {'workflow_id': workflow, 'origin': json.loads(flow['origin']),
                    'stage': self._view(row), 'previous_evidence': prior}

    def bind_child(self, owner, workflow, ordinal, child_key, child_id):
        """Record the queue's child receipt once; cancellation may already be pending."""
        if not isinstance(child_id, str) or not _HEX32.fullmatch(child_id):
            raise ValueError('invalid child job')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow, row = self._owned(db, owner, workflow), self._row(db, workflow, ordinal)
            if row['child_key'] != child_key:
                raise WorkflowConflict('child key mismatch')
            if row['child_id']:
                if row['child_id'] != child_id:
                    raise WorkflowConflict('stage already bound to different child')
                return {'state': flow['state'], 'cancel_required': flow['state'] == 'cancelling'}
            if row['state'] != 'prepared' or flow['state'] not in ('active', 'cancelling'):
                raise WorkflowConflict('stage has no bindable intent')
            db.execute("UPDATE workflow_stages SET child_id=?,state='running' WHERE workflow=? AND ordinal=?",
                       (child_id, workflow, ordinal))
            db.execute('UPDATE workflows SET updated=? WHERE id=?', (time.time(), workflow))
            return {'state': flow['state'], 'cancel_required': flow['state'] == 'cancelling'}

    def finish(self, owner, workflow, ordinal, child_id, evidence):
        """Record caller-verified terminal evidence once; it is immutable and untrusted.

        On a cancellation race, record the completed child but close the workflow
        as cancelled rather than admitting another stage.
        """
        serialized = _evidence(evidence)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow, row = self._owned(db, owner, workflow), self._row(db, workflow, ordinal)
            if row['child_id'] != child_id or not child_id:
                raise WorkflowConflict('child receipt mismatch')
            if row['state'] == 'completed':
                if row['evidence'] != serialized:
                    raise WorkflowConflict('evidence is immutable')
                return flow['state']
            if row['state'] != 'running' or flow['state'] not in ('active', 'cancelling'):
                raise WorkflowConflict('stage cannot finish')
            db.execute("UPDATE workflow_stages SET state='completed',evidence=? WHERE workflow=? AND ordinal=?",
                       (serialized, workflow, ordinal))
            pending = db.execute("SELECT count(*) FROM workflow_stages WHERE workflow=? AND state='queued'",
                                 (workflow,)).fetchone()[0]
            state = 'cancelled' if flow['state'] == 'cancelling' else ('completed' if not pending else 'active')
            if state == 'cancelled':
                db.execute("UPDATE workflow_stages SET state='cancelled' WHERE workflow=? AND state='queued'", (workflow,))
            self._set_state(db, workflow, state)
            return state

    def fail(self, owner, workflow, ordinal, child_id, reason):
        """Record a caller-observed terminal child failure using a bounded reason code."""
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            raise ValueError('invalid failure code')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow, row = self._owned(db, owner, workflow), self._row(db, workflow, ordinal)
            if row['child_id'] != child_id or not child_id:
                raise WorkflowConflict('child receipt mismatch')
            if row['state'] == 'failed':
                if row['failure'] != reason:
                    raise WorkflowConflict('failure is immutable')
                return flow['state']
            if row['state'] != 'running' or flow['state'] not in ('active', 'cancelling'):
                raise WorkflowConflict('stage cannot fail')
            state = 'cancelled' if flow['state'] == 'cancelling' else 'failed'
            db.execute("UPDATE workflow_stages SET state='failed',failure=? WHERE workflow=? AND ordinal=?",
                       (reason, workflow, ordinal))
            db.execute("UPDATE workflow_stages SET state='cancelled' WHERE workflow=? AND state='queued'", (workflow,))
            self._set_state(db, workflow, state)
            return state

    def reject_unsubmitted(self, owner, workflow, ordinal, child_key, reason, *, submission_fenced=False):
        """Fail an unsubmitted stage after the trusted caller fences submitters.

        The caller must hold ``submission_guard`` and verify the stable child
        key is absent in that stage's administrator-owned queue. This method
        cannot inspect or prove the state of that separate queue.
        """
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            raise ValueError('invalid failure code')
        if submission_fenced is not True:
            raise WorkflowConflict('unbound submission not fenced')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow, row = self._owned(db, owner, workflow), self._row(db, workflow, ordinal)
            if row['child_key'] != child_key:
                raise WorkflowConflict('child key mismatch')
            if row['state'] == 'failed' and row['child_id'] is None:
                if row['failure'] != reason:
                    raise WorkflowConflict('failure is immutable')
                return flow['state']
            if row['state'] != 'prepared' or row['child_id'] is not None or flow['state'] != 'active':
                raise WorkflowConflict('stage cannot be rejected')
            db.execute("UPDATE workflow_stages SET state='failed',failure=? WHERE workflow=? AND ordinal=?",
                       (reason, workflow, ordinal))
            db.execute("UPDATE workflow_stages SET state='cancelled' WHERE workflow=? AND state='queued'", (workflow,))
            self._set_state(db, workflow, 'failed')
            return 'failed'

    def request_stage_abort(self, owner, workflow, ordinal, child_id, reason):
        """Persist a reason to cancel a bound child before recording terminal failure."""
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            raise ValueError('invalid failure code')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow, row = self._owned(db, owner, workflow), self._row(db, workflow, ordinal)
            if row['child_id'] != child_id or not child_id:
                raise WorkflowConflict('child receipt mismatch')
            if row['abort_reason']:
                if row['abort_reason'] != reason:
                    raise WorkflowConflict('abort reason is immutable')
                return False
            if row['state'] != 'running' or flow['state'] not in ('active', 'cancelling'):
                raise WorkflowConflict('stage cannot be aborted')
            db.execute('UPDATE workflow_stages SET abort_reason=? WHERE workflow=? AND ordinal=?',
                       (reason, workflow, ordinal))
            return True

    def cancel(self, owner, workflow):
        """Stop future stages and return the child key/id requiring reconciliation.

        ``cancelling`` is deliberately not a terminal result when a submit intent
        may be in flight. The caller cancels or reconciles the real queue child.
        """
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow = self._owned(db, owner, workflow)
            if flow['state'] in _TERMINAL:
                return {'state': flow['state'], 'pending_child': None}
            pending = db.execute("SELECT * FROM workflow_stages WHERE workflow=? AND state IN ('prepared','running')",
                                 (workflow,)).fetchone()
            state = 'cancelling' if pending else 'cancelled'
            db.execute("UPDATE workflow_stages SET state='cancelled' WHERE workflow=? AND state='queued'", (workflow,))
            self._set_state(db, workflow, state)
            return {'state': state, 'pending_child': self._view(pending) if pending else None}

    def settle_cancel(self, owner, workflow, ordinal, *, child_id=None, submission_fenced=False):
        """Finish cancellation only after trusted external reconciliation.

        A bound child requires a matching ``child_id`` and the caller must have
        observed it terminal. For an unbound intent, ``submission_fenced=True``
        asserts that every submitter has stopped and the stable key is absent in
        the destination queue. This store cannot independently prove either fact.
        """
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow, row = self._owned(db, owner, workflow), self._row(db, workflow, ordinal)
            if flow['state'] == 'cancelled' and row['state'] == 'cancelled':
                if row['child_id'] != child_id:
                    raise WorkflowConflict('child receipt mismatch')
                return 'cancelled'
            if flow['state'] != 'cancelling' or row['state'] not in ('prepared', 'running'):
                raise WorkflowConflict('no cancellable intent')
            if row['child_id']:
                if child_id != row['child_id']:
                    raise WorkflowConflict('child receipt mismatch')
                proof = 'terminal_child'
            else:
                if child_id is not None or submission_fenced is not True:
                    raise WorkflowConflict('unbound submission not fenced')
                proof = 'fenced_absence'
            db.execute("UPDATE workflow_stages SET state='cancelled',cancellation_proof=? WHERE workflow=? AND ordinal=?",
                       (proof, workflow, ordinal))
            self._set_state(db, workflow, 'cancelled')
            return 'cancelled'

    def pending_child_acks(self, owner, workflow):
        """List terminal child receipts that must be reconciled and internally ACKed.

        The caller first persists this ledger's outcome, then ACKs the original
        queue receipt. If a crash occurs between queue ACK and this marker, check
        the queue's persisted ACK and call ``mark_child_ack`` on restart. This
        list cannot claim or modify a separate queue.
        """
        with self._db() as db:
            self._owned(db, owner, workflow)
            rows = db.execute("SELECT * FROM workflow_stages WHERE workflow=? AND child_id IS NOT NULL "
                              "AND state IN ('completed','failed','cancelled') AND internal_acked=0 "
                              "ORDER BY ordinal", (workflow,)).fetchall()
            return [self._view(row) for row in rows]

    def mark_child_ack(self, owner, workflow, ordinal, child_id, receipt_id):
        """Record a verified internal queue ACK; never acknowledges the queue itself."""
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 512:
            raise ValueError('internal receipt required')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, owner, workflow)
            row = self._row(db, workflow, ordinal)
            if row['child_id'] != child_id or not child_id or row['state'] not in ('completed','failed','cancelled'):
                raise WorkflowConflict('terminal child receipt mismatch')
            if row['internal_acked']:
                if row['internal_ack_receipt'] != receipt_id:
                    raise WorkflowConflict('internal receipt is immutable')
                return False
            db.execute('UPDATE workflow_stages SET internal_acked=1,internal_ack_receipt=? '
                       'WHERE workflow=? AND ordinal=?', (receipt_id, workflow, ordinal))
            return True

    def inspect_final(self, owner, workflow):
        """Read final delivery state without claiming or sending a platform message."""
        with self._db() as db:
            self._owned(db, owner, workflow)
            row = db.execute('SELECT * FROM workflow_final_outbox WHERE workflow=?', (workflow,)).fetchone()
            if row is None:
                return None
            return {'state': row['state'], 'delivery_key': row['delivery_key'],
                    'payload': json.loads(row['payload']), 'receipt_id': row['receipt_id'],
                    'lease_until': row['lease_until']}

    def claim_final(self, owner, workflow, *, lease=60):
        """Fence one external send; expired in-flight sends become unknown, not retries.

        A final claim is held until every terminal internal child ACK has been
        recorded. The caller must send with the returned stable delivery_key and
        call ``ack_final`` only after receiving an explicit platform receipt.
        """
        if isinstance(lease, bool) or not isinstance(lease, (int,float)) or not 1 <= lease <= 3600:
            raise ValueError('invalid final delivery lease')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, owner, workflow)
            row = db.execute('SELECT * FROM workflow_final_outbox WHERE workflow=?', (workflow,)).fetchone()
            if row is None:
                return {'state': 'not_terminal'}
            unacked = db.execute("SELECT count(*) FROM workflow_stages WHERE workflow=? AND child_id IS NOT NULL "
                                 "AND state IN ('completed','failed','cancelled') AND internal_acked=0",
                                 (workflow,)).fetchone()[0]
            if unacked:
                return {'state': 'waiting_for_child_ack', 'count': unacked}
            now = time.time()
            if row['state'] == 'sending' and row['lease_until'] <= now:
                db.execute("UPDATE workflow_final_outbox SET state='unknown' WHERE workflow=?", (workflow,))
                return {'state': 'unknown', 'delivery_key': row['delivery_key']}
            if row['state'] != 'pending':
                return {'state': row['state'], 'delivery_key': row['delivery_key']}
            token = uuid.uuid4().hex
            db.execute("UPDATE workflow_final_outbox SET state='sending',lease_token=?,lease_until=? WHERE workflow=?",
                       (token, now + lease, workflow))
            return {'state': 'sending', 'token': token, 'delivery_key': row['delivery_key'],
                    'payload': json.loads(row['payload']), 'lease_until': now + lease}

    def ack_final(self, owner, workflow, token, delivery_key, receipt_id):
        """Mark a claimed send delivered only with a matching explicit receipt."""
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 512:
            raise ValueError('platform receipt required')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, owner, workflow)
            row = db.execute('SELECT * FROM workflow_final_outbox WHERE workflow=?', (workflow,)).fetchone()
            if row is None or row['delivery_key'] != delivery_key:
                raise WorkflowConflict('final delivery identity mismatch')
            if row['state'] == 'delivered':
                if row['receipt_id'] != receipt_id:
                    raise WorkflowConflict('final receipt is immutable')
                return False
            if row['state'] != 'sending' or row['lease_token'] != token or row['lease_until'] <= time.time():
                raise WorkflowConflict('final delivery lease unavailable')
            db.execute("UPDATE workflow_final_outbox SET state='delivered',receipt_id=?,lease_token=NULL,lease_until=NULL "
                       'WHERE workflow=?', (receipt_id, workflow))
            return True

    def mark_final_unknown(self, owner, workflow, token):
        """Fence an ambiguous platform send; reconciliation is needed before ACK."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, owner, workflow)
            row = db.execute('SELECT * FROM workflow_final_outbox WHERE workflow=?', (workflow,)).fetchone()
            if row is None or row['state'] != 'sending' or row['lease_token'] != token:
                raise WorkflowConflict('final delivery lease unavailable')
            db.execute("UPDATE workflow_final_outbox SET state='unknown' WHERE workflow=?", (workflow,))
            return 'unknown'

    def reconcile_final(self, owner, workflow, delivery_key, receipt_id):
        """Record a verified existing platform receipt for an unknown send; never resend."""
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 512:
            raise ValueError('platform receipt required')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, owner, workflow)
            row = db.execute('SELECT * FROM workflow_final_outbox WHERE workflow=?', (workflow,)).fetchone()
            if row is None or row['delivery_key'] != delivery_key:
                raise WorkflowConflict('final delivery identity mismatch')
            if row['state'] == 'delivered':
                if row['receipt_id'] != receipt_id:
                    raise WorkflowConflict('final receipt is immutable')
                return False
            if row['state'] != 'unknown':
                raise WorkflowConflict('only an unknown send can be reconciled')
            db.execute("UPDATE workflow_final_outbox SET state='delivered',receipt_id=?,lease_token=NULL,lease_until=NULL "
                       'WHERE workflow=?', (receipt_id, workflow))
            return True

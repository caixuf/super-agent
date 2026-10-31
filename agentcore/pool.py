"""Durable local worker queue. POSIX job locks fence surviving child processes.

Runner protocol: JSON job on stdin, JSON {ok: true, result: ...} on stdout.
Runner commands are administrator supplied argv arrays; never task supplied shell.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from agentcore import colleague_containment as containment


ORIGIN_FIELDS = ('profile', 'session', 'route', 'channel', 'chat', 'thread', 'turn')
# The dispatcher is only guaranteed to hold these two; the rest are recorded when
# the host supplies them, never inferred from an ambient session.
ORIGIN_REQUIRED = ('profile', 'session')


def normalize_origin(origin):
    """Serialize addressable task ownership, refusing partial or unknown fields.

    Returns the canonical JSON text stored in ``jobs.origin`` (None when no
    ownership was supplied). Blank strings are refused rather than coerced: an
    empty route would look addressable while routing nowhere.
    """
    if origin is None:
        return None
    if not isinstance(origin, dict):
        raise ValueError('origin must be a mapping')
    unknown = set(origin) - set(ORIGIN_FIELDS)
    if unknown:
        raise ValueError('unknown origin fields: ' + ', '.join(sorted(unknown)))
    normalized = {}
    for name in ORIGIN_FIELDS:
        if name not in origin:
            continue
        value = origin[name]
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError('origin %s must be a nonempty string of at most 512 characters' % name)
        normalized[name] = value
    missing = [name for name in ORIGIN_REQUIRED if name not in normalized]
    if missing:
        raise ValueError('origin requires ' + ', '.join(missing))
    return json.dumps(normalized, sort_keys=True)


class Pool:
    def __init__(self, root):
        self._db_lock = threading.RLock()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / 'locks').mkdir(exist_ok=True)
        (self.root / 'attempts').mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', attempt TEXT, heartbeat REAL,
              tries INTEGER NOT NULL DEFAULT 0, max_tries INTEGER NOT NULL,
              replay_safe INTEGER NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS deps (
              job TEXT NOT NULL, dependency TEXT NOT NULL, PRIMARY KEY(job,dependency));
            CREATE TABLE IF NOT EXISTS outbox (
              job TEXT PRIMARY KEY, receipt TEXT NOT NULL,
              acknowledged INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS origin_repairs (
              job TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
              origin TEXT NOT NULL, proof_id TEXT NOT NULL,
              proof_sha256 TEXT NOT NULL, created REAL NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            for name, kind in [('pid', 'INTEGER'), ('pid_start', 'TEXT'), ('deadline', 'REAL'),
                               ('origin', 'TEXT'), ('containment_token', 'TEXT')]:
                if name not in columns:
                    db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + kind)
            outbox_columns = {r[1] for r in db.execute('PRAGMA table_info(outbox)')}
            for name, kind in [('lease_token', 'TEXT'), ('lease_until', 'REAL'),
                               ('receipt_id', 'TEXT'), ('delivered', 'REAL')]:
                if name not in outbox_columns:
                    db.execute('ALTER TABLE outbox ADD COLUMN ' + name + ' ' + kind)

    @contextlib.contextmanager
    def db(self):
        # Serialize local transactions, leaving SQLite to arbitrate processes.
        # Otherwise heartbeat writers on slow storage can exhaust busy_timeout.
        with self._db_lock:
            db = sqlite3.connect(str(self.root / 'pool.db'), timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def submit(self, key, payload, dependencies=(), replay_safe=False, max_tries=2, origin=None):
        """Enqueue work. ``origin`` is the addressable ownership to persist with it.

        A completion can only be routed back to the conversation that submitted the
        work if the submission recorded who that was; resolving it later from
        whatever session happens to be active is the failure this field prevents.
        """
        dependencies = tuple(dependencies)
        if not key or max_tries < 1:
            raise ValueError('dedupe key and positive max_tries required')
        origin = normalize_origin(origin)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE dedupe=?', (key,)).fetchone()
            serialized = json.dumps(payload, sort_keys=True)
            if existing:
                old_deps = {r[0] for r in db.execute('SELECT dependency FROM deps WHERE job=?', (existing['id'],))}
                if (existing['payload'] != serialized or old_deps != set(dependencies)
                        or existing['replay_safe'] != bool(replay_safe) or existing['max_tries'] != max_tries
                        or existing['origin'] != origin):
                    raise ValueError('dedupe key reused with different task')
                return existing['id']
            for dependency in dependencies:
                if not db.execute('SELECT 1 FROM jobs WHERE id=?', (dependency,)).fetchone():
                    raise ValueError('unknown dependency')
            job = uuid.uuid4().hex
            db.execute('INSERT INTO jobs(id,dedupe,payload,max_tries,replay_safe,created,origin) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (job, key, serialized, max_tries, bool(replay_safe), time.time(), origin))
            db.executemany('INSERT INTO deps VALUES(?,?)', [(job, d) for d in set(dependencies)])
            return job

    def origin(self, job):
        """Ownership persisted at submit time, or None for work admitted before it was recorded."""
        with self.db() as db:
            row = db.execute('SELECT origin FROM jobs WHERE id=?', (job,)).fetchone()
        return json.loads(row['origin']) if row is not None and row['origin'] else None

    def attach_origin(self, job, *, origin, expected_payload_sha256, proof_id, proof_sha256):
        """Administrative recovery of one proven, terminal unaddressed result.

        The host must establish request/tool provenance before calling this API.
        No active-chat inference, resubmission, payload/result edits or queue ACK.
        Existing ownership and evidence cannot be replaced, even by a retry.
        """
        if (not isinstance(job,str) or not re.fullmatch('[0-9a-f]{32}',job)
                or not isinstance(proof_id,str) or not 1 <= len(proof_id) <= 128
                or any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value)
                       for value in (expected_payload_sha256,proof_sha256))):
            raise ValueError('invalid origin recovery proof')
        serialized = normalize_origin(origin)
        if serialized is None or not origin.get('route'):raise ValueError('addressable recovery origin required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,origin,status FROM jobs WHERE id=?',(job,)).fetchone()
            if row is None or hashlib.sha256(row['payload'].encode()).hexdigest()!=expected_payload_sha256:
                raise ValueError('origin recovery payload mismatch')
            proof = db.execute('SELECT * FROM origin_repairs WHERE job=?',(job,)).fetchone()
            if proof is not None:
                if (proof['payload_sha256'],proof['origin'],proof['proof_id'],proof['proof_sha256'],row['origin']) != (
                        expected_payload_sha256,serialized,proof_id,proof_sha256,serialized):
                    raise ValueError('origin recovery conflicts with recorded proof')
                return dict(job=job,attached=True,already_attached=True,proof_sha256=proof_sha256)
            pending = db.execute('SELECT * FROM outbox WHERE job=?',(job,)).fetchone()
            if (row['origin'] is not None or row['status'] not in ('completed','failed','cancelled')
                    or pending is None or pending['acknowledged'] or pending['receipt_id'] or pending['delivered']
                    or pending['lease_token'] is not None):
                raise ValueError('origin recovery requires an untouched terminal unowned receipt')
            db.execute('INSERT INTO origin_repairs VALUES(?,?,?,?,?,?)',
                (job,expected_payload_sha256,serialized,proof_id,proof_sha256,time.time()))
            changed = db.execute('UPDATE jobs SET origin=? WHERE id=? AND origin IS NULL AND payload=?',
                                 (serialized,job,row['payload']))
            if changed.rowcount!=1:raise RuntimeError('origin recovery changed concurrently')
            return dict(job=job,attached=True,already_attached=False,proof_sha256=proof_sha256)

    def _lock(self, job):
        fd = os.open(str(self.root / 'locks' / job), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            return None

    def _terminal(self, db, job, status, result):
        receipt = json.dumps({'job': job, 'status': status, 'result': result})
        db.execute('UPDATE jobs SET status=?,result=?,heartbeat=NULL WHERE id=?', (status, json.dumps(result), job))
        db.execute('INSERT OR IGNORE INTO outbox(job,receipt) VALUES(?,?)', (job, receipt))

    def claim(self, lease=60):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created").fetchall()
            for row in rows:
                if row['status'] == 'running':
                    self._stop_expired_runner(row)
                if row['status'] == 'running' and (row['heartbeat'] or 0) > time.time() - lease:
                    continue
                fd = self._lock(row['id'])
                if fd is None:
                    self._stop_expired_runner(row)
                    continue  # A surviving runner still owns this job, even after lease expiry.
                keep = False
                try:
                    if row['cancel']:
                        self._terminal(db, row['id'], 'cancelled', {'reason': 'cancelled'})
                        continue
                    if row['status'] == 'running':
                        token = row['containment_token'] if 'containment_token' in row.keys() else None
                        safe = row['replay_safe'] and row['tries'] < row['max_tries']
                        if safe and token:
                            if containment.tagged(token):
                                sw = containment.sweep(token, grace=0.1, timeout=0.2)
                                if sw.get('survivors') or not sw.get('token_scan_empty'):
                                    safe = False
                        elif not row['replay_safe'] or row['tries'] >= row['max_tries']:
                            safe = False
                        if not safe:
                            self._terminal(db, row['id'], 'failed', {'reason': 'interrupted; replay unavailable'})
                            continue
                    deps = [r[0] for r in db.execute('SELECT j.status FROM deps d JOIN jobs j ON j.id=d.dependency WHERE d.job=?', (row['id'],))]
                    if any(s in ('failed', 'cancelled') for s in deps):
                        self._terminal(db, row['id'], 'failed', {'reason': 'dependency failed'})
                        continue
                    if any(s != 'completed' for s in deps):
                        continue
                    token = uuid.uuid4().hex
                    # The attempt token travels in the child environment, so even
                    # descendants that leave the process group can be found later.
                    attempt_token = containment.new_token()
                    db.execute("UPDATE jobs SET status='running',attempt=?,heartbeat=?,tries=tries+1,pid=NULL,pid_start=NULL,deadline=NULL,containment_token=? WHERE id=?",
                               (token, time.time(), attempt_token, row['id']))
                    keep = True
                    return dict(row, attempt=token, containment_token=attempt_token, tries=row['tries'] + 1), fd
                finally:
                    if not keep:
                        os.close(fd)
        return None

    @staticmethod
    def _process_start(pid):
        try:
            # comm may contain spaces and parentheses; split only after its last ')'.
            fields = Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')', 1)[1].split()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return boot + ':' + fields[19]  # starttime = field 22
        except (OSError, IndexError):
            return None

    def _stop_expired_runner(self, row):
        """Linux orphan recovery; match boot-local PID starttime before signalling.

        A worker crash before PID registration remains conservatively locked.
        Runners must stay in their assigned process group; this is not a sandbox.
        """
        if not row['cancel'] and (not row['deadline'] or row['deadline'] > time.time()):
            return
        token = row['containment_token'] if 'containment_token' in row.keys() else None
        if token:
            # Covers descendants whose worker died before it could sweep them.
            containment.sweep(token)
        pid = row['pid']
        if not pid or not row['pid_start'] or self._process_start(pid) != row['pid_start']:
            return
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def heartbeat(self, job, attempt):
        with self.db() as db:
            updated = db.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND attempt=? AND status='running' AND cancel=0", (time.time(), job, attempt))
            return updated.rowcount == 1

    def is_current(self, job, attempt):
        with self.db() as db:
            return db.execute("SELECT 1 FROM jobs WHERE id=? AND attempt=? AND status='running' AND cancel=0", (job, attempt)).fetchone() is not None

    def finish(self, job, attempt, ok, result):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE id=? AND attempt=? AND status='running'", (job, attempt)).fetchone()
            if row is None:
                return False
            if row['cancel']:
                self._terminal(db, job, 'cancelled', {'reason': 'cancelled'})
            elif not ok and row['replay_safe'] and row['tries'] < row['max_tries']:
                is_unquiesced = False
                if isinstance(result, dict):
                    if result.get('attempt_quiescent') is False:
                        is_unquiesced = True
                    elif result.get('error') == 'attempt_not_quiescent':
                        is_unquiesced = True
                    elif result.get('attempt_token_scan_empty') is False:
                        is_unquiesced = True
                if not is_unquiesced:
                    db.execute("UPDATE jobs SET status='queued',heartbeat=NULL WHERE id=?", (job,))
                else:
                    unquiesced_res = dict(result) if isinstance(result, dict) else {'error': str(result)}
                    unquiesced_res['reason'] = 'replay_aborted_unquiesced'
                    self._terminal(db, job, 'failed', unquiesced_res)
            else:
                self._terminal(db, job, 'completed' if ok else 'failed', result)
            return True

    def cancel(self, job):
        with self.db() as db:
            db.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status IN ('queued','running')", (job,))


    def wait_for_user(self, job, *, reason="waiting_user"):
        """Park job as waiting_user so claim skips it (HITL frees worker slot).

        claim() only selects status IN (queued, running), so waiting_user is
        never claimable. Wake with resume_from_user().
        """
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                return False
            if row["status"] in ("completed", "failed", "cancelled"):
                return False
            db.execute(
                "UPDATE jobs SET status=?, heartbeat=NULL WHERE id=?",
                ("waiting_user", job),
            )
            return True

    def await_plan_approval(self, job):
        """Mark job awaiting_plan_approval (non-claimable; frees worker slot)."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                return False
            if row["status"] in ("completed", "failed", "cancelled"):
                return False
            db.execute(
                "UPDATE jobs SET status=?, heartbeat=NULL WHERE id=?",
                ("awaiting_plan_approval", job),
            )
            return True

    def resume_from_user(self, job):
        """Return waiting_user / awaiting_plan_approval job to queued for re-claim."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                return False
            if row["status"] not in ("waiting_user", "awaiting_plan_approval"):
                return False
            db.execute(
                "UPDATE jobs SET status='queued', attempt=NULL, heartbeat=NULL, "
                "pid=NULL, pid_start=NULL, deadline=NULL WHERE id=?",
                (job,),
            )
            return True

    def deny_from_user(self, job, *, reason, policy="cancelled"):
        """Deny HITL with a required reason; no silent resume (Prefer-C1).

        Job must be waiting_user / awaiting_plan_approval. ``policy`` is
        ``cancelled`` (default) or ``failed``. Reason is trimmed to <=240 chars.
        """
        if reason is None:
            raise ValueError("deny_reason_required")
        text_reason = str(reason).strip()
        if not text_reason:
            raise ValueError("deny_reason_required")
        text_reason = text_reason[:240]
        if policy not in ("cancelled", "failed"):
            raise ValueError("deny_policy_invalid")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                return False
            if row["status"] not in ("waiting_user", "awaiting_plan_approval"):
                return False
            self._terminal(db, job, policy, {
                "reason": text_reason,
                "hitl": "denied",
                "event": "hitl_denied",
            })
            return True

    def status(self, job):
        with self.db() as db:
            row = db.execute('SELECT id,status,attempt,tries,cancel,result,heartbeat,created,deadline FROM jobs WHERE id=?', (job,)).fetchone()
            return dict(row) if row else None

    def receipts(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT receipt FROM outbox WHERE acknowledged=0')]

    def claim_receipt(self, lease=60, skip=(), *, not_before=None, origin_filter=None):
        """Single-consumer lease; transport must dedupe by job across crash windows.

        ``skip`` names receipts the caller already knows it cannot address (for example
        a task admitted before its route was persisted). Skipping happens in the ledger,
        not by acknowledging: such a row stays pending for an operator while a later
        deliverable completion is not blocked behind it.
        """
        if lease <= 0:
            raise ValueError('lease must be positive')
        if not_before is not None and (not isinstance(not_before, (int, float)) or not 0 <= not_before < float('inf')):
            raise ValueError('not_before must be a finite nonnegative timestamp')
        skip = tuple(skip)
        if any(not isinstance(job, str) or not job for job in skip):
            raise ValueError('skip must contain task ids')
        if origin_filter is not None and not callable(origin_filter):
            raise ValueError('origin_filter must be callable')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            query = 'SELECT * FROM outbox WHERE acknowledged=0 AND (lease_until IS NULL OR lease_until<?)'
            values = [time.time()]
            if origin_filter is not None:
                # Apply the address policy inside the same transaction as the
                # claim. No unaddressable receipt is leased merely because it
                # lies beyond an operator inspection page.
                def allowed(raw):
                    try:
                        address = json.loads(raw) if raw else None
                        return int(bool(origin_filter(address)))
                    except (TypeError, ValueError):
                        return 0
                db.create_function('agentcore_receipt_addressable', 1, allowed)
                query += (' AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id=outbox.job '
                          'AND agentcore_receipt_addressable(jobs.origin)=1)')
            if not_before is not None:
                query += ' AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id=outbox.job AND jobs.created>=?)'
                values.append(not_before)
            if skip:
                query += ' AND job NOT IN (%s)' % ','.join('?' * len(skip))
                values.extend(skip)
            row = db.execute(query + ' ORDER BY rowid LIMIT 1', values).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute('UPDATE outbox SET lease_token=?,lease_until=? WHERE job=?', (token, time.time() + lease, row['job']))
            return {'token': token, 'receipt': json.loads(row['receipt'])}

    def acknowledge(self, job, token=None, *, receipt_id=None):
        """Mark a delivered receipt acknowledged, recording the transport's receipt id.

        ``receipt_id`` is audit trail only: the ack itself is still fenced by the lease
        token, so a stale consumer cannot acknowledge work another consumer now owns.
        """
        if receipt_id is not None and (not isinstance(receipt_id, str) or not receipt_id.strip()):
            raise ValueError('receipt id must be a nonempty string when given')
        delivered_at = time.time() if receipt_id is not None else None
        with self.db() as db:
            if token is None:
                updated = db.execute('UPDATE outbox SET acknowledged=1, receipt_id=COALESCE(?,receipt_id), '
                                     'delivered=COALESCE(?,delivered) WHERE job=? AND lease_token IS NULL '
                                     'AND acknowledged=0', (receipt_id, delivered_at, job))
            else:
                updated = db.execute('UPDATE outbox SET acknowledged=1, receipt_id=COALESCE(?,receipt_id), '
                                     'delivered=COALESCE(?,delivered) WHERE job=? AND lease_token=? '
                                     'AND lease_until>? AND acknowledged=0',
                                     (receipt_id, delivered_at, job, token, time.time()))
            return updated.rowcount == 1

    def delivery_record(self, job):
        """How a completion was delivered, or None while it is still pending."""
        with self.db() as db:
            row = db.execute('SELECT acknowledged,receipt_id,delivered FROM outbox WHERE job=?',
                             (job,)).fetchone()
        return dict(row) if row is not None else None

    def run_once(self, command, timeout=300, lease=60, max_output=1048576, max_file=67108864):
        if timeout <= 0 or lease <= 0 or max_output <= 0 or max_file <= 0:
            raise ValueError('budgets must be positive')
        claimed = self.claim(lease)
        if not claimed:
            return False
        return self.run_claimed(claimed, command, timeout, lease, max_output, max_file)

    def run_claimed(self, claimed, command, timeout=300, lease=60, max_output=1048576, max_file=67108864):
        """Execute a claim owned by this process; caller transfers lock ownership here."""
        row, fd = claimed
        job, attempt = row['id'], row['attempt']
        token = row.get('containment_token')
        directory = self.root / 'attempts' / attempt
        proc = None
        try:
            if timeout <= 0 or lease <= 0 or max_output <= 0 or max_file <= 0:
                raise ValueError('budgets must be positive')
            directory.mkdir(mode=0o700)
            with (directory / 'input.json').open('w+') as inp, (directory / 'stdout').open('w+') as out, (directory / 'stderr').open('w+') as err:
                json.dump({'job': job, 'attempt': attempt, 'payload': json.loads(row['payload'])}, inp)
                inp.seek(0)
                if not self.is_current(job, attempt):
                    raise RuntimeError('claim cancelled before runner launch')
                import sys
                guarded_command = [sys.executable, str(Path(__file__).with_name('exec_runner.py')),
                                   str(max_file)] + command
                env = dict(os.environ)
                if token:
                    env[containment.ENV] = token
                proc = subprocess.Popen(guarded_command, stdin=inp, stdout=out, stderr=err, cwd=directory,
                                        start_new_session=True, pass_fds=(fd,), env=env)
                with self.db() as db:
                    db.execute("UPDATE jobs SET pid=?,pid_start=?,deadline=? WHERE id=? AND attempt=? AND status='running'",
                               (proc.pid, self._process_start(proc.pid), time.time() + timeout, job, attempt))
                deadline = time.monotonic() + timeout
                next_heartbeat = time.monotonic() + lease / 3
                reason = None
                while proc.poll() is None:
                    if time.monotonic() >= next_heartbeat:
                        current = self.heartbeat(job, attempt)
                        next_heartbeat = time.monotonic() + lease / 3
                    else:
                        current = self.is_current(job, attempt)
                    if not current:
                        reason = 'cancelled or lease revoked'
                    elif time.monotonic() > deadline:
                        reason = 'timeout'
                    elif os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > max_output:
                        reason = 'output budget exceeded'
                    if reason:
                        break
                    time.sleep(min(0.2, lease / 4))
                # Always quiesce descendants before releasing the inherited job lock.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                if reason is None:
                    if time.monotonic() > deadline:
                        reason = 'timeout'
                    elif os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > max_output:
                        reason = 'output budget exceeded'
                result = {'reason': reason or 'runner failed', 'exit_code': proc.returncode}
                ok = False
                if reason is None and proc.returncode == 0:
                    out.seek(0)
                    try:
                        response = json.loads(out.read(max_output))
                        ok = isinstance(response, dict) and response.get('ok') is True
                        result = response if isinstance(response, dict) else {'reason': 'invalid receipt'}
                    except (ValueError, UnicodeError):
                        result = {'reason': 'invalid JSON receipt'}
                elif reason is None:
                    # Preserve a runner's bounded public error code on exit 1.
                    # Do not expose arbitrary stdout/stderr in the queue ledger.
                    out.seek(0)
                    try:
                        failure = json.loads(out.read(max_output))
                        code = failure.get('error') if isinstance(failure, dict) else None
                        if (failure.get('ok') is False and isinstance(code, str)
                                and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code)):
                            result['runner_error'] = code
                    except (ValueError, UnicodeError, AttributeError):
                        pass
                evidence = containment.sweep(token) if token else None
                if evidence is not None:
                    has_survivors = bool(evidence.get('survivors'))
                    token_empty = evidence.get('token_scan_empty', True)
                    main_exited = (proc.poll() is not None)
                    attempt_quiescent = main_exited and token_empty and not has_survivors
                    result['attempt_quiescent'] = attempt_quiescent
                    result['attempt_token_scan_empty'] = token_empty
                    if not attempt_quiescent or has_survivors:
                        ok = False
                        result.setdefault('reason', 'attempt did not quiesce')
                self.finish(job, attempt, ok, result)
        except Exception as exc:
            self.finish(job, attempt, False, {'reason': 'execution error', 'type': type(exc).__name__,
                                             'sqlite_code': getattr(exc, 'sqlite_errorname', None)})
        finally:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            os.close(fd)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    submit = sub.add_parser('submit')
    submit.add_argument('--key', required=True)
    submit.add_argument('--payload-file', required=True)
    submit.add_argument('--dependency', action='append', default=[])
    submit.add_argument('--replay-safe', action='store_true')
    for action in ('status', 'cancel', 'ack'):
        action_parser = sub.add_parser(action)
        action_parser.add_argument('job')
        if action == 'ack':
            action_parser.add_argument('--token')
    sub.add_parser('receipts')
    sub.add_parser('claim-receipt')
    worker = sub.add_parser('worker')
    worker.add_argument('--runner-config', required=True, help='JSON argv array; trusted administrator config')
    worker.add_argument('--timeout', type=float, default=300)
    worker.add_argument('--once', action='store_true')
    worker.add_argument('--drain', action='store_true', help='Process all currently runnable work, then exit')
    worker.add_argument('--workers', type=int, default=1)
    worker.add_argument('--backend', choices=['stdlib', 'flowcoro'], default='stdlib')
    worker.add_argument('--max-output', type=int, default=1048576)
    worker.add_argument('--max-file', type=int, default=67108864)
    args = parser.parse_args()
    pool = Pool(args.root)
    if args.action == 'submit':
        print(pool.submit(args.key, json.loads(Path(args.payload_file).read_text()), args.dependency, args.replay_safe))
    elif args.action == 'status':
        print(json.dumps(pool.status(args.job)))
    elif args.action == 'cancel':
        pool.cancel(args.job)
    elif args.action == 'ack':
        if not pool.acknowledge(args.job, args.token):
            parser.exit(1, 'receipt absent, already acknowledged, or lease token invalid\n')
    elif args.action == 'receipts':
        print(json.dumps(pool.receipts()))
    elif args.action == 'claim-receipt':
        print(json.dumps(pool.claim_receipt()))
    else:
        command = json.loads(Path(args.runner_config).read_text())
        if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
            parser.error('runner-config must be a nonempty argv array')
        if args.once:
            pool.run_once(command, timeout=args.timeout, max_output=args.max_output, max_file=args.max_file)
        else:
            from agentcore.worker import Worker
            import threading
            stop = threading.Event()
            previous = {}
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            try:
                stats = Worker(pool, command, workers=args.workers, backend=args.backend,
                               timeout=args.timeout, max_output=args.max_output, max_file=args.max_file).run(stop, drain=args.drain)
                print(json.dumps(stats))
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


if __name__ == '__main__':
    main()

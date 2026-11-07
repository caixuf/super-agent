"""Durable local worker queue. POSIX job locks fence surviving child processes.

Runner protocol: JSON job on stdin, JSON {ok: true, result: ...} on stdout.
Runner commands are administrator supplied argv arrays; never task supplied shell.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import uuid

from agentcore import colleague_containment as containment

logger = logging.getLogger(__name__)


ORIGIN_FIELDS = ('profile', 'session', 'route', 'channel', 'chat', 'thread', 'turn')
# The dispatcher is only guaranteed to hold these two; the rest are recorded when
# the host supplies them, never inferred from an ambient session.
ORIGIN_REQUIRED = ('profile', 'session')


# Prefer-D2 — Dagster-style hard windows on SQLite pool (config; local defaults).
# STARTING: claim stamped running with pid NULL until run_claimed Popen.
# CANCELING: soft cancel() sets cancel=1 while status may still be running.
START_WINDOW_S = 30.0   # fail-closed claim-without-PID (suggest 15–60s)
CANCEL_WINDOW_S = 60.0  # stop-confirmation escalation; never proof of stop
PAUSE_QUIESCE_S = 30.0  # optional: escalate if pause left status=running (deliver-or-reason)


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


def display_stage(row):
    """Prefer-D2 CardView/status stage — never healthy Running for pid-null or cancel=1.

    Returns one of: queued, starting, running, canceling, paused, waiting_user,
    awaiting_plan_approval, completed, failed, cancelled, or the raw status.
    ``paused`` is the CardView label when pool status is waiting_user after pause.
    """
    if row is None:
        return "unknown"
    if hasattr(row, "keys") and not isinstance(row, dict):
        row = dict(row)
    status = str(row.get("status") or "")
    if status in ("completed", "failed", "cancelled"):
        return status
    if row.get("cancel"):
        return "canceling"
    if row.get('execution_state') in ('unconfirmed', 'launching'):
        return "stop_unconfirmed" if row.get('execution_state') == 'unconfirmed' else "starting"
    if row.get('execution_state') == 'supervised':
        # Keep the supervisor identity for later proof validation, but do not
        # present its failed stop observation as a healthy running execution.
        result = row.get('result')
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = None
        if isinstance(result, dict) and result.get('reason') == 'attempt_stop_unproven':
            return 'stop_unconfirmed'
    if status == "waiting_user":
        # pause() aliases to waiting_user; CardView shows paused/waiting_user.
        return "paused"
    if status == "awaiting_plan_approval":
        return status
    if status == "running":
        pid = row.get("pid")
        if pid is None:
            return "starting"
        return "running"
    if status == "queued":
        return "queued"
    return status or "unknown"


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
            CREATE TABLE IF NOT EXISTS card_outbox (
              job TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending',
              error TEXT, updated REAL NOT NULL, progress_state TEXT NOT NULL DEFAULT 'pending');
            CREATE INDEX IF NOT EXISTS card_outbox_pending_order
              ON card_outbox(state, updated, job);
            CREATE TABLE IF NOT EXISTS origin_repairs (
              job TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
              origin TEXT NOT NULL, proof_id TEXT NOT NULL,
              proof_sha256 TEXT NOT NULL, created REAL NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            card_columns = {r[1] for r in db.execute('PRAGMA table_info(card_outbox)')}
            if 'progress_state' not in card_columns:
                db.execute("ALTER TABLE card_outbox ADD COLUMN progress_state TEXT NOT NULL DEFAULT 'pending'")
            db.execute('CREATE INDEX IF NOT EXISTS card_outbox_progress_order '
                       'ON card_outbox(state, progress_state, updated, job)')
            db.execute('CREATE INDEX IF NOT EXISTS card_outbox_progress_health '
                       'ON card_outbox(progress_state, updated, job)')
            columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            for name, kind in [('pid', 'INTEGER'), ('pid_start', 'TEXT'), ('deadline', 'REAL'),
                               ('origin', 'TEXT'), ('containment_token', 'TEXT'),
                               ('cancel_requested_at', 'REAL'),
                               ('execution_state', 'TEXT'), ('supervisor_nonce', 'TEXT')]:
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

    def submit(self, key, payload, dependencies=(), replay_safe=False, max_tries=2, origin=None,
               request_card=False):
        """Enqueue work. ``origin`` is the addressable ownership to persist with it.

        A completion can only be routed back to the conversation that submitted the
        work if the submission recorded who that was; resolving it later from
        whatever session happens to be active is the failure this field prevents.
        """
        dependencies = tuple(dependencies)
        if not key or max_tries < 1:
            raise ValueError('dedupe key and positive max_tries required')
        if type(request_card) is not bool:
            raise ValueError('request_card must be boolean')
        origin = normalize_origin(origin)
        if request_card:
            address = json.loads(origin) if origin else {}
            if (address.get('channel') != 'feishu' or not address.get('route')
                    or not address.get('chat')):
                raise ValueError('card requires a bound messaging origin')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE dedupe=?', (key,)).fetchone()
            serialized = json.dumps(payload, sort_keys=True)
            if existing:
                old_deps = {r[0] for r in db.execute('SELECT dependency FROM deps WHERE job=?', (existing['id'],))}
                old_card = db.execute('SELECT 1 FROM card_outbox WHERE job=?', (existing['id'],)).fetchone() is not None
                if (existing['payload'] != serialized or old_deps != set(dependencies)
                        or existing['replay_safe'] != bool(replay_safe) or existing['max_tries'] != max_tries
                        or existing['origin'] != origin or old_card != request_card):
                    raise ValueError('dedupe key reused with different task')
                return existing['id']
            for dependency in dependencies:
                if not db.execute('SELECT 1 FROM jobs WHERE id=?', (dependency,)).fetchone():
                    raise ValueError('unknown dependency')
            job = uuid.uuid4().hex
            db.execute('INSERT INTO jobs(id,dedupe,payload,max_tries,replay_safe,created,origin) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (job, key, serialized, max_tries, bool(replay_safe), time.time(), origin))
            if request_card:
                db.execute('INSERT INTO card_outbox(job,updated) VALUES(?,?)', (job, time.time()))
            db.executemany('INSERT INTO deps VALUES(?,?)', [(job, d) for d in set(dependencies)])
            return job

    def origin(self, job):
        """Ownership persisted at submit time, or None for work admitted before it was recorded."""
        with self.db() as db:
            row = db.execute('SELECT origin FROM jobs WHERE id=?', (job,)).fetchone()
        return json.loads(row['origin']) if row is not None and row['origin'] else None

    def record_colleague_completion(self, *, source_id, origin, created, status, summary, member):
        """Record a host-verified colleague result in the existing delivery outbox.

        This method does not submit executable work or infer a destination. The
        caller must prove terminal settlement and result/session binding first.
        """
        import math
        if not isinstance(source_id, str) or not re.fullmatch(r"[0-9a-f]{32}", source_id):
            raise ValueError("invalid_colleague_source")
        address = normalize_origin(origin)
        if address is None or not isinstance(origin.get("route"), str) or not origin["route"].strip():
            raise ValueError("bound_delivery_route_required")
        if (isinstance(created, bool) or not isinstance(created, (int, float)) or
                not math.isfinite(created) or created < 0):
            raise ValueError("invalid_colleague_creation_time")
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid_colleague_completion_status")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 12000:
            raise ValueError("invalid_colleague_completion_summary")
        if not isinstance(member, dict) or set(member) != {"role_id", "display_name", "role_label"}:
            raise ValueError("invalid_colleague_identity")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 80 for value in member.values()):
            raise ValueError("invalid_colleague_identity")
        key = "colleague-completion:" + source_id
        job = hashlib.sha256(key.encode("ascii")).hexdigest()[:32]
        payload = json.dumps({"_colleague_role": member["role_id"], "_colleague_name": member["display_name"],
                              "_colleague_label": member["role_label"], "_source": key}, sort_keys=True)
        result = json.dumps({"summary": summary}, sort_keys=True)
        receipt = json.dumps({"job": job, "status": status, "result": {"summary": summary}})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE id=? OR dedupe=?", (job, key)).fetchall()
            if existing:
                if (len(existing) != 1 or existing[0]["id"] != job or existing[0]["dedupe"] != key
                        or existing[0]["payload"] != payload or existing[0]["origin"] != address
                        or existing[0]["status"] != status or existing[0]["result"] != result
                        or existing[0]["created"] != created):
                    raise ValueError("colleague_completion_conflict")
                if db.execute("SELECT 1 FROM outbox WHERE job=?", (job,)).fetchone() is None:
                    raise RuntimeError("colleague_completion_receipt_missing")
                return job
            db.execute("INSERT INTO jobs(id,dedupe,payload,status,max_tries,replay_safe,created,result,origin,execution_state) "
                       "VALUES(?,?,?,?,1,0,?,?,?,'finished')", (job, key, payload, status, created, result, address))
            db.execute("INSERT INTO outbox(job,receipt) VALUES(?,?)", (job, receipt))
        return job

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

    def _terminal(self, db, job, status, result, *, attempt=None):
        """Conditional terminal (Prefer-D6): only non-terminal rows may flip.

        Optional ``attempt`` fences the update so a stale/superseded attempt cannot
        clobber a live claimer. Outbox uses INSERT OR IGNORE so the first receipt wins.
        Returns True when the jobs row was updated.
        """
        receipt = json.dumps({'job': job, 'status': status, 'result': result}, ensure_ascii=False)
        if attempt is None:
            updated = db.execute(
                "UPDATE jobs SET status=?,result=?,heartbeat=NULL "
                "WHERE id=? AND status NOT IN ('completed','failed','cancelled')",
                (status, json.dumps(result, ensure_ascii=False), job),
            )
        else:
            updated = db.execute(
                "UPDATE jobs SET status=?,result=?,heartbeat=NULL "
                "WHERE id=? AND attempt=? AND status NOT IN ('completed','failed','cancelled')",
                (status, json.dumps(result, ensure_ascii=False), job, attempt),
            )
        if updated.rowcount == 1:
            db.execute('INSERT OR IGNORE INTO outbox(job,receipt) VALUES(?,?)', (job, receipt))
            if status in ('completed', 'failed'):
                try:
                    from agentcore.memory.config import get_config
                    if get_config().trace_db_path().is_file():
                        from agentcore.memory.memory_plasticity import apply_task_outcome
                        failure_text = ''
                        if status == 'failed' and isinstance(result, dict):
                            failure_text = str(result.get('error') or result.get('reason') or result.get('stderr') or '')
                        apply_task_outcome(job, passed=(status == 'completed'), failure_text=failure_text)
                except Exception:
                    pass
        return updated.rowcount == 1

    def claim(self, lease=60):
        # Prefer-D2: clear STARTING/CANCELING hard windows before selecting work.
        # Pool remains ledger authority; liveness tick may also call enforce_windows.
        self.enforce_windows()
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
                    if row['status'] == 'running' and not self._attempt_stopped(row):
                        continue
                    if row['cancel']:
                        if self._attempt_stopped(row):
                            self._terminal(db, row['id'], 'cancelled', {'reason': 'cancelled'})
                        continue
                    if row['status'] == 'running':
                        if not self._replay_allowed(row):
                            self._terminal(db, row['id'], 'failed', {'reason': 'interrupted; replay unavailable'})
                            continue
                    deps = [r[0] for r in db.execute('SELECT j.status FROM deps d JOIN jobs j ON j.id=d.dependency WHERE d.job=?', (row['id'],))]
                    if any(s in ('failed', 'cancelled') for s in deps):
                        self._terminal(db, row['id'], 'failed', {'reason': 'dependency failed'})
                        continue
                    if any(s != 'completed' for s in deps):
                        continue
                    token = uuid.uuid4().hex
                    # The token assists legacy cleanup; only the supervisor receipt
                    # proves that this trusted local process tree has stopped.
                    attempt_token = containment.new_token()
                    db.execute("UPDATE jobs SET status='running',attempt=?,heartbeat=?,tries=tries+1,pid=NULL,pid_start=NULL,deadline=NULL,containment_token=?,execution_state='claimed',supervisor_nonce=NULL WHERE id=?",
                               (token, time.time(), attempt_token, row['id']))
                    keep = True
                    return dict(row, attempt=token, containment_token=attempt_token,
                                execution_state='claimed', supervisor_nonce=None,
                                tries=row['tries'] + 1), fd
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

        Supervised attempts use the supervisor stop protocol. Legacy cleanup is
        best effort only and cannot certify descendant exit. This is not a sandbox.
        """
        if not row['cancel'] and (not row['deadline'] or row['deadline'] > time.time()):
            return
        if row['execution_state'] == 'supervised':
            pid = row['pid']
            if pid and row['pid_start'] and self._process_start(pid) == row['pid_start']:
                try:
                    # Killing the supervisor would orphan precisely the children
                    # it must adopt and reap. Its stop protocol performs cleanup.
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
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

    def _attempt_stopped(self, row):
        """Private, attempt-bound supervisor receipt, never a runner JSON assertion.

        Private queue files and the administrator runner are trusted. This proves
        supervised process-tree exit, not hostile-code containment or remote work.
        """
        row = dict(row)
        if not row.get('attempt') and not row.get('tries') and not row.get('pid'):
            return True
        # Every executor must atomically leave 'claimed' before any side effect.
        # A terminal/reclaimed attempt then cannot pass its admission fence.
        if row.get('execution_state') == 'claimed' and row.get('pid') is None:
            return True
        if row.get('execution_state') == 'finished':
            return True
        if row.get('execution_state') != 'supervised' or not row.get('supervisor_nonce'):
            return False
        try:
            path = self.root / 'attempts' / row['attempt'] / 'stop.json'
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                import stat
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4096
                        or info.st_mode & 0o077 or info.st_nlink != 1
                        or info.st_uid != os.getuid()):
                    return False
                evidence = json.loads(os.read(fd, 4097))
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError):
            return False
        return (isinstance(evidence, dict)
                and evidence.get('job') == row['id']
                and evidence.get('attempt') == row['attempt']
                and evidence.get('token') == row.get('containment_token')
                and evidence.get('nonce') == row['supervisor_nonce']
                and evidence.get('supervisor_pid') == row.get('pid')
                and isinstance(row.get('pid_start'), str) and bool(row['pid_start'])
                and evidence.get('supervisor_start') == row.get('pid_start')
                and evidence.get('quiescent') is True
                and type(evidence.get('exit_code')) is int
                and type(evidence.get('child_started')) is bool
                and (evidence.get('proof_scope') == 'subreaper_tree'
                     or (evidence.get('proof_scope') == 'not_started'
                         and evidence.get('child_started') is False)))

    def _replay_allowed(self, row):
        """Whether an expired running job may be re-queued (claim / Prefer-D2 shared)."""
        if not self._attempt_stopped(row):
            return False
        if isinstance(row, dict):
            token = row.get("containment_token")
            replay_safe = bool(row.get("replay_safe"))
            tries = int(row.get("tries") or 0)
            max_tries = int(row.get("max_tries") or 0)
        else:
            token = row["containment_token"] if "containment_token" in row.keys() else None
            replay_safe = bool(row["replay_safe"])
            tries = int(row["tries"] or 0)
            max_tries = int(row["max_tries"] or 0)
        safe = replay_safe and tries < max_tries
        if dict(row).get('execution_state') == 'supervised':
            return safe  # Already verified the attempt-bound tree receipt above.
        if safe and token:
            if containment.tagged(token):
                sw = containment.sweep(token, grace=0.1, timeout=0.2)
                if sw.get("survivors") or not sw.get("token_scan_empty"):
                    safe = False
        elif not replay_safe or tries >= max_tries:
            safe = False
        return safe

    def enforce_windows(self, now=None, *, start_window_s=None, cancel_window_s=None):
        """Prefer-D2 windows revoke admission or escalate cleanup, never invent stop.

        STARTING: status=running AND pid IS NULL past START_WINDOW_S (clock = claim heartbeat)
          → terminal failed reason=starting_timeout / claim_without_pid.
        CANCELING requires a never-started job or an attempt-bound supervisor receipt.
        Legacy attempts without stop evidence stay canceling, even past the window.
        """
        at = time.time() if now is None else float(now)
        start_w = float(START_WINDOW_S if start_window_s is None else start_window_s)
        cancel_w = float(CANCEL_WINDOW_S if cancel_window_s is None else cancel_window_s)
        if start_w <= 0 or cancel_w <= 0:
            raise ValueError("window durations must be positive")
        forced = []
        kill_rows = []
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') OR "
                "(cancel=1 AND status IN ('waiting_user','awaiting_plan_approval'))"
            ).fetchall()
            for row in rows:
                row = dict(row)
                job_id = row["id"]
                # CANCELING escalation window; elapsed time is not stop evidence.
                if row.get("cancel"):
                    stamp = row.get("cancel_requested_at")
                    if stamp is None:
                        # Legacy soft-cancel without stamp: use heartbeat/created as floor.
                        stamp = row.get("heartbeat") or row.get("created") or at
                    age = at - float(stamp)
                    if age > cancel_w:
                        self._stop_expired_runner(row)
                        if self._attempt_stopped(row):
                            self._terminal(
                                db, job_id, "cancelled",
                                {"reason": "cancelled", "event": "canceling_stop_confirmed"},
                            )
                            forced.append({"id": job_id, "status": "cancelled",
                                           "reason": "cancelled"})
                        else:
                            db.execute("UPDATE jobs SET result=? WHERE id=?",
                                       (json.dumps({"reason": "cancel_stop_unproven"}), job_id))
                        continue
                    # Within window: still attempt kill/sweep (soft cancel happy path assist).
                    if row.get("status") in ("running", "waiting_user", "awaiting_plan_approval"):
                        kill_rows.append(row)
                    continue
                # STARTING fail-closed: running + pid NULL past window.
                # Lock held (claimer alive, never registered pid) -> starting_timeout.
                # Lock free (worker dead): preserve interrupted when replay unavailable;
                # otherwise still fail-closed starting_timeout (do not silently replay).
                if row.get("status") == "running" and row.get("pid") is None:
                    clock = row.get("heartbeat")
                    if clock is None:
                        clock = row.get("created") or at
                    if at - float(clock) > start_w:
                        if not self._attempt_stopped(row):
                            db.execute("UPDATE jobs SET result=? WHERE id=?",
                                       (json.dumps({"reason": "starting_stop_unproven"}), job_id))
                            continue
                        lock_fd = self._lock(job_id)
                        if lock_fd is None:
                            reason = "starting_timeout"
                            event = "claim_without_pid"
                        else:
                            try:
                                if not self._replay_allowed(row):
                                    reason = "interrupted; replay unavailable"
                                    event = None
                                else:
                                    reason = "starting_timeout"
                                    event = "claim_without_pid"
                            finally:
                                os.close(lock_fd)
                        payload = {"reason": reason}
                        if event:
                            payload["event"] = event
                        self._terminal(db, job_id, "failed", payload)
                        forced.append({"id": job_id, "status": "failed",
                                       "reason": reason})
                        continue
        # Best-effort kills outside the write transaction for soft-cancel within window.
        for row in kill_rows:
            try:
                self._stop_expired_runner(row)
            except Exception:
                pass
        return forced


    def _runner_identity_gone(self, row):
        """True when a registered PID identity is gone (exited / recycled).

        Prefer-D6 dead evidence — not mere heartbeat silence. pid NULL is Prefer-D2
        STARTING territory; silence alone must not invent terminal here.
        """
        if isinstance(row, dict):
            pid = row.get('pid')
            pid_start = row.get('pid_start')
        else:
            pid = row['pid']
            pid_start = row['pid_start']
        if not pid or not pid_start:
            return False
        current = self._process_start(pid)
        return current is None or current != pid_start

    def reconcile_watchdog(self, now=None, *, lease=60):
        """Prefer-D6: OpenHands-style watchdog reconcile on the SQLite pool ledger.

        One clear sweeper: runs Prefer-D2 ``enforce_windows`` first, then closes
        finish-callback-loss into conditional terminal OR replay-safe requeue when
        evidence proves the supervised process tree stopped (bound stop receipt,
        PID identity gone, job lock free and past lease). A legacy PID/token scan
        alone is insufficient. Never marks terminal from stall silence alone.
        Watchdog passes the same attempt fence as finish/heartbeat — never force-
        terminals with a guessed/null attempt that could clobber a live claimer.
        Also observes expired outbox leases (reclaimable; logs delivered-missing-ack).
        Pool remains the sole ledger authority — no Temporal / second scheduler.
        """
        if lease <= 0:
            raise ValueError('lease must be positive')
        at = time.time() if now is None else float(now)
        forced = list(self.enforce_windows(now=at))
        actions = []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(
                "SELECT * FROM jobs WHERE status='running'"
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                job_id = row['id']
                attempt = row.get('attempt')
                if not attempt:
                    continue
                hb = row.get('heartbeat')
                if hb is None or (at - float(hb)) <= float(lease):
                    continue
                if not self._runner_identity_gone(row):
                    continue
                token = row.get('containment_token')
                if token and row.get('execution_state') != 'supervised':
                    try:
                        sw = containment.sweep(token, grace=0.05, timeout=0.15)
                        if sw.get('survivors') or not sw.get('token_scan_empty', True):
                            continue
                    except Exception:
                        continue
                lock_fd = self._lock(job_id)
                if lock_fd is None:
                    continue
                try:
                    fresh = db.execute(
                        "SELECT * FROM jobs WHERE id=? AND attempt=? AND status='running'",
                        (job_id, attempt),
                    ).fetchone()
                    if fresh is None:
                        continue
                    fresh = dict(fresh)
                    if not self._attempt_stopped(fresh):
                        continue
                    if fresh.get('cancel'):
                        ok = self._terminal(
                            db, job_id, 'cancelled',
                            {'reason': 'cancelled', 'event': 'watchdog_finish_callback_lost'},
                            attempt=attempt,
                        )
                        if ok:
                            actions.append({
                                'id': job_id, 'status': 'cancelled',
                                'reason': 'cancelled',
                                'event': 'watchdog_finish_callback_lost',
                            })
                        continue
                    if self._replay_allowed(fresh):
                        updated = db.execute(
                            "UPDATE jobs SET status='queued',heartbeat=NULL,attempt=NULL,"
                            "pid=NULL,pid_start=NULL,deadline=NULL,containment_token=NULL,"
                            "execution_state='finished',supervisor_nonce=NULL "
                            "WHERE id=? AND attempt=? AND status='running'",
                            (job_id, attempt),
                        )
                        if updated.rowcount == 1:
                            actions.append({
                                'id': job_id, 'status': 'queued',
                                'reason': 'finish_callback_lost_requeue',
                                'event': 'watchdog_finish_callback_lost',
                            })
                        continue
                    ok = self._terminal(
                        db, job_id, 'failed',
                        {
                            'reason': 'finish_callback_lost',
                            'event': 'watchdog_finish_callback_lost',
                        },
                        attempt=attempt,
                    )
                    if ok:
                        actions.append({
                            'id': job_id, 'status': 'failed',
                            'reason': 'finish_callback_lost',
                            'event': 'watchdog_finish_callback_lost',
                        })
                finally:
                    os.close(lock_fd)

        outbox_obs = self.observe_outbox_reclaims(now=at)
        return {
            'windows': forced,
            'actions': actions,
            'outbox': outbox_obs,
        }

    def observe_outbox_reclaims(self, now=None):
        """Prefer-D6 thin: observe expired outbox leases (at-least-once; no exactly-once).

        Wrong/stale lease_token still cannot ack (claim_receipt fencing unchanged).
        Returns counts; logs when a reclaimable row has delivered set but missing ack
        (crash after transport success before ack → redelivery OK).
        """
        at = time.time() if now is None else float(now)
        with self.db() as db:
            rows = db.execute(
                "SELECT job, lease_token, lease_until, delivered, acknowledged "
                "FROM outbox WHERE acknowledged=0"
            ).fetchall()
        reclaimable = 0
        delivered_missing_ack = 0
        for row in rows:
            row = dict(row)
            until = row.get('lease_until')
            if until is None:
                continue
            if float(until) >= at:
                continue
            reclaimable += 1
            if row.get('lease_token') and row.get('delivered') is not None:
                delivered_missing_ack += 1
                logger.info(
                    'prefer-d6 outbox reclaim after delivered missing ack job=%s',
                    row.get('job'),
                )
        return {
            'reclaimable': reclaimable,
            'delivered_missing_ack': delivered_missing_ack,
        }

    def heartbeat(self, job, attempt):
        with self.db() as db:
            updated = db.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND attempt=? AND status='running' AND cancel=0", (time.time(), job, attempt))
            return updated.rowcount == 1

    def is_current(self, job, attempt):
        with self.db() as db:
            return db.execute("SELECT 1 FROM jobs WHERE id=? AND attempt=? AND status='running' AND cancel=0", (job, attempt)).fetchone() is not None

    def begin_execution(self, job, attempt, *, mode='synchronous'):
        """Trusted executor admission fence; claim alone never authorizes effects.

        Synchronous owners acknowledge their own callback completion with finish.
        A crash while synchronous/launching is uncertain, not safe to auto-replay.
        """
        if mode not in ('synchronous', 'launching'):
            raise ValueError('invalid_execution_mode')
        with self.db() as db:
            changed = db.execute(
                "UPDATE jobs SET execution_state=? WHERE id=? AND attempt=? "
                "AND status='running' AND cancel=0 AND execution_state='claimed'",
                (mode, job, attempt),
            )
            return changed.rowcount == 1

    def finish(self, job, attempt, ok, result):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE id=? AND attempt=? "
                             "AND status IN ('running','waiting_user','awaiting_plan_approval')",
                             (job, attempt)).fetchone()
            if row is None:
                return False
            unquiesced = (isinstance(result, dict) and (
                result.get('attempt_quiescent') is False
                or result.get('error') == 'attempt_not_quiescent'
                or result.get('attempt_token_scan_empty') is False))
            supervised_unknown = (row['execution_state'] in ('supervised', 'launching', 'unconfirmed')
                                  and not self._attempt_stopped(row))
            if unquiesced or supervised_unknown:
                # Persist uncertainty without a terminal outbox or retry. A
                # request/finish callback cannot overrule known surviving work.
                db.execute("UPDATE jobs SET result=?,execution_state=CASE "
                           "WHEN execution_state='supervised' THEN execution_state ELSE 'unconfirmed' END "
                           "WHERE id=? AND attempt=?",
                           (json.dumps({'reason': 'attempt_stop_unproven'}), job, attempt))
                return True
            if row['execution_state'] != 'supervised':
                db.execute("UPDATE jobs SET execution_state='finished' WHERE id=? AND attempt=?",
                           (job, attempt))
            if row['cancel']:
                self._terminal(db, job, 'cancelled', {'reason': 'cancelled'})
            elif row['status'] in ('waiting_user', 'awaiting_plan_approval'):
                # Stop acknowledgement may arrive after a pause. Keep the human
                # gate parked, but let a later resume verify this attempt stopped.
                db.execute("UPDATE jobs SET result=? WHERE id=? AND attempt=?",
                           (json.dumps(result, ensure_ascii=False), job, attempt))
            elif not ok and row['replay_safe'] and row['tries'] < row['max_tries']:
                db.execute("UPDATE jobs SET status='queued',heartbeat=NULL WHERE id=?", (job,))
            else:
                self._terminal(db, job, 'completed' if ok else 'failed', result)
            return True

    def cancel(self, job):
        """Soft-cancel: set cancel=1 and stamp CANCELING clock (Prefer-D2).

        Does not force terminal — runner / claim / enforce_windows observe the flag.
        ``cancel_requested_at`` is set once (COALESCE) so repeated cancel calls do not
        refresh the CANCEL_WINDOW_S deadline. ``finalize_cancel`` additionally accepts
        parked jobs, but cannot certify that an executing attempt has stopped.
        """
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET cancel=1, "
                "cancel_requested_at=COALESCE(cancel_requested_at, ?) "
                "WHERE id=? AND status IN ('queued','running','waiting_user','awaiting_plan_approval')",
                (time.time(), job),
            )

    def pause(self, job, *, reason="paused"):
        """Prefer-D1 thin alias: park as waiting_user (no named suspend / second state DB)."""
        return self.wait_for_user(job, reason=reason or "paused")

    def finalize_cancel(self, job):
        """Request cancellation, including parked jobs; return whether accepted.

        Only a never-claimed job can finish immediately. An attempt token, even with
        no registered PID or a free lock, is not proof that execution never started
        or that all descendants stopped. Callers must read the authoritative status
        rather than interpret this acknowledgement as a terminal receipt.
        """
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                return False
            if row["status"] in ("completed", "failed", "cancelled"):
                return False
            db.execute(
                "UPDATE jobs SET cancel=1, "
                "cancel_requested_at=COALESCE(cancel_requested_at, ?) WHERE id=?",
                (time.time(), job),
            )
            if row["attempt"] is None and row["tries"] == 0 and row["pid"] is None:
                self._terminal(db, job, "cancelled", {"reason": "cancelled"})
            return True


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
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                return False
            if row["status"] not in ("waiting_user", "awaiting_plan_approval"):
                return False
            if row['cancel'] or not self._attempt_stopped(row):
                return False
            db.execute(
                "UPDATE jobs SET status='queued', attempt=NULL, heartbeat=NULL, "
                "pid=NULL, pid_start=NULL, deadline=NULL, execution_state='finished',supervisor_nonce=NULL WHERE id=?",
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
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                return False
            if row["status"] not in ("waiting_user", "awaiting_plan_approval"):
                return False
            if not self._attempt_stopped(row):
                if policy == 'cancelled':
                    db.execute("UPDATE jobs SET cancel=1,cancel_requested_at=COALESCE(cancel_requested_at,?),"
                               "result=? WHERE id=?", (time.time(), json.dumps({
                                   'reason': text_reason, 'event': 'hitl_denied',
                                   'stop_status': 'unproven'}), job))
                    return True
                return False
            self._terminal(db, job, policy, {
                "reason": text_reason,
                "hitl": "denied",
                "event": "hitl_denied",
            })
            return True

    def status(self, job):
        with self.db() as db:
            row = db.execute(
                "SELECT id,status,attempt,tries,cancel,result,heartbeat,created,deadline,"
                "pid,pid_start,cancel_requested_at,execution_state FROM jobs WHERE id=?",
                (job,),
            ).fetchone()
            if row is None:
                return None
            out = dict(row)
            out["stage"] = display_stage(out)
            return out

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

    def outbox_receipt(self, job):
        """Return the parsed terminal receipt for ``job`` from outbox, or None."""
        with self.db() as db:
            row = db.execute('SELECT receipt FROM outbox WHERE job=?', (job,)).fetchone()
        return json.loads(row['receipt']) if row is not None else None

    def record_card_finalization_state(self, job, state, error):
        """Record terminal card finalization status in card_outbox without raw SQL leakage."""
        with self.db() as db:
            db.execute(
                "UPDATE card_outbox SET state=?,error=?,updated=? "
                "WHERE job=? AND state IN ('pending','sent')",
                (state, error, time.time(), job),
            )

    def pending_card_creations(self, *, limit=20, not_before=None):
        """Return bounded pending card creation intents with job origin and payload."""
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    'SELECT c.job,j.origin,j.payload FROM card_outbox c '
                    'JOIN jobs j ON j.id=c.job WHERE c.state=? '
                    'AND (? IS NULL OR j.created>=?) ORDER BY c.updated,c.job LIMIT ?',
                    ('pending', not_before, not_before, limit),
                ).fetchall()
            ]

    def update_card_creation_state(self, job, state, error=None):
        """Transition a pending card creation row to ``state``."""
        with self.db() as db:
            db.execute(
                'UPDATE card_outbox SET state=?,error=?,updated=? WHERE job=? AND state=?',
                (state, error, time.time(), job, 'pending'),
            )

    def pending_running_card_jobs(self, *, limit=20, not_before=None):
        """Retire terminal rows and return running jobs eligible for a progress card update."""
        with self.db() as db:
            db.execute(
                "UPDATE card_outbox SET progress_state='terminal' WHERE job IN "
                "(SELECT c.job FROM card_outbox c JOIN jobs j ON j.id=c.job "
                "WHERE c.state='sent' AND c.progress_state='pending' "
                "AND j.status IN ('completed','failed','cancelled') LIMIT 100)"
            )
            return [
                dict(row)
                for row in db.execute(
                    "SELECT c.job FROM card_outbox c JOIN jobs j ON j.id=c.job "
                    "WHERE c.state='sent' AND c.progress_state='pending' "
                    "AND j.status='running' AND j.pid IS NOT NULL AND j.cancel=0 "
                    "AND (? IS NULL OR j.created>=?) ORDER BY c.updated,c.job LIMIT ?",
                    (not_before, not_before, limit),
                ).fetchall()
            ]

    def claim_running_card_progress(self, job):
        """Atomically claim the running progress update for ``job``."""
        with self.db() as db:
            claimed = db.execute(
                "UPDATE card_outbox SET progress_state='sending',updated=? WHERE job=? "
                "AND state='sent' AND progress_state='pending' AND EXISTS "
                "(SELECT 1 FROM jobs WHERE id=? AND status='running' AND pid IS NOT NULL AND cancel=0)",
                (time.time(), job, job),
            ).rowcount
        return bool(claimed)

    def update_running_card_progress(self, job, state, error=None):
        """Complete a claimed running progress update for ``job``."""
        with self.db() as db:
            db.execute(
                "UPDATE card_outbox SET progress_state=?,error=?,updated=? "
                "WHERE job=? AND progress_state='sending'",
                (state, error, time.time(), job),
            )

    def card_outbox_health_counts(self, *, not_before=None, now=None):
        """Return consistent snapshot counts for card_outbox health states."""
        current = time.time() if now is None else now
        checks = (
            ('create_unknown', "c.state='unknown' AND (c.error IS NULL OR c.error<>'card_patch_unknown')", ()),
            ('terminal_unknown', "c.state='unknown' AND c.error='card_patch_unknown'", ()),
            ('parked', "c.state='parked'", ()),
            ('progress_unknown', "c.progress_state='unknown'", ()),
            ('stale_progress', "c.progress_state='sending' AND c.updated<?", (current - 90,)),
            ('delayed', "c.state='pending' AND j.created<?", (current - 120,)),
        )
        counts = {}
        with self.db() as db:
            db.execute('BEGIN')
            for name, condition, values in checks:
                counts[name] = db.execute(
                    'SELECT COUNT(*) FROM card_outbox c JOIN jobs j ON j.id=c.job WHERE '
                    + condition + ' AND (? IS NULL OR j.created>=?)',
                    (*values, not_before, not_before),
                ).fetchone()[0]
        return counts

    def job_ownership_record(self, job):
        """Return payload, origin, and repaired origin for ``job``, or None."""
        with self.db() as db:
            row = db.execute('SELECT payload,origin FROM jobs WHERE id=?', (job,)).fetchone()
            repaired = db.execute('SELECT origin FROM origin_repairs WHERE job=?', (job,)).fetchone()
        if row is None:
            return None
        return {
            'payload': row['payload'],
            'origin': row['origin'],
            'repaired_origin': repaired['origin'] if repaired is not None else None,
        }

    def job_payload(self, job):
        """Return the parsed JSON payload dict for ``job``, or None."""
        with self.db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job,)).fetchone()
        return json.loads(row['payload']) if row and row['payload'] else None

    def unacknowledged_outbox_rows(self, *, limit=20):
        """Return bounded ``[{'job': ..., 'receipt': ...}, ...]`` rows from outbox where acknowledged=0."""
        if limit < 1:
            raise ValueError('limit must be positive')
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    'SELECT job,receipt FROM outbox WHERE acknowledged=0 ORDER BY rowid LIMIT ?',
                    (limit,),
                ).fetchall()
            ]

    def pending_outbox_addresses(self, *, limit=500):
        """Return bounded ``(job, parsed_origin)`` pairs for unacknowledged outbox rows."""
        if limit < 1:
            raise ValueError('limit must be positive')
        with self.db() as db:
            rows = db.execute(
                'SELECT outbox.job AS job, jobs.origin AS origin '
                'FROM outbox LEFT JOIN jobs ON jobs.id = outbox.job '
                'WHERE outbox.acknowledged=0 ORDER BY outbox.rowid LIMIT ?',
                (limit,),
            ).fetchall()
        addresses = []
        for row in rows:
            try:
                address = json.loads(row['origin']) if row['origin'] else None
            except (TypeError, ValueError):
                address = None
            addresses.append((row['job'], address))
        return addresses

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
        control = None
        peer = None
        evidence_fd = None
        stop_sent = False
        admitted = False
        try:
            if timeout <= 0 or lease <= 0 or max_output <= 0 or max_file <= 0:
                raise ValueError('budgets must be positive')
            directory.mkdir(mode=0o700)
            with (directory / 'input.json').open('w+') as inp, (directory / 'stdout').open('w+') as out, (directory / 'stderr').open('w+') as err:
                json.dump({'job': job, 'attempt': attempt, 'payload': json.loads(row['payload'])}, inp)
                inp.seek(0)
                if not self.begin_execution(job, attempt, mode='launching'):
                    raise RuntimeError('claim cancelled before runner launch')
                admitted = True
                import sys
                control, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
                nonce = uuid.uuid4().hex
                evidence_fd = os.open(directory / 'stop.json',
                                      os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                guarded_command = [sys.executable, str(Path(__file__).with_name('attempt_supervisor.py')),
                                   str(peer.fileno()), str(evidence_fd), str(fd)]
                control.sendall(json.dumps({'job': job, 'attempt': attempt, 'token': token,
                                             'nonce': nonce, 'max_file': max_file,
                                             'command': command}).encode())
                env = dict(os.environ)
                # Runner failure cleanup may sweep its own attempt token. The
                # supervisor must survive that sweep to adopt/reap descendants.
                env.pop(containment.ENV, None)
                # Slow interpreter startup must not hold the dispatch database
                # lock. Until registration commits and go is sent, this process
                # cannot start a runner; Worker EOF closes the no-launch gap.
                proc = subprocess.Popen(guarded_command, stdin=inp, stdout=out, stderr=err, cwd=directory,
                                        start_new_session=True,
                                        pass_fds=(fd, peer.fileno(), evidence_fd), env=env)
                # Our duplicate would otherwise hide EOF from a failed peer.
                peer.close()
                peer = None
                control.settimeout(5)
                if control.recv(16) != b'ready':
                    raise RuntimeError('supervisor did not become ready')
                control.settimeout(None)
                pid_start = self._process_start(proc.pid)
                if not pid_start:
                    raise RuntimeError('supervisor identity unavailable')
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute("SELECT status,cancel FROM jobs WHERE id=? AND attempt=? "
                                         "AND status IN ('running','waiting_user','awaiting_plan_approval') "
                                         "AND execution_state='launching'",
                                         (job, attempt)).fetchone()
                    if current is None:
                        raise RuntimeError('claim revoked before supervisor registration')
                    authorized = current['status'] == 'running' and not current['cancel']
                    # Bind a revoked, never-launched supervisor too: its private
                    # no-child receipt must remain verifiable after cancel/pause.
                    db.execute("UPDATE jobs SET pid=?,pid_start=?,deadline=?,execution_state='supervised',"
                               "supervisor_nonce=? WHERE id=? AND attempt=?",
                               (proc.pid, pid_start, time.time() + timeout, nonce, job, attempt))
                os.close(evidence_fd)
                evidence_fd = None
                control.sendall(b'go' if authorized else b'stop')
                deadline = time.monotonic() + timeout
                next_heartbeat = time.monotonic() + lease / 3
                reason = None if authorized else 'cancelled or lease revoked'
                stop_deadline = None
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
                        if not stop_sent:
                            try:
                                control.sendall(b'stop')
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                            stop_sent = True
                            stop_deadline = time.monotonic() + 5
                    if stop_deadline is not None and time.monotonic() > stop_deadline:
                        raise TimeoutError('supervisor stop confirmation unavailable')
                    time.sleep(min(0.05, lease / 4))
                proc.wait()
                if reason is None:
                    if time.monotonic() > deadline:
                        reason = 'timeout'
                    elif os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > max_output:
                        reason = 'output budget exceeded'
                with self.db() as db:
                    latest = db.execute('SELECT * FROM jobs WHERE id=? AND attempt=?', (job, attempt)).fetchone()
                stopped = latest is not None and self._attempt_stopped(latest)
                receipt = json.loads((directory / 'stop.json').read_text()) if stopped else {}
                exit_code = receipt.get('exit_code', proc.returncode)
                result = {'reason': reason or 'runner failed', 'exit_code': exit_code}
                ok = False
                if reason is None and stopped and exit_code == 0:
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
                result['attempt_quiescent'] = stopped
                if not stopped:
                    ok = False
                    result['reason'] = 'attempt_stop_unproven'
                self.finish(job, attempt, ok, result)
        except Exception as exc:
            if proc is None and admitted:
                # This executor has not created any child. Close the admission
                # gap using its own fenced no-launch observation, not a timer.
                with self.db() as db:
                    db.execute("UPDATE jobs SET execution_state='finished' WHERE id=? AND attempt=? "
                               "AND execution_state='launching'", (job, attempt))
            self.finish(job, attempt, False, {'reason': 'execution error', 'type': type(exc).__name__,
                                             'attempt_quiescent': proc is None,
                                             'sqlite_code': getattr(exc, 'sqlite_errorname', None)})
        finally:
            if peer is not None:
                peer.close()
            if evidence_fd is not None:
                os.close(evidence_fd)
            if control is not None:
                control.close()  # EOF asks a surviving supervisor to stop and reap.
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Never SIGKILL the supervisor and manufacture an empty tree.
                    # It retains the inherited job lock until reaping succeeds.
                    pass
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
    worker.add_argument('--poll-interval', type=float, default=0.05,
                        help='Queue polling and reaping interval in seconds (default: 0.05s)')
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
                               timeout=args.timeout, max_output=args.max_output, max_file=args.max_file,
                               poll_interval=args.poll_interval).run(stop, drain=args.drain)
                print(json.dumps(stats))
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


if __name__ == '__main__':
    main()

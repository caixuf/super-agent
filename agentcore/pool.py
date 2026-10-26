"""Durable local worker queue. POSIX job locks fence surviving child processes.

Runner protocol: JSON job on stdin, JSON {ok: true, result: ...} on stdout.
Runner commands are administrator supplied argv arrays; never task supplied shell.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import uuid


class Pool:
    def __init__(self, root):
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
            ''')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(str(self.root / 'pool.db'), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, key, payload, dependencies=(), replay_safe=False, max_tries=2):
        if not key or max_tries < 1:
            raise ValueError('dedupe key and positive max_tries required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE dedupe=?', (key,)).fetchone()
            serialized = json.dumps(payload, sort_keys=True)
            if existing:
                old_deps = {r[0] for r in db.execute('SELECT dependency FROM deps WHERE job=?', (existing['id'],))}
                if existing['payload'] != serialized or old_deps != set(dependencies):
                    raise ValueError('dedupe key reused with different task')
                return existing['id']
            for dependency in dependencies:
                if not db.execute('SELECT 1 FROM jobs WHERE id=?', (dependency,)).fetchone():
                    raise ValueError('unknown dependency')
            job = uuid.uuid4().hex
            db.execute('INSERT INTO jobs(id,dedupe,payload,max_tries,replay_safe,created) VALUES(?,?,?,?,?,?)',
                       (job, key, serialized, max_tries, bool(replay_safe), time.time()))
            db.executemany('INSERT INTO deps VALUES(?,?)', [(job, d) for d in set(dependencies)])
            return job

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
                if row['status'] == 'running' and (row['heartbeat'] or 0) > time.time() - lease:
                    continue
                fd = self._lock(row['id'])
                if fd is None:
                    continue  # A surviving runner still owns this job, even after lease expiry.
                keep = False
                try:
                    if row['cancel']:
                        self._terminal(db, row['id'], 'cancelled', {'reason': 'cancelled'})
                        continue
                    if row['status'] == 'running' and (not row['replay_safe'] or row['tries'] >= row['max_tries']):
                        self._terminal(db, row['id'], 'failed', {'reason': 'interrupted; replay unavailable'})
                        continue
                    deps = [r[0] for r in db.execute('SELECT j.status FROM deps d JOIN jobs j ON j.id=d.dependency WHERE d.job=?', (row['id'],))]
                    if any(s in ('failed', 'cancelled') for s in deps):
                        self._terminal(db, row['id'], 'failed', {'reason': 'dependency failed'})
                        continue
                    if any(s != 'completed' for s in deps):
                        continue
                    token = uuid.uuid4().hex
                    db.execute("UPDATE jobs SET status='running',attempt=?,heartbeat=?,tries=tries+1 WHERE id=?",
                               (token, time.time(), row['id']))
                    keep = True
                    return dict(row, attempt=token), fd
                finally:
                    if not keep:
                        os.close(fd)
        return None

    def heartbeat(self, job, attempt):
        with self.db() as db:
            row = db.execute("SELECT cancel FROM jobs WHERE id=? AND attempt=? AND status='running'", (job, attempt)).fetchone()
            if row is None or row['cancel']:
                return False
            db.execute('UPDATE jobs SET heartbeat=? WHERE id=? AND attempt=?', (time.time(), job, attempt))
            return True

    def finish(self, job, attempt, ok, result):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE id=? AND attempt=? AND status='running'", (job, attempt)).fetchone()
            if row is None:
                return False
            if row['cancel']:
                self._terminal(db, job, 'cancelled', {'reason': 'cancelled'})
            elif not ok and row['replay_safe'] and row['tries'] < row['max_tries']:
                db.execute("UPDATE jobs SET status='queued',heartbeat=NULL WHERE id=?", (job,))
            else:
                self._terminal(db, job, 'completed' if ok else 'failed', result)
            return True

    def cancel(self, job):
        with self.db() as db:
            db.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status IN ('queued','running')", (job,))

    def status(self, job):
        with self.db() as db:
            row = db.execute('SELECT id,status,attempt,tries,cancel,result FROM jobs WHERE id=?', (job,)).fetchone()
            return dict(row) if row else None

    def receipts(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT receipt FROM outbox WHERE acknowledged=0')]

    def acknowledge(self, job):
        with self.db() as db:
            db.execute('UPDATE outbox SET acknowledged=1 WHERE job=?', (job,))

    def run_once(self, command, timeout=300, lease=60, max_output=1048576):
        if timeout <= 0 or lease <= 0 or max_output <= 0:
            raise ValueError('budgets must be positive')
        claimed = self.claim(lease)
        if not claimed:
            return False
        row, fd = claimed
        job, attempt = row['id'], row['attempt']
        directory = self.root / 'attempts' / attempt
        directory.mkdir(mode=0o700)
        proc = None
        try:
            with (directory / 'input.json').open('w+') as inp, (directory / 'stdout').open('w+') as out, (directory / 'stderr').open('w+') as err:
                json.dump({'job': job, 'attempt': attempt, 'payload': json.loads(row['payload'])}, inp)
                inp.seek(0)
                proc = subprocess.Popen(command, stdin=inp, stdout=out, stderr=err, cwd=directory,
                                        start_new_session=True, pass_fds=(fd,))
                deadline = time.monotonic() + timeout
                reason = None
                while proc.poll() is None:
                    if not self.heartbeat(job, attempt):
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
                result = {'reason': reason or 'runner failed', 'exit_code': proc.returncode}
                ok = False
                if reason is None and proc.returncode == 0 and os.fstat(out.fileno()).st_size <= max_output:
                    out.seek(0)
                    try:
                        response = json.loads(out.read(max_output))
                        ok = isinstance(response, dict) and response.get('ok') is True
                        result = response if isinstance(response, dict) else {'reason': 'invalid receipt'}
                    except (ValueError, UnicodeError):
                        result = {'reason': 'invalid JSON receipt'}
                self.finish(job, attempt, ok, result)
        except Exception as exc:
            self.finish(job, attempt, False, {'reason': 'execution error', 'type': type(exc).__name__})
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
        sub.add_parser(action).add_argument('job')
    sub.add_parser('receipts')
    worker = sub.add_parser('worker')
    worker.add_argument('--runner-config', required=True, help='JSON argv array; trusted administrator config')
    worker.add_argument('--timeout', type=float, default=300)
    worker.add_argument('--once', action='store_true')
    args = parser.parse_args()
    pool = Pool(args.root)
    if args.action == 'submit':
        print(pool.submit(args.key, json.loads(Path(args.payload_file).read_text()), args.dependency, args.replay_safe))
    elif args.action == 'status':
        print(json.dumps(pool.status(args.job)))
    elif args.action == 'cancel':
        pool.cancel(args.job)
    elif args.action == 'ack':
        pool.acknowledge(args.job)
    elif args.action == 'receipts':
        print(json.dumps(pool.receipts()))
    else:
        command = json.loads(Path(args.runner_config).read_text())
        if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
            parser.error('runner-config must be a nonempty argv array')
        while True:
            did_work = pool.run_once(command, timeout=args.timeout)
            if args.once:
                break
            if not did_work:
                time.sleep(1)


if __name__ == '__main__':
    main()

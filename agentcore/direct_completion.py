"""Direct receipt delivery with durable per-operation uncertainty fencing.

The sender returns a real platform message ID. A crash/timeout after starting a
send leaves an unknown operation; it is never automatically sent a second time.
Successful text and files are reused when a later part fails or queue ACK is lost.
"""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat


class DirectCompletionTransport:
    def __init__(self, root, *, profile, sender, render, attempt_root, attempt_provider):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile, self.sender, self.render = profile, sender, render
        self.attempt_root = Path(attempt_root).resolve()
        self.attempt_provider = attempt_provider
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations ('
                       'key TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, receipt_id TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, request TEXT NOT NULL, plan TEXT NOT NULL)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root/'delivery.db', timeout=30)
        db.execute('PRAGMA synchronous=FULL')
        os.chmod(self.root/'delivery.db', 0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _stage(self, path, attempt):
        source = Path(path)
        allowed = (self.attempt_root/attempt).resolve()
        resolved = source.resolve(strict=True)
        if (not allowed.is_relative_to(self.attempt_root) or resolved.parent != allowed
                or source.name.startswith('.') or source.is_symlink()):
            raise ValueError('artifact outside the owning attempt')
        directory_fd = os.open(allowed, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 16*1024*1024:
                raise ValueError('artifact file budget')
            raw = stream.read(16*1024*1024+1)
            after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or (before.st_ino, before.st_mtime_ns, before.st_size)
                    != (after.st_ino, after.st_mtime_ns, after.st_size)):
                raise ValueError('artifact changed during snapshot')
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.root/'files'/digest
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory/source.name
        try:
            with target.open('xb') as out:
                target.chmod(0o600); out.write(raw); out.flush(); os.fsync(out.fileno())
        except FileExistsError:
            if target.read_bytes() != raw:
                raise ValueError('artifact snapshot conflict')
        return dict(kind='file', path=str(target), name=source.name, sha256=digest)

    def _operation(self, address, item, key):
        payload = json.dumps(dict(address=address, item=item), sort_keys=True, ensure_ascii=False)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,state,receipt_id FROM operations WHERE key=?', (key,)).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError('delivery payload conflict')
                if row[1] == 'sent':
                    return row[2]
                if row[1] in ('sending', 'unknown'):
                    return None
            if item['kind'] == 'file' and hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('artifact snapshot changed')
            if row:
                db.execute("UPDATE operations SET state='sending' WHERE key=?", (key,))
            else:
                db.execute("INSERT INTO operations VALUES(?,?,'sending',NULL)", (key, payload))
        try:
            response = self.sender(address, item, idempotency_key=key)
        except Exception:
            response = None
        receipt = response.get('message_id') if isinstance(response, dict) and response.get('ok') is True else None
        if not isinstance(receipt, str) or not receipt.strip():
            receipt = None
        # Only an explicit pre-send refusal permits another attempt. Adapter
        # errors may be post-send failures and must remain unknown.
        state = 'sent' if receipt else ('rejected' if isinstance(response, dict)
                                       and response.get('not_sent') is True else 'unknown')
        with self.db() as db:
            db.execute('UPDATE operations SET state=?,receipt_id=? WHERE key=?', (state, receipt, key))
        return receipt

    def deliver(self, address, message, *, idempotency_key):
        if address.get('profile') != self.profile or not address.get('route'):
            return None
        if message.get('task_id') != idempotency_key:
            raise ValueError('completion identity mismatch')
        files = message.get('artifacts', [])
        if not isinstance(files, list) or len(files) > 8:
            raise ValueError('artifact count budget')
        identity = json.dumps([self.profile, address['route'], idempotency_key])
        request = json.dumps(dict(address=address, message=message), sort_keys=True)
        with self.db() as db:
            existing = db.execute('SELECT request,plan FROM plans WHERE key=?', (identity,)).fetchone()
        if existing:
            if existing[0] != request:
                raise ValueError('delivery plan conflict')
            plan = json.loads(existing[1])
        else:
            attempt = self.attempt_provider(idempotency_key)
            if files and (not isinstance(attempt, str) or len(attempt) != 32
                          or any(c not in '0123456789abcdef' for c in attempt)):
                raise ValueError('owning attempt unavailable')
            # Validate and snapshot ALL attachments before sending any part.
            plan = [dict(kind='text', text=self.render(message))] + [self._stage(p, attempt) for p in files]
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO plans VALUES(?,?,?)', (identity, request, json.dumps(plan)))
                saved = db.execute('SELECT request,plan FROM plans WHERE key=?', (identity,)).fetchone()
                if saved[0] != request or json.loads(saved[1]) != plan:
                    raise ValueError('concurrent delivery plan conflict')
        receipts = []
        for i, item in enumerate(plan):
            key = hashlib.sha256((identity+'\n'+str(i)).encode()).hexdigest()
            receipt = self._operation(address, item, key)
            if receipt is None:
                return dict(accepted=False, event_id=idempotency_key, deferred=True)
            receipts.append(receipt)
        return dict(accepted=True, event_id=idempotency_key,
                    receipt_id='direct:'+hashlib.sha256(json.dumps(receipts).encode()).hexdigest())

    def inspect(self):
        """Bounded status view without conversation text, routes or file contents."""
        with self.db() as db:
            return [dict(key=r[0], state=r[1], receipt_id=r[2]) for r in db.execute(
                'SELECT key,state,receipt_id FROM operations ORDER BY rowid DESC LIMIT 100')]

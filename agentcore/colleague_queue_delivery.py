"""Sign one read-only colleague result on the existing queue delivery path.

This is a transport decorator for ``completion_backfill``; it neither scans an
outbox nor sends directly. The existing queue consumer must be the *only*
external delivery owner. A trusted host resolver binds a queue job to the
actual runtime request and owner; model output is never an identity source.

The wrapped DirectCompletionTransport performs receipt journaling and attachment
snapshotting. No platform sender, queue producer or production integration is
provided. A host artifact policy is mandatory for any attachments, including
paths the runtime reports; the wrapped transport must independently check
the queue's owning attempt and snapshot files before sending anything.
The trusted sender must explicitly attest literal-text handling. The existing
generic sender does not provide this marker; a marker alone is not proof that
an actual platform prevents markup and mentions.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import stat

from agentcore.colleague_readonly_dialogue import ReadOnlyDialogue
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.direct_completion import DirectCompletionTransport


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class SignedQueueDelivery:
    """Attach the persisted executor's identity to a queue completion.

    ``resolve_binding(job)`` must consult durable host-owned data linking this
    exact queue job to ``{'owner': {'profile':..., 'session':...}, 'request_id':...}``.
    It must not infer execution from queue payload, model text or role. The
    runtime verifies actual execution separately. ``authorize_artifacts``, when
    supplied, must validate every proposed path against the host's policy for
    that particular queue attempt, and return exactly True; it never supplies
    paths itself. Existing DirectCompletionTransport then independently
    snapshots only regular files from that job's active attempt.

    An immutable signature snapshot is committed before the wrapped transport
    runs so a name change, worker retry or transport restart cannot alter a
    partially sent plan. Snapshot data lives in a host-private directory.
    Missing/invalid bindings and authorization failures stay deferred.
    """

    def __init__(self, runtime, *, resolve_binding, direct_transport, snapshot_path,
                 authorize_artifacts=None):
        if (not isinstance(runtime, ReadOnlyColleagueRuntime)
                or not isinstance(direct_transport, DirectCompletionTransport)
                or not callable(resolve_binding)
                or (authorize_artifacts is not None and not callable(authorize_artifacts))):
            raise ValueError('trusted_delivery_components_required')
        if getattr(direct_transport.sender, 'LITERAL_TEXT_DELIVERY', None) is not True:
            raise ValueError('literal_text_transport_required')
        self.runtime, self.resolve_binding, self.direct = runtime, resolve_binding, direct_transport
        self.authorize_artifacts = authorize_artifacts
        self.path = Path(snapshot_path)
        if not self.path.is_absolute() or self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError('private_snapshot_directory_required')
        info = self.path.parent.stat()
        if info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            raise ValueError('private_snapshot_directory_required')
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('private_snapshot_database_required')
        with self._db() as db:
            os.chmod(self.path, 0o600, follow_symlinks=False)
            db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
                job TEXT PRIMARY KEY, address TEXT NOT NULL, original TEXT NOT NULL,
                owner TEXT NOT NULL, request_id TEXT NOT NULL, runtime_task TEXT NOT NULL,
                signed_message TEXT NOT NULL)''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _deferred(job):
        return {'accepted': False, 'event_id': job, 'deferred': True}

    def deliver(self, address, message, *, idempotency_key):
        """Called by the existing queue outbox; never callable from the model."""
        job = idempotency_key
        if (not isinstance(job, str) or not job or not isinstance(message, dict)
                or message.get('task_id') != job or message.get('status') != 'completed'
                or not isinstance(address, dict) or not address.get('route')):
            return self._deferred(job)
        try:
            binding = self.resolve_binding(job)
            if not isinstance(binding, dict) or set(binding) != {'owner', 'request_id'}:
                return self._deferred(job)
            owner, request = binding['owner'], binding['request_id']
            if (not isinstance(owner, dict) or set(owner) != {'profile', 'session'}
                    or owner['profile'] != address.get('profile') or owner['session'] != address.get('session')
                    or not isinstance(request, str) or not request):
                return self._deferred(job)
            # Recheck owner authorization and the actual executed assignment on
            # each attempt, even when an immutable presentation already exists.
            execution = self.runtime.inspect_result(owner=deepcopy(owner), request_id=request)
            if (not isinstance(execution, dict) or execution.get('state') != 'completed'
                    or execution.get('checked_result') is not True or execution.get('owner') != owner):
                return self._deferred(job)
            artifacts = message.get('artifacts', [])
            if (not isinstance(artifacts, list) or len(artifacts) > 8
                    or any(not isinstance(p, str) or not p for p in artifacts)):
                return self._deferred(job)
            if artifacts and (self.authorize_artifacts is None
                    or self.authorize_artifacts(job, deepcopy(owner), list(artifacts)) is not True):
                return self._deferred(job)
            address_json, original_json, owner_json = _json(address), _json(message), _json(owner)
            with self._db() as db:
                saved = db.execute('SELECT * FROM snapshots WHERE job=?', (job,)).fetchone()
            if saved is None:
                presented = ReadOnlyDialogue(self.runtime, owner=owner).present(request)
                if (presented.get('state') != 'completed' or presented.get('task_id') != execution.get('task_id')
                        or presented.get('sender') != 'primary_relay' or not isinstance(presented.get('text'), str)):
                    return self._deferred(job)
                signed = dict(message, summary=presented['text'])
                with self._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?)',
                               (job, address_json, original_json, owner_json, request,
                                execution['task_id'], _json(signed)))
                    saved = db.execute('SELECT * FROM snapshots WHERE job=?', (job,)).fetchone()
            if ((saved['address'], saved['original'], saved['owner'], saved['request_id'], saved['runtime_task'])
                    != (address_json, original_json, owner_json, request, execution.get('task_id'))):
                return self._deferred(job)
            signed = json.loads(saved['signed_message'])
        except Exception:
            return self._deferred(job)
        # Exceptions from the transport are handled by the queue consumer as
        # an uncertain delivery; do not call a second sender here.
        return self.direct.deliver(deepcopy(address), signed, idempotency_key=job)

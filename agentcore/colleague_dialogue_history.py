"""Host-owned, bounded read-only conversation history for one colleague.

This is a persistence primitive, not an instruction channel or model adapter.
The host authenticates the bound owner; the runtime state directory and this
store must be protected from model access. A turn can only be copied from that
owner's completed, citation-checked runtime record and its recorded member
generation. Returned prior turns are untrusted data, never system messages,
source permissions, file contents, tool calls or facts established as true.

Do not share one member's history with another. A caller using this history
must independently authorize current source files; history must not expand
the current source list, change policy, or become a reason to access a path.
There is no automatic injection into a model or cross-channel delivery here.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime

_MAX_BYTE_BUDGET = 8000
_MAX_TURNS = 10


def _private_dir(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
        raise ValueError('private_history_directory_required')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class ColleagueDialogueHistory:
    """A view bound by the host to exactly one owner and member generation.

    The bounded context method preserves complete turns and always takes a
    consecutive suffix. The byte budget counts the UTF-8 bytes of the returned
    JSON data itself. Older records stay on disk: this is not storage pruning.
    The member may later be inactive and its prior turns remain available.
    """

    def __init__(self, runtime, *, root, owner, member_id, generation, authorize_ingress=None):
        if not isinstance(runtime, ReadOnlyColleagueRuntime):
            raise ValueError('trusted_runtime_required')
        self.runtime, self.owner = runtime, deepcopy(owner)
        self.member_id, self.generation = member_id, generation
        if authorize_ingress is not None and not callable(authorize_ingress):
            raise ValueError('invalid_ingress_authorizer')
        self.authorize_ingress = authorize_ingress
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError('private_history_directory_required')
        _private_dir(self.root)
        self.db_path = self.root / 'colleague-dialogue.sqlite3'
        self._authorize()
        if self.db_path.is_symlink():
            raise ValueError('invalid_history_database')
        if self.db_path.exists():
            info = self.db_path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('invalid_history_database')
        # Do not raw-open/close a SQLite database; SQLite owns its lock FDs.
        with self._db() as db:
            os.chmod(self.db_path, 0o600, follow_symlinks=False)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    task TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','completed','failed')),
                    content TEXT,
                    UNIQUE(scope, task), UNIQUE(scope, sequence));
                CREATE INDEX IF NOT EXISTS turns_scope_sequence ON turns(scope, sequence);
            ''')

    @contextmanager
    def _db(self):
        _private_dir(self.root)
        if self.db_path.is_symlink():
            raise ValueError('invalid_history_database')
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _authorize(self):
        # The runtime authenticates the host-bound owner and validates its form.
        self.runtime._owner(deepcopy(self.owner))
        member = self.runtime.registry.role_for_member(
            supervisor=self.runtime.supervisor, member_id=self.member_id, generation=self.generation)
        if self.member_id == self.runtime.registry.primary_member(supervisor=self.runtime.supervisor):
            raise PermissionError('primary_not_colleague')
        return member

    def _scope(self):
        material = [self.runtime.registry.profile_id, self.owner, self.member_id, self.generation]
        return hashlib.sha256(_json(material).encode('utf-8')).hexdigest()

    def admit(self, request_id, *, sequence):
        """Bind an ordered user turn before the runtime starts the task.

        The host-only ingress authorizer must verify the ordering against its
        own durable message scheduler, not a value selected by the model. For
        each owner/member/generation stream sequences must begin at 1 and be
        admitted contiguously. An uncertain gap fails closed. An exact replay
        is idempotent and cannot change the sequence of a prior task.
        """
        self._authorize()
        if type(sequence) is not int or not 1 <= sequence <= 9223372036854775807:
            raise ValueError('invalid_conversation_sequence')
        owner, task = self.runtime._identity(deepcopy(self.owner), request_id)
        if self.authorize_ingress is None:
            raise PermissionError('trusted_ingress_order_required')
        try:
            permitted = self.authorize_ingress(deepcopy(owner), self.member_id,
                                                self.generation, request_id, sequence) is True
        except Exception:
            permitted = False
        if not permitted:
            raise PermissionError('trusted_ingress_order_required')
        scope = self._scope()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT sequence FROM turns WHERE scope=? AND task=?',
                                  (scope, task)).fetchone()
            if existing:
                if existing['sequence'] != sequence:
                    raise ValueError('conversation_sequence_conflict')
                return {'task_id': task, 'sequence': sequence, 'created': False}
            last = db.execute('SELECT COALESCE(MAX(sequence),0) AS n FROM turns WHERE scope=?',
                              (scope,)).fetchone()['n']
            if sequence != last + 1:
                raise ValueError('conversation_sequence_gap')
            db.execute("INSERT INTO turns(scope,task,sequence,state) VALUES (?,?,?,'pending')",
                       (scope, task, sequence))
        return {'task_id': task, 'sequence': sequence, 'created': True}

    def record_completed(self, request_id):
        """Idempotently record a completed turn directly from the runtime.

        The caller cannot provide either the user goal or colleague answer.
        Missing legacy goals, a different member/generation, unverified results,
        and incomplete tasks are rejected. Concurrent identical records collapse
        into one; an inconsistent replay fails rather than rewriting history.
        """
        member = self._authorize()
        record = self.runtime.inspect_result(owner=deepcopy(self.owner), request_id=request_id)
        if not isinstance(record, dict) or record.get('state') != 'completed' or record.get('checked_result') is not True:
            raise ValueError('completed_result_required')
        if (record.get('owner') != self.owner or record.get('member_id') != self.member_id
                or record.get('generation') != self.generation or record.get('role_id') != member['role_id']):
            raise PermissionError('task_member_mismatch')
        goal, result, task = record.get('request_goal'), record.get('result'), record.get('task_id')
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 8000:
            raise ValueError('original_goal_unavailable')
        if (not isinstance(result, dict) or result.get('verification') != 'citation_traceability_only'
                or not isinstance(result.get('summary'), str) or len(result['summary']) > 2000
                or not isinstance(task, str) or len(task) != 32):
            raise ValueError('invalid_completed_result')
        sequence = record.get('conversation_sequence')
        if type(sequence) is not int or sequence < 1:
            raise ValueError('conversation_sequence_unavailable')
        turn = {'user_goal': goal, 'colleague_summary': result['summary']}
        content, scope = _json(turn), self._scope()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT sequence,state,content FROM turns WHERE scope=? AND task=?',
                                  (scope, task)).fetchone()
            if previous is None or previous['sequence'] != sequence:
                raise ValueError('admitted_sequence_required')
            if previous['state'] == 'completed':
                if previous['content'] != content:
                    raise ValueError('history_replay_conflict')
                return {'task_id': task, 'created': False}
            if previous['state'] != 'pending':
                raise ValueError('history_state_conflict')
            db.execute("UPDATE turns SET content=?,state='completed' WHERE scope=? AND task=?",
                       (content, scope, task))
        return {'task_id': task, 'created': True}

    def record_failed(self, request_id):
        """Unblock later turns only for a host-recorded terminal failed task."""
        member = self._authorize()
        record = self.runtime.inspect_result(owner=deepcopy(self.owner), request_id=request_id)
        if not isinstance(record, dict) or record.get('state') != 'failed':
            raise ValueError('failed_task_required')
        if (record.get('owner') != self.owner or record.get('member_id') != self.member_id
                or record.get('generation') != self.generation or record.get('role_id') != member['role_id']):
            raise PermissionError('task_member_mismatch')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT sequence,state FROM turns WHERE scope=? AND task=?',
                             (self._scope(), record['task_id'])).fetchone()
            if row is None or type(record.get('conversation_sequence')) is not int or row['sequence'] != record['conversation_sequence']:
                raise ValueError('admitted_sequence_required')
            if row['state'] == 'failed':
                return False
            if row['state'] != 'pending':
                raise ValueError('history_state_conflict')
            db.execute("UPDATE turns SET state='failed' WHERE scope=? AND task=?", (self._scope(), record['task_id']))
        return True

    def context(self, current_request_id, *, authorize_prior=None, max_turns=10, byte_budget=8000):
        """Return a bounded, consecutive suffix of untrusted prior turns.

        Budget includes JSON punctuation and escaping, not just body lengths;
        an individual oversized turn is omitted instead of silently truncated.
        Returned text cannot be used as authority for sources or instructions.
        ``authorize_prior(task_id)`` must be a host-owned, fail-closed policy
        that proves *all* old source content remains permitted under the current
        request's authenticated principal, exact source ranges and current
        permissions, including transitive sources inherited from earlier turns.
        The callback must consult trusted execution/source lineage records, not
        model input or a role label. Missing, false or failed authorization
        yields no earlier turns. The current runtime does not yet furnish that
        full reauthorization source record; never replace it with ``lambda: True``
        in integration. Previously permitted data may since have been revoked.
        """
        self._authorize()
        if (type(max_turns) is not int or not 1 <= max_turns <= _MAX_TURNS
                or type(byte_budget) is not int or not 2 <= byte_budget <= _MAX_BYTE_BUDGET):
            raise ValueError('invalid_history_budget')
        if authorize_prior is not None and not callable(authorize_prior):
            raise ValueError('invalid_history_authorizer')
        _, task = self.runtime._identity(deepcopy(self.owner), current_request_id)
        scope = self._scope()
        with self._db() as db:
            current = db.execute('SELECT sequence FROM turns WHERE scope=? AND task=?', (scope, task)).fetchone()
            if current is None:
                return {'state': 'unavailable', 'trust': 'untrusted_prior_conversation',
                        'turns': [], 'encoded_turns_bytes': 2}
            pending = db.execute("SELECT COUNT(*) AS n FROM turns WHERE scope=? AND sequence<? AND state='pending'",
                                 (scope, current['sequence'])).fetchone()['n']
            if pending:
                return {'state': 'waiting', 'trust': 'untrusted_prior_conversation',
                        'turns': [], 'encoded_turns_bytes': 2}
            rows = db.execute("SELECT task,content FROM turns WHERE scope=? AND sequence<? AND state='completed' "
                              'ORDER BY sequence DESC LIMIT ?', (scope, current['sequence'], max_turns)).fetchall()
        selected = []
        for row in rows:
            if authorize_prior is None:
                break
            try:
                authorized = authorize_prior(row['task']) is True
            except Exception:
                authorized = False
            if not authorized:
                break
            try:
                turn = json.loads(row['content'])
            except (TypeError, ValueError) as exc:
                raise ValueError('invalid_history_record') from exc
            if (not isinstance(turn, dict) or set(turn) != {'user_goal', 'colleague_summary'}
                    or not all(isinstance(v, str) for v in turn.values())):
                raise ValueError('invalid_history_record')
            candidate = [turn] + selected
            if len(_json(candidate).encode('utf-8')) > byte_budget:
                break
            selected = candidate
        return {'state': 'ready', 'trust': 'untrusted_prior_conversation', 'turns': selected,
                'encoded_turns_bytes': len(_json(selected).encode('utf-8'))}

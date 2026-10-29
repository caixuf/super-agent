"""Host-bound colleague selection for a conversation; not a message transport.

The host authenticates owners and assigns monotonic per-conversation sequence
numbers. The caller-supplied assignment verifier must consult durable host
records, never model text. This module records no message contents and does not
start a worker, admit work, or provide a persistent model conversation.
The host must register messages with select in incoming order before allowing
subsequent messages to select; sequence numbers alone cannot prove arrival order.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from agentcore.colleague_registry import Actor, Registry, Supervisor


def _owner(value, profile):
    if (not isinstance(value, dict) or set(value) != {'profile', 'session'}
            or value['profile'] != profile
            or not isinstance(value['session'], str) or not value['session'].strip()
            or len(value['session']) > 512):
        raise PermissionError('trusted_owner_required')
    return value['profile'], value['session']


def _sequence(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError('invalid_sequence')
    return value


class ConversationRouter:
    """Track a verified last target; host must re-authorize at actual admission.

    ``authorize(owner, member)`` and ``verify_assignment(owner, sequence,
    member_id, generation, work_id)`` are trusted host callbacks. The latter
    must prove a durable accepted assignment, not just a model's claim. A
    returned conversation key is an opaque namespace only: a separate runner
    must actually maintain the member's context using it.
    """

    def __init__(self, path, registry, supervisor, *, authorize, verify_assignment, owner_profile=None):
        if not isinstance(registry, Registry) or type(supervisor) is not Supervisor or supervisor.profile_id != registry.profile_id:
            raise PermissionError('trusted_supervisor_required')
        if not callable(authorize) or not callable(verify_assignment):
            raise ValueError('trusted_callbacks_required')
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError('private_directory_required')
        info = self.path.parent.stat()
        if info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            raise ValueError('private_directory_required')
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('private_database_required')
        self.registry, self.supervisor = registry, supervisor
        self.authorize, self.verify_assignment = authorize, verify_assignment
        self.profile = registry.profile_id if owner_profile is None else owner_profile
        if not isinstance(self.profile, str) or not self.profile.strip() or len(self.profile) > 512:
            raise ValueError('invalid_owner_profile')
        self.actor = Actor(registry.profile_id, registry.primary_member(supervisor=supervisor))
        with self._db() as db:
            os.chmod(self.path, 0o600, follow_symlinks=False)
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS conversation_scope (id INTEGER PRIMARY KEY CHECK(id=1), team TEXT NOT NULL, owner_profile TEXT NOT NULL)')
            scope = db.execute('SELECT team,owner_profile FROM conversation_scope WHERE id=1').fetchone()
            if scope is None:
                db.execute('INSERT INTO conversation_scope VALUES (1,?,?)', (registry.profile_id, self.profile))
            elif (scope['team'], scope['owner_profile']) != (registry.profile_id, self.profile):
                raise ValueError('conversation_scope_mismatch')
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_targets (
                profile TEXT NOT NULL, session TEXT NOT NULL, sequence INTEGER NOT NULL,
                member_id TEXT NOT NULL, generation INTEGER NOT NULL, work_id TEXT NOT NULL,
                PRIMARY KEY (profile, session))''')
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_inbox (
                profile TEXT NOT NULL, session TEXT NOT NULL, sequence INTEGER NOT NULL,
                target TEXT NOT NULL, state TEXT NOT NULL,
                selected_member TEXT, selected_generation INTEGER,
                PRIMARY KEY (profile, session, sequence))''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _members(self):
        result, after = [], ''
        while True:
            page = self.registry.list_members(actor=self.actor, after=after, limit=200)
            result.extend(page)
            if len(page) < 200:
                return result
            after = page[-1]['member_id']

    @staticmethod
    def _key(owner, member):
        data = [owner['profile'], owner['session'], member['member_id'], member['generation']]
        encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
        return 'colleague-dialogue:' + hashlib.sha256(encoded).hexdigest()

    def select(self, owner, sequence, *, target=None):
        """Resolve explicit {'id':...} / {'name':...}, or a previous target.

        This is only a snapshot: actual dispatch must recheck rights, current
        generation, readiness and the retirement admission fence atomically.
        Never fall back to an arbitrary member when a target is unavailable.
        """
        profile, session = _owner(owner, self.profile)
        sequence = _sequence(sequence)
        if target is not None:
            if (not isinstance(target, dict) or len(target) != 1 or next(iter(target)) not in ('id', 'name')
                    or not isinstance(next(iter(target.values())), str)
                    or not next(iter(target.values())).strip() or len(next(iter(target.values()))) > 80):
                raise ValueError('invalid_target')
        target_key = json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM conversation_inbox WHERE profile=? AND session=? AND sequence=?',
                             (profile, session, sequence)).fetchone()
            if row is None:
                later = db.execute('SELECT 1 FROM conversation_inbox WHERE profile=? AND session=? AND sequence>? LIMIT 1',
                                   (profile, session, sequence)).fetchone()
                if later:
                    raise ValueError('out_of_order_registration')
                db.execute('INSERT INTO conversation_inbox VALUES (?,?,?,?,?,NULL,NULL)',
                           (profile, session, sequence, target_key, 'pending'))
            elif row['target'] != target_key:
                raise ValueError('sequence_conflict')
            elif row['state'] != 'pending':
                return {'state': 'turn_already_settled'}
            prior = db.execute('SELECT 1 FROM conversation_inbox WHERE profile=? AND session=? AND sequence<? AND state=? LIMIT 1',
                               (profile, session, sequence, 'pending')).fetchone()
            if prior:
                return {'state': 'prior_turn_pending'}
        if target is None:
            with self._db() as db:
                row = db.execute('SELECT * FROM conversation_targets WHERE profile=? AND session=?',
                                 (profile, session)).fetchone()
            if row is None or row['sequence'] >= sequence:
                self._abandon_unselected(profile, session, sequence)
                return {'state': 'target_required'}
            target, expected_generation = {'id': row['member_id']}, row['generation']
        else:
            expected_generation = None
        field = 'member_id' if 'id' in target else 'display_name'
        value = next(iter(target.values()))
        matches = []
        for member in self._members():
            if member[field] != value:
                continue
            try:
                allowed = self.authorize(dict(owner), dict(member)) is True
            except Exception:
                allowed = False
            if (allowed and member['status'] == 'active' and member['runtime_state'] == 'running'
                    and (expected_generation is None or member['generation'] == expected_generation)):
                matches.append(member)
        if not matches:
            self._abandon_unselected(profile, session, sequence)
            return {'state': 'target_unavailable'}
        if len(matches) > 1:
            self._abandon_unselected(profile, session, sequence)
            return {'state': 'ambiguous', 'candidates': [
                {'member_id': m['member_id'], 'display_name': m['display_name'], 'role_id': m['role_id']}
                for m in matches]}
        member = matches[0]
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            updated = db.execute('''UPDATE conversation_inbox SET selected_member=?,selected_generation=?
                WHERE profile=? AND session=? AND sequence=? AND state='pending'
                AND (selected_member IS NULL OR (selected_member=? AND selected_generation=?))''',
                (member['member_id'], member['generation'], profile, session, sequence,
                 member['member_id'], member['generation']))
            if updated.rowcount != 1:
                raise ValueError('selection_changed')
        return {'state': 'selected', 'member_id': member['member_id'],
                'generation': member['generation'], 'display_name': member['display_name'],
                'role_id': member['role_id'], 'conversation_key': self._key(owner, member)}

    def remember(self, owner, sequence, *, member_id, generation, work_id):
        """Advance only after host proves accepted work; older completions cannot overwrite newer ones."""
        profile, session = _owner(owner, self.profile)
        sequence = _sequence(sequence)
        if (not isinstance(member_id, str) or not member_id or type(generation) is not int or generation < 1
                or not isinstance(work_id, str) or not work_id or len(work_id) > 128):
            raise ValueError('invalid_assignment')
        try:
            verified = self.verify_assignment(dict(owner), sequence, member_id, generation, work_id) is True
        except Exception:
            verified = False
        if not verified:
            raise PermissionError('assignment_unverified')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute('SELECT * FROM conversation_inbox WHERE profile=? AND session=? AND sequence=?',
                                 (profile, session, sequence)).fetchone()
            if pending is None or (pending['selected_member'], pending['selected_generation']) != (member_id, generation):
                raise ValueError('assignment_selection_mismatch')
            if pending['state'] not in ('pending', 'done'):
                raise ValueError('turn_already_abandoned')
            prior = db.execute('SELECT 1 FROM conversation_inbox WHERE profile=? AND session=? AND sequence<? AND state=? LIMIT 1',
                               (profile, session, sequence, 'pending')).fetchone()
            if prior:
                raise ValueError('prior_turn_pending')
            row = db.execute('SELECT * FROM conversation_targets WHERE profile=? AND session=?',
                             (profile, session)).fetchone()
            if row and row['sequence'] > sequence:
                return {'state': 'superseded'}
            if row and row['sequence'] == sequence:
                if (row['member_id'], row['generation'], row['work_id']) != (member_id, generation, work_id):
                    raise ValueError('sequence_conflict')
                return {'state': 'remembered', 'replayed': True}
            db.execute('''INSERT INTO conversation_targets VALUES (?,?,?,?,?,?)
                ON CONFLICT(profile,session) DO UPDATE SET sequence=excluded.sequence,
                member_id=excluded.member_id,generation=excluded.generation,work_id=excluded.work_id''',
                (profile, session, sequence, member_id, generation, work_id))
            db.execute('UPDATE conversation_inbox SET state=? WHERE profile=? AND session=? AND sequence=?',
                       ('done', profile, session, sequence))
        return {'state': 'remembered', 'replayed': False}

    def abandon(self, owner, sequence):
        """Host closes an unassigned turn only after resolving any uncertain admission."""
        profile, session = _owner(owner, self.profile)
        sequence = _sequence(sequence)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM conversation_inbox WHERE profile=? AND session=? AND sequence=?',
                             (profile, session, sequence)).fetchone()
            if row is None:
                raise ValueError('turn_not_registered')
            if row['state'] == 'done':
                raise ValueError('turn_already_settled')
            db.execute('UPDATE conversation_inbox SET state=? WHERE profile=? AND session=? AND sequence=?',
                       ('abandoned', profile, session, sequence))
        return {'state': 'abandoned'}

    def _abandon_unselected(self, profile, session, sequence):
        # A previous attempt may already have selected and dispatched. Do not
        # close that turn based on a later permission/readiness snapshot.
        with self._db() as db:
            db.execute('''UPDATE conversation_inbox SET state='abandoned'
                WHERE profile=? AND session=? AND sequence=? AND state='pending'
                AND selected_member IS NULL''', (profile, session, sequence))

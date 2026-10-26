"""Persistent read-only analysis dispatch through public Hermes tool registration.

Identity is supplied by the dispatcher keyword, never model arguments or ambient
session environment. The shared queue remains the only scheduling authority.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
import re

from agentcore.pool import Pool


class DispatchService:
    def __init__(self, root, profile, allowed_roots, *, max_sources=8, max_lines=400,
                 denied_globs=None):
        if not isinstance(profile, str) or not profile:
            raise ValueError('profile identity required')
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValueError('queue_root must be absolute')
        if not isinstance(allowed_roots, list) or not allowed_roots:
            raise ValueError('allowed_roots must be a nonempty administrator list')
        self.roots = tuple(Path(value).resolve(strict=True) for value in allowed_roots)
        if any(not value.is_dir() for value in self.roots):
            raise ValueError('allowed roots must be directories')
        self.profile = profile
        self.pool = Pool(root)
        self.max_sources, self.max_lines = max_sources, max_lines
        self.denied_globs = tuple(denied_globs or ()) + (
            '.env*', '*.pem', '*.key', '*credentials*', '*token*', 'auth.json',
            'config.yaml', 'llm_config.json',
        )

    def _owner(self, session_id):
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
            raise PermissionError('trusted dispatcher session identity required')
        return hashlib.sha256(json.dumps([self.profile, session_id]).encode()).hexdigest()

    def _sources(self, sources):
        if not isinstance(sources, list) or not 1 <= len(sources) <= self.max_sources:
            raise ValueError('source count outside policy')
        normalized = []
        for source in sources:
            if not isinstance(source, dict) or set(source) - {'path', 'start_line', 'end_line'}:
                raise ValueError('invalid source fields')
            raw = source.get('path')
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise ValueError('absolute source path required')
            path = Path(raw).resolve(strict=True)
            if not any(path == root or root in path.parents for root in self.roots):
                raise PermissionError('source outside administrator roots')
            if not path.is_file():
                raise ValueError('regular source file required')
            if any(part in ('.ssh', '.aws', '.git') for part in Path(raw).parts + path.parts):
                raise PermissionError('sensitive source path denied')
            if any(fnmatch.fnmatch(part.lower(), pattern.lower())
                   for part in Path(raw).parts + path.parts for pattern in self.denied_globs):
                raise PermissionError('sensitive source path denied')
            start = source.get('start_line', 1)
            end = source.get('end_line')
            if (type(start) is not int or start < 1 or ('end_line' in source and
                    (type(end) is not int or end < start or end - start + 1 > self.max_lines))):
                raise ValueError('source line range outside policy')
            item = {'path': str(path), 'start_line': start}
            if 'end_line' in source:
                item['end_line'] = end
            normalized.append(item)
        return normalized

    def submit(self, session_id, args):
        owner = self._owner(session_id)
        if not isinstance(args, dict) or set(args) - {'goal', 'sources', 'request_key'}:
            raise ValueError('unexpected dispatch arguments')
        goal, key = args.get('goal'), args.get('request_key')
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            raise ValueError('goal must contain 1 to 4000 characters')
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise ValueError('request_key must contain 1 to 128 characters')
        payload = {'goal': goal, 'sources': self._sources(args.get('sources')),
                   '_dispatch_owner': owner}
        dedupe = owner + ':' + hashlib.sha256(key.encode()).hexdigest()
        job = self.pool.submit(dedupe, payload, replay_safe=True, max_tries=2)
        return {'ok': True, 'task_id': job, 'status': self.pool.status(job)['status'],
                'next_action': 'Return the task ID to the user and finish this turn. Do not poll.'}

    def _owned(self, session_id, job):
        owner = self._owner(session_id)
        if not isinstance(job, str) or not re.fullmatch('[0-9a-f]{32}', job):
            raise PermissionError('task unavailable in this session')
        with self.pool.db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None or json.loads(row['payload']).get('_dispatch_owner') != owner:
            raise PermissionError('task unavailable in this session')

    def status(self, session_id, args):
        if not isinstance(args, dict) or set(args) != {'task_id'}:
            raise ValueError('task_id required')
        job = args['task_id']
        self._owned(session_id, job)
        status = self.pool.status(job)
        # Internal attempt tokens are not needed by a user-facing status tool.
        result = {'ok': True, 'task_id': job, 'status': status['status'],
                  'tries': status['tries'], 'cancellation_requested': bool(status['cancel'])}
        if status['result']:
            raw = status['result']
            if len(raw) <= 10000:
                result['result'] = json.loads(raw)
            else:
                result['result_preview'] = raw[:10000]
                result['result_truncated'] = True
        return result

    def cancel(self, session_id, args):
        if not isinstance(args, dict) or set(args) != {'task_id'}:
            raise ValueError('task_id required')
        job = args['task_id']
        self._owned(session_id, job)
        self.pool.cancel(job)
        return {'ok': True, 'task_id': job, 'cancellation_requested': True,
                'status': self.pool.status(job)['status']}


def _handler(service, action):
    def handle(args, *, session_id=None, task_id=None, user_task=None):
        try:
            result = getattr(service, action)(session_id, args)
        except (PermissionError, ValueError, FileNotFoundError) as error:
            result = {'ok': False, 'error': str(error)}
        except Exception as error:
            # Backend failures stay explicit without copying secrets or raw SQL.
            result = {'ok': False, 'error': 'dispatch backend unavailable',
                      'error_type': type(error).__name__}
        return json.dumps(result, ensure_ascii=False)
    return handle


def register(ctx):
    if ctx.get_config('enabled', False) is not True:
        return
    service = DispatchService(ctx.get_config('queue_root'), ctx.profile_name,
                              ctx.get_config('allowed_roots'),
                              denied_globs=ctx.get_config('denied_globs', []))
    source_schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'path': {'type': 'string'}, 'start_line': {'type': 'integer', 'minimum': 1},
        'end_line': {'type': 'integer', 'minimum': 1}}, 'required': ['path']}
    submit_properties = {
        'goal': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'request_key': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                        'description': 'Stable key for retries of the same request; use a new key for an intentional rerun.'},
        'sources': {'type': 'array', 'items': source_schema, 'minItems': 1, 'maxItems': 8},
    }
    entries = (
        ('submit', 'Persist a read-only evidence analysis task. Return its ID and finish the turn; do not wait or poll.',
         submit_properties, ['goal', 'sources', 'request_key']),
        ('status', 'Read a durable task belonging to this same session, only when progress is requested.',
         {'task_id': {'type': 'string'}}, ['task_id']),
        ('cancel', 'Request cancellation of a durable task belonging to this same session.',
         {'task_id': {'type': 'string'}}, ['task_id']),
    )
    for action, description, properties, required in entries:
        name = 'durable_' + action
        ctx.register_tool(name=name, toolset='durable_dispatch', description=description,
                          schema={'name': name, 'description': description, 'parameters': {
                              'type': 'object', 'additionalProperties': False,
                              'properties': properties, 'required': required}},
                          handler=_handler(service, action))

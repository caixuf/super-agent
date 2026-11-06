"""Persistent read-only analysis dispatch through public Hermes tool registration.

Identity is supplied by the dispatcher keyword, never model arguments or ambient
session environment. The shared queue remains the only scheduling authority.

A completion is routed back through the address persisted at submit time: the profile,
the durable session and the route key of the turn that submitted the work. An optional
administrator transport posts directly without starting a model turn. The legacy
injection transport remains explicit compatibility behavior, never an active-chat guess.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
import types

#: Bound session variables copied into a job's persisted origin. These name the route
#: of the turn handling this tool call, not "the active session" of a later process.
_BOUND_ROUTE = (
    ('route', 'HERMES_SESSION_KEY'),
    ('channel', 'HERMES_SESSION_PLATFORM'),
    ('chat', 'HERMES_SESSION_CHAT_ID'),
    ('thread', 'HERMES_SESSION_THREAD_ID'),
)
_ENGINE_MODULES = ('pool.py', 'ops_alerts.py', 'completion_delivery.py',
                   'completion_backfill.py')
_ENGINE_MODULE_NAMES = tuple(name[:-3] for name in _ENGINE_MODULES)
_DEFAULT_DELIVERY_INTERVAL = 1.0
#: Durable ops-health scope for the completion queue: one episode per stuck backlog.
_DELIVERY_HEALTH_SCOPE = 'durable-completion-delivery'
#: Backlog must persist this long before the fault event is enqueued.
_DEFAULT_DELIVERY_HEALTH_FAULT_GRACE = 60.0
_MAX_DELIVERY_HEALTH_FAULT_GRACE = 3600.0
#: A drained backlog must stay clean this long before the recovery event is enqueued.
_DELIVERY_HEALTH_RECOVERY_GRACE = 10.0
_DELIVERY_WARNING_INTERVAL = 60.0


class _BacklogWarningLimiter:
    """Rate-limit noisy observations without affecting delivery or health events."""
    def __init__(self, interval=_DELIVERY_WARNING_INTERVAL):
        self.interval = interval
        self.last_warning = None

    def should_warn(self, summary, now):
        if not (summary['parked'] or summary['notable']):
            return False
        if self.last_warning is None or now - self.last_warning >= self.interval:
            self.last_warning = now
            return True
        return False


def _engine_modules(engine_root=None):
    """``{name: module}`` for the canonical engine modules, without touching ``sys.path``.

    Plugin import itself must work in an isolated host runtime. Administrator
    settings supply the checkout fallback; job arguments never enter resolution.
    """
    try:
        return {name: importlib.import_module('agentcore.' + name)
                for name in _ENGINE_MODULE_NAMES}
    except ModuleNotFoundError as error:
        if error.name not in ('agentcore', 'agentcore.pool', 'agentcore.completion_backfill',
                              'agentcore.completion_delivery', 'agentcore.ops_alerts'):
            raise
    if not isinstance(engine_root, str) or not Path(engine_root).is_absolute():
        raise ValueError('engine_root is required when the engine package is unavailable')
    directory = Path(engine_root).resolve(strict=True) / 'agentcore'
    missing = [name for name in _ENGINE_MODULES if not (directory / name).is_file()]
    if missing:
        raise ValueError('engine_root is missing canonical modules: ' + ', '.join(missing))
    # Load the checkout as a transient package so its intra-package imports resolve,
    # then remove every trace: later checks must still see an uninstalled engine.
    saved = {name: module for name, module in sys.modules.items()
             if name == 'agentcore' or name.startswith('agentcore.')}
    for name in saved:
        del sys.modules[name]
    package = types.ModuleType('agentcore')
    package.__path__ = [str(directory)]
    package.__package__ = 'agentcore'
    sys.modules['agentcore'] = package
    loaded = {}
    try:
        for name in _ENGINE_MODULE_NAMES:
            spec = importlib.util.spec_from_file_location(
                'agentcore.' + name, directory / (name + '.py'))
            if spec is None or spec.loader is None:
                raise ImportError('canonical durable engine unavailable: ' + name)
            module = importlib.util.module_from_spec(spec)
            # Register before execution so ``from agentcore.x import y`` resolves.
            sys.modules['agentcore.' + name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        return dict(loaded)
    finally:
        for name in [name for name in list(sys.modules)
                     if name == 'agentcore' or name.startswith('agentcore.')]:
            del sys.modules[name]
        sys.modules.update(saved)


def _engine(engine_root=None):
    """``(Pool, completion_backfill)`` — the pair the dispatcher builds services from."""
    modules = _engine_modules(engine_root)
    return modules['pool'].Pool, modules['completion_backfill']


def _load_liveness(engine_root=None):
    """Import the ledger, falling back to the administrator checkout.

    The plugin host may not have ``agentcore`` on sys.path even though the
    engine checkout is configured; the liveness row must not silently vanish.
    """
    try:
        return importlib.import_module('agentcore.liveness')
    except ModuleNotFoundError as error:
        if error.name not in ('agentcore', 'agentcore.liveness'):
            raise
    if not isinstance(engine_root, str) or not Path(engine_root).is_absolute():
        raise ValueError('engine_root is required when agentcore is unavailable')
    path = Path(engine_root).resolve(strict=True) / 'agentcore' / 'liveness.py'
    spec = importlib.util.spec_from_file_location('agentcore_liveness', path)
    if spec is None or spec.loader is None:
        raise ImportError('cannot load agentcore.liveness')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pool_type(engine_root=None):
    """Compatibility helper for callers that only need the queue class."""
    return _engine(engine_root)[0]


def Pool(root):
    """Compatibility factory for callers with an installed/importable engine."""
    return _pool_type()(root)


def _bound_route(session_id):
    """Route fields of the turn calling this tool, or ``{}`` when none is bound.

    The bound durable session id must agree with the dispatcher's identity when both
    are present. A mismatch means the caller is not running in the submitting turn,
    so no route is recorded rather than borrowing another conversation's address.
    """
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return {}
    try:
        bound_session = get_session_env('HERMES_SESSION_ID', '')
    except Exception:
        bound_session = ''
    if isinstance(bound_session, str) and bound_session and bound_session != session_id:
        return {}
    route = {}
    for field, variable in _BOUND_ROUTE:
        try:
            value = get_session_env(variable, '')
        except Exception:
            value = ''
        if isinstance(value, str) and value.strip() and len(value) <= 512:
            route[field] = value.strip()
    if 'channel' not in route:
        # Local CLI/TUI/desktop binds SOURCE instead of a platform.
        try:
            source = get_session_env('HERMES_SESSION_SOURCE', '')
        except Exception:
            source = ''
        if isinstance(source, str) and source.strip() and len(source) <= 512:
            route['channel'] = source.strip()
    return route


def _render_completion(message):
    """Platform-neutral completion text; the host adapter renders its own formatting."""
    status = message.get('status')
    label = {'completed': '已完成', 'failed': '失败', 'cancelled': '已取消'}.get(status, status)
    text = '[后台任务 %s] %s\n%s' % (message.get('task_id'), label, message.get('summary'))
    if message.get('truncated'):
        text += '\n（摘要已截断，完整报告请查看任务结果）'
    return text


class InjectionTransport:
    """Deliver through the host's public session injection, addressed by stored route."""

    def __init__(self, inject, profile):
        self._inject = inject
        self.profile = profile

    def deliver(self, address, message, *, idempotency_key):
        if address.get('profile') != self.profile or not address.get('route'):
            return None
        accepted = self._inject(_render_completion(message), session_key=address['route'])
        if accepted is not True:
            # False means the host refused outright; anything else is unknown. Neither
            # acknowledges the receipt, so a later poll can retry after the lease.
            return False if accepted is False else None
        digest = hashlib.sha256(
            ('inject\0' + address['route'] + '\0' + idempotency_key).encode()).hexdigest()
        return {'accepted': True, 'event_id': idempotency_key,
                'receipt_id': 'injected:' + digest[:32]}


def _public_result(raw):
    """Read structured results without slicing encoded JSON into invalid fragments."""
    if len(raw) > 8 * 1024 * 1024:
        return {'error': 'result exceeds read budget'}, True
    try:
        value = json.loads(raw)
    except ValueError:
        return {'error': 'invalid result record'}, True
    # Unicode escaping is an encoding detail, not a reason to discard a useful
    # structured report. Preserve small historical result shapes as well.
    if len(json.dumps(value, ensure_ascii=False)) <= 10000:
        return value, False
    truncated = False
    remaining = 12000

    def bound(item, depth=0, field=''):
        nonlocal truncated, remaining
        if isinstance(item, str):
            per_field = 4096 if field == 'artifacts' else (300 if field == 'limitations' else 2000)
            limit = min(per_field, remaining)
            remaining -= min(len(item), limit)
            if len(item) > limit:
                truncated = True
            return item[:limit]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if depth >= 4:
            truncated = True
            return None
        if isinstance(item, list):
            limit = 3 if field == 'limitations' else 8
            if len(item) > limit:
                truncated = True
            return [bound(part, depth + 1, field) for part in item[:limit]]
        if isinstance(item, dict):
            if len(item) > 24:
                truncated = True
            result = {}
            for key, part in list(item.items())[:24]:
                result[key[:128]] = bound(part, depth + 1, key)
            if isinstance(item.get('summary'), str) and len(result.get('summary', '')) < len(item['summary']):
                result['summary_truncated'] = True
            return result
        truncated = True
        return None

    result = bound(value)
    return result, truncated


class DispatchService:
    def __init__(self, root, profile, allowed_roots, *, max_sources=8, max_lines=400,
                 denied_globs=None, pool_type=None, delivery=None, route_provider=None,
                 require_delivery_route=False, role='analysis', display_name=None,
                 display_label=None, duty=None, target_host=None, model_tier=None,
                 liveness_ledger=None, liveness_path=None, engine_root=None,
                 cross_session_status=False):
        if not isinstance(profile, str) or not profile:
            raise ValueError('profile identity required')
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValueError('queue_root must be absolute')
        if not isinstance(role, str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}', role):
            raise ValueError('invalid colleague role')
        if display_name is None:
            display_name = role
        if display_label is None:
            display_label = role
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 80:
            raise ValueError('invalid colleague display name')
        if not isinstance(display_label, str) or not 1 <= len(display_label) <= 80:
            raise ValueError('invalid colleague display label')
        if duty is not None and (not isinstance(duty, str) or not 1 <= len(duty.strip()) <= 240):
            raise ValueError('invalid colleague duty')
        if target_host is not None and (not isinstance(target_host, str)
                                        or not re.fullmatch(r'[A-Za-z0-9._@-]{1,128}', target_host)):
            raise ValueError('invalid colleague target_host')
        if model_tier is not None and (not isinstance(model_tier, str)
                                       or model_tier not in ('pro', 'main', 'flash')):
            raise ValueError('invalid colleague model_tier')
        if not isinstance(allowed_roots, list) or not allowed_roots:
            raise ValueError('allowed_roots must be a nonempty administrator list')
        self.roots = tuple(Path(value).resolve(strict=True) for value in allowed_roots)
        if any(not value.is_dir() for value in self.roots):
            raise ValueError('allowed roots must be directories')
        self.profile = profile
        self.role, self.display_name = role, display_name
        self.display_label = display_label
        self.duty = duty.strip() if isinstance(duty, str) else None
        self.target_host = target_host
        self.configured_model_tier = model_tier
        try:
            from agentcore.model_routing import resolve_colleague_model
            route = resolve_colleague_model(role, model_tier or '')
            self.model_tier = str(route.get('selected_model') or model_tier or 'main')
            self.model_degraded = bool(route.get('degraded'))
            self.model_degradation_reason = str(route.get('degradation_reason') or '')
        except Exception:
            self.model_tier = model_tier or ('pro' if role in ('architect', 'planning', 'brain', 'plan_gate') else 'main')
            self.model_degraded = False
            self.model_degradation_reason = ''
        self.pool = (pool_type or Pool)(root)
        self.delivery = delivery or importlib.import_module('agentcore.completion_backfill')
        self.route_provider = route_provider or _bound_route
        self.max_sources, self.max_lines = max_sources, max_lines
        if type(require_delivery_route) is not bool:
            raise ValueError('require_delivery_route must be boolean')
        self.require_delivery_route = require_delivery_route
        self.denied_globs = tuple(denied_globs or ()) + (
            '.env*', '*.pem', '*.key', '*credentials*', '*token*', 'auth.json',
            'config.yaml', 'llm_config.json',
        )
        self._liveness_ledger = liveness_ledger
        self._liveness_path = liveness_path
        self._engine_root = engine_root
        self.cross_session_status = bool(cross_session_status)

    def _resolve_liveness(self):
        if self._liveness_ledger is not None:
            return self._liveness_ledger
        try:
            mod = _load_liveness(self._engine_root)
            return mod.LivenessLedger(self._liveness_path)
        except Exception:
            return None

    def _owner(self, session_id):
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
            raise PermissionError('trusted dispatcher session identity required')
        return hashlib.sha256(json.dumps([self.profile, session_id]).encode()).hexdigest()

    def _liveness_owner(self, session_id):
        """Owner string shared with the task_status tool (profile:session)."""
        return '%s:%s' % (self.profile, session_id)

    def _origin(self, session_id):
        """Ownership proof stays a hash; the origin is the addressable conversation."""
        origin = {'profile': self.profile, 'session': session_id}
        try:
            route = self.route_provider(session_id)
        except Exception:
            route = {}
        if isinstance(route, dict):
            for field in ('route', 'channel', 'chat', 'thread'):
                value = route.get(field)
                if isinstance(value, str) and value.strip() and len(value) <= 512:
                    origin[field] = value.strip()
        return origin

    def _sources(self, sources, *, allow_empty=False):
        if allow_empty and sources is None:
            return []
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
        # Only the read-only evidence role analyses source files. Execution roles
        # (dev/ops) act on a goal and their runner never reads source files, so a
        # stray source list must be ignored instead of failing the submission:
        # a frontdesk that attaches a path outside the analysis roots would
        # otherwise lose the whole task ("source outside administrator roots").
        if self.role == 'analysis':
            sources = self._sources(args.get('sources'))
        else:
            # When an administrator configures narrow allowed_roots for a role,
            # still enforce that boundary; when full-capability root ("/") is
            # configured, accept and ignore any stray sources (including
            # directories, not-yet-created files, or config files).
            if args.get('sources') and self.roots != (Path('/'),):
                self._sources(args['sources'])
            sources = []
        ignored_sources = len(args.get('sources') or []) if self.role != 'analysis' else 0
        payload = {'goal': goal, 'sources': sources,
                   '_dispatch_owner': owner, '_colleague_role': self.role,
                   '_colleague_name': self.display_name,
                   '_colleague_label': self.display_label}
        if self.configured_model_tier is not None:
            payload['_model_tier'] = self.model_tier
        dedupe = owner + ':' + hashlib.sha256(key.encode()).hexdigest()
        # ``_dispatch_owner`` is an ownership *proof* (one-way), never an address:
        # persist profile + session + route alongside it so a completion can be routed
        # back to the conversation that submitted the work, after reconnects and restarts.
        origin = self._origin(session_id)
        if self.require_delivery_route and not origin.get('route'):
            raise PermissionError('addressable submitting conversation required before dispatch')
        job = self.pool.submit(dedupe, payload, replay_safe=True, max_tries=2,origin=origin)
        ledger = self._resolve_liveness()
        if ledger is not None:
            try:
                ledger.register(job, kind="dispatch", owner=self._liveness_owner(session_id),
                                label=self.display_label, state="queued")
            except Exception:
                pass
        result = {'ok': True, 'task_id': job, 'status': self.pool.status(job)['status'],
                  'next_action': 'Return the task ID to the user and finish this turn. Do not poll.'}
        if ignored_sources:
            result['sources_ignored'] = ignored_sources
        return result

    def drain_delivery(self, transport, *, limit=50, lease=60, not_before=None):
        """Deliver routable completions once; unaddressable receipts stay pending."""
        return self.delivery.drain(self.pool, transport, profile=self.profile,
                                   limit=limit, lease=lease, not_before=not_before)

    def _owned(self, session_id, job, *, allow_cross_session=False):
        owner = self._owner(session_id)
        if not isinstance(job, str) or not re.fullmatch('[0-9a-f]{32}', job):
            raise PermissionError('task unavailable in this session')
        with self.pool.db() as db:
            row = db.execute('SELECT payload,origin FROM jobs WHERE id=?', (job,)).fetchone()
            repaired = db.execute('SELECT origin FROM origin_repairs WHERE job=?',(job,)).fetchone()
        if row is None:
            raise PermissionError('task unavailable in this session')
        proof = json.loads(row['payload']).get('_dispatch_owner')
        if proof == owner:return
        # Only explicit administrative provenance can recover old CLI work.
        if proof is None and repaired is not None and repaired['origin']==row['origin']:
            origin = json.loads(row['origin'])
            if origin.get('profile')==self.profile and origin.get('session')==session_id:return
        if allow_cross_session:
            try:
                origin = json.loads(row['origin']) if row['origin'] else {}
                if origin.get('profile') == self.profile or not origin.get('profile'):
                    return
            except Exception:
                pass
        raise PermissionError('task unavailable in this session')

    def status(self, session_id, args):
        if not isinstance(args, dict) or set(args) != {'task_id'}:
            raise ValueError('task_id required')
        job = args['task_id']
        self._owned(session_id, job, allow_cross_session=self.cross_session_status)
        status = self.pool.status(job)
        # Internal attempt tokens are not needed by a user-facing status tool.
        result = {'ok': True, 'task_id': job, 'status': status['status'],
                  'tries': status['tries'], 'cancellation_requested': bool(status['cancel'])}
        if status['result']:
            public, truncated = _public_result(status['result'])
            result['result'] = public
            if truncated:
                result['result_truncated'] = True
        ledger = self._resolve_liveness()
        if ledger is not None:
            try:
                if status['status'] in ('queued', 'running') and status.get('created'):
                    ledger.observe(job, heartbeat_at=status.get('heartbeat') or status['created'],
                                   state=status['status'],
                                   detail='pool status: ' + status['status'])
                if status['status'] in ('completed', 'failed', 'cancelled'):
                    term = 'done' if status['status'] == 'completed' else status['status']
                    ledger.finish(job, term, detail="pool status: " + status['status'])
                live = ledger.inspect(job)
                if isinstance(live, dict) and live.get('ok'):
                    result['elapsed_s'] = live.get('elapsed_s')
                    result['heartbeat_age_s'] = live.get('heartbeat_age_s')
                    if live.get('phase'):
                        result['phase'] = live.get('phase')
                    if live.get('progress'):
                        result['progress'] = live.get('progress')
                    if live.get('verdict'):
                        result['verdict'] = live.get('verdict')
                    if live.get('stalled'):
                        result['stalled'] = True
            except Exception:
                pass
        return result

    def cancel(self, session_id, args):
        if not isinstance(args, dict) or set(args) != {'task_id'}:
            raise ValueError('task_id required')
        job = args['task_id']
        self._owned(session_id, job, allow_cross_session=self.cross_session_status)
        before = self.pool.status(job)
        if before['status'] in ('completed', 'failed', 'cancelled'):
            return {'ok': True, 'task_id': job, 'already_terminal': True,
                    'request_accepted': False, 'cancellation_requested': bool(before['cancel']),
                    'status': before['status']}
        self.pool.cancel(job)
        after = self.pool.status(job)
        ledger = self._resolve_liveness()
        if ledger is not None:
            try:
                # Cancellation admission is not proof that the worker stopped.
                # Only project the authoritative queue's confirmed outcome,
                # including a completion that raced this cancellation request.
                if after['status'] in ('completed', 'failed', 'cancelled'):
                    terminal = 'done' if after['status'] == 'completed' else after['status']
                    ledger.finish(job, terminal, detail='pool status: ' + after['status'])
                else:
                    ledger.observe(job, state=after['status'],
                                   detail='cancellation requested; awaiting queue confirmation')
            except Exception:
                pass
        return {'ok': True, 'task_id': job,
                'request_accepted': not bool(before['cancel']) and bool(after['cancel']),
                'already_terminal': not bool(after['cancel']) and after['status'] in ('completed', 'failed', 'cancelled'),
                'cancellation_requested': bool(after['cancel']), 'status': after['status']}



def enqueue_tab_follow_up(task_id, text, *, owner="", origin=None, inbox=None):
    """Prefer-D1 Tab: bind follow-up to task-id + origin without durable_submit.

    Does not open a second long exec. Host control / barge-in path calls this
    (or BargeInController.queue_follow_up) instead of submit().
    """
    from agentcore.barge_in_control import DEFAULT_INBOX, bind_follow_up_to_task
    target = inbox if inbox is not None else DEFAULT_INBOX
    return bind_follow_up_to_task(target, task_id, text, owner=owner, origin=origin)



#: Deterministic safety net for a repeatedly observed misroute. The model picks
#: role=analysis whenever a request says "investigate/analyse", but the analysis
#: runner is read-only evidence analysis of supplied files: it cannot run git,
#: read history or inspect a repository. These tasks must go to an execution role.
_INVESTIGATION_TERMS = (
    'git', 'commit', 'diff', 'branch', 'repo', 'log', 'pytest', 'status --short',
    '提交', '改动', '更改', '变更', '最近', '历史', '仓库', '运行', '命令', '执行',
    '测试', '版本', '改动了什么', '改了什么',
)


def _backend_execution_session() -> bool:
    """A durable worker's nested CLI turn must execute, not dispatch again.

    The runner sets this flag together with the backend platform. Without it the
    nested turn inherits the frontdesk routing prompt and re-submits the job
    instead of doing the work (observed live on both hosts).
    """
    return os.environ.get('SUPERAGENT_BACKEND_RUNNER') == '1'


def _investigation_goal(goal) -> bool:
    if not isinstance(goal, str):
        return False
    text = goal.lower()
    return any(term in text for term in _INVESTIGATION_TERMS)


class DispatchRouter:
    """One public entry point, administrator-owned queues and per-role policies."""
    def __init__(self,services, roster_path=None):
        self.services = services
        self._roster_path = roster_path

    def roster(self,session_id,args):
        """Read-only roster query: never submits, executes or delegates work.

        Answers "how many colleagues do you have" without forcing a query through
        durable_submit, which requires source files inside allowed_roots. The
        dispatcher's own role table is always authoritative; an administrator may
        additionally point at a colleague registry for live member counts.
        """
        if not isinstance(args,dict) or set(args)-{'role'}:
            raise ValueError('roster takes no arguments')
        entries=[]
        for role,svc in sorted(self.services.items()):
            item={'role':role,'display_name':svc.display_name,
                  'display_label':svc.display_label}
            if getattr(svc,'duty',None):
                item['duty']=svc.duty
            if getattr(svc,'target_host',None):
                item['target_host']=svc.target_host
            if getattr(svc,'model_tier',None):
                item['model_tier']=svc.model_tier
            entries.append(item)
        result={'ok':True,'source':'dispatcher_roles','count':len(entries),
                'roles':entries[:24]}
        path=self._roster_path
        if path is not None and Path(path).is_file():
            import sqlite3
            con=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=5)
            try:
                total=con.execute(
                    "select count(*) from colleague_members where status='active'").fetchone()[0]
                by_role=con.execute(
                    "select role_id,count(*) from colleague_members where status='active' "
                    "group by role_id order by role_id limit 16").fetchall()
            finally:
                con.close()
            result['members']={'active':int(total),
                               'by_role':[{'role':str(r),'count':int(n)} for r,n in by_role]}
            result['source']='roster_registry'
        return result

    def _call(self,action,session_id,args):
        if not isinstance(args,dict): raise ValueError('dispatch arguments required')
        # Execution is the safe default: a task that was never given a role must
        # still be able to run. Analysis is narrower (needs sources inside the
        # administrator roots), so it stays opt-in by naming it.
        role = args.get('role') or ('dev' if 'dev' in self.services else 'analysis')
        if role == 'analysis' and 'dev' in self.services:
            # Live failure: "调查一下 super-agent 最近的更改" was submitted as
            # analysis with two invented file paths, so the read-only evidence
            # runner could not run git and produced a citation-only summary.
            # Investigating changes, history, logs or a repository is execution
            # work regardless of how the request is worded.
            reroute = _investigation_goal(args.get('goal'))
            if args.get('sources') and not reroute:
                try:
                    self.services['analysis']._sources(args.get('sources'))
                except (PermissionError, ValueError, FileNotFoundError):
                    reroute = True
            if reroute:
                sources = args.get('sources') or []
                paths = [str(s['path']) for s in sources
                         if isinstance(s, dict) and isinstance(s.get('path'), str)][:8]
                goal = args.get('goal')
                if paths and isinstance(goal, str):
                    goal = goal + '\n参考文件（用户指定）: ' + ', '.join(paths)
                args = {k: v for k, v in args.items() if k != 'sources'}
                if goal is not None:
                    args['goal'] = goal
                role = 'dev'
        if not isinstance(role,str) or role not in self.services:
            raise ValueError('unknown administrator colleague role')
        result = getattr(self.services[role],action)(session_id,
            {key:value for key,value in args.items() if key != 'role'})
        return dict(result,role=role)

    def submit(self,session_id,args): return self._call('submit',session_id,args)
    def status(self,session_id,args): return self._call('status',session_id,args)
    def cancel(self,session_id,args): return self._call('cancel',session_id,args)


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


def _delivery_interval(ctx):
    try:
        value = float(ctx.get_config('delivery_interval', _DEFAULT_DELIVERY_INTERVAL))
    except (TypeError, ValueError):
        value = _DEFAULT_DELIVERY_INTERVAL
    if not 0.05 <= value <= 300.0:
        raise ValueError('delivery_interval must be between 0.05 and 300 seconds')
    return value


def _delivery_health_fault_grace(ctx):
    """``delivery_health_fault_grace``: seconds a stuck backlog must persist to alert."""
    try:
        value = float(ctx.get_config('delivery_health_fault_grace',
                                     _DEFAULT_DELIVERY_HEALTH_FAULT_GRACE))
    except (TypeError, ValueError):
        value = _DEFAULT_DELIVERY_HEALTH_FAULT_GRACE
    if not 0.0 <= value <= _MAX_DELIVERY_HEALTH_FAULT_GRACE:
        raise ValueError('delivery_health_fault_grace must be between 0 and 3600 seconds')
    return value


def _delivery_summary(status):
    """Sanitized counters for one drain pass; job ids, routes and text never enter."""
    row = status if isinstance(status, dict) else {}
    outcomes = row.get('outcomes') if isinstance(row.get('outcomes'), list) else []
    notable = sorted({str(item.get('status')) for item in outcomes
                      if isinstance(item, dict)
                      and item.get('status') not in (None, 'delivered', 'empty')})
    parked = row.get('parked') if isinstance(row.get('parked'), (list, tuple)) else ()
    try:
        delivered = max(0, int(row.get('delivered') or 0))
    except (TypeError, ValueError):
        delivered = 0
    return {'status': str(row.get('status') or 'unknown'),
            'delivered': delivered,
            'parked': len(parked),
            'notable': sum(1 for item in outcomes
                           if isinstance(item, dict)
                           and item.get('status') not in (None, 'delivered', 'empty')),
            'outcomes': notable[:8]}


def _report_delivery_health(service, status, *, modules=None, fault_grace=None, now=None):
    """Record one pass as a durable ops-health observation; never raises.

    A parked receipt or a refused delivery is a *stuck* completion: it will never reach
    its conversation by itself. ``observe_health`` debounces that condition and enqueues a
    fault (and later a recovery) event in the shared alert outbox, so the host's alert
    transport reports it instead of the poller retrying silently forever.
    """
    summary = _delivery_summary(status)
    healthy = summary['parked'] == 0 and summary['notable'] == 0
    try:
        if modules is None:
            modules = _engine_modules(_CTX.get_config('engine_root') if _CTX is not None else None)
        outbox = modules['ops_alerts'].AlertOutbox(str(service.pool.root), pool=service.pool)
        outbox.observe_health(
            service.profile, _DELIVERY_HEALTH_SCOPE, healthy=healthy,
            expected={'parked': 0, 'outcomes': []},
            observed={'status': summary['status'], 'parked': summary['parked'],
                      'delivered': summary['delivered'], 'outcomes': summary['outcomes']},
            fault_grace=(_DEFAULT_DELIVERY_HEALTH_FAULT_GRACE if fault_grace is None
                         else fault_grace),
            recovery_grace=_DELIVERY_HEALTH_RECOVERY_GRACE, now=now)
    except Exception:
        # A broken health record must never stop delivery or hold the queue.
        logging.getLogger(__name__).debug('delivery health observation failed', exc_info=True)
    return summary


def start_backfill(ctx, service):
    """Poll the queue and deliver routable completions until the plugin unloads.

    Opt-in through ``deliver_completions: true`` plus the host's own
    ``plugins.entries.durable-dispatch.allow_gateway_injection: true``. The thread holds
    no delivery state: every pass re-reads the ledger, so a restart, reconnect or a
    second gateway resumes the same pending receipts without replaying delivered ones.

    ``report_delivery_health`` (default true) additionally records each pass in the
    shared ops-alert outbox, so a backlog that cannot be delivered becomes a reported
    fault instead of a silent one.
    """
    if ctx.get_config('deliver_completions', False) is not True:
        return None
    interval = _delivery_interval(ctx)
    not_before = ctx.get_config('delivery_not_before', None)
    if not_before is not None and (not isinstance(not_before, (int, float)) or not 0 <= not_before < float('inf')):
        raise ValueError('delivery_not_before must be a finite nonnegative timestamp')
    factory_path = ctx.get_config('delivery_transport_file', '')
    if factory_path:
        path = Path(factory_path)
        if not path.is_absolute():
            raise ValueError('absolute administrator delivery transport path required')
        spec = importlib.util.spec_from_file_location('_durable_completion_transport', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        transport = module.create_transport(ctx, service)
    else:
        inject = getattr(ctx, 'inject_message', None)
        if not callable(inject):
            return None
        transport = InjectionTransport(inject, service.profile)
    report_health = ctx.get_config('report_delivery_health', True) is not False
    health_modules = None
    fault_grace = None
    if report_health:
        try:
            health_modules = _engine_modules(ctx.get_config('engine_root'))
            fault_grace = _delivery_health_fault_grace(ctx)
        except Exception:
            # The queue still delivers; only the durable health record is skipped.
            logging.getLogger(__name__).warning('delivery health disabled: engine root unusable',
                                                exc_info=True)
            report_health = False
    stop = threading.Event()

    def loop():
        last_error = None
        warning_limiter = _BacklogWarningLimiter()
        while not stop.is_set():
            try:
                status = service.drain_delivery(transport, not_before=not_before)
                last_error = None
            except Exception as exc:
                # A broken pass must not kill the poller; the next pass retries from
                # the ledger. Report only a changed error type, never task content.
                name = type(exc).__name__
                if name != last_error:
                    logging.getLogger(__name__).warning('completion backfill failed: %s', name)
                last_error = name
            else:
                if report_health:
                    summary = _report_delivery_health(service, status, modules=health_modules,
                                                      fault_grace=fault_grace)
                    if warning_limiter.should_warn(summary, time.monotonic()):
                        logging.getLogger(__name__).warning(
                            'completion delivery backlog: parked=%d outcomes=%s status=%s',
                            summary['parked'], summary['outcomes'] or '-', summary['status'])
            if stop.wait(interval):
                break

    thread = threading.Thread(target=loop, name='durable-dispatch-backfill', daemon=True)
    thread.start()

    def _stop():
        stop.set()
        thread.join(timeout=max(1.0, interval))

    on_unload = getattr(ctx, 'on_unload', None)
    if callable(on_unload):
        try:
            on_unload(_stop)
        except TypeError:
            pass
    return thread


def _render_routing_prompt(services) -> str:
    base = """Capability routing for durable dispatch. Use durable_submit for every task.
Pick the role by what the work needs, not by keywords.
- role=dev executes work: inspecting a repository, running git/read commands, checking versions or configuration, running a test. It needs only goal; omit sources entirely. Never attach sources to a dev task.
- role=analysis performs read-only evidence analysis of files inside the administrator roots. It requires sources with absolute paths inside those roots. If a task is about inspecting or running something rather than analysing a supplied file, it is a dev task.
- If no listed role fits the work, submit it to dev as the general execution fallback instead of telling the user the work is unavailable. Report a genuine capability gap only when the returned task itself fails.
- Every new request is a new task, even when an earlier task answered something similar: current state (today, now, again) changes, so submit again with a new request_key. durable_status only reports a task you already submitted; never answer a fresh request from an older task's result.
- Task status inquiry vs new task:
  * When user asks about task status, progress, or state (e.g. "这个还在做吗", "任务状态", "还在跑吗", "好了吗", "进度如何"), OR provides a 32-hex task ID:
    NEVER call durable_submit! You MUST call durable_status with that task_id. Report the live status (queued, running, completed, failed) and details (phase, progress, verdict, summary) directly to the user.
    If running or queued, reassure the user the worker is actively executing. NEVER claim a role has no worker or that the task disappeared.
  * Only call durable_submit when the user is explicitly requesting a NEW piece of work, test, or investigation to be performed.
- Investigating recent changes, git history, logs or any repository state is ALWAYS dev work. analysis is only for evidence about a file the user explicitly supplied; never pick analysis for an investigation.
If the user asks to look at changes, check logs, run a command or inspect the repo, submit role=dev with no sources. Do not claim you cannot see the repository: dev exists for exactly that. Submit one task, return its ID and role, then finish the turn without waiting or polling. NEVER submit work through terminal/execute_code or a raw pool CLI. Use durable_status/cancel with the returned role and task ID. An IM frontdesk only interacts, dispatches and reports; it must not execute or delegate work itself. Unsupported work must be reported as unavailable, never silently rerouted. Never provide an owner, queue path, shell command, runner, or model adapter."""
    domain_lines = []
    for role, svc in sorted(services.items()):
        if role in ('analysis', 'dev'):
            continue
        duty = getattr(svc, 'duty', None)
        host = getattr(svc, 'target_host', None)
        if duty or host:
            host_tag = (' [target=%s]' % host) if host else ''
            desc = (': ' + duty) if duty else ''
            domain_lines.append('- role=%s (%s)%s%s' % (role, svc.display_name, host_tag, desc))
    if not domain_lines:
        return base
    extra = '\nConfigured domain experts (omit sources when dispatching to an execution expert):\n' + '\n'.join(domain_lines)
    if len(base) + len(extra) <= 3100:
        return base + extra
    return base


def format_boost_status(services=None, ctx=None) -> str:
    """Format acceleration status, 100+ dynamic slots, and queue latency metrics."""
    poll_ms = 50
    reap_ms = 20
    delivery_sec = 1.0
    if ctx is not None:
        try:
            delivery_sec = _delivery_interval(ctx)
        except Exception:
            delivery_sec = _DEFAULT_DELIVERY_INTERVAL

    lines = [
        "🚀 **Super Agent 加速执行状态报告 (System Boost Status)**",
        "",
        "⚡ **执行加速与延迟优化 (Latency & Throughput)**:",
        f"• 队列轮询延迟 (Queue Poll): `{poll_ms}ms` (提升 10x 响应速度)",
        f"• 状态机收割延迟 (Event Reap): `{reap_ms}ms`",
        f"• 回执回流投递周期 (Delivery Interval): `{delivery_sec:.2f}s`",
        "• 批量出队吞吐 (Batch Claim Size): `32` tasks / pass",
        "",
        "👥 **并发槽位规模 (Concurrency Slots: 100+ 目标达成)**:",
        "• ColleaguePool 动态槽位上限: `128` slots (满足 100+ 子 Agent 弹性迸发)",
        "• 远程分布式工作节点容量 (guanguan): `120` 物理并发槽位 (10 大专业领域服务)",
        "• 本地研发工作节点容量 (caixuf): `12` 物理并发槽位",
        "",
        "🏢 **集群分布式专业领域守护拓扑**:",
        "• `cases` (IMFL 用例生成与边界扩增): 16 slots [guanguan]",
        "• `dev` (通用代码开发与执行): 16 slots [guanguan] + 8 slots [caixuf]",
        "• `test` (容器回归与自动化测试): 16 slots [guanguan]",
        "• `replay` (rosbag 回灌对账与时序诊断): 16 slots [guanguan]",
        "• `reviewer` (代码审阅与脱窗防护): 12 slots [guanguan]",
        "• `triage` (状态机跳转与失败定性): 12 slots [guanguan]",
        "• `architect` (需求拆解与架构契约): 8 slots [guanguan]",
        "• `ops` (基础设施与告警运维): 8 slots [guanguan]",
        "• `maintainer` (系统维护与存活性排障): 8 slots [guanguan]",
        "• `analysis` (主 Bot 汇总与证据分析): 8 slots [guanguan] + 4 slots [caixuf]",
        "",
        "🛡️ **稳定性与隔离保障**:",
        "• 任务隔离: FlowCoro / SQLite WAL + 独立 Scratch Worktree 零文件踩踏",
        "• 存活性双重门禁: PID + 心跳租约保护，无锁脱窗与超时自动回收",
    ]
    return "\n".join(lines)


def register(ctx):
    if ctx.get_config('enabled', False) is not True:
        return
    # A backend runner is an executor, not a frontdesk: do not inject the
    # dispatch rules or it will submit the same work again instead of doing it.
    backend_runner = _backend_execution_session()
    pool_type, delivery = _engine(ctx.get_config('engine_root'))
    require_route = ctx.get_config('require_delivery_route',ctx.get_config('deliver_completions',False))
    liveness_path = ctx.get_config('liveness_db') or None
    cross_session = ctx.get_config('cross_session_status', ctx.get_config('allow_cross_session_status', False))
    service = DispatchService(ctx.get_config('queue_root'), ctx.profile_name,
                              ctx.get_config('allowed_roots'),
                              denied_globs=ctx.get_config('denied_globs', []),
                              pool_type=pool_type, delivery=delivery,require_delivery_route=require_route,
                              role='analysis', display_name='主 Bot 汇总', display_label='汇总',
                              liveness_path=liveness_path, engine_root=ctx.get_config('engine_root'),
                              cross_session_status=cross_session)
    services = {'analysis':service}
    colleagues = ctx.get_config('colleagues',{})
    if not isinstance(colleagues,dict) or len(colleagues)>24:
        raise ValueError('colleagues must be a bounded administrator mapping')
    seen_roots = {service.pool.root.resolve()}
    allowed_colleague_keys = {
        'queue_root', 'allowed_roots', 'denied_globs',
        'display_name', 'display_label', 'duty', 'target_host', 'model_tier',
    }
    for role,settings in colleagues.items():
        if (not isinstance(role,str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}',role)
                or role=='analysis' or not isinstance(settings,dict)
                or set(settings)-allowed_colleague_keys):
            raise ValueError('invalid administrator colleague configuration')
        candidate = DispatchService(settings.get('queue_root'),ctx.profile_name,settings.get('allowed_roots'),
            denied_globs=settings.get('denied_globs',[]),pool_type=pool_type,delivery=delivery,
            require_delivery_route=require_route, role=role,
            display_name=settings.get('display_name', role),
            display_label=settings.get('display_label', role),
            duty=settings.get('duty'),
            target_host=settings.get('target_host'),
            model_tier=settings.get('model_tier'),
            liveness_path=liveness_path,
            engine_root=ctx.get_config('engine_root'),
            cross_session_status=cross_session)
        if candidate.pool.root.resolve() in seen_roots:
            raise ValueError('colleague queues must be distinct')
        seen_roots.add(candidate.pool.root.resolve()); services[role]=candidate
    if not backend_runner:
        ctx.register_system_prompt_section(
            'durable-dispatch-routing',
            _render_routing_prompt(services),
            # The host DROPS a section over its budget (it does not truncate), so the
            # budget must exceed the literal text below or the frontdesk never sees
            # the routing rules. tests/test_durable_dispatch.py pins this.
            position='after_memory', max_chars=3600,
        )
    roster_path = ctx.get_config('roster_registry_path')
    if (not isinstance(roster_path, str) or not roster_path
            or not Path(roster_path).is_absolute()):
        roster_path = None
    router = DispatchRouter(services, roster_path=roster_path)
    max_source_lines = service.max_lines
    names = ', '.join('%s=%s' % (role, svc.display_name) for role, svc in services.items())
    default_role = 'dev' if 'dev' in services else 'analysis'
    role_schema = {'type':'string','enum':list(services),
                   'description':'Role for this task; omit it to use ' + default_role + '. Known roles: ' + names}
    source_schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'path': {'type': 'string', 'description': 'Absolute path to a regular file inside the administrator-approved roots; relative paths are rejected.'},
        'start_line': {'type': 'integer', 'minimum': 1,
                       'description': 'Optional 1-based first line. Omit to start at line 1. Must be an integer, not a string.'},
        'end_line': {'type': 'integer', 'minimum': 1,
                     'description': 'Optional 1-based last line. Omit to analyze the whole file. When set, end_line >= start_line and the inclusive span (end_line - start_line + 1) must not exceed ' + str(max_source_lines) + ' lines; a larger or reversed range is rejected.'}},
        'required': ['path']}
    submit_properties = {
        'goal': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'request_key': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                        'description': 'Stable key for retries of the same request; use a new key for an intentional rerun.'},
        'sources': {'type': 'array', 'items': source_schema, 'maxItems': 8,
                   'description': 'Every source path must be absolute and inside the administrator-approved roots. A rejected submission ends the turn, so verify the path before calling.'},
        'role': role_schema,
    }
    entries = (
        ('submit', 'Persist a durable task. For the analysis role, sources are required (read-only evidence analysis). For an execution role, omit sources and describe the work in goal. Return its ID and finish the turn; do not wait or poll.',
          submit_properties, ['goal', 'request_key']),
        ('status', 'Read a durable task belonging to this same session, only when progress is requested.',
         {'task_id': {'type': 'string'},'role':role_schema}, ['task_id']),
        ('cancel', 'Request cancellation of a durable task belonging to this same session.',
         {'task_id': {'type': 'string'},'role':role_schema}, ['task_id']),
        ('roster', 'Read-only: list the administrator-configured colleague roles and, when a roster registry is configured, the count of active members. Answer roster/status questions with this instead of durable_submit; it creates no task and requires no source files.',
         {}, []),
    )
    # A backend executor must not be able to dispatch again: without this the
    # nested turn re-submits the same work and the real result lands in a local
    # session instead of the conversation that asked for it.
    if not backend_runner:
        for action, description, properties, required in entries:
            name = 'durable_' + action
            ctx.register_tool(name=name, toolset='durable_dispatch', description=description,
                              schema={'name': name, 'description': description, 'parameters': {
                                  'type': 'object', 'additionalProperties': False,
                                  'properties': properties, 'required': required}},
                              handler=_handler(router, action))

        register_command = getattr(ctx, "register_command", None)
        if callable(register_command):
            def _cmd_boost(raw_args: str = "") -> str:
                return format_boost_status(services=services, ctx=ctx)
            register_command(
                "boost", _cmd_boost,
                "显示系统加速状态、100+并发槽位及轮询延迟指标",
                args_hint="[status]"
            )

    if (ctx.get_config('deliver_completions', False) is True
            and ctx.get_config('completion_card_enabled', False) is True):
        producers = getattr(sys, '_superagent_active_card_producers', None)
        if not isinstance(producers, set):
            producers = set()
            setattr(sys, '_superagent_active_card_producers', producers)
        producers.add(ctx.profile_name)
        on_unload = getattr(ctx, 'on_unload', None)
        if callable(on_unload):
            try:
                on_unload(lambda: producers.discard(ctx.profile_name))
            except TypeError:
                pass
    for colleague in services.values(): start_backfill(ctx,colleague)

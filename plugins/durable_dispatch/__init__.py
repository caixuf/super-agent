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
_DEFAULT_DELIVERY_INTERVAL = 5.0
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
                 display_label=None, liveness_ledger=None, liveness_path=None,
                 engine_root=None):
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
        if not isinstance(allowed_roots, list) or not allowed_roots:
            raise ValueError('allowed_roots must be a nonempty administrator list')
        self.roots = tuple(Path(value).resolve(strict=True) for value in allowed_roots)
        if any(not value.is_dir() for value in self.roots):
            raise ValueError('allowed roots must be directories')
        self.profile = profile
        self.role, self.display_name = role, display_name
        self.display_label = display_label
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
                   '_dispatch_owner': owner, '_colleague_role': self.role,
                   '_colleague_name': self.display_name,
                   '_colleague_label': self.display_label}
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
        return {'ok': True, 'task_id': job, 'status': self.pool.status(job)['status'],
                'next_action': 'Return the task ID to the user and finish this turn. Do not poll.'}

    def drain_delivery(self, transport, *, limit=50, lease=60, not_before=None):
        """Deliver routable completions once; unaddressable receipts stay pending."""
        return self.delivery.drain(self.pool, transport, profile=self.profile,
                                   limit=limit, lease=lease, not_before=not_before)

    def _owned(self, session_id, job):
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
        self._owned(session_id, job)
        before = self.pool.status(job)
        if before['status'] in ('completed', 'failed', 'cancelled'):
            return {'ok': True, 'task_id': job, 'already_terminal': True,
                    'request_accepted': False, 'cancellation_requested': bool(before['cancel']),
                    'status': before['status']}
        self.pool.cancel(job)
        ledger = self._resolve_liveness()
        if ledger is not None:
            try:
                ledger.finish(job, 'cancelled', detail='cancelled by user')
            except Exception:
                pass
        after = self.pool.status(job)
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


class DispatchRouter:
    """One public entry point, administrator-owned queues and per-role policies."""
    def __init__(self,services):
        self.services = services

    def _call(self,action,session_id,args):
        if not isinstance(args,dict): raise ValueError('dispatch arguments required')
        role = args.get('role','analysis')
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
    if not 1.0 <= value <= 300.0:
        raise ValueError('delivery_interval must be between 1 and 300 seconds')
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


def register(ctx):
    if ctx.get_config('enabled', False) is not True:
        return
    ctx.register_system_prompt_section(
        'durable-dispatch-routing',
        """Capability routing for bounded read-only evidence analysis:
Use durable_submit for bounded read-only analysis of approved files, including named colleague roles. Choose by the work and source bounds, not by keywords. When the user explicitly names a colleague or says to continue with one, use the matching role from the allowed roles; if the name is ambiguous or the previous assignment is not settled, ask instead of guessing. Choose an available role, submit one task, return its ID and role, then finish the turn without waiting or polling. NEVER submit interactive colleague work through terminal/execute_code or raw pool CLI: that loses the submitting conversation and cannot promise automatic delivery. Use durable_status/cancel with the returned role and task ID. Use delegate_task for independent reasoning requiring separate agent context. Never provide an owner, queue path, shell command, runner, or model adapter.""",
        position='after_memory', max_chars=1000,
    )
    pool_type, delivery = _engine(ctx.get_config('engine_root'))
    require_route = ctx.get_config('require_delivery_route',ctx.get_config('deliver_completions',False))
    liveness_path = ctx.get_config('liveness_db') or None
    service = DispatchService(ctx.get_config('queue_root'), ctx.profile_name,
                              ctx.get_config('allowed_roots'),
                              denied_globs=ctx.get_config('denied_globs', []),
                              pool_type=pool_type, delivery=delivery,require_delivery_route=require_route,
                              role='analysis', display_name='主 Bot 汇总', display_label='汇总',
                              liveness_path=liveness_path, engine_root=ctx.get_config('engine_root'))
    services = {'analysis':service}
    colleagues = ctx.get_config('colleagues',{})
    if not isinstance(colleagues,dict) or len(colleagues)>15:
        raise ValueError('colleagues must be a bounded administrator mapping')
    seen_roots = {service.pool.root.resolve()}
    for role,settings in colleagues.items():
        if (not isinstance(role,str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}',role)
                or role=='analysis' or not isinstance(settings,dict)
                or set(settings)-{'queue_root','allowed_roots','denied_globs','display_name','display_label'}):
            raise ValueError('invalid administrator colleague configuration')
        candidate = DispatchService(settings.get('queue_root'),ctx.profile_name,settings.get('allowed_roots'),
            denied_globs=settings.get('denied_globs',[]),pool_type=pool_type,delivery=delivery,
            require_delivery_route=require_route, role=role,
            display_name=settings.get('display_name', role),
            display_label=settings.get('display_label', role), liveness_path=liveness_path,
            engine_root=ctx.get_config('engine_root'))
        if candidate.pool.root.resolve() in seen_roots:
            raise ValueError('colleague queues must be distinct')
        seen_roots.add(candidate.pool.root.resolve()); services[role]=candidate
    router = DispatchRouter(services)
    names = ', '.join('%s=%s' % (role, service.display_name) for role, service in services.items())
    role_schema = {'type':'string','enum':list(services),
                   'description':'Administrator colleague role; use this to select the named colleague and preserve it for status/cancel. Known roles: ' + names}
    source_schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'path': {'type': 'string'}, 'start_line': {'type': 'integer', 'minimum': 1},
        'end_line': {'type': 'integer', 'minimum': 1}}, 'required': ['path']}
    submit_properties = {
        'goal': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'request_key': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                        'description': 'Stable key for retries of the same request; use a new key for an intentional rerun.'},
        'sources': {'type': 'array', 'items': source_schema, 'minItems': 1, 'maxItems': 8},
        'role': role_schema,
    }
    entries = (
        ('submit', 'Persist a read-only evidence analysis task. Return its ID and finish the turn; do not wait or poll.',
         submit_properties, ['goal', 'sources', 'request_key']),
        ('status', 'Read a durable task belonging to this same session, only when progress is requested.',
         {'task_id': {'type': 'string'},'role':role_schema}, ['task_id']),
        ('cancel', 'Request cancellation of a durable task belonging to this same session.',
         {'task_id': {'type': 'string'},'role':role_schema}, ['task_id']),
    )
    for action, description, properties, required in entries:
        name = 'durable_' + action
        ctx.register_tool(name=name, toolset='durable_dispatch', description=description,
                          schema={'name': name, 'description': description, 'parameters': {
                              'type': 'object', 'additionalProperties': False,
                              'properties': properties, 'required': required}},
                          handler=_handler(router, action))
    for colleague in services.values(): start_backfill(ctx,colleague)

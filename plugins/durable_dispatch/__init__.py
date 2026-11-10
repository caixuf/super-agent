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
_ENGINE_MODULES = ('pool.py', 'ops_alerts.py', 'delivery_sanitizer.py',
                   'completion_delivery.py', 'completion_backfill.py')
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

#: Requester context captured from the submitting turn. A frontdesk paraphrase is
#: lossy: backend colleagues repeatedly received goals that dropped the user's
#: actual wording, referenced IDs/paths and earlier turns, so the task could not be
#: completed. The original request travels with the job alongside the brief.
_REQUEST_CONTEXT_TTL = 1800.0
_REQUEST_CONTEXT_MAX_SESSIONS = 256
_USER_REQUEST_MAX = 4000
_CONVERSATION_MAX = 4000
_CONVERSATION_TURNS = 8
_request_context_lock = threading.Lock()
_request_context = {}


def _message_text(message):
    """Plain text of a chat message; tool calls, images and tool results are skipped."""
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get('type') in ('text', 'input_text'):
                text = part.get('text')
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return '\n'.join(parts)
    return ''


def _conversation_excerpt(history, current):
    """Recent user/assistant text turns before the current message, newest kept."""
    lines = []
    messages = [m for m in (history or []) if isinstance(m, dict)
                and m.get('role') in ('user', 'assistant')]
    if (messages and messages[-1].get('role') == 'user'
            and _message_text(messages[-1]).strip() == (current or '').strip()):
        messages = messages[:-1]
    for message in messages:
        text = _message_text(message).strip()
        if not text:
            continue
        label = '用户' if message['role'] == 'user' else '前台'
        lines.append('%s: %s' % (label, text[:1200]))
    lines = lines[-_CONVERSATION_TURNS:]
    excerpt = '\n'.join(lines)
    if len(excerpt) > _CONVERSATION_MAX:
        excerpt = '…' + excerpt[-(_CONVERSATION_MAX - 1):]
    return excerpt


def remember_request_context(session_id, user_message, conversation_history=None, *, now=None):
    if not isinstance(session_id, str) or not session_id:
        return
    text = _message_text(user_message).strip()
    if not text:
        return
    now = time.time() if now is None else now
    entry = {'user_request': text[:_USER_REQUEST_MAX],
             'conversation': _conversation_excerpt(conversation_history, text),
             'at': now}
    with _request_context_lock:
        _request_context[session_id] = entry
        if len(_request_context) > _REQUEST_CONTEXT_MAX_SESSIONS:
            for key, _ in sorted(_request_context.items(), key=lambda kv: kv[1]['at'])[
                    :len(_request_context) - _REQUEST_CONTEXT_MAX_SESSIONS]:
                _request_context.pop(key, None)


def recall_request_context(session_id, *, now=None):
    now = time.time() if now is None else now
    with _request_context_lock:
        entry = _request_context.get(session_id)
        if entry is None or now - entry['at'] > _REQUEST_CONTEXT_TTL:
            return None
        return dict(entry)


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
                              'agentcore.completion_delivery', 'agentcore.delivery_sanitizer',
                              'agentcore.ops_alerts'):
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


_META_STATUS_UTTERANCE_RE = re.compile(
    r'^\s*(?:这个|刚才那个|上面那个|后台)?(?:任务|重跑|执行)?'
    r'(?:跑了|重跑|执行了)?(?:这么久了[，,]?)?\s*'
    r'(?:还没完事吗|还在跑吗|还在做吗|跑完了吗|做完了吗|好了吗|进度如何|到哪一步了|现在什么状态)'
    r'(?:[，,]\s*(?:完事|跑完|做好)[^\n]{0,16})?[？?！!。.\s]*$'
)
_META_STATUS_GOAL_RE = re.compile(
    r'^\s*(?:查询|查看|检查)(?:一下)?(?:刚才|当前|后台)(?:提交的)?任务(?:是否还在运行|运行状态|执行进度|状态)[。.\s]*$'
)


def _is_meta_status_inquiry(goal):
    """Detect pure task-status inquiries that must call durable_status instead of spawning a meta-job."""
    if not isinstance(goal, str) or not goal.strip():
        return False
    text = goal.strip()
    m = re.match(r'^【用户原话】[：:]\s*([^\n]+)', text)
    if m:
        utterance = m.group(1).strip()
        if _META_STATUS_UTTERANCE_RE.match(utterance):
            return True
    if len(text) <= 160 and (_META_STATUS_UTTERANCE_RE.match(text) or _META_STATUS_GOAL_RE.match(text)):
        return True
    return False


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


_SCHEDULED_ROUTE_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_SCHEDULED_ROUTE_FIELDS = ('route', 'channel', 'chat')


def validate_scheduled_routes(routes):
    """Validate administrator scheduled routes mapping.

    Bounded mapping (<= 16) of name -> {route, channel, chat, thread?}.
    Names match [a-z][a-z0-9_-]{0,31}.
    Fields route, channel, chat must be non-empty strings <= 512 chars.
    Field thread is optional; if present, non-empty string <= 512 chars.
    """
    if routes is None:
        return {}
    if not isinstance(routes, dict) or len(routes) > 16:
        raise ValueError('scheduled_routes must be a bounded administrator mapping')
    normalized = {}
    for name, target in routes.items():
        if not isinstance(name, str) or not _SCHEDULED_ROUTE_NAME_PATTERN.fullmatch(name):
            raise ValueError('invalid scheduled route name: ' + repr(name))
        if not isinstance(target, dict):
            raise ValueError('invalid scheduled route definition for ' + repr(name))
        if set(target) - {'route', 'channel', 'chat', 'thread'}:
            raise ValueError('unexpected fields in scheduled route: ' + repr(name))
        item = {}
        for field in _SCHEDULED_ROUTE_FIELDS:
            value = target.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError('invalid scheduled route field %s for %s' % (field, name))
            item[field] = value.strip()
        if target.get('thread') is not None:
            thread_value = target.get('thread')
            if not isinstance(thread_value, str) or not thread_value.strip() or len(thread_value) > 512:
                raise ValueError('invalid scheduled route field thread for ' + name)
            item['thread'] = thread_value.strip()
        normalized[name] = item
    return normalized


def _get_setting(provider, key, default=None):
    if hasattr(provider, 'get_config'):
        return provider.get_config(key, default)
    if isinstance(provider, dict):
        return provider.get(key, default)
    return getattr(provider, key, default)


def build_dispatch_services(provider, profile_name, *, pool_type=None, delivery=None,
                            route_provider=None):
    """Construct administrator DispatchService mapping from provider settings."""
    engine_root = _get_setting(provider, 'engine_root')
    if pool_type is None or delivery is None:
        p_type, deliv = _engine(engine_root)
        if pool_type is None:
            pool_type = p_type
        if delivery is None:
            delivery = deliv
    require_route = _get_setting(provider, 'require_delivery_route',
                                 _get_setting(provider, 'deliver_completions', False))
    liveness_path = _get_setting(provider, 'liveness_db') or None
    cross_session = _get_setting(provider, 'cross_session_status',
                                 _get_setting(provider, 'allow_cross_session_status', False))
    task_cards = _get_setting(provider, 'task_lifecycle_cards_enabled', False)
    if type(task_cards) is not bool:
        raise ValueError('task_lifecycle_cards_enabled must be boolean')
    if task_cards and (_get_setting(provider, 'deliver_completions', False) is not True
                       or _get_setting(provider, 'direct_delivery_enabled', False) is not True
                       or not _get_setting(provider, 'delivery_transport_file', '')):
        raise ValueError('task cards require direct completion delivery')
    scheduled_routes = validate_scheduled_routes(_get_setting(provider, 'scheduled_routes', {}))
    service = DispatchService(_get_setting(provider, 'queue_root'), profile_name,
                              _get_setting(provider, 'allowed_roots'),
                              denied_globs=_get_setting(provider, 'denied_globs', []),
                              pool_type=pool_type, delivery=delivery,
                              route_provider=route_provider,
                              require_delivery_route=require_route,
                              role='analysis', display_name='主 Bot 汇总', display_label='汇总',
                              liveness_path=liveness_path, engine_root=engine_root,
                              cross_session_status=cross_session, task_cards_enabled=task_cards,
                              scheduled_routes=scheduled_routes)
    services = {'analysis': service}
    colleagues = _get_setting(provider, 'colleagues', {})
    if not isinstance(colleagues, dict) or len(colleagues) > 24:
        raise ValueError('colleagues must be a bounded administrator mapping')
    seen_roots = {service.pool.root.resolve()}
    allowed_colleague_keys = {
        'queue_root', 'allowed_roots', 'denied_globs',
        'display_name', 'display_label', 'duty', 'target_host', 'model_tier',
    }
    for role, settings in colleagues.items():
        if (not isinstance(role, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', role)
                or role == 'analysis' or not isinstance(settings, dict)
                or set(settings) - allowed_colleague_keys):
            raise ValueError('invalid administrator colleague configuration')
        candidate = DispatchService(settings.get('queue_root'), profile_name, settings.get('allowed_roots'),
                                    denied_globs=settings.get('denied_globs', []), pool_type=pool_type,
                                    delivery=delivery, route_provider=route_provider,
                                    require_delivery_route=require_route, role=role,
                                    display_name=settings.get('display_name', role),
                                    display_label=settings.get('display_label', role),
                                    duty=settings.get('duty'),
                                    target_host=settings.get('target_host'),
                                    model_tier=settings.get('model_tier'),
                                    liveness_path=liveness_path,
                                    engine_root=engine_root,
                                    cross_session_status=cross_session, task_cards_enabled=task_cards,
                                    scheduled_routes=scheduled_routes)
        if candidate.pool.root.resolve() in seen_roots:
            raise ValueError('colleague queues must be distinct')
        seen_roots.add(candidate.pool.root.resolve())
        services[role] = candidate
    return services


def build_dispatch_service(provider, profile_name, role, *, pool_type=None, delivery=None,
                           route_provider=None):
    services = build_dispatch_services(provider, profile_name, pool_type=pool_type,
                                       delivery=delivery, route_provider=route_provider)
    if not isinstance(role, str) or role not in services:
        raise PermissionError('colleague role not configured: ' + repr(role))
    return services[role]


class DispatchService:
    def __init__(self, root, profile, allowed_roots, *, max_sources=8, max_lines=400,
                 denied_globs=None, pool_type=None, delivery=None, route_provider=None,
                 require_delivery_route=False, role='analysis', display_name=None,
                 display_label=None, duty=None, target_host=None, model_tier=None,
                 liveness_ledger=None, liveness_path=None, engine_root=None,
                 cross_session_status=False, task_cards_enabled=False,
                 scheduled_routes=None):
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
        if type(task_cards_enabled) is not bool:
            raise ValueError('task_cards_enabled must be boolean')
        self.task_cards_enabled = task_cards_enabled
        if scheduled_routes is not None:
            self.scheduled_routes = validate_scheduled_routes(scheduled_routes)
        else:
            self.scheduled_routes = {}

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

    def _submit_internal(self, session_id, goal, key, *, sources=None, origin,
                         max_goal_len=4000, is_scheduled=False):
        owner = self._owner(session_id)
        if not isinstance(goal, str) or not goal.strip() or len(goal) > max_goal_len:
            raise ValueError('goal must contain 1 to %d characters' % max_goal_len)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise ValueError('request_key must contain 1 to 128 characters')
        # Only the read-only evidence role analyses source files. Execution roles
        # (dev/ops) act on a goal and their runner never reads source files, so a
        # stray source list must be ignored instead of failing the submission:
        # a frontdesk that attaches a path outside the analysis roots would
        # otherwise lose the whole task ("source outside administrator roots").
        if self.role == 'analysis':
            normalized_sources = self._sources(sources)
        else:
            # When an administrator configures narrow allowed_roots for a role,
            # still enforce that boundary; when full-capability root ("/") is
            # configured, accept and ignore any stray sources (including
            # directories, not-yet-created files, or config files).
            if sources and self.roots != (Path('/'),):
                self._sources(sources)
            normalized_sources = []
        ignored_sources = len(sources or []) if self.role != 'analysis' else 0
        payload = {'goal': goal, 'sources': normalized_sources,
                   '_dispatch_owner': owner, '_colleague_role': self.role,
                   '_colleague_name': self.display_name,
                   '_colleague_label': self.display_label}
        if self.configured_model_tier is not None:
            payload['_model_tier'] = self.model_tier
        if not is_scheduled:
            requester = recall_request_context(session_id)
            if requester:
                payload['_user_request'] = requester['user_request']
                if requester.get('conversation'):
                    payload['_conversation'] = requester['conversation']
        dedupe = owner + ':' + hashlib.sha256(key.encode()).hexdigest()
        # ``_dispatch_owner`` is an ownership *proof* (one-way), never an address:
        # persist profile + session + route alongside it so a completion can be routed
        # back to the conversation that submitted the work, after reconnects and restarts.
        if self.require_delivery_route and not origin.get('route'):
            raise PermissionError('addressable submitting conversation required before dispatch')
        card_requested = (self.task_cards_enabled and origin.get('channel') == 'feishu'
                          and bool(origin.get('route')) and bool(origin.get('chat')))
        options = {'request_card': True} if card_requested else {}
        job, is_dedup = self.pool.submit(dedupe, payload, replay_safe=True, max_tries=2,
                                          origin=origin, return_meta=True, **options)
        ledger = self._resolve_liveness()
        if ledger is not None:
            try:
                ledger.register(job, kind="dispatch", owner=self._liveness_owner(session_id),
                                label=self.display_label, state="queued")
            except Exception:
                pass
        status_info = self.pool.status(job)
        if is_scheduled:
            result = {'ok': True, 'task_id': job, 'status': status_info['status'],
                      'deduplicated': is_dedup}
        else:
            result = {'ok': True, 'task_id': job, 'status': status_info['status'],
                      'next_action': 'Return the task ID to the user and finish this turn. Do not poll.'}
        if ignored_sources:
            result['sources_ignored'] = ignored_sources
        if card_requested:
            result['card_requested'] = True
        return result

    def submit(self, session_id, args):
        if not isinstance(args, dict) or set(args) - {'goal', 'sources', 'request_key'}:
            raise ValueError('unexpected dispatch arguments')
        goal, key = args.get('goal'), args.get('request_key')
        origin = self._origin(session_id)
        return self._submit_internal(session_id, goal, key, sources=args.get('sources'),
                                      origin=origin, max_goal_len=4000, is_scheduled=False)

    def submit_scheduled(self, goal, *, route_name, routes=None, idempotency_key, sources=None):
        if routes is not None:
            available = validate_scheduled_routes(routes)
        else:
            available = self.scheduled_routes
        if not isinstance(route_name, str) or route_name not in available:
            raise PermissionError('scheduled route not registered')
        target = available[route_name]
        session_id = 'scheduled:' + route_name
        origin = {
            'profile': self.profile,
            'session': session_id,
            'route': target['route'],
            'channel': target['channel'],
            'chat': target['chat'],
            'scheduled': True,
        }
        if 'thread' in target:
            origin['thread'] = target['thread']
        return self._submit_internal(session_id, goal, idempotency_key, sources=sources,
                                      origin=origin, max_goal_len=65536, is_scheduled=True)

    def drain_cards(self, transport, *, limit=20, not_before=None, stop_event=None):
        """Process bounded durable create intents without holding a queue DB lock during I/O."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError('invalid card batch limit')
        if not_before is not None and (isinstance(not_before, bool)
                or not isinstance(not_before, (int, float)) or not 0 <= not_before < float('inf')):
            raise ValueError('invalid card not_before')
        bridge, creator = getattr(transport, 'card_bridge', None), getattr(transport, 'card_creator', None)
        if bridge is None or not callable(creator):
            raise ValueError('card transport unavailable')
        rows = self.pool.pending_card_creations(limit=limit, not_before=not_before)
        outcomes = []
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                break
            state, error = 'pending', None
            try:
                origin = json.loads(row['origin'])
                payload = json.loads(row['payload'])
                if (not isinstance(origin, dict) or origin.get('profile') != self.profile
                        or origin.get('channel') != 'feishu' or not origin.get('route') or not origin.get('chat')
                        or not isinstance(payload, dict) or payload.get('_colleague_role') != self.role
                        or not isinstance(payload.get('goal'), str)):
                    state, error = 'parked', 'invalid_card_origin'
                else:
                    bound = bridge.bind_task(row['job'], origin, payload['goal'], self.role,
                                             payload.get('_colleague_name') or self.display_name)
                    if not bound.get('ok'):
                        state, error = 'parked', 'card_binding_conflict'
                    else:
                        result = bridge.create_initial(row['job'], creator)
                        if result.get('ok') is True:
                            reconcile = getattr(transport, 'reconcile_card', None)
                            state = 'sent'
                            if callable(reconcile):
                                checked = reconcile(row['job'])
                                if isinstance(checked, dict) and checked.get('terminal') is True:
                                    final = checked.get('result')
                                    patch = final.get('patch_result') if isinstance(final, dict) else None
                                    confirmed = (isinstance(final, dict) and final.get('ok') is True
                                                 and (final.get('already_finalized') is True
                                                      or isinstance(patch, dict) and patch.get('ok') is True))
                                    if not confirmed:
                                        if isinstance(patch, dict) and patch.get('not_sent') is True:
                                            state, error = 'pending', 'card_patch_not_sent'
                                        else:
                                            state, error = 'unknown', 'card_patch_unknown'
                        elif result.get('not_sent') is True:
                            error = 'card_not_sent'
                        else:
                            state, error = 'unknown', 'card_create_unknown'
            except Exception as exc:
                error = type(exc).__name__
                logging.getLogger(__name__).warning('card backfill failed: %s', error)
            self.pool.update_card_creation_state(row['job'], state, error)
            outcomes.append({'task_id': row['job'], 'state': state, 'error': error})
        return outcomes

    def sync_running_cards(self, transport, *, limit=20, not_before=None, stop_event=None):
        """Publish one running transition per task; fence an interrupted patch."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError('invalid card batch limit')
        if not_before is not None and (isinstance(not_before, bool)
                or not isinstance(not_before, (int, float)) or not 0 <= not_before < float('inf')):
            raise ValueError('invalid card not_before')
        bridge = getattr(transport, 'card_bridge', None)
        if bridge is None:
            raise ValueError('card transport unavailable')
        rows = self.pool.pending_running_card_jobs(limit=limit, not_before=not_before)
        outcomes = []
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                break
            job = row['job']
            if not self.pool.claim_running_card_progress(job):
                continue
            state, error = 'unknown', 'card_progress_unknown'
            try:
                result = bridge.update_progress(job, status='running', phase='执行进程已启动')
                patch = result.get('patch_result') if isinstance(result, dict) else None
                if isinstance(result, dict) and result.get('error') == 'already_finalized':
                    state, error = 'terminal', None
                elif isinstance(patch, dict) and patch.get('ok') is True:
                    state, error = 'sent', None
                elif (isinstance(result, dict) and result.get('skipped_throttle') is True
                      or isinstance(patch, dict) and patch.get('not_sent') is True):
                    state, error = 'pending', 'card_progress_not_sent'
            except Exception as exc:
                logging.getLogger(__name__).warning('card progress failed: %s', type(exc).__name__)
            self.pool.update_running_card_progress(job, state, error)
            outcomes.append({'task_id': job, 'state': state, 'error': error})
        return outcomes

    def refresh_running_cards(self, transport, *, limit=20, min_age=15.0, not_before=None, stop_event=None):
        """Refresh active running task cards with live phase, progress, and elapsed time."""
        bridge = getattr(transport, 'card_bridge', None)
        if bridge is None or not hasattr(self.pool, 'active_running_card_jobs'):
            return []
        now = time.time()
        rows = self.pool.active_running_card_jobs(limit=limit, min_age=min_age, not_before=not_before, now=now)
        if not rows:
            return []
        ledger = self._resolve_liveness()
        outcomes = []
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                break
            job = row['job']
            job_st = self.pool.status(job) if hasattr(self.pool, 'status') else None
            if isinstance(job_st, dict) and job_st.get('status') in ('completed', 'failed', 'cancelled'):
                reconcile = getattr(transport, 'reconcile_card', None)
                if callable(reconcile):
                    try:
                        reconcile(job)
                        outcomes.append({'task_id': job, 'status': job_st['status'], 'terminal': True})
                        continue
                    except Exception as exc:
                        logging.getLogger(__name__).debug('card terminal reconcile skipped: %s', type(exc).__name__)
            created = float(row.get('created') or now)
            elapsed = round(max(0.0, now - created), 1)
            phase = '执行进程运行中'
            progress = 0.0
            if ledger is not None:
                try:
                    live = ledger.inspect(job)
                    if isinstance(live, dict) and live.get('ok'):
                        if isinstance(live.get('elapsed_s'), (int, float)) and live['elapsed_s'] > 0:
                            elapsed = round(float(live['elapsed_s']), 1)
                        if isinstance(live.get('phase'), str) and live['phase'].strip():
                            phase = live['phase'].strip()
                        if isinstance(live.get('progress'), (int, float)) and 0.0 <= float(live['progress']) <= 1.0:
                            progress = float(live['progress'])
                        elif isinstance(live.get('progress'), dict):
                            pct = live['progress'].get('percent')
                            if isinstance(pct, (int, float)) and 0.0 <= pct <= 100.0:
                                progress = round(float(pct) / 100.0, 3)
                            elif live['progress'].get('total', 0) > 0:
                                progress = round(float(live['progress']['done']) / float(live['progress']['total']), 3)
                except Exception:
                    pass
            if phase == '执行进程运行中' or progress == 0.0:
                try:
                    if isinstance(job_st, dict) and job_st.get('attempt'):
                        attempt_dir = getattr(self.pool, 'root', None)
                        if attempt_dir is not None:
                            prog_file = attempt_dir / 'attempts' / job_st['attempt'] / 'progress.json'
                            if prog_file.is_file():
                                pdata = json.loads(prog_file.read_text(encoding='utf-8'))
                                if isinstance(pdata, dict):
                                    if pdata.get('phase') and isinstance(pdata['phase'], str):
                                        phase = pdata['phase'].strip()
                                    if isinstance(pdata.get('progress'), (int, float)) and 0.0 <= float(pdata['progress']) <= 1.0:
                                        progress = float(pdata['progress'])
                except Exception:
                    pass
            try:
                bridge.update_progress(job, status='running', phase=phase, progress=progress, elapsed=elapsed)
            except Exception as exc:
                logging.getLogger(__name__).debug('card live refresh skipped: %s', type(exc).__name__)
            self.pool.touch_running_card_progress(job, now=now)
            outcomes.append({'task_id': job, 'elapsed': elapsed, 'phase': phase})
        return outcomes

    def reconcile_terminal_cards(self, transport, *, limit=20, min_age=30.0, not_before=None, stop_event=None):
        """Retry or retire terminal_unknown cards (3-strike degradation) without creating duplicate cards."""
        reconcile = getattr(transport, 'reconcile_card', None)
        if not callable(reconcile) or not hasattr(self.pool, 'terminal_unknown_card_jobs'):
            return []
        now = time.time()
        rows = self.pool.terminal_unknown_card_jobs(
            limit=limit, min_age=min_age, not_before=not_before, now=now
        )
        outcomes = []
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                break
            job = row['job']
            try:
                checked = reconcile(job)
                outcomes.append({'task_id': job, 'terminal': bool(isinstance(checked, dict) and checked.get('terminal'))})
            except Exception as exc:
                logging.getLogger(__name__).debug('card terminal unknown reconcile failed: %s', type(exc).__name__)
        return outcomes

    def inspect_card_health(self, *, not_before=None, now=None, bridge=None):
        """Return aggregate operator health without task contents or destinations."""
        if not_before is not None and (isinstance(not_before, bool)
                or not isinstance(not_before, (int, float)) or not 0 <= not_before < float('inf')):
            raise ValueError('invalid card not_before')
        current = time.time() if now is None else now
        counts = self.pool.card_outbox_health_counts(not_before=not_before, now=current)
        counts['bridge_terminal_unknown'] = 0
        counts['bridge_terminal_stale'] = 0
        if bridge is not None:
            inspect = getattr(bridge, 'terminal_health', None)
            if not callable(inspect):
                raise ValueError('card bridge health unavailable')
            terminal = inspect(stale_before=current - 90)
            counts['bridge_terminal_unknown'] = terminal['unknown']
            counts['bridge_terminal_stale'] = terminal['stale_sending']
        return counts

    def drain_delivery(self, transport, *, limit=50, lease=60, not_before=None):
        """Deliver routable completions once; unaddressable receipts stay pending."""
        return self.delivery.drain(self.pool, transport, profile=self.profile,
                                   limit=limit, lease=lease, not_before=not_before)

    def _owned(self, session_id, job, *, allow_cross_session=False):
        owner = self._owner(session_id)
        if not isinstance(job, str) or not re.fullmatch('[0-9a-f]{32}', job):
            raise PermissionError('task unavailable in this session')
        row = self.pool.job_ownership_record(job)
        if row is None:
            raise PermissionError('task unavailable in this session')
        proof = json.loads(row['payload']).get('_dispatch_owner')
        if proof == owner:return
        try:
            origin = json.loads(row['origin']) if row['origin'] else {}
        except Exception:
            origin = {}
        # Only explicit administrative provenance can recover old CLI work.
        if proof is None and row['repaired_origin'] is not None and row['repaired_origin']==row['origin']:
            if isinstance(origin, dict) and origin.get('profile')==self.profile and origin.get('session')==session_id:return
        if isinstance(origin, dict) and origin.get('scheduled') is True:
            if origin.get('profile') == self.profile:
                try:
                    route_info = self.route_provider(session_id)
                except Exception:
                    route_info = {}
                caller_route = None
                if isinstance(route_info, dict):
                    caller_route = route_info.get('route')
                elif isinstance(route_info, str):
                    caller_route = route_info
                stored_route = origin.get('route')
                if (isinstance(caller_route, str) and caller_route.strip()
                        and isinstance(stored_route, str) and stored_route.strip()
                        and caller_route.strip() == stored_route.strip()):
                    return
        if allow_cross_session:
            if isinstance(origin, dict) and origin.get('profile') == self.profile:
                return
        raise PermissionError('task unavailable in this session')

    def list_owned_statuses(self, session_id, *, limit=5):
        """Return bounded active and recent tasks belonging to this session (or profile when cross_session_status=True)."""
        if not hasattr(self.pool, 'list_recent_jobs'):
            return {'ok': True, 'tasks': [], 'count': 0}
        rows = self.pool.list_recent_jobs(limit=30)
        ledger = self._resolve_liveness()
        matched = []
        for row in rows:
            job = row['id']
            try:
                self._owned(session_id, job, allow_cross_session=self.cross_session_status)
            except PermissionError:
                continue
            try:
                payload = json.loads(row['payload']) if row.get('payload') else {}
            except Exception:
                payload = {}
            item = {
                'task_id': job,
                'role': self.role,
                'status': row['status'],
                'tries': row['tries'],
                'cancellation_requested': bool(row['cancel']),
                'created': row.get('created'),
            }
            goal_text = payload.get('goal') if isinstance(payload, dict) else None
            if isinstance(goal_text, str) and goal_text.strip():
                preview = goal_text.strip().splitlines()[0][:100]
                item['goal_preview'] = preview
            if row.get('result'):
                public, truncated = _public_result(row['result'])
                item['result'] = public
                if truncated:
                    item['result_truncated'] = True
            if ledger is not None:
                try:
                    live = ledger.inspect(job)
                    if isinstance(live, dict) and live.get('ok'):
                        if live.get('elapsed_s') is not None:
                            item['elapsed_s'] = live.get('elapsed_s')
                        if live.get('phase'):
                            item['phase'] = live.get('phase')
                        if live.get('progress'):
                            item['progress'] = live.get('progress')
                except Exception:
                    pass
            matched.append(item)
            if len(matched) >= limit:
                break
        return {'ok': True, 'tasks': matched, 'count': len(matched)}

    def status(self, session_id, args):
        if not isinstance(args, dict) or (set(args) - {'task_id'}):
            raise ValueError('task_id required')
        job = args.get('task_id')
        if job is None or (isinstance(job, str) and not job.strip()):
            return self.list_owned_statuses(session_id, limit=5)
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

    def submit(self,session_id,args):
        if isinstance(args, dict) and _is_meta_status_inquiry(args.get('goal')):
            raise ValueError('status_inquiry_use_durable_status: call durable_status({}) instead of durable_submit')
        return self._call('submit',session_id,args)

    def status(self,session_id,args):
        if not isinstance(args, dict):
            raise ValueError('dispatch arguments required')
        task_id = args.get('task_id')
        explicit_role = args.get('role')
        if (task_id is None or (isinstance(task_id, str) and not task_id.strip())) and not explicit_role:
            if set(args) - {'task_id', 'role'}:
                raise ValueError('task_id required')
            merged = []
            for r_name, svc in sorted(self.services.items()):
                listed = svc.list_owned_statuses(session_id, limit=5)
                for item in listed.get('tasks', []):
                    merged.append(dict(item, role=r_name))
            merged.sort(
                key=lambda x: (
                    0 if x.get('status') in ('running', 'queued') else 1,
                    -float(x.get('created') or 0.0),
                )
            )
            top = merged[:8]
            for item in top:
                item.pop('created', None)
            return {'ok': True, 'tasks': top, 'count': len(top)}
        return self._call('status',session_id,args)

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
    if service.task_cards_enabled and (getattr(transport, 'card_bridge', None) is None
                                       or not callable(getattr(transport, 'card_creator', None))):
        raise ValueError('task cards require a compatible direct transport')
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
    card_thread = None
    if service.task_cards_enabled:
        def card_loop():
            warning_limiter = _BacklogWarningLimiter()
            next_inspection = 0.0
            while not stop.is_set():
                try:
                    service.drain_cards(transport, not_before=not_before, stop_event=stop)
                    service.sync_running_cards(transport, not_before=not_before, stop_event=stop)
                    refresh = getattr(service, 'refresh_running_cards', None)
                    if callable(refresh):
                        refresh(transport, not_before=not_before, stop_event=stop)
                    reconcile_unknown = getattr(service, 'reconcile_terminal_cards', None)
                    if callable(reconcile_unknown):
                        reconcile_unknown(transport, not_before=not_before, stop_event=stop)
                    now = time.monotonic()
                    if now >= next_inspection:
                        next_inspection = now + _DELIVERY_WARNING_INTERVAL
                        health = service.inspect_card_health(not_before=not_before, bridge=transport.card_bridge)
                        warning = {'parked': health['parked'], 'notable': sum(health.values())}
                        if warning_limiter.should_warn(warning, now):
                            logging.getLogger(__name__).warning(
                                'card delivery requires attention: create_unknown=%d terminal_unknown=%d parked=%d '
                                'progress_unknown=%d stale_progress=%d delayed=%d '
                                'bridge_terminal_unknown=%d bridge_terminal_stale=%d',
                                health['create_unknown'], health['terminal_unknown'], health['parked'], health['progress_unknown'],
                                health['stale_progress'], health['delayed'], health['bridge_terminal_unknown'],
                                health['bridge_terminal_stale'])
                except Exception as exc:
                    logging.getLogger(__name__).warning('card backfill pass failed: %s', type(exc).__name__)
                if stop.wait(interval):
                    break
        card_thread = threading.Thread(target=card_loop, name='durable-dispatch-card-backfill', daemon=True)
        card_thread.start()

    def _stop():
        stop.set()
        thread.join(timeout=max(1.0, interval))
        if card_thread is not None:
            card_thread.join(timeout=max(1.0, interval))

    on_unload = getattr(ctx, 'on_unload', None)
    if callable(on_unload):
        try:
            on_unload(_stop)
        except TypeError:
            pass
    return thread


_SECTION_BUDGET = 4000  # host maximum per system prompt section
_BRIEF_RULES = """
Writing the goal (the colleague cannot see this chat; the user's latest message and recent turns are attached automatically, but your goal is the work order):
- CRITICAL DELIVERY TOPOLOGY: The dispatched colleague's output is delivered DIRECTLY to the user/chat via the outbox as the final delivery. The frontdesk will NOT receive the colleague's output and will NOT get a second turn to post-process or analyze it. Therefore, the goal MUST represent the user's FULL END-TO-END objective (including both data retrieval AND domain analysis/conclusions). NEVER treat delegation as a sub-routine (e.g. "you just scrape/fetch data, and I'll analyze it later"); NEVER tell the colleague "do not judge / that is upper layer's job".
- Make it self-contained: what to do, on what (copy every repository, branch, path, case/story/task ID, link, version and number from the conversation verbatim, never paraphrase them), what to hand back, and how to tell it is done.
- Resolve references like "这个/上面那个/刚才的/继续" into the concrete object before submitting.
- If something essential is missing and cannot be found from the conversation (e.g. which case, which version, which environment), ask the user one short question instead of submitting a task that cannot be completed. Do not ask for details a colleague can look up itself.
- Do not narrow, soften or reinterpret the user's request; keep their constraints and acceptance criteria.
- Mandatory verbatim user request: Always prepend the user's verbatim request at the start of the goal: begin with "【用户原话】：<copy user message verbatim>" followed by "【终态目标】：<end-to-end goal>". Never paraphrase away or strip the user's direct words or questions.
- Pick the right domain role: For requirements analysis, table/spec suitability evaluation, or case derivation, dispatch to domain experts (architect or cases), NOT a general dev scraper."""


def _render_routing_base() -> str:
    return """Capability routing for durable dispatch. Submit work with durable_submit when background execution is needed. Do not reflexively dispatch to a subagent for questions, chatting, advice, or architectural discussions; answer those directly in clear human language.
Pick the role by what the work needs, not by keywords.
- role=dev executes work: inspecting a repository, running git/read commands, checking versions or configuration, running a test. It needs only goal; omit sources entirely. Never attach sources to a dev task.
- role=analysis performs read-only evidence analysis of files inside the administrator roots. It requires sources with absolute paths inside those roots. If a task is about inspecting or running something rather than analysing a supplied file, it is a dev task.
- If no listed role fits the work, submit it to dev as the general execution fallback instead of telling the user the work is unavailable. Report a genuine capability gap only when the returned task itself fails.
- Every new request is a new task, even when an earlier task answered something similar: current state (today, now, again) changes, so submit again with a new request_key. durable_status only reports a task you already submitted; never answer a fresh request from an older task's result.
- Task status inquiry vs new task:
  * When user asks about task status, progress, or state (e.g. "这个还在做吗", "任务状态", "还在跑吗", "还没完事吗", "好了吗", "进度如何"), OR provides a 32-hex task ID:
    NEVER call durable_submit! Call durable_status(task_id=...) or durable_status({}) to list chat tasks. Report the live status (queued, running, completed, failed) and details (phase, progress, verdict, summary) directly to the user.
    If running or queued, reassure the user the worker is actively executing. NEVER claim a role has no worker or that the task disappeared.
  * Only call durable_submit when the user is explicitly requesting a NEW piece of work, test, or investigation to be performed.
- Conceptual, architectural, workflow, and colleague-role discussions (e.g. "你现在的agent架构是怎样的", "有用到langgraph么", "有哪些专家角色"): Answer directly in the current turn with deep expertise on Super Agent's 5-layer architecture (1. Thin Frontdesk + CardKit 2.0; 2. Durable SQLite Queue + Colleague Pool; 3. StateGraph + SqliteCheckpointer + AttemptSupervisor bwrap/subreaper sandbox + Hermes/Pi dual engines; 4. Synaptic Memory (Heuristic Surprise Gate + ACT-R + Hebbian Spreading + FTS5/vec0 RRF) + SkillForge; 5. Progressive Delivery + Outbound Sanitizer) and use durable_roster for colleague roles; NEVER call durable_submit to inspect systemd/host processes for architecture or role questions.
- Investigating recent changes, git history, logs or any repository state is ALWAYS dev work. analysis is only for evidence about a file the user explicitly supplied; never pick analysis for an investigation.
If the user asks to look at changes, check logs, run a command or inspect the repo, submit role=dev with no sources. Do not claim you cannot see the repository: dev exists for exactly that. Submit one task, return its ID and role, then finish the turn without waiting or polling. NEVER submit work through terminal/execute_code or a raw pool CLI. Use durable_status/cancel with the returned role and task ID. An IM frontdesk only interacts, dispatches and reports; it must not execute or delegate work itself. Unsupported work must be reported as unavailable, never silently rerouted. Never provide an owner, queue path, shell command, runner, or model adapter."""


def _render_briefing_prompt(services) -> str:
    domain_lines = []
    for role, svc in sorted(services.items()):
        if role in ('analysis', 'dev'):
            continue
        duty = getattr(svc, 'duty', None)
        host = getattr(svc, 'target_host', None)
        if duty or host:
            host_tag = (' [target=%s]' % host) if host else ''
            desc = (': ' + duty[:120]) if duty else ''
            domain_lines.append('- role=%s (%s)%s%s' % (role, svc.display_name, host_tag, desc))
    text = _BRIEF_RULES.lstrip('\n')
    if domain_lines:
        extra = ('\nConfigured domain experts (omit sources when dispatching to an execution expert):\n'
                 + '\n'.join(domain_lines))
        if len(text) + len(extra) <= _SECTION_BUDGET - 50:
            text += extra
    return text


def _render_routing_prompt(services) -> str:
    """Both frontdesk sections; kept for callers that want the full rule set."""
    return _render_routing_base() + '\n' + _render_briefing_prompt(services)


def format_boost_status(services=None, ctx=None) -> str:
    """Describe configured dispatch capabilities without inventing runtime metrics."""
    lines = ["**Super Agent 调度配置**"]
    if isinstance(services, dict) and services:
        lines.append("已配置角色：" + "、".join(sorted(services)))
    else:
        lines.append("已配置角色：未知")
    if ctx is None:
        lines.append("完成回执投递：未知")
    elif ctx.get_config('deliver_completions', False) is True:
        try:
            interval = _delivery_interval(ctx)
        except (TypeError, ValueError):
            lines.append("完成回执投递：已启用，轮询周期配置无效")
        else:
            lines.append(f"完成回执投递：已启用，配置轮询周期 {interval:.2f} 秒")
    else:
        lines.append("完成回执投递：未启用")
    lines.append("实际工作节点、可用并发和性能指标：未测量")
    return "\n".join(lines)


def register(ctx):
    if ctx.get_config('enabled', False) is not True:
        return
    # A backend runner is an executor, not a frontdesk: do not inject the
    # dispatch rules or it will submit the same work again instead of doing it.
    backend_runner = _backend_execution_session()
    services = build_dispatch_services(ctx, ctx.profile_name)
    if not backend_runner:
        ctx.register_system_prompt_section(
            'durable-dispatch-routing',
            _render_routing_base(),
            # The host DROPS a section over its budget (it does not truncate), so the
            # budget must exceed the literal text below or the frontdesk never sees
            # the routing rules. tests/test_durable_dispatch.py pins this.
            position='after_memory', max_chars=_SECTION_BUDGET,
        )
        # Brief-writing rules and the expert roster live in their own section: in
        # one section the roster overflowed the host cap and was silently dropped.
        ctx.register_system_prompt_section(
            'durable-dispatch-briefing',
            _render_briefing_prompt(services),
            position='after_memory', max_chars=_SECTION_BUDGET,
        )
        register_hook = getattr(ctx, 'register_hook', None)
        if callable(register_hook):
            def _capture_request(session_id='', user_message=None,
                                 conversation_history=None, **_):
                try:
                    remember_request_context(session_id, user_message, conversation_history)
                except Exception:
                    logging.getLogger(__name__).debug('request context capture failed',
                                                      exc_info=True)
                return None
            register_hook('pre_llm_call', _capture_request)
    roster_path = ctx.get_config('roster_registry_path')
    if (not isinstance(roster_path, str) or not roster_path
            or not Path(roster_path).is_absolute()):
        roster_path = None
    router = DispatchRouter(services, roster_path=roster_path)
    max_source_lines = services['analysis'].max_lines
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
        'goal': {'type': 'string', 'minLength': 1, 'maxLength': 4000,
                 'description': 'The work order. MUST start with "【用户原话】：<verbatim user message>" to preserve the exact user request, followed by the end-to-end objective and execution details. Never instruct the colleague to skip analysis or claim judgment belongs to the upper layer.'},
        'request_key': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                        'description': 'Stable key for retries of the same request; use a new key for an intentional rerun.'},
        'sources': {'type': 'array', 'items': source_schema, 'maxItems': 8,
                   'description': 'Every source path must be absolute and inside the administrator-approved roots. A rejected submission ends the turn, so verify the path before calling.'},
        'role': role_schema,
    }
    entries = (
        ('submit', 'Persist a durable task. For the analysis role, sources are required (read-only evidence analysis). For an execution role, omit sources and describe the work in goal. Return its ID and finish the turn; do not wait or poll.',
          submit_properties, ['goal', 'request_key']),
        ('status', 'Read durable task progress for this session or chat. Pass task_id for a specific task, or omit task_id (durable_status({})) to list active and recent tasks in this chat. Never call durable_submit to check if a task is running.',
         {'task_id': {'type': 'string'},'role':role_schema}, []),
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
                "显示已配置的调度角色和回执投递设置",
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

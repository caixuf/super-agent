"""Opt-in team administration; actual worker lifecycle belongs to the host."""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import re
import sys


def _engine(ctx):
    try:
        return importlib.import_module('agentcore.colleague_registry')
    except ModuleNotFoundError as error:
        if error.name not in ('agentcore', 'agentcore.colleague_registry'):
            raise
    root = ctx.get_config('engine_root', '')
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError('absolute engine_root required')
    path = Path(root).resolve(strict=True) / 'agentcore/colleague_registry.py'
    # The module is standalone. Keep it registered so dataclass annotations
    # resolve, without replacing the host's agentcore package or search path.
    import hashlib
    name = '_colleague_registry_' + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError('registry engine unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def register(ctx):
    if ctx.get_config('enabled', False) is not True:
        return
    engine = _engine(ctx)
    team_id = ctx.get_config('team_id')
    primary_id = ctx.get_config('primary_id')
    registry_path = ctx.get_config('registry_path')
    if not isinstance(registry_path, str):
        raise ValueError('registry_path required')
    registry = engine.Registry(registry_path, profile_id=team_id, primary_id=primary_id,
                               primary_name=ctx.get_config('primary_name', 'Primary'))
    # Profile is provided by Hermes at registration; model arguments never select it.
    actor = engine.Actor(team_id, ctx.profile_name)

    def handle(action):
        def invoke(args, *, session_id=None, task_id=None, user_task=None):
            if ctx.get_config('enabled', False) is not True:
                return json.dumps({'ok': False, 'error': 'team_disabled'})
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
                return json.dumps({'ok': False, 'error': 'team_session_required'})
            if not isinstance(args, dict):
                return json.dumps({'ok': False, 'error': 'invalid_arguments'})
            try:
                if action == 'list':
                    if set(args) - {'member_after', 'role_after', 'limit'}:
                        raise ValueError('invalid_arguments')
                    limit = args.get('limit', 100)
                    members = registry.list_members(actor=actor, after=args.get('member_after', ''), limit=limit)
                    roles = registry.list_roles(actor=actor, after=args.get('role_after', ''), limit=limit)
                    result = {'members': members, 'roles': roles,
                              'member_next_after': members[-1]['member_id'] if len(members) == limit else None,
                              'role_next_after': roles[-1]['role_id'] if len(roles) == limit else None}
                elif action == 'define_role':
                    if set(args) != {'role_id', 'label', 'duty', 'boundaries', 'request_id'}:
                        raise ValueError('invalid_arguments')
                    result = registry.define_role(actor=actor, **args)
                elif action == 'hire':
                    if set(args) != {'member_id', 'role_id', 'display_name', 'request_id'}:
                        raise ValueError('invalid_arguments')
                    result = registry.hire(actor=actor, **args)
                    historical_state = result.pop('status', None)
                    result = {**result, 'membership_state_at_command': historical_state,
                              'provisioning_required': True if result.get('created') and not result.get('replayed') else None,
                              'note': 'Membership is not worker readiness; on replay the command result is historical. Inspect current state.'}
                elif action == 'grant':
                    if set(args) != {'member_id', 'enabled', 'request_id'}:
                        raise ValueError('invalid_arguments')
                    result = registry.grant_management(actor=actor, **args)
                elif action == 'retire':
                    if set(args) != {'member_id', 'request_id'}:
                        raise ValueError('invalid_arguments')
                    result = registry.request_deactivation(actor=actor, **args)
                    result = {**result, 'note': 'Retirement is requested; the host must drain work and stop the worker.'}
                else:
                    raise ValueError('invalid_action')
                result = {'ok': True, **result}
            except (ValueError, PermissionError) as error:
                code = str(error)
                result = {'ok': False, 'error': code if re.fullmatch(r'[a-z_]{1,64}', code) else 'team_request_rejected'}
            except Exception:
                result = {'ok': False, 'error': 'team_registry_unavailable'}
            return json.dumps(result, ensure_ascii=False)
        return invoke

    slug = {'type': 'string', 'pattern': '^[a-z][a-z0-9_-]{0,63}$', 'maxLength': 64}
    role_slug = {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$', 'maxLength': 32}
    request = {'type': 'string', 'maxLength': 128, 'minLength': 1,
               'description': 'Stable identifier for this exact action; use a fresh value for a new decision.'}
    defs = [
        ('list', 'List team membership and role definitions, including observed worker status. Use cursors for additional pages.',
         {'member_after': {'type': 'string', 'maxLength': 64},
          'role_after': {'type': 'string', 'maxLength': 32},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}),
        ('define_role', 'Define a role description; it grants no tool or file permissions.',
         {'role_id': role_slug, 'label': {'type': 'string', 'maxLength': 80},
          'duty': {'type': 'string', 'maxLength': 500},
          'boundaries': {'type': 'array', 'items': {'type': 'string', 'maxLength': 180}, 'minItems': 1, 'maxItems': 6},
          'request_id': request}),
        ('hire', 'Register a colleague for host provisioning. Registration alone does not start a worker.',
         {'member_id': slug, 'role_id': role_slug, 'display_name': {'type': 'string', 'maxLength': 80}, 'request_id': request}),
        ('grant', 'Delegate or revoke the ability to manage colleagues.',
         {'member_id': slug, 'enabled': {'type': 'boolean'}, 'request_id': request}),
        ('retire', 'Request colleague retirement; host drains outstanding work before stopping.',
         {'member_id': slug, 'request_id': request}),
    ]
    for action, description, properties in defs:
        name = 'colleague_' + action
        ctx.register_tool(name=name, toolset='colleague_team', description=description,
                          schema={'name': name, 'description': description, 'parameters': {
                              'type': 'object', 'additionalProperties': False, 'properties': properties,
                              'required': [] if action == 'list' else list(properties)}}, handler=handle(action))

    # Dispatch is a separate opt-in capability. The plugin never constructs a
    # Provisioner, Gateway connection, owner identity or session binding itself.
    if ctx.get_config('gateway_dispatch.enabled', False) is True:
        from gateway.session_context import get_session_env
        from agentcore import colleague_gateway_service as gateway_service

        gateway_service.configure_session_env(get_session_env)

        def dispatch_context(session_id):
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
                raise PermissionError('trusted_session_context_required')
            profile = ctx.profile_name
            if not isinstance(profile, str) or not profile:
                raise PermissionError('trusted_profile_context_required')
            context_session = get_session_env('HERMES_SESSION_ID', '')
            context_profile = get_session_env('HERMES_SESSION_PROFILE', '')
            if context_session != session_id:
                raise PermissionError('session_context_mismatch')
            if context_profile and context_profile != profile:
                raise PermissionError('profile_context_mismatch')
            return profile, context_profile, context_session

        def dispatch_tool(action):
            def invoke(args, *, session_id=None, **_kwargs):
                if ctx.get_config('enabled', False) is not True or ctx.get_config('gateway_dispatch.enabled', False) is not True:
                    return gateway_service.json_result({'state': 'unavailable', 'error': 'dispatch_disabled'})
                if not isinstance(args, dict):
                    return gateway_service.json_result({'state': 'unavailable', 'error': 'invalid_arguments'})
                try:
                    profile, context_profile, context_session = dispatch_context(session_id)
                    if action == 'assign':
                        if set(args) != {'colleague', 'request_id', 'task'}:
                            raise ValueError('invalid_arguments')
                        result = gateway_service.assign(profile, session_id, context_profile, context_session,
                                                        args['colleague'], args['request_id'], args['task'])
                    else:
                        if 'request_id' not in args or set(args) - {'request_id', 'response_offset', 'response_revision'}:
                            raise ValueError('invalid_arguments')
                        result = gateway_service.inspect(profile, session_id, context_profile, context_session,
                                                         args['request_id'], args.get('response_offset', 0),
                                                         args.get('response_revision'))
                    return gateway_service.json_result(result)
                except (ValueError, PermissionError, RuntimeError) as error:
                    code = str(error)
                    return gateway_service.json_result({'state': 'unavailable',
                        'error': code if re.fullmatch(r'[a-z_]{1,64}', code) else 'gateway_dispatch_unavailable'})
                except Exception:
                    return gateway_service.json_result({'state': 'unavailable', 'error': 'gateway_dispatch_unavailable'})
            return invoke

        dispatch_defs = [
            ('assign', 'Assign a task to an active colleague. The host binds this request to the current Hermes profile and session; task status is queryable in that same conversation.',
             {'colleague': {'type': 'string', 'minLength': 1, 'maxLength': 80,
                            'description': 'Exact colleague member ID or unique registered display name.'},
              'request_id': request,
              'task': {'type': 'string', 'minLength': 1, 'maxLength': 12000}}),
            ('task_status', 'Check a colleague task submitted from this same conversation. To read the next page, pass response_next_offset as response_offset and preserve response_revision.',
             {'request_id': request, 'response_offset': {'type': 'integer', 'minimum': 0,
                 'description': 'Optional character offset returned by the previous page.'},
              'response_revision': {'type': 'string', 'pattern': '^[0-9a-f]{64}$',
                 'description': 'Response revision returned by the previous page; required when offset is nonzero.'}}),
        ]
        for action, description, properties in dispatch_defs:
            name = 'colleague_' + action
            ctx.register_tool(name=name, toolset='colleague_team', description=description,
                              schema={'name': name, 'description': description, 'parameters': {
                                  'type': 'object', 'additionalProperties': False, 'properties': properties,
                                  'required': [key for key in properties if key not in ('response_offset', 'response_revision')]}}, handler=dispatch_tool(action))

    ctx.register_system_prompt_section(
        'colleague-team-operations',
        'The team registry can define roles, register colleagues, delegate management and request retirement. '
        'The host controls actual worker provisioning and draining; do not claim a member is working or removed '
        'until the current runtime state confirms it. Never attempt to retire the primary colleague.',
        position='after_memory', max_chars=500)

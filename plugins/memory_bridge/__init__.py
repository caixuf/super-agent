"""Capture text and terminal verdict separately; recall stays with MemoryProvider.

The gateway never imports the memory engine or invokes a model. Extraction is
performed by the separately supervised runtime_service after a completed verdict.
"""
import importlib.util
import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def register(ctx):
    if ctx.get_config('enabled', False) is not True:
        return
    engine = Path(ctx.get_config('engine_root', ''))
    if not engine.is_absolute():
        raise ValueError('absolute engine_root required')
    spec = importlib.util.spec_from_file_location(
        '_agentcore_turn_spool', engine / 'agentcore/memory/turn_spool.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = object()
    router_file = ctx.get_config('identity_routes_file', missing)
    router = None
    router_enabled = router_file is not missing
    if router_enabled:
        try:
            spec = importlib.util.spec_from_file_location(
                '_superagent_memory_identity', engine / 'runtime/hermes_memory_identity.py')
            identity = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(identity)
            router = identity.IdentityRouter(router_file, profile=ctx.profile_name)
        except Exception:
            log.warning('memory identity routing unavailable; legacy fallback refused')
    spool = None if router_enabled else module.TurnSpool(
        ctx.get_config('spool_dir', ''), profile=ctx.profile_name,
        user_id=ctx.get_config('user_id', ''), agent_id=ctx.get_config('agent_id', ''))

    def scoped_spool(session_id):
        if not router_enabled:
            return spool
        route = router.resolve(session_id) if router is not None else None
        if route is None:
            raise ValueError('memory identity required')
        # Include the policy revision in the adapter's turn id. A remapping
        # between capture and verdict cannot authorize the previously captured
        # turn, even when the new principal uses the same session/turn strings.
        return module.TurnSpool(route['spool_dir'], profile=route['profile'],
                                user_id=route['user_id'], agent_id=route['agent_id']), route['cache_scope']

    def capture_target(kwargs):
        target = scoped_spool(kwargs.get('session_id'))
        turn = kwargs.get('turn_id') or kwargs.get('task_id')
        if router_enabled:
            target, revision = target
            if not isinstance(turn, str) or not turn.strip():
                raise ValueError('turn id required')
            automated = ':cron:' if ':cron:' in turn else ''
            turn = automated + hashlib.sha256(json.dumps([revision, turn]).encode()).hexdigest()
        return target, turn
    resolver = None
    resolver_file = ctx.get_config('input_resolver', '')
    if resolver_file:
        path = Path(resolver_file)
        if not path.is_absolute():
            raise ValueError('absolute input_resolver required')
        rspec = importlib.util.spec_from_file_location('_memory_input_resolver', path)
        rmodule = importlib.util.module_from_spec(rspec)
        rspec.loader.exec_module(rmodule)
        resolver = rmodule.resolve

    def text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return '\n'.join(item.get('text', '') for item in value if isinstance(item, dict))
        return ''

    def _maybe_flash_write(session_id, turn_id, user_msg, reply_msg):
        if ctx.get_config('flash_write', True) is False:
            return
        if module.is_automated_turn(turn_id):
            return
        try:
            if router_enabled:
                route = router.resolve(session_id) if router is not None else None
                if route is None:
                    return
                data_dir = route.get('data_dir', '')
                u_id = route.get('user_id', '')
                a_id = route.get('agent_id', '')
                prof = route.get('profile', '')
            else:
                data_dir = ctx.get_config('data_dir', '')
                u_id = ctx.get_config('user_id', '')
                a_id = ctx.get_config('agent_id', '')
                prof = getattr(ctx, 'profile_name', '')
            if not data_dir or not Path(data_dir).is_dir():
                return
            import sys
            if str(engine) not in sys.path:
                sys.path.insert(0, str(engine))
            from agentcore.memory.memory_surprise import flash_write_explicit_mandate
            flash_write_explicit_mandate(
                user_msg,
                reply_msg,
                data_dir=str(data_dir),
                user_id=str(u_id),
                agent_id=str(a_id),
                profile=str(prof),
                entity_db=ctx.get_config('entity_db') or None,
            )
        except Exception as exc:
            log.debug('flash write skipped: %s', type(exc).__name__)

    def capture(**kwargs):
        try:
            target, turn = capture_target(kwargs)
            user = text(kwargs.get('user_message'))
            if resolver is not None:
                user = resolver(kwargs.get('session_id'), user)
                if user is None:
                    log.warning('memory capture skipped: original input unavailable')
                    return
            reply = text(kwargs.get('assistant_response'))
            target.capture(session_id=kwargs.get('session_id'),
                          turn_id=turn,
                          user_message=user,
                          assistant_reply=reply)
            _maybe_flash_write(kwargs.get('session_id'), turn, user, reply)
        except Exception as exc:
            log.warning('memory capture failed: %s', type(exc).__name__)

    def verdict(**kwargs):
        try:
            target, turn = capture_target(kwargs)
            target.finalize(session_id=kwargs.get('session_id'),
                           turn_id=turn,
                           completed=kwargs.get('completed') is True,
                           failed=kwargs.get('failed', False),
                           interrupted=kwargs.get('interrupted', False))
        except Exception as exc:
            log.warning('memory terminal verdict failed: %s', type(exc).__name__)

    ctx.register_hook('post_llm_call', capture)
    ctx.register_hook('on_session_end', verdict)
    _register_progressive_tools(ctx, engine, router=router, router_enabled=router_enabled)


def _register_progressive_tools(ctx, engine: Path, *, router=None, router_enabled=False) -> None:
    """Expose scoped L1/L2/L3 operations without importing engine modules into Hermes."""
    import json
    import subprocess
    import sys

    data = ctx.get_config("data_dir", "")
    user = ctx.get_config("user_id", "")
    agent = ctx.get_config("agent_id", "")
    profile = getattr(ctx, "profile_name", "")
    endpoint = ctx.get_config("embed_url", "")
    script = engine / "tools/memory_progressive.py"
    if (not router_enabled and (not isinstance(data, str) or not Path(data).is_absolute()
            or not all(isinstance(x, str) and x.strip() for x in (user, agent, profile))
            or not isinstance(endpoint, str) or not script.is_file())):
        log.warning("progressive tools unavailable: scoped memory settings missing")
        return
    private_python = engine / ".superagent-venv/bin/python"
    python = str(private_python) if private_python.is_file() else sys.executable
    definitions = (
        ("memory_search_l1", "search", "Search scoped memory index without returning full bodies.",
         {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["query"]),
        ("memory_timeline_l2", "timeline", "Read scoped memory timeline stubs.",
         {"ids": {"type": "array", "items": {"type": "string"}}, "query": {"type": "string"},
          "window": {"type": "integer", "minimum": 1, "maximum": 50}}, []),
        ("memory_get_observations_l3", "get", "Read selected scoped memory observations.",
         {"ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, ["ids"]),
    )

    def handler(operation):
        def call(args=None, *, session_id=None, **kwargs):
            if not isinstance(session_id, str) or not session_id.strip():
                return json.dumps({"ok": False, "error": "memory_session_required"})
            route = None
            if router_enabled:
                try:
                    route = router.resolve(session_id) if router is not None else None
                except Exception:
                    pass
                if route is None:
                    return json.dumps({"ok": False, "error": "memory_identity_required"})
            if not isinstance(args, dict):
                return json.dumps({"ok": False, "error": "invalid_arguments"})
            try:
                scope = route or dict(data_dir=data, user_id=user, agent_id=agent, profile=profile)
                command = [python, str(script), "--data-dir", scope['data_dir'],
                           "--user", scope['user_id'], "--agent", scope['agent_id'],
                           "--profile", scope['profile'], "--embed-url", endpoint]
                request = json.dumps({"operation": operation, "arguments": args}, ensure_ascii=False)
                if len(request.encode("utf-8")) > 32768:
                    raise ValueError("request too large")
                proc = subprocess.run(command, input=request, text=True, capture_output=True, timeout=20)
                if proc.returncode or len(proc.stdout.encode("utf-8")) > 131072:
                    raise RuntimeError("memory subprocess failed")
                result = json.loads(proc.stdout)
                if not isinstance(result, dict):
                    raise ValueError("invalid memory result")
                if router_enabled and router.resolve(session_id) != route:
                    return json.dumps({"ok": False, "error": "memory_identity_required"})
                return json.dumps(result, ensure_ascii=False)
            except Exception:
                return json.dumps({"ok": False, "error": "memory_operation_unavailable"})
        return call

    for name, operation, description, properties, required in definitions:
        schema = {"name": name, "description": description, "parameters": {
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False}}
        ctx.register_tool(name=name, toolset="memory_bridge", description=description,
                          schema=schema, handler=handler(operation))

"""Capture text and terminal verdict separately; recall stays with MemoryProvider.

The gateway never imports the memory engine or invokes a model. Extraction is
performed by the separately supervised runtime_service after a completed verdict.
"""
import importlib.util
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
    spool = module.TurnSpool(ctx.get_config('spool_dir', ''),
                            profile=ctx.profile_name,
                            user_id=ctx.get_config('user_id', ''),
                            agent_id=ctx.get_config('agent_id', ''))
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

    def capture(**kwargs):
        try:
            user = text(kwargs.get('user_message'))
            if resolver is not None:
                user = resolver(kwargs.get('session_id'), user)
                if user is None:
                    log.warning('memory capture skipped: original input unavailable')
                    return
            spool.capture(session_id=kwargs.get('session_id'),
                          turn_id=kwargs.get('turn_id') or kwargs.get('task_id'),
                          user_message=user,
                          assistant_reply=text(kwargs.get('assistant_response')))
        except Exception as exc:
            log.warning('memory capture failed: %s', type(exc).__name__)

    def verdict(**kwargs):
        try:
            spool.finalize(session_id=kwargs.get('session_id'),
                           turn_id=kwargs.get('turn_id') or kwargs.get('task_id'),
                           completed=kwargs.get('completed') is True,
                           failed=kwargs.get('failed', False),
                           interrupted=kwargs.get('interrupted', False))
        except Exception as exc:
            log.warning('memory terminal verdict failed: %s', type(exc).__name__)

    ctx.register_hook('post_llm_call', capture)
    ctx.register_hook('on_session_end', verdict)


def _register_progressive_tools(ctx, engine: Path) -> None:
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
    if (not isinstance(data, str) or not Path(data).is_absolute()
            or not all(isinstance(x, str) and x.strip() for x in (user, agent, profile))
            or not isinstance(endpoint, str) or not script.is_file()):
        log.warning("progressive tools unavailable: scoped memory settings missing")
        return
    private_python = engine / ".superagent-venv/bin/python"
    python = str(private_python) if private_python.is_file() else sys.executable
    command = [python, str(script), "--data-dir", data, "--user", user, "--agent", agent,
               "--profile", profile, "--embed-url", endpoint]
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
            if not isinstance(args, dict):
                return json.dumps({"ok": False, "error": "invalid_arguments"})
            try:
                request = json.dumps({"operation": operation, "arguments": args}, ensure_ascii=False)
                if len(request.encode("utf-8")) > 32768:
                    raise ValueError("request too large")
                proc = subprocess.run(command, input=request, text=True, capture_output=True, timeout=20)
                if proc.returncode or len(proc.stdout.encode("utf-8")) > 131072:
                    raise RuntimeError("memory subprocess failed")
                result = json.loads(proc.stdout)
                if not isinstance(result, dict):
                    raise ValueError("invalid memory result")
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


# Patch register() to also expose progressive tools when enabled.
_orig_register = register


def register(ctx):
    _orig_register(ctx)
    if ctx.get_config("enabled", False) is not True:
        return
    engine = Path(ctx.get_config("engine_root", ""))
    if engine.is_absolute():
        _register_progressive_tools(ctx, engine)

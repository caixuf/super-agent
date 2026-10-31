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
    """Optional L1/L2/L3 tool ops. Prefetch default stays L1 via runtime_bridge."""
    register = getattr(ctx, "register_tool", None)
    if not callable(register):
        return
    import importlib.util

    def _load(name: str, rel: str):
        spec = importlib.util.spec_from_file_location(name, engine / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    try:
        progressive = _load("_agentcore_progressive", "agentcore/memory/progressive.py")
    except Exception as exc:
        log.warning("progressive tools unavailable: %s", type(exc).__name__)
        return

    def memory_search_l1(**kwargs):
        return progressive.search(
            str(kwargs.get("query") or ""),
            limit=int(kwargs.get("limit") or progressive.PREFETCH_L1_LIMIT),
            ebm=kwargs.get("ebm"),
        )

    def memory_timeline_l2(**kwargs):
        ids = kwargs.get("ids") or []
        if isinstance(ids, str):
            ids = [x.strip() for x in ids.split(",") if x.strip()]
        return progressive.timeline(
            ids or None,
            query=kwargs.get("query"),
            window=int(kwargs.get("window") or progressive.PREFETCH_L1_LIMIT),
            ebm=kwargs.get("ebm"),
        )

    def memory_get_observations_l3(**kwargs):
        ids = kwargs.get("ids") or []
        if isinstance(ids, str):
            ids = [x.strip() for x in ids.split(",") if x.strip()]
        return progressive.get_observations(ids)

    for name, fn, desc in (
        ("memory_search_l1", memory_search_l1,
         "L1 memory index search: fact_id/subject/kind/class/cost — no full body."),
        ("memory_timeline_l2", memory_timeline_l2,
         "L2 chronological stubs for ids or query window — body-light."),
        ("memory_get_observations_l3", memory_get_observations_l3,
         "L3 full statement+tags+details for explicit fact_ids only."),
    ):
        try:
            register(name=name, toolset="memory_bridge", description=desc, handler=fn)
        except TypeError:
            try:
                register(name=name, toolset="memory_bridge", description=desc, fn=fn)
            except Exception as exc:
                log.warning("register %s failed: %s", name, type(exc).__name__)
        except Exception as exc:
            log.warning("register %s failed: %s", name, type(exc).__name__)


# Patch register() to also expose progressive tools when enabled.
_orig_register = register


def register(ctx):
    _orig_register(ctx)
    if ctx.get_config("enabled", False) is not True:
        return
    engine = Path(ctx.get_config("engine_root", ""))
    if engine.is_absolute():
        _register_progressive_tools(ctx, engine)

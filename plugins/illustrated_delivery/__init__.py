"""Per-turn illustrated-delivery advice through the public Hermes hook API."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _engine(ctx):
    try:
        from agentcore.document_intent import advise_delivery, delivery_context
        return advise_delivery, delivery_context
    except ModuleNotFoundError as error:
        if error.name not in ('agentcore', 'agentcore.document_intent'):
            raise
    # Installed Hermes runtimes can be isolated from the source checkout. The
    # administrator points at the single canonical engine; do not vendor a copy
    # or add task-controlled paths to the import search path.
    root = ctx.get_config('engine_root', '')
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError('engine_root must identify the administrator-installed source checkout')
    source = Path(root).resolve(strict=True) / 'agentcore/document_intent.py'
    spec = importlib.util.spec_from_file_location('illustrated_delivery_engine', source)
    if spec is None or spec.loader is None:
        raise ImportError('document intent engine unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.advise_delivery, module.delivery_context


def _location_hint(ctx):
    root = ctx.get_config('engine_root', '')
    if not isinstance(root, str) or not Path(root).is_absolute():
        return ''
    try:
        root = Path(root).resolve(strict=True)
    except OSError:
        return ''
    renderer = root / 'tools/arch_doc.py'
    skill = root / 'skills/illustrated-doc-rendering/SKILL.md'
    if not renderer.is_file() or not skill.is_file():
        return ''
    return (' Canonical renderer file: ' + str(renderer) + '. Canonical skill file: ' + str(skill) +
            '. These are administrator-configured locations, not shell commands. '
            'Keep internal installation paths out of the delivered artifact.')


def register(ctx):
    if ctx.get_config('enabled', False) is not True:
        return
    advise_delivery, delivery_context = _engine(ctx)
    location_hint = _location_hint(ctx)

    def advise(*, user_message='', conversation_history=None, parent_session_id='', **kwargs):
        # Internal delegated calls should not recursively turn every tool task into
        # another document. The top-level caller owns the final deliverable.
        if parent_session_id and ctx.get_config('include_delegated', False) is not True:
            return None
        if not isinstance(user_message, str):
            return {'context': delivery_context(advise_delivery('')) + location_hint}
        decision = advise_delivery(user_message, recent_messages=conversation_history)
        return {'context': delivery_context(decision) + location_hint}

    ctx.register_hook('pre_llm_call', advise)

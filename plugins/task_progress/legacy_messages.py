"""Narrow existing-session transport for Hermes hosts without plugin_messages.

No model injection, session creation, cross-profile routing or edit fallback.
The host still owns identity canonicalization, authorization and adapter choice.
"""
from pathlib import Path
from types import SimpleNamespace


def resolve(runner, *, session_key, route, plugin_home, plugin_id):
    if route is not None or not session_key:
        return None, {'error': 'no_conversation'}
    try:
        if not runner._running or runner._draining:
            return None, {'error': 'gateway_unavailable'}
        profile = runner._served_profile_for_home(Path(plugin_home))
        if profile is None:
            return None, {'error': 'profile_not_served'}
        entry = runner.session_store.lookup_by_session_key(session_key)
        if entry is None or entry.origin is None:
            return None, {'error': 'no_conversation'}
        source = runner._restored_source(entry)
        identity = runner._canonicalize(source)
        if identity is None or identity.runtime_profile != profile:
            return None, {'error': 'profile_mismatch'}
        if not runner._plugin_injection_authorized(source, plugin_id=plugin_id,
                                                   target='task-progress'):
            return None, {'error': 'unauthorized'}
        adapter = runner._delivery_adapter_for(source)
        platform = getattr(source.platform, 'value', source.platform)
        if platform not in ('feishu', 'lark') or adapter is None or not adapter.is_connected:
            return None, {'error': 'adapter_unavailable'}
        return SimpleNamespace(
            runner=runner, source=source, adapter=adapter, platform=platform,
            chat_id=source.chat_id, live=None, session_key=session_key,
            plugin_home=Path(plugin_home), plugin_id=plugin_id,
        ), None
    except Exception:
        # Missing/changed host methods must never widen the destination scope.
        return None, {'error': 'host_transport_unsupported'}


def _supports_edit(adapter):
    return callable(getattr(adapter, 'edit_message', None))


def _fit(adapter, chat_id, text):
    text = adapter.format_message(text)
    chunks = adapter.truncate_message(text, adapter.MAX_MESSAGE_LENGTH)
    return chunks[0] if chunks else ''


def _publish_local(target, text):
    return {'ok': False, 'error': 'local_transport_unsupported'}


async def post(target, text, *, plugin_id):
    if not isinstance(text, str) or not text.strip():
        return {'ok': False, 'error': 'empty_message'}
    # Recheck mutable authorization/profile/adapter state on the gateway loop.
    current, error = resolve(target.runner, session_key=target.session_key, route=None,
                             plugin_home=target.plugin_home, plugin_id=plugin_id)
    if current is None:
        return {'ok': False, **(error or {'error': 'no_conversation'})}
    content = _fit(current.adapter, current.chat_id, text)
    if not content.strip():
        return {'ok': False, 'error': 'empty_message'}
    metadata = dict(current.runner._thread_metadata_for_source(current.source) or {})
    metadata.update(expect_edits=True, hermes_plugin_id=plugin_id)
    async with current.runner._plugin_injection_scope(current.source):
        sent = await current.adapter.send(current.chat_id, content, metadata=metadata)
    if not getattr(sent, 'success', False):
        return {'ok': False, 'error': 'send_outcome_unknown'}
    message_id = str(getattr(sent, 'message_id', '') or '')
    if not message_id:
        return {'ok': False, 'error': 'missing_message_id'}
    return {'ok': True, 'message_id': message_id}

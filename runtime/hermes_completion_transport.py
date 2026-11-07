"""Administrator-opted direct completion transport; never injects model turns.

The host resolves stored routes and checks profile ownership. This bridge uses
its canonical adapters, not a second platform client or copied credentials.
"""
import asyncio
import concurrent.futures
import importlib.util
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _finalize_card(bridge, task_id, message):
    """Report card failures without retrying an already accepted completion."""
    try:
        result = bridge.finalize(
            task_id, status=message.get('status', 'completed'),
            summary=message.get('summary', ''), artifacts=message.get('artifacts'),
            elapsed=message.get('elapsed', 0.0))
    except Exception as exc:
        logger.warning('task-card finalization failed for %s (%s)', task_id, type(exc).__name__)
        return
    patch = result.get('patch_result') if isinstance(result, dict) else None
    if isinstance(result, dict) and result.get('error') == 'task_not_found':
        # Many completions have no task-card binding; this is normal until a
        # host explicitly creates one and should not flood the operator log.
        logger.debug('no task-card binding for %s', task_id)
        return result
    if isinstance(result, dict) and result.get('ok') is True and result.get('already_finalized') is True:
        return result
    if (not isinstance(result, dict) or result.get('ok') is not True
            or not isinstance(patch, dict) or patch.get('ok') is not True):
        logger.warning('task-card finalization not confirmed for %s', task_id)
    return result


def _transport_type(engine):
    spec = importlib.util.spec_from_file_location('_direct_completion_transport',
                                                engine/'agentcore/direct_completion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DirectCompletionTransport


def _origin_matches(address, target):
    # A missing stored thread means the original address was not bound to a
    # thread (or legacy provenance is incomplete). Never silently add one from
    # a later session resolution; it changes the destination of the message.
    return (not address.get('channel') or address['channel'] == target.platform) and (
        not address.get('chat') or str(address['chat']) == target.chat_id) and (
        (address.get('thread') or '') == ((target.metadata or {}).get('thread_id') or ''))


class HostSender:
    def __init__(self, ctx):
        self.ctx = ctx

    def create_card(self, address, card_payload, delivery_key):
        """Create a task card through the resolved host adapter, with a stable UUID."""
        from hermes_cli.plugin_messages import _gateway_loop
        import json
        runner, loop = _gateway_loop()
        if runner is None or loop is None or loop.is_closed():
            return dict(ok=False, not_sent=True, error='gateway_unavailable')
        try:
            if asyncio.get_running_loop() is loop:
                return dict(ok=False, not_sent=True, error='blocking_on_gateway_loop')
        except RuntimeError:
            pass

        async def _do_create():
            from gateway import plugin_messages
            if (not isinstance(address, dict) or address.get('profile') != getattr(self.ctx, 'profile_name', None)
                    or not address.get('route') or not address.get('channel') or not address.get('chat')
                    or not isinstance(delivery_key, str) or len(delivery_key) != 64
                    or any(c not in '0123456789abcdef' for c in delivery_key)
                    or not isinstance(card_payload, dict) or card_payload.get('schema') != '2.0'):
                return dict(ok=False, not_sent=True, error='invalid_card_create_request')
            try:
                content = json.dumps(card_payload, ensure_ascii=False)
                if len(content.encode('utf-8')) > 30000:
                    return dict(ok=False, not_sent=True, error='card_payload_too_large')
                target, refusal = plugin_messages.resolve(runner, session_key=address['route'], route=None,
                    plugin_home=self.ctx._manager.home_path, plugin_id=self.ctx.plugin_id)
                if target is None or target.adapter is None:
                    return dict(ok=False, not_sent=True, error=(refusal or {}).get('error', 'no_conversation'))
                if not _origin_matches(address, target):
                    return dict(ok=False, not_sent=True, error='stored_origin_mismatch')
                adapter = target.adapter
                if target.live is not None or target.platform != 'feishu':
                    return dict(ok=False, not_sent=True, error='card_create_unsupported')
                if (not getattr(adapter, 'DURABLE_DELIVERY_UUID', False)
                        or not callable(getattr(adapter, '_send_raw_message', None))
                        or not getattr(adapter, '_client', None)
                        or not getattr(adapter, '_app_id', None)):
                    return dict(ok=False, not_sent=True, error='card_adapter_unavailable')
                metadata = {**(target.metadata or {}), '_interim_send': True,
                            '_delivery_uuid': delivery_key[:32]}
            except Exception as exc:
                return dict(ok=False, not_sent=True, error='card_preflight_failed',
                            error_type=type(exc).__name__)
            try:
                response = await adapter._send_raw_message(chat_id=target.chat_id, msg_type='interactive',
                                                           payload=content, reply_to=None, metadata=metadata)
                if adapter._response_succeeded(response):
                    message_id = adapter._extract_response_field(response, 'message_id')
                    if isinstance(message_id, str) and message_id.strip():
                        return dict(ok=True, message_id=message_id)
            except Exception:
                pass
            return dict(ok=False, error='card_create_outcome_unknown')

        coroutine = _do_create()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return dict(ok=False, not_sent=True, error='gateway_unavailable')
        try:
            return future.result(timeout=30)
        except Exception as exc:
            return dict(ok=False, error='card_create_outcome_unknown', error_type=type(exc).__name__)

    async def send(self, runner, address, item):
        from gateway import plugin_messages
        target, refusal = plugin_messages.resolve(runner, session_key=address['route'], route=None,
            plugin_home=self.ctx._manager.home_path, plugin_id=self.ctx.plugin_id)
        if target is None:
            return dict(ok=False, not_sent=True, error=(refusal or {}).get('error', 'no_conversation'))
        if not _origin_matches(address, target):
            return dict(ok=False, not_sent=True, error='stored_origin_mismatch')
        if target.live is not None:
            # A local viewer publication is not proof of messaging-platform delivery.
            return dict(ok=False, not_sent=True, error='messaging_transport_required')
        if target.platform == 'feishu' and not getattr(target.adapter, 'DURABLE_DELIVERY_UUID', False):
            return dict(ok=False, not_sent=True, error='stable_delivery_uuid_patch_required')
        metadata = {**(target.metadata or {}), '_interim_send': True,
                    '_delivery_uuid': item['delivery_uuid']}
        if item['kind'] == 'text':
            # Completion summaries are bounded by the queue consumer. Refuse
            # splitting, which would need one idempotency key per chunk.
            text = item['text']
            if item.get('card'):
                # Administrator opted into a Card 2.0 report. Use the host's own
                # card renderer; a missing seam is a refusal, never a silent text fallback.
                send_card = getattr(target.adapter, '_send_reply_card', None)
                if send_card is None:
                    return dict(ok=False, not_sent=True, error='card_adapter_unavailable')
                result = await send_card(chat_id=target.chat_id, content=text,
                                         reply_to=None, metadata=metadata, finalize=True)
                if getattr(result, 'success', False) and getattr(result, 'message_id', None):
                    return dict(ok=True, message_id=str(result.message_id))
                return dict(ok=False, error='card_send_failed')
            if plugin_messages._fit(target.adapter, target.chat_id, text) != text:
                return dict(ok=False, not_sent=True, error='completion_text_budget')
            result = await target.adapter.send(target.chat_id, text, metadata=metadata)
        else:
            from gateway.platforms.base import BasePlatformAdapter
            if getattr(type(target.adapter), 'send_document', None) is BasePlatformAdapter.send_document:
                return dict(ok=False, not_sent=True, error='native_documents_unsupported')
            result = await target.adapter.send_document(target.chat_id, item['path'],
                                                        file_name=item['name'], metadata=metadata)
        if getattr(result, 'success', False) and getattr(result, 'message_id', None):
            return dict(ok=True, message_id=str(result.message_id))
        return dict(ok=False, error='adapter_result_unknown')

    def patch_card(self, address, message_id, card_payload):
        from hermes_cli.plugin_messages import _gateway_loop
        import json
        runner, loop = _gateway_loop()
        if runner is None or loop is None or loop.is_closed():
            return dict(ok=False, not_sent=True, error='gateway_unavailable')
        try:
            if asyncio.get_running_loop() is loop:
                return dict(ok=False, not_sent=True, error='blocking_on_gateway_loop')
        except RuntimeError:
            pass
        async def _do_patch():
            from gateway import plugin_messages
            target, refusal = plugin_messages.resolve(runner, session_key=address['route'], route=None,
                plugin_home=self.ctx._manager.home_path, plugin_id=self.ctx.plugin_id)
            if target is None or target.adapter is None:
                return dict(ok=False, not_sent=True, error=(refusal or {}).get('error', 'no_conversation'))
            if not _origin_matches(address, target):
                return dict(ok=False, not_sent=True, error='stored_origin_mismatch')
            if target.live is not None:
                return dict(ok=False, not_sent=True, error='messaging_transport_required')
            adapter = target.adapter
            if (target.platform != 'feishu' or not isinstance(message_id, str) or not message_id
                    or not isinstance(card_payload, dict) or card_payload.get('schema') != '2.0'):
                return dict(ok=False, not_sent=True, error='card_patch_unsupported')
            try:
                content = json.dumps(card_payload, ensure_ascii=False)
                if len(content.encode('utf-8')) > 30000:
                    return dict(ok=False, not_sent=True, error='card_payload_too_large')
                client = adapter._client
                app_id = adapter._app_id
                if not client or not isinstance(app_id, str) or not app_id:
                    return dict(ok=False, not_sent=True, error='card_adapter_unavailable')
                # Resolve the message itself before changing it; a valid route alone
                # does not prove that a stored message ID belongs to this chat/bot.
                request = adapter._build_get_message_request(message_id)
                response = await adapter._run_blocking(client.im.v1.message.get, request)
                if not adapter._response_succeeded(response):
                    return dict(ok=False, not_sent=True, error='card_ownership_unverified')
                items = getattr(getattr(response, 'data', None), 'items', None) or []
                message = items[0] if len(items) == 1 else None
                sender = getattr(message, 'sender', None)
                if (message is None or str(getattr(message, 'message_id', '') or '') != message_id
                        or str(getattr(message, 'chat_id', '') or '') != target.chat_id
                        or str(getattr(message, 'msg_type', '') or '') != 'interactive'
                        or str(getattr(sender, 'id', '') or '') != app_id
                        or getattr(message, 'deleted', False) is True):
                    return dict(ok=False, not_sent=True, error='card_ownership_unverified')
                body = adapter._build_patch_message_body(content=content)
                request = adapter._build_patch_message_request(message_id=message_id, request_body=body)
            except Exception as exc:
                return dict(ok=False, not_sent=True, error='card_preflight_failed',
                            error_type=type(exc).__name__)
            try:
                # Call the platform PATCH seam directly: generic edit_message can
                # render the JSON as text or fall back to sending a new message.
                response = await adapter._run_blocking(client.im.v1.message.patch, request)
                if adapter._response_succeeded(response):
                    return dict(ok=True, message_id=message_id)
            except Exception:
                pass
            return dict(ok=False, error='card_patch_outcome_unknown')
        coroutine = _do_patch()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return dict(ok=False, not_sent=True, error='gateway_unavailable')
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return dict(ok=False, error='card_patch_outcome_unknown', error_type=type(exc).__name__)

    def __call__(self, address, item, *, idempotency_key):
        from hermes_cli.plugin_messages import _gateway_loop
        runner, loop = _gateway_loop()
        if runner is None or loop is None or loop.is_closed():
            return dict(ok=False, not_sent=True, error='gateway_unavailable')
        try:
            if asyncio.get_running_loop() is loop:
                return dict(ok=False, not_sent=True, error='blocking_on_gateway_loop')
        except RuntimeError:
            pass
        operation = dict(item, delivery_uuid=idempotency_key[:32])
        coroutine = self.send(runner, address, operation)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return dict(ok=False, not_sent=True, error='gateway_unavailable')
        try:
            return future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            # It may still send. The durable journal fences this operation, so
            # neither the next poll nor a restart will send it again blindly.
            return None


def _render(message):
    from agentcore.delivery_sanitizer import sanitize_and_extract_artifacts
    role = message.get('colleague_role')
    name = message.get('colleague_name')
    label = message.get('colleague_label')
    if role == 'analysis':
        prefix = '**主 Bot 汇总**\n'
    elif isinstance(role, str) and role and isinstance(name, str) and name:
        prefix = '**%s · %s**（由主 Bot 转交）\n' % (
            name, label if isinstance(label, str) and label else role)
    else:
        prefix = ''
    status = {'completed': '已完成', 'failed': '失败', 'cancelled': '已取消'}.get(
        message['status'], message['status'])
    summary = message.get('summary', '')
    sanitized_summary, _ = sanitize_and_extract_artifacts(summary)
    return prefix + '[后台任务 ' + message['task_id'] + '] ' + status + '\n' + sanitized_summary


def create_transport(ctx, service):
    if ctx.get_config('direct_delivery_enabled', False) is not True:
        raise ValueError('direct delivery requires administrator opt-in')
    engine = Path(ctx.get_config('engine_root', ''))
    if not engine.is_absolute():
        raise ValueError('absolute engine root required')
    base_type = _transport_type(engine)

    class _HumanCompletionTransport(base_type):
        def _prepare_message(self, message):
            from agentcore.delivery_sanitizer import sanitize_and_extract_artifacts
            summary = message.get('summary', '')
            attempt = self.attempt_provider(message.get('task_id')) if callable(self.attempt_provider) else None
            # Restrict discovery to the owning attempt before the durable transport
            # snapshots and sends files. Never copy paths supplied in model prose.
            allowed_dir = None
            if (isinstance(attempt, str) and len(attempt) == 32
                    and all(c in '0123456789abcdef' for c in attempt)):
                allowed_dir = self.attempt_root / attempt
            sanitized_summary, detected = sanitize_and_extract_artifacts(
                summary, base_dir=allowed_dir, allow_nonexistent=True)
            message = dict(message, summary=sanitized_summary)

            files = list(message.get('artifacts') or [])
            for art in detected:
                if art['path'] not in files and len(files) < 8:
                    files.append(art['path'])
            message = dict(message, artifacts=files)

            if isinstance(files, list):
                excluded = {'manifest.json', 'report.json'}
                if self.card and not message.get('truncated'):
                    excluded.add('report.md')
                filtered = [p for p in files if Path(str(p)).name not in excluded]
                if filtered != files:
                    message = dict(message, artifacts=filtered)
            return message

        def reconcile_card(self, task_id):
            # A second gateway may have delivered completion before this worker
            # created the card. Re-read the durable terminal receipt so a fast
            # task cannot leave a newly created card permanently queued.
            from agentcore.completion_delivery import build_message
            receipt = service.pool.outbox_receipt(task_id)
            if receipt is not None:
                message = self._prepare_message(build_message(receipt).render())
                return {'terminal': True, 'result': _finalize_card(self.card_bridge, task_id, message)}
            return {'terminal': False}

        def _record_card_finalization(self, task_id, final):
            """Keep failed terminal patches visible after the text receipt is acknowledged."""
            if isinstance(final, dict) and final.get('error') == 'task_not_found':
                return
            patch = final.get('patch_result') if isinstance(final, dict) else None
            if (isinstance(final, dict) and final.get('ok') is True
                    and (final.get('already_finalized') is True
                         or isinstance(patch, dict) and patch.get('ok') is True)):
                return
            if isinstance(final, dict) and final.get('ok') is True and patch is None:
                # A bound task may finish before its initial card is created.
                # The durable create consumer will render the stored terminal state.
                card = self.card_bridge.get_card(task_id)
                if card is not None and not card.get('message_id'):
                    return
            unsent = isinstance(patch, dict) and patch.get('not_sent') is True
            state = 'pending' if unsent else 'unknown'
            error = 'card_patch_not_sent' if unsent else 'card_patch_unknown'
            service.pool.record_card_finalization_state(task_id, state, error)

        def deliver(self, address, message, *, idempotency_key):
            message = self._prepare_message(message)
            result = super().deliver(address, message, idempotency_key=idempotency_key)
            if result and result.get('accepted') and getattr(self, 'card_bridge', None):
                final = _finalize_card(self.card_bridge, idempotency_key, message)
                try:
                    self._record_card_finalization(idempotency_key, final)
                except Exception as exc:
                    logger.warning('card terminal health update failed for %s (%s)',
                                   idempotency_key, type(exc).__name__)
            return result

    sender = HostSender(ctx)
    card_bridge = None
    try:
        from agentcore.task_card_bridge import TaskCardBridge
        card_bridge = TaskCardBridge(service.pool.root.parent / 'cards',
                                     profile=service.profile,
                                     sender=lambda address, message_id, card_payload: sender.patch_card(address, message_id, card_payload))
    except Exception:
        pass

    transport = _HumanCompletionTransport(service.pool.root/'direct-delivery', profile=service.profile,
        sender=sender, render=_render,
        attempt_root=service.pool.root/'attempts',
        attempt_provider=lambda job: service.pool.status(job).get('attempt'),
        card=ctx.get_config('completion_card_enabled', False) is True)
    transport.card_bridge = card_bridge
    transport.card_creator = sender.create_card
    return transport

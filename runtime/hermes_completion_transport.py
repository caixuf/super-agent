"""Administrator-opted direct completion transport; never injects model turns.

The host resolves stored routes and checks profile ownership. This bridge uses
its canonical adapters, not a second platform client or copied credentials.
"""
import asyncio
import concurrent.futures
import importlib.util
from pathlib import Path


def _transport_type(engine):
    spec = importlib.util.spec_from_file_location('_direct_completion_transport',
                                                engine/'agentcore/direct_completion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DirectCompletionTransport


class HostSender:
    def __init__(self, ctx):
        self.ctx = ctx

    async def send(self, runner, address, item):
        from gateway import plugin_messages
        target, refusal = plugin_messages.resolve(runner, session_key=address['route'], route=None,
            plugin_home=self.ctx._manager.home_path, plugin_id=self.ctx.plugin_id)
        if target is None:
            return dict(ok=False, not_sent=True, error=(refusal or {}).get('error', 'no_conversation'))
        if ((address.get('channel') and address['channel'] != target.platform)
                or (address.get('chat') and str(address['chat']) != target.chat_id)
                or (address.get('thread') and address['thread'] != (target.metadata or {}).get('thread_id'))):
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

    def __call__(self, address, item, *, idempotency_key):
        from hermes_cli.plugin_messages import _gateway_loop
        runner, loop = _gateway_loop()
        if runner is None or loop is None:
            return dict(ok=False, not_sent=True, error='gateway_unavailable')
        try:
            if asyncio.get_running_loop() is loop:
                return dict(ok=False, not_sent=True, error='blocking_on_gateway_loop')
        except RuntimeError:
            pass
        operation = dict(item, delivery_uuid=idempotency_key[:32])
        future = asyncio.run_coroutine_threadsafe(self.send(runner, address, operation), loop)
        try:
            return future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            # It may still send. The durable journal fences this operation, so
            # neither the next poll nor a restart will send it again blindly.
            return None


def _render(message):
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
    return prefix + '[后台任务 ' + message['task_id'] + '] ' + status + '\n' + message['summary']


def create_transport(ctx, service):
    if ctx.get_config('direct_delivery_enabled', False) is not True:
        raise ValueError('direct delivery requires administrator opt-in')
    engine = Path(ctx.get_config('engine_root', ''))
    if not engine.is_absolute():
        raise ValueError('absolute engine root required')
    transport_type = _transport_type(engine)
    return transport_type(service.pool.root/'direct-delivery', profile=service.profile,
        sender=HostSender(ctx), render=_render,
        attempt_root=service.pool.root/'attempts',
        attempt_provider=lambda job: service.pool.status(job).get('attempt'))

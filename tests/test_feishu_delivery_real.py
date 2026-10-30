"""The read-only delivery seam driven by the real Feishu channel.

The adapter decides what to send and when; this exercises that decision path
against the live platform instead of a stand-in transport. Needs the same
credentials and a bot-owned test chat as ``test_feishu_transport_real.py``.
"""
import os
import uuid

import pytest

from agentcore.colleague_readonly_delivery import ReadOnlyDelivery
from agentcore.feishu_transport import FeishuTransport
from test_colleague_readonly_queue import env as queue_env  # noqa: F401  (pytest fixture)

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
CHAT = os.environ.get("FEISHU_TEST_CHAT_ID")

pytestmark = pytest.mark.skipif(
    not (APP_ID and APP_SECRET and CHAT),
    reason="set FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_TEST_CHAT_ID")


@pytest.fixture
def env(queue_env):  # noqa: F811
    return queue_env


def test_delivery_is_settled_by_a_real_platform_receipt(env, tmp_path):
    queue, runtime, reg, leader, host, args, calls = env
    origin = {**args['origin'], 'channel': 'feishu', 'chat': CHAT}
    # Task id comes from profile/session/request id, so the request must be new per
    # run: the platform deduplicates a repeated uuid within its window and would
    # answer the earlier, already-recalled message instead of creating one.
    job = queue.submit(**{**args, 'origin': origin,
                          'request_id': 'request-' + uuid.uuid4().hex})['job_id']
    assert queue.run_once()['job_state'] == 'completed'
    transport = FeishuTransport(app_id=APP_ID, app_secret=APP_SECRET, journal=tmp_path / 'sends.db')
    sender = ReadOnlyDelivery(queue, owner=args['owner'], transport=transport,
                              literal_text_only=True, no_implicit_retries=True)
    # The adapter turns a send failure into ``unknown``; keep the cause for the assert.
    failures, send = [], transport.send_plaintext

    def recorded(**kwargs):
        try:
            return send(**kwargs)
        except Exception as error:  # noqa: BLE001 - re-raised, only kept for the message
            failures.append(repr(error))
            raise
    transport.send_plaintext = recorded
    view = queue.inspect_completion(owner=args['owner'], job_id=job)
    delivery_key = 'readonly-final:' + view['presentation']['task_id']
    try:
        assert sender.deliver(job)['state'] == 'delivered', failures
        record = queue.pool.delivery_record(job)
        assert record['acknowledged'] == 1
        # The acknowledged receipt is a platform message, and the platform still holds it.
        found = transport.lookup(origin=origin, delivery_key=delivery_key)
        assert found is not None and found['receipt_id'] == record['receipt_id']
        assert reg.work_status(supervisor=host, member_id='alice', generation=1)['tracked_open'] == 0
        # A second deliver call must not send again.
        assert sender.deliver(job)['state'] == 'delivered', failures
    finally:
        transport.recall(origin=origin, delivery_key=delivery_key)


def test_durable_completion_is_accepted_only_after_a_real_platform_receipt(tmp_path):
    from agentcore.direct_completion import DirectCompletionTransport
    from agentcore.feishu_transport import FeishuCompletionSender

    transport = FeishuTransport(app_id=APP_ID, app_secret=APP_SECRET, journal=tmp_path / 'sends.db')
    calls = []

    def sender(address, item, *, idempotency_key):
        calls.append(idempotency_key)
        return FeishuCompletionSender(transport)(address, item, idempotency_key=idempotency_key)

    address = {'profile': 'team', 'session': 'owner', 'route': 'gateway',
               'channel': 'feishu', 'chat': CHAT}
    task = 'task-' + uuid.uuid4().hex
    completion = DirectCompletionTransport(
        tmp_path / 'delivery', profile='team', sender=sender,
        render=lambda message: message['text'],
        attempt_root=tmp_path / 'attempts', attempt_provider=lambda key: None)
    try:
        outcome = completion.deliver(address, {'task_id': task, 'text': '后台任务已完成：real'},
                                     idempotency_key=task)
        assert outcome['accepted'] is True and outcome['event_id'] == task
        # At-most-once survives a repeated drain: the sender is not called again.
        again = completion.deliver(address, {'task_id': task, 'text': '后台任务已完成：real'},
                                   idempotency_key=task)
        assert again['accepted'] is True and len(calls) == 1
        key = completion.inspect()[0]['key']
        found = transport.lookup(origin=address, delivery_key=key)
        assert found is not None and found['receipt_id'].startswith('om_')
        assert outcome['receipt_id'].startswith('direct:')
    finally:
        for row in completion.inspect():
            transport.recall(origin=address, delivery_key=row['key'])

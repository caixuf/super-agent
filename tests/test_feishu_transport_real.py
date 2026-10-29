"""Real Feishu delivery. Set the app credentials and a dedicated test chat.

Requires ``FEISHU_APP_ID``, ``FEISHU_APP_SECRET`` and ``FEISHU_TEST_CHAT_ID``:
a chat the bot owns and no other member reads, so the self test is real without
interrupting anyone. Skipped when they are absent -- a skip is not a pass.

This is the delivery step of the colleague pipeline exercised for real: the
message leaves the host, the platform accepts it, the transport reads it back
and compares the chat and text digest, and the message is then recalled so the
next run starts from an empty chat.
"""
import os
import uuid

import pytest

from agentcore.feishu_transport import FeishuError, FeishuTransport

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
CHAT = os.environ.get("FEISHU_TEST_CHAT_ID")

pytestmark = pytest.mark.skipif(
    not (APP_ID and APP_SECRET and CHAT),
    reason="set FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_TEST_CHAT_ID")


@pytest.fixture
def transport(tmp_path):
    return FeishuTransport(app_id=APP_ID, app_secret=APP_SECRET, journal=tmp_path / "sends.db")


def _origin():
    return {"profile": "team", "session": "self-test", "route": "gateway",
            "channel": "feishu", "chat": CHAT}


def test_real_send_is_confirmed_by_read_back_and_can_be_recalled(transport):
    key = "self-test-" + uuid.uuid4().hex
    text = "colleague delivery self test " + uuid.uuid4().hex
    receipt = transport.send_plaintext(origin=_origin(), delivery_key=key,
                                      payload={"member_id": "reviewer", "generation": 1, "text": text})
    try:
        assert receipt["receipt_id"].startswith("om_")
        # An independent platform lookup must agree without sending anything.
        found = transport.lookup(origin=_origin(), delivery_key=key)
        assert found is not None and found["receipt_id"] == receipt["receipt_id"]
        # Re-sending the same key must not create a second message.
        again = transport.send_plaintext(origin=_origin(), delivery_key=key,
                                        payload={"member_id": "reviewer", "generation": 1, "text": text})
        assert again["receipt_id"] == receipt["receipt_id"]
    finally:
        transport.recall(origin=_origin(), delivery_key=key)
    assert transport.lookup(origin=_origin(), delivery_key=key) is None


def test_real_lookup_never_invents_a_receipt_for_an_unknown_key(transport):
    assert transport.lookup(origin=_origin(), delivery_key="never-sent-" + uuid.uuid4().hex) is None


def test_real_send_to_a_bad_chat_fails_closed(transport):
    bad = dict(_origin(), chat="oc_" + "0" * 32)
    with pytest.raises(FeishuError):
        transport.send_plaintext(origin=bad, delivery_key="bad-" + uuid.uuid4().hex,
                                 payload={"text": "must not arrive"})

"""Offline checks of the Feishu transport's sequencing, not of Feishu itself.

The opener is a stand-in server so every branch that must never depend on the
network -- token reuse, receipt verification, at-most-once per key, conflict
refusal, recall -- is reachable. The real platform is exercised separately in
``test_feishu_transport_real.py``.
"""
import io
import json

import pytest

from agentcore.feishu_transport import FeishuError, FeishuTransport

APP_ID = "cli_test_app"
APP_SECRET = "app-secret-that-must-never-leak"
CHAT = "oc_49c195f21a3979c53edeba1c33e34ed5"


class Server:
    """Minimal Feishu stand-in: one bot, one chat, message read-back."""

    def __init__(self):
        self.tokens = 0
        self.sent = []
        self.messages = {}
        self.deleted = set()
        self.override_chat = None
        self.override_text = None
        self.fail_status = None
        self.code = 0
        self.next_id = 0

    def handle(self, request):
        method = request.get_method()
        if method == "DELETE":
            self.deleted.add(request.full_url.rsplit("/", 1)[-1])
            return {"code": 0, "data": {}}
        if method == "GET":
            message_id = request.full_url.rsplit("/", 1)[-1].split("?")[0]
            if message_id in self.deleted or message_id not in self.messages:
                return {"code": 230001, "msg": "message not found"}
            return {"code": 0, "data": {"items": [self.messages[message_id]]}}
        body = json.loads(request.data.decode())
        if request.full_url.endswith("/tenant_access_token/internal"):
            self.tokens += 1
            return {"code": 0, "tenant_access_token": "t-%d" % self.tokens, "expire": 7200}
        assert "/im/v1/messages" in request.full_url
        self.next_id += 1
        message_id = "om_%d" % self.next_id
        text = self.override_text if self.override_text is not None \
            else json.loads(body["content"])["text"]
        chat = self.override_chat or body["receive_id"]
        self.sent.append(body)
        self.messages[message_id] = {"message_id": message_id, "chat_id": chat, "deleted": False,
                                     "body": {"content": json.dumps({"text": text}, ensure_ascii=False)}}
        return {"code": 0, "data": {"message_id": message_id}}


class Opener:
    def __init__(self, server):
        self.server = server

    def open(self, request, timeout=None):
        if self.server.fail_status:
            raise _http_error(request.full_url, self.server.fail_status)
        payload = self.server.handle(request)
        return _Response(json.dumps(payload).encode())


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _http_error(url, status):
    import urllib.error
    return urllib.error.HTTPError(url, status, "error", {}, None)


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def transport(server, tmp_path):
    return FeishuTransport(app_id=APP_ID, app_secret=APP_SECRET,
                           journal=tmp_path / "state" / "sends.db", opener=Opener(server))


def _origin(chat=CHAT):
    return {"profile": "team", "session": "owner-session", "route": "gateway",
            "channel": "feishu", "chat": chat}


def test_send_returns_a_server_confirmed_receipt(server, transport):
    receipt = transport.send_plaintext(origin=_origin(), delivery_key="k1",
                                      payload={"text": "结果: 2 passed"})
    assert set(receipt) == {"origin", "delivery_key", "receipt_id"}
    assert receipt["receipt_id"].startswith("om_")
    assert receipt["origin"]["chat"] == CHAT
    assert server.sent[0]["receive_id"] == CHAT


def test_one_token_is_reused_across_sends(server, transport):
    transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    transport.send_plaintext(origin=_origin(), delivery_key="k2", payload={"text": "two"})
    assert server.tokens == 1


def test_same_key_never_sends_twice(server, transport):
    first = transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    second = transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    assert first["receipt_id"] == second["receipt_id"]
    assert len(server.sent) == 1


def test_same_key_with_different_text_is_refused_not_overwritten(server, transport):
    transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    with pytest.raises(FeishuError, match="delivery_key_conflict"):
        transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "other"})
    assert len(server.sent) == 1


def test_a_jeopardised_receipt_is_not_a_delivery(server, transport):
    # Server accepted but the read-back disagrees: the caller must see a failure.
    transport._verify = lambda *a, **k: False
    with pytest.raises(FeishuError, match="feishu_receipt_mismatch"):
        transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    # While the read-back still disagrees the same key must not send a second copy.
    with pytest.raises(FeishuError, match="prior_delivery_unverified"):
        transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    assert len(server.sent) == 1
    # Once the platform can prove the earlier send, the original receipt is settled.
    transport._verify = lambda *a, **k: True
    assert transport.send_plaintext(origin=_origin(), delivery_key="k1",
                                    payload={"text": "one"})["receipt_id"] == "om_1"
    assert len(server.sent) == 1


def test_text_mismatch_from_the_server_is_caught(server, transport):
    server.override_text = "something else entirely"
    with pytest.raises(FeishuError, match="feishu_receipt_mismatch"):
        transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})


def test_chat_mismatch_from_the_server_is_caught(server, transport):
    server.override_chat = "oc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with pytest.raises(FeishuError, match="feishu_receipt_mismatch"):
        transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})


def test_lookup_verifies_on_the_server_and_reports_absent_after_recall(server, transport):
    receipt = transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    assert transport.lookup(origin=_origin(), delivery_key="k1")["receipt_id"] == receipt["receipt_id"]
    assert transport.recall(origin=_origin(), delivery_key="k1") is True
    assert transport.lookup(origin=_origin(), delivery_key="k1") is None


def test_lookup_returns_none_when_the_platform_dropped_the_message(server, transport):
    transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    server.deleted.add("om_1")
    assert transport.lookup(origin=_origin(), delivery_key="k1") is None


def test_lookup_never_sends(server, transport):
    transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    before = len(server.sent)
    transport.lookup(origin=_origin(), delivery_key="k1")
    assert len(server.sent) == before


def test_transport_failures_are_typed_and_carry_no_secret(server, transport):
    server.fail_status = 502
    with pytest.raises(FeishuError) as info:
        transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    assert str(info.value) == "feishu_http_502"
    assert APP_SECRET not in repr(info.value)


def test_platform_error_code_is_reported_not_swallowed(server, transport):
    server.handle = lambda request: {"code": 230002, "msg": "bot not in chat"}
    with pytest.raises(FeishuError, match="feishu_code_230002"):
        transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})


def test_origin_without_a_chat_is_refused_before_any_network_call(server, transport):
    with pytest.raises(ValueError, match="origin_chat_required"):
        transport.send_plaintext(origin={"profile": "team", "session": "s"}, delivery_key="k1",
                                 payload={"text": "one"})
    assert server.sent == []


def test_oversized_text_is_refused_before_sending(server, transport):
    with pytest.raises(ValueError, match="payload_text_too_large"):
        transport.send_plaintext(origin=_origin(), delivery_key="k1",
                                 payload={"text": "x" * 30001})
    assert server.sent == []


def test_journal_is_private(transport):
    transport.send_plaintext(origin=_origin(), delivery_key="k1", payload={"text": "one"})
    assert (transport.journal.stat().st_mode & 0o077) == 0


# ---- completion sender contract (DirectCompletionTransport's ``sender``) ----
def test_completion_sender_reports_confirmed_message_id(server, transport):
    from agentcore.feishu_transport import FeishuCompletionSender
    sender = FeishuCompletionSender(transport)
    response = sender(_origin(), {"kind": "text", "text": "任务完成"}, idempotency_key="k1")
    assert response == {"ok": True, "message_id": "om_1"}
    assert len(server.sent) == 1


def test_completion_sender_never_claims_not_sent_after_reaching_the_platform(transport, server):
    from agentcore.feishu_transport import FeishuCompletionSender
    sender = FeishuCompletionSender(transport)
    # Read-back disagreement means a copy may exist: the queue must stay unresolved.
    server.override_text = "something else"
    assert sender(_origin(), {"kind": "text", "text": "任务完成"}, idempotency_key="k1") \
        == {"ok": False}


def test_completion_sender_marks_only_pre_send_refusals_as_not_sent(server, transport):
    from agentcore.feishu_transport import FeishuCompletionSender
    sender = FeishuCompletionSender(transport)
    # No chat in the origin: refused before any request, so a retry is safe.
    assert sender({"profile": "team", "session": "s"}, {"kind": "text", "text": "x"},
                  idempotency_key="k1") == {"ok": False, "not_sent": True}
    assert server.sent == []


def test_completion_sender_refuses_attachments_it_cannot_deliver(transport):
    from agentcore.feishu_transport import FeishuCompletionSender
    sender = FeishuCompletionSender(transport)
    with pytest.raises(ValueError, match="feishu_file_delivery_unsupported"):
        sender(_origin(), {"kind": "file", "path": "/tmp/x"}, idempotency_key="k1")

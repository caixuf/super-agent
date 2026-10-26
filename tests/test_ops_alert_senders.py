"""Offline regression suite for the optional capability-alert webhook sender.

Every request goes to a loopback ``http.server`` started in a daemon thread;
no external network is used and no production endpoint is contacted.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentcore.ops_alert_senders import WebhookSender
from agentcore.ops_alerts import AlertOutbox, DeliveryReceipt, deliver_once

PAYLOAD = {
    "kind": "capability_drift",
    "event_id": "evt-1",
    "profile": "profile-test",
    "observed": {"capability": 1},
    "note": "漂移",
}
KEY = "evt-1"


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


class LocalWebhook:
    """Loopback HTTP server; each POST is recorded, then answered by callback."""

    def __init__(self, responder):
        self.requests = []
        self.responder = responder
        record = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                record.requests.append({
                    "method": "POST",
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": body,
                })
                status, headers, payload, delay = record.responder()
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if payload:
                        self.wfile.write(payload)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/alerts"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def webhooks():
    started = []

    def start(responder):
        server = LocalWebhook(responder)
        started.append(server)
        return server

    yield start
    for server in started:
        server.close()


def respond(status=202, body=b'{"receipt_id": "rcpt-1"}', headers=None,
            delay=0.0):
    def responder():
        return status, headers or {}, body, delay

    return responder


def test_accepted_json_receipt_returns_delivery_receipt(webhooks):
    server = webhooks(respond(202, b'{"receipt_id": "rcpt-1"}'))

    receipt = WebhookSender(server.url).send(PAYLOAD, idempotency_key=KEY)

    assert receipt == DeliveryReceipt(accepted=True, event_id=KEY,
                                      receipt_id="rcpt-1")
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/alerts"
    assert json.loads(request["body"]) == PAYLOAD
    assert _header(request["headers"], "Idempotency-Key") == KEY
    assert _header(request["headers"], "Content-Type") == "application/json"
    assert _header(request["headers"], "Authorization") is None


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected"),
    [
        (200, b'{"message_id": "m-1"}', {}, "m-1"),
        (204, b"", {"X-Receipt-Id": "h-1"}, "h-1"),
        (200, b'{"receipt_id": "json-2", "message_id": "m-2"}',
         {"X-Receipt-Id": "h-2"}, "json-2"),
        (200, b'{"receipt_id": 42}', {}, "42"),
        (200, b'{"receipt_id": "  padded  "}', {}, "padded"),
    ],
)
def test_explicit_receipt_sources_are_accepted(webhooks, status, body, headers,
                                               expected):
    server = webhooks(respond(status, body, headers))

    receipt = WebhookSender(server.url).send(PAYLOAD, idempotency_key=KEY)

    assert receipt == DeliveryReceipt(accepted=True, event_id=KEY,
                                      receipt_id=expected)
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 400, 401, 429, 500, 503])
def test_non_2xx_never_accepts_and_is_not_retried(webhooks, status):
    server = webhooks(respond(
        status,
        b'{"receipt_id": "should-not-count"}',
        {"X-Receipt-Id": "also-not", "Location": "/elsewhere"},
    ))

    receipt = WebhookSender(server.url).send(PAYLOAD, idempotency_key=KEY)

    assert receipt is None
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (200, b"", {}),
        (200, b"not-json", {}),
        (200, b"[]", {}),
        (200, b'"receipt"', {}),
        (200, b'{"event_id": "evt-1"}', {}),
        (200, b'{"receipt_id": ""}', {}),
        (200, b'{"receipt_id": "   "}', {}),
        (200, b'{"receipt_id": null}', {}),
        (200, b'{"receipt_id": true}', {}),
        (200, b'{"receipt_id": 12.5}', {}),
        (200, b'{"receipt_id": "ok"', {}),
        (200, b"", {"X-Receipt-Id": "  "}),
    ],
)
def test_2xx_without_usable_receipt_is_none(webhooks, status, body, headers):
    server = webhooks(respond(status, body, headers))

    assert WebhookSender(server.url).send(PAYLOAD,
                                          idempotency_key=KEY) is None
    assert len(server.requests) == 1


def test_timeout_returns_none_without_retry(webhooks):
    server = webhooks(respond(202, b'{"receipt_id": "late"}', delay=0.4))
    sender = WebhookSender(server.url, timeout=0.15)

    assert sender.send(PAYLOAD, idempotency_key=KEY) is None
    assert len(server.requests) == 1


def test_bearer_token_is_sent_but_never_logged_or_exposed(webhooks, caplog):
    token = "s3cr3t-token"
    server = webhooks(respond(200, b'{"receipt_id": "rcpt-t"}'))
    sender = WebhookSender(server.url, token=token)

    with caplog.at_level(logging.DEBUG):
        receipt = sender.send(PAYLOAD, idempotency_key=KEY)

    assert receipt == DeliveryReceipt(accepted=True, event_id=KEY,
                                      receipt_id="rcpt-t")
    headers = server.requests[0]["headers"]
    assert _header(headers, "Idempotency-Key") == KEY
    assert _header(headers, "Authorization") == f"Bearer {token}"
    assert token not in caplog.text
    assert token not in repr(sender)
    assert token not in repr(receipt)


def test_timeout_with_token_returns_none_and_never_logs_token(webhooks, caplog):
    token = "s3cr3t-token"
    server = webhooks(respond(202, b'{"receipt_id": "late"}', delay=0.4))
    sender = WebhookSender(server.url, token=token, timeout=0.15)

    with caplog.at_level(logging.DEBUG):
        assert sender.send(PAYLOAD, idempotency_key=KEY) is None

    assert token not in caplog.text
    assert token not in repr(sender)


def test_redirect_is_refused_so_token_never_reaches_another_target(webhooks):
    target = webhooks(respond(200, b'{"receipt_id": "stolen"}'))
    source = webhooks(respond(302, b"", {"Location": target.url}))
    sender = WebhookSender(source.url, token="s3cr3t-token")

    assert sender.send(PAYLOAD, idempotency_key=KEY) is None
    assert len(source.requests) == 1
    assert target.requests == []


def test_send_rejects_invalid_payload_and_idempotency_key():
    sender = WebhookSender("http://127.0.0.1:9/alerts")

    with pytest.raises(ValueError, match="payload"):
        sender.send(["not", "a", "mapping"], idempotency_key=KEY)
    with pytest.raises(ValueError, match="idempotency_key"):
        sender.send(PAYLOAD, idempotency_key="   ")
    with pytest.raises(TypeError):
        sender.send({"unserializable": object()}, idempotency_key=KEY)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com/hook",
        "ftp://example.com/hook",
        "http://",
        "http://user:secret@example.com/hook",
    ],
)
def test_constructor_rejects_invalid_urls(url):
    with pytest.raises(ValueError, match="url"):
        WebhookSender(url)


@pytest.mark.parametrize("token", ["", "   ", "line\nbreak", 123, object()])
def test_constructor_rejects_invalid_tokens(token):
    with pytest.raises(ValueError, match="token"):
        WebhookSender("http://127.0.0.1:9/alerts", token=token)


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), float("-inf"), True, "5"],
)
def test_constructor_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout"):
        WebhookSender("http://127.0.0.1:9/alerts", timeout=timeout)


def _observe(outbox):
    return outbox.observe(
        "profile-test", "scope-test",
        {"drifted": True, "expected": {"capability": 8},
         "observed": {"capability": 1}},
        config_persisted=False,
        runtime_loaded=False,
    )


def test_confirmed_webhook_acknowledges_outbox_event(webhooks, tmp_path):
    server = webhooks(respond(202, b'{"receipt_id": "rcpt-outbox"}'))
    outbox = AlertOutbox(str(tmp_path / "queue"))
    event_id = _observe(outbox)["event_id"]

    result = deliver_once(outbox, WebhookSender(server.url))

    assert result == {"status": "delivered", "event_id": event_id,
                      "receipt_id": "rcpt-outbox", "attempt": 1}
    assert outbox.pending() == []
    assert len(server.requests) == 1
    assert _header(server.requests[0]["headers"],
                   "Idempotency-Key") == event_id


def test_unconfirmed_webhook_keeps_outbox_event_pending(webhooks, tmp_path):
    server = webhooks(respond(500, b'{"receipt_id": "nope"}'))
    outbox = AlertOutbox(str(tmp_path / "queue"))
    event_id = _observe(outbox)["event_id"]

    result = deliver_once(outbox, WebhookSender(server.url))

    assert result["status"] == "delivery_unknown"
    assert result["event_id"] == event_id
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0]["event_id"] == event_id
    assert pending[0]["attempts"] == 1
    with outbox.pool.db() as db:
        row = db.execute(
            "SELECT delivered,receipt_id FROM ops_alert_outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert row["delivered"] is None
    assert row["receipt_id"] is None

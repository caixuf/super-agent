"""HTTP webhook transport for capability drift alerts.

Default-off: nothing in this repository constructs this sender automatically.
It reads no environment variables and registers no background task. A host
opts in by building it explicitly and passing it to
``agentcore.ops_alerts.deliver_once`` (directly or through
``agentcore.capability_guard.run_guard``).

Configuration keys (host-owned; supplied by the caller):

=================  ========  =============================================
key                required  meaning
=================  ========  =============================================
``url``            yes       absolute ``http``/``https`` endpoint
``token``          no        bearer token; sent as ``Authorization: Bearer
                             <token>`` and never logged or stored in a
                             receipt
``timeout``        no        transport timeout in seconds (default ``5.0``)
=================  ========  =============================================

A host that binds these to its own configuration can use, for example,
``OPS_ALERT_WEBHOOK_URL`` / ``OPS_ALERT_WEBHOOK_TOKEN`` /
``OPS_ALERT_WEBHOOK_TIMEOUT`` and construct the sender only when the URL is
present; that mapping is a host decision, not a module default.

Delivery contract:

* ``POST`` the event payload as UTF-8 JSON with
  ``Idempotency-Key: <idempotency_key>``.
* Acceptance requires both an explicit ``2xx`` status *and* a receipt id.
  The receipt id is read from a JSON object field ``receipt_id`` (preferred)
  or ``message_id``, or else from the ``X-Receipt-Id`` response header.
* Accepted sends return
  ``DeliveryReceipt(accepted=True, event_id=<idempotency_key>,
  receipt_id=<receipt>)`` so the outbox can acknowledge the event.
* Non-2xx responses, timeouts, connection failures, redirects, and ``2xx``
  responses without a usable receipt id return ``None``; the outbox then
  leaves the event pending.
* The sender performs no retries and follows no redirects. The durable
  outbox owns lease-based retry, and a redirect could otherwise forward the
  bearer token to another host.
"""
from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from agentcore.ops_alerts import DeliveryReceipt

RECEIPT_HEADER = "X-Receipt-Id"
MAX_RESPONSE_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the token and event body never travel elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _receipt_text(value: Any) -> str | None:
    """Normalize an explicit receipt value; reject booleans and empties."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, int):
        text = str(value)
    else:
        return None
    return text or None


class WebhookSender:
    """Strict, single-attempt HTTP sender implementing ``ops_alerts.Sender``."""

    def __init__(self, url: str, *, token: str | None = None,
                 timeout: float = 5.0):
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("url must not embed credentials; use token")
        if token is not None:
            if not isinstance(token, str) or not token.strip():
                raise ValueError("token must be a non-empty string when provided")
            if any(ch in token for ch in "\r\n"):
                raise ValueError("token must not contain newlines")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a finite positive number")
        self._url = url.strip()
        self._token = token.strip() if token is not None else None
        self._timeout = float(timeout)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        token = "<set>" if self._token is not None else None
        return (f"WebhookSender(url={self._url!r}, token={token}, "
                f"timeout={self._timeout!r})")

    def send(self, payload: Mapping[str, Any], *,
             idempotency_key: str) -> DeliveryReceipt | None:
        """Send one event; ``None`` means "not confirmed, keep it pending"."""
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        key = idempotency_key.strip()
        # A payload that cannot be serialized is a caller bug, so it raises
        # instead of masquerading as an ambiguous transport outcome.
        body = json.dumps(dict(payload), ensure_ascii=False,
                          sort_keys=True).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": key,
            "User-Agent": "agentcore-ops-alert/1",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self._url, data=body, headers=headers,
                                         method="POST")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.getcode()
                response_headers = response.headers
                raw = response.read(MAX_RESPONSE_BYTES)
        except (OSError, ValueError, http.client.HTTPException):
            # URLError/HTTPError/timeouts/refused connections all mean the
            # remote acceptance is unknown; leaving the event pending is safe
            # because the outbox retries with the same idempotency key.
            return None

        if not isinstance(status, int) or not 200 <= status < 300:
            return None
        receipt_id = self._extract_receipt(raw, response_headers)
        if receipt_id is None:
            return None
        return DeliveryReceipt(accepted=True, event_id=key,
                               receipt_id=receipt_id)

    @staticmethod
    def _extract_receipt(raw: bytes, headers: Any) -> str | None:
        """Return an explicit receipt id from the JSON body or header."""
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                receipt = (_receipt_text(parsed.get("receipt_id"))
                           or _receipt_text(parsed.get("message_id")))
                if receipt is not None:
                    return receipt
        if headers is not None:
            return _receipt_text(headers.get(RECEIPT_HEADER))
        return None

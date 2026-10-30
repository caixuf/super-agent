"""Real Feishu transport for the colleague delivery seams.

``colleague_readonly_delivery`` and ``completion_backfill`` accept a transport
object; this is one that actually talks to the Feishu open platform. It sends a
plain-text message into the chat named by the task origin, then reads the
message back from the server and compares the chat and the text digest. A send
is only reported as a receipt after that server-side confirmation, so an
ambiguous send can never be mistaken for a delivered result.

Delivery is at-most-once per ``delivery_key``: the key is journalled locally and
sent to the platform as the message ``uuid``, so the same key cannot produce two
messages even if the journal is lost within the platform's dedup window. A key
reused for different text or a different origin is refused, never overwritten.

Credentials come from the caller. They are never logged, never embedded in an
exception and never written to the journal. Nothing here is a sandbox: the host
decides which chats exist and which one an origin may address.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from agentcore.pool import normalize_origin

API = "https://open.feishu.cn/open-apis"
#: Feishu rejects oversized text bodies; refuse locally instead of failing the send.
MAX_TEXT_BYTES = 30000
_CHAT = re.compile(r"oc_[A-Za-z0-9]{8,64}\Z")
_TOKEN_MARGIN = 120.0
#: Platform answers meaning "no such message" rather than "the call failed".
_ABSENT_CODES = {"feishu_code_230001"}


class FeishuError(RuntimeError):
    """A platform call failed. The reason is a stable token, never a secret."""


def _opener():
    # Staging sits behind a host proxy that may be down; that must not read as an API error.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FeishuTransport:
    """Send/lookup plain-text Feishu messages for one app bot.

    ``send_plaintext``/``lookup`` match the transport contract used by the
    colleague delivery seams. ``recall`` exists so a self test can clean up.
    """

    def __init__(self, *, app_id, app_secret, journal, api=API, opener=None,
                 timeout=20.0, clock=time.monotonic):
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError("feishu_app_id_required")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise ValueError("feishu_app_secret_required")
        self._app_id, self._app_secret = app_id, app_secret
        self._opener = _opener() if opener is None else opener
        self.api, self.timeout, self._clock = api.rstrip("/"), timeout, clock
        self._token, self._token_expiry = None, 0.0
        self.journal = Path(journal)
        self._prepare_journal()

    # ---- journal -----------------------------------------------------
    def _prepare_journal(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sends ("
                       "delivery_key TEXT PRIMARY KEY, origin TEXT NOT NULL,"
                       " chat TEXT NOT NULL, receipt_id TEXT NOT NULL,"
                       " text_sha256 TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.journal, timeout=30)
        db.execute("PRAGMA synchronous=FULL")
        os.chmod(self.journal, 0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _record(self, key, *, origin, chat, receipt_id, text_sha256, state):
        with self._db() as db:
            db.execute("INSERT INTO sends(delivery_key,origin,chat,receipt_id,text_sha256,state,updated)"
                       " VALUES(?,?,?,?,?,?,?) ON CONFLICT(delivery_key) DO UPDATE SET"
                       " state=excluded.state, updated=excluded.updated",
                       (key, origin, chat, receipt_id, text_sha256, state, time.time()))

    def _journal_row(self, key):
        with self._db() as db:
            return db.execute("SELECT origin,chat,receipt_id,text_sha256,state"
                              " FROM sends WHERE delivery_key=?", (key,)).fetchone()

    # ---- platform ----------------------------------------------------
    def _request(self, method, path, *, body=None, params=None, token=None):
        url = self.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1048577)
        except urllib.error.HTTPError as exc:
            raise FeishuError("feishu_http_%s" % exc.code) from None
        except (urllib.error.URLError, OSError):
            raise FeishuError("feishu_transport_unavailable") from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise FeishuError("feishu_non_json") from None
        if not isinstance(payload, dict):
            raise FeishuError("feishu_non_json")
        if payload.get("code") != 0:
            raise FeishuError("feishu_code_%s" % payload.get("code"))
        return payload

    def _call(self, method, path, *, body=None, params=None):
        return self._request(method, path, body=body, params=params,
                             token=self._access_token()).get("data") or {}

    def _access_token(self):
        if self._token is not None and self._clock() < self._token_expiry:
            return self._token
        payload = self._request("POST", "/auth/v3/tenant_access_token/internal",
                                body={"app_id": self._app_id, "app_secret": self._app_secret})
        token, expires = payload.get("tenant_access_token"), payload.get("expire")
        if not isinstance(token, str) or not token or isinstance(expires, bool) \
                or not isinstance(expires, (int, float)) or expires <= 0:
            raise FeishuError("feishu_token_invalid")
        self._token = token
        self._token_expiry = self._clock() + float(expires) - _TOKEN_MARGIN
        return token

    # ---- origin / payload --------------------------------------------
    @staticmethod
    def _target(origin):
        try:
            canonical = normalize_origin(origin)
        except (TypeError, ValueError):
            raise ValueError("origin_unaddressable") from None
        chat = json.loads(canonical).get("chat")
        if not isinstance(chat, str) or not _CHAT.match(chat):
            raise ValueError("origin_chat_required")
        return canonical, chat

    @staticmethod
    def _text(payload):
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError("payload_text_required")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("payload_text_too_large")
        return text

    def _verify(self, receipt_id, chat, text_sha256):
        """Read the message back from the server; True only on an exact match."""
        try:
            data = self._call("GET", "/im/v1/messages/" + receipt_id,
                              params={"user_id_type": "open_id"})
        except FeishuError as error:
            # A recalled or purged message is an absence, not a transport fault.
            if str(error) in _ABSENT_CODES:
                return False
            raise
        items = data.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return False
        message = items[0]
        if message.get("deleted") is True or message.get("chat_id") != chat:
            return False
        body = message.get("body")
        content = body.get("content") if isinstance(body, dict) else None
        try:
            decoded = json.loads(content) if isinstance(content, str) else None
        except ValueError:
            return False
        if not isinstance(decoded, dict) or not isinstance(decoded.get("text"), str):
            return False
        return _digest(decoded["text"]) == text_sha256

    @staticmethod
    def _receipt(origin, delivery_key, receipt_id):
        return {"origin": json.loads(normalize_origin(origin)),
                "delivery_key": delivery_key, "receipt_id": receipt_id}

    # ---- transport contract -----------------------------------------
    def send_plaintext(self, *, origin, delivery_key, payload):
        if not isinstance(delivery_key, str) or not delivery_key.strip() or len(delivery_key) > 200:
            raise ValueError("delivery_key_required")
        canonical, chat = self._target(origin)
        text = self._text(payload)
        text_sha256 = _digest(text)

        prior = self._journal_row(delivery_key)
        if prior is not None:
            prior_origin, prior_chat, prior_id, prior_digest, _state = prior
            if prior_origin != canonical or prior_chat != chat or prior_digest != text_sha256:
                raise FeishuError("delivery_key_conflict")
            if self._verify(prior_id, chat, text_sha256):
                return self._receipt(origin, delivery_key, prior_id)
            # Journalled but no longer provable: do not send a second copy.
            raise FeishuError("prior_delivery_unverified")

        data = self._call("POST", "/im/v1/messages", params={"receive_id_type": "chat_id"},
                          body={"receive_id": chat, "msg_type": "text",
                                "content": json.dumps({"text": text}, ensure_ascii=False),
                                "uuid": delivery_key[:50]})
        receipt_id = data.get("message_id")
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise FeishuError("feishu_message_id_missing")
        if not self._verify(receipt_id, chat, text_sha256):
            self._record(delivery_key, origin=canonical, chat=chat, receipt_id=receipt_id,
                         text_sha256=text_sha256, state="unverified")
            raise FeishuError("feishu_receipt_mismatch")
        self._record(delivery_key, origin=canonical, chat=chat, receipt_id=receipt_id,
                     text_sha256=text_sha256, state="confirmed")
        return self._receipt(origin, delivery_key, receipt_id)

    def lookup(self, *, origin, delivery_key):
        canonical, chat = self._target(origin)
        row = self._journal_row(delivery_key)
        if row is None:
            return None
        prior_origin, prior_chat, prior_id, prior_digest, _state = row
        if prior_origin != canonical or prior_chat != chat:
            return None
        if not self._verify(prior_id, chat, prior_digest):
            return None
        return self._receipt(origin, delivery_key, prior_id)

    def recall(self, *, origin, delivery_key):
        """Withdraw a message from this key; used by self tests and by host cleanup."""
        canonical, chat = self._target(origin)
        row = self._journal_row(delivery_key)
        if row is None or row[0] != canonical or row[1] != chat:
            return False
        self._call("DELETE", "/im/v1/messages/" + row[2])
        self._record(delivery_key, origin=canonical, chat=chat, receipt_id=row[2],
                     text_sha256=row[3], state="recalled")
        return True


class FeishuCompletionSender:
    """Adapt the platform transport to ``DirectCompletionTransport``'s sender.

    That seam wants ``{'ok': True, 'message_id': ...}`` for a confirmed send and
    ``{'not_sent': True}`` only when nothing reached the platform, because that is
    the sole case where retrying is safe. Any other failure is left unresolved so
    the queue reports ``ack_unknown`` instead of risking a second message.
    """

    def __init__(self, transport):
        if not callable(getattr(transport, "send_plaintext", None)):
            raise ValueError("receipt_capable_transport_required")
        self.transport = transport

    def __call__(self, address, item, *, idempotency_key):
        if not isinstance(item, dict) or item.get("kind") != "text":
            # Attachments are not implemented here; say so instead of deferring forever.
            raise ValueError("feishu_file_delivery_unsupported")
        try:
            receipt = self.transport.send_plaintext(
                origin=address, delivery_key=idempotency_key,
                payload={"text": item.get("text")})
        except ValueError:
            # Refused before any platform call: the address or the text is unusable.
            return {"ok": False, "not_sent": True}
        except FeishuError:
            return {"ok": False}
        return {"ok": True, "message_id": receipt["receipt_id"]}


def from_env(journal, env=None, opener=None):
    """Build a transport from ``FEISHU_APP_ID``/``FEISHU_APP_SECRET`` if both are set."""
    source = os.environ if env is None else env
    app_id, app_secret = source.get("FEISHU_APP_ID", ""), source.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        return None
    return FeishuTransport(app_id=app_id, app_secret=app_secret, journal=journal,
                           opener=opener)

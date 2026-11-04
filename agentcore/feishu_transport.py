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


# --------------------------------------------------------------------------- #
# Card Kit adapter — one writer ↔ one card_id (colleague visibility knife C)
# --------------------------------------------------------------------------- #

#: Card Kit entities remain patchable for this many days; past that, resume must
#: deliver-or-reason instead of silently orphaning a second card.
CARD_KIT_UPDATE_WINDOW_DAYS = 14


def colleague_card_payload(view) -> dict:
    """Map a platform-neutral CardView onto an interactive card body.

    Title carries display_name + task_title (already composed by card_emit). No
    invented percent/ETA. Identity row is plain text from the view fields only.

    Prefer-C1: HITL states render Approve / Deny buttons when ``actions`` include
    them, plus an honest source-label row (from_bot / source_thread / role). When
    the platform cannot host interactive controls, callers still get the same
    body text with text-fallback instructions (deliver-or-reason).
    """
    from agentcore.hitl_bridge import (
        callback_value,
        hitl_text_fallback_instructions,
    )

    rendered = view.render() if hasattr(view, "render") else dict(view)
    title = str(rendered.get("title") or "任务")[:120]
    state = str(rendered.get("state") or "")
    phase = str(rendered.get("phase") or "")
    note = str(rendered.get("note") or "")
    role = str(rendered.get("display_name") or rendered.get("role_id") or "")
    task_title = str(rendered.get("task_title") or "")
    age = rendered.get("heartbeat_age_s")
    lines = []
    if role or task_title:
        lines.append("岗位：%s" % (role or "—"))
    if task_title:
        lines.append("任务：%s" % task_title)
    lines.append("状态：%s" % (state or "—"))
    if phase:
        lines.append("阶段：%s" % phase)
    summary = rendered.get("hitl_summary") or rendered.get("detail") or ""
    if summary and state in ("waiting_user", "awaiting_plan_approval"):
        lines.append("摘要：%s" % str(summary)[:500])
    if age is not None:
        try:
            lines.append("心跳：%d秒前" % max(0, int(float(age))))
        except (TypeError, ValueError):
            pass
    attempt_id = rendered.get("attempt_id") or ""
    if attempt_id:
        lines.append("attempt：%s" % attempt_id)
    # Prefer-D5: optional B fields (blank omitted).
    queued = rendered.get("queued_follow_up_count")
    try:
        queued_n = int(queued) if queued not in (None, "") else 0
    except (TypeError, ValueError):
        queued_n = 0
    if queued_n:
        lines.append("排队跟进：%d" % queued_n)
    live = (rendered.get("liveness_verdict") or rendered.get("verdict") or "").strip()
    if live:
        lines.append("活性：%s" % live)
    pool_window = (rendered.get("pool_window") or "").strip()
    if pool_window:
        lines.append("窗口：%s" % pool_window)
    if rendered.get("ack_miss"):
        lines.append("ack_miss：是")
    if note:
        lines.append(note)
    waiting = rendered.get("waiting_role")
    if waiting and state in ("waiting_user", "awaiting_plan_approval"):
        lines.append("待确认岗位：%s" % waiting)
    from_bot = rendered.get("from_bot") or ""
    source_thread = rendered.get("source_thread") or ""
    role_id = rendered.get("role_id") or ""
    if state in ("waiting_user", "awaiting_plan_approval"):
        src_bits = []
        if from_bot:
            src_bits.append("from_bot=%s" % from_bot)
        if source_thread:
            src_bits.append("source_thread=%s" % source_thread)
        if role_id or role:
            src_bits.append("role=%s" % (role or role_id))
        if src_bits:
            lines.append("来源：" + " · ".join(src_bits))
        else:
            lines.append("来源：（未绑定）")
    arts = rendered.get("artifacts") or []
    if arts:
        lines.append("产物：" + "、".join(str(a) for a in list(arts)[:6]))
    actions = list(rendered.get("actions") or [])
    hitl_kind = str(rendered.get("hitl_kind") or "")
    request_id = str(rendered.get("hitl_request_id") or "")
    is_hitl = state in ("waiting_user", "awaiting_plan_approval")
    if is_hitl and ("approve" in actions or "deny" in actions or "approve_plan" in actions):
        lines.append(hitl_text_fallback_instructions())
    body = "\n".join(lines)
    header_color = {
        "queued": "grey",
        "running": "blue",
        "waiting_user": "orange",
        "awaiting_plan_approval": "orange",
        "done": "green",
        "failed": "red",
        "cancelled": "grey",
    }.get(state, "blue")
    elements = [
        {
            "tag": "div",
            "text": {"tag": "plain_text", "content": body[:3500]},
        }
    ]
    if is_hitl:
        button_actions = []
        tid = str(rendered.get("task_id") or "")
        if "approve" in actions or "approve_plan" in actions:
            button_actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "批准"},
                "type": "primary",
                "value": callback_value(
                    task_id=tid or request_id or "pending",
                    action="approve",
                    hitl_kind=hitl_kind,
                    request_id=request_id,
                ),
            })
        if "deny" in actions:
            button_actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "拒绝"},
                "type": "danger",
                "value": callback_value(
                    task_id=tid or request_id or "pending",
                    action="deny",
                    hitl_kind=hitl_kind,
                    request_id=request_id,
                ),
            })
        if button_actions:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": "拒绝时请在回调/回复中附原因（必填，≤240字）",
                },
            })
            elements.append({"tag": "action", "actions": button_actions})
    # Prefer-D4: stalled/lost ops buttons (not HITL Approve/Deny).
    phase_l = phase.strip().lower()
    verdict = str(rendered.get("verdict") or "")
    is_stalled_ops = (
        phase_l in ("stalled", "lost") or verdict in ("stalled", "lost")
    ) and state not in (
        "done", "failed", "cancelled",
        "waiting_user", "awaiting_plan_approval",
    )
    if is_stalled_ops:
        from agentcore.stalled_card_bridge import (
            ops_callback_value,
            stalled_text_fallback_instructions,
        )
        lines.append(stalled_text_fallback_instructions(
            task_id=str(rendered.get("task_id") or "")))
        body = chr(10).join(lines)
        elements[0] = {
            "tag": "div",
            "text": {"tag": "plain_text", "content": body[:3500]},
        }
        tid = str(rendered.get("task_id") or "")
        ops_buttons = []
        label_for = {
            "cancel": ("取消", "danger"),
            "pause": ("暂停/等待", "default"),
            "status": ("查看进度", "default"),
            "resume_observe": ("恢复观察", "primary"),
        }
        for token in ("cancel", "pause", "status", "resume_observe"):
            label, btype = label_for[token]
            ops_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": btype,
                "value": ops_callback_value(task_id=tid or "pending", action=token),
            })
        if ops_buttons:
            elements.append({"tag": "action", "actions": ops_buttons})
        header_color = "orange" if phase_l == "stalled" else "red"
    # Prefer-D5/D1: pause/status/cancel still on card when actions say so
    # (running / starting / canceling — not HITL, not stalled-ops which already painted).
    if (
        not is_hitl
        and not is_stalled_ops
        and state not in ("done", "failed", "cancelled")
    ):
        from agentcore.stalled_card_bridge import ops_callback_value
        d1_label = {
            "cancel": ("取消", "danger"),
            "pause": ("暂停", "default"),
            "status": ("查看进度", "default"),
        }
        tid = str(rendered.get("task_id") or "")
        d1_buttons = []
        for token in ("cancel", "pause", "status"):
            if token in actions:
                label, btype = d1_label[token]
                d1_buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": btype,
                    "value": ops_callback_value(task_id=tid or "pending", action=token),
                })
        if d1_buttons:
            elements.append({"tag": "action", "actions": d1_buttons})
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": header_color,
        },
        "elements": elements,
    }



class _ViewProxy:
    """Thin proxy so colleague_card_payload sees task_id without mutating CardView."""

    def __init__(self, base, rendered: dict):
        self._base = base
        self._rendered = rendered

    def render(self):
        return dict(self._rendered)

    def __getattr__(self, name):
        return getattr(self._base, name)


class FeishuCardKitAdapter:
    """One CardWriter-facing transport: create Card 2.0 once, then patch in place.

    Contract for :class:`agentcore.card_emit.CardEmitter`:
    ``emit(handle, view, *, final)``.

    * Create → persist ``card_id`` on the handle (caller should ``store.set_card_id``).
    * Update → Card Kit ``batch_update`` / message PATCH with monotonic sequence.
    * uuid = ``{task_id}:{sequence}`` (matches CardWriter) for platform idempotency.
    * Fail → fall back to existing plaintext ``send_plaintext`` path.
    * Restart resume: if handle has ``card_id`` + ``message_id``, patch that card;
      if the platform rejects as expired (14d window), return an honest reason dict
      instead of opening a second card silently.
    """

    def __init__(self, transport: FeishuTransport, *, store=None, clock=None):
        if not isinstance(transport, FeishuTransport):
            raise ValueError("feishu_transport_required")
        self.transport = transport
        self.store = store
        self._clock = clock or time.time
        self._last_uuid: dict = {}

    def _persist_card_id(self, handle, card_id: str) -> None:
        handle.card_id = card_id
        if self.store is not None and hasattr(self.store, "set_card_id"):
            try:
                self.store.set_card_id(handle.task_id, card_id)
            except Exception:
                if hasattr(self.store, "save"):
                    self.store.save(handle)

    def _origin_for(self, handle):
        # Minimal addressable origin: chat is required by FeishuTransport._target.
        return {"profile": "card", "session": handle.task_id, "chat": handle.chat_id}

    def _create(self, handle, view) -> dict:
        card = colleague_card_payload(view)
        # Card Kit create (entity), then send as interactive message referencing it.
        # When Card Kit is unavailable, fall back to sending the interactive body
        # directly as an IM message (still one message_id for later PATCH).
        card_id = ""
        try:
            data = self.transport._call(
                "POST", "/cardkit/v1/cards",
                body={"type": "card_json", "data": json.dumps(card, ensure_ascii=False)},
            )
            card_id = data.get("card_id") or data.get("card", {}).get("card_id") or ""
        except FeishuError:
            card_id = ""
        body = {
            "receive_id": handle.chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
            "uuid": ("%s:%d" % (handle.task_id, max(1, handle.sequence)))[:50],
        }
        try:
            data = self.transport._call(
                "POST", "/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                body=body,
            )
        except FeishuError as err:
            return self._text_fallback(handle, view, reason=str(err))
        message_id = data.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            return self._text_fallback(handle, view, reason="feishu_message_id_missing")
        handle.message_id = message_id
        if card_id:
            self._persist_card_id(handle, card_id)
        if self.store is not None and hasattr(self.store, "save"):
            self.store.save(handle)
        return {"ok": True, "message_id": message_id, "card_id": handle.card_id,
                "created": True}

    def _patch(self, handle, view, *, final: bool) -> dict:
        card = colleague_card_payload(view)
        uuid = ("%s:%d" % (handle.task_id, handle.sequence))[:50]
        if self._last_uuid.get(handle.task_id) == uuid:
            return {"ok": True, "deduped": True, "uuid": uuid}
        # Prefer Card Kit batch_update when card_id is known.
        if handle.card_id:
            try:
                self.transport._call(
                    "POST",
                    "/cardkit/v1/cards/%s/batch_update" % handle.card_id,
                    body={
                        "uuid": uuid,
                        "sequence": int(handle.sequence),
                        # Full card replace with monotonic sequence + uuid idempotency.
                        "card": card,
                        "sequence": int(handle.sequence),
                        "update_multi": True,
                    },
                )
                self._last_uuid[handle.task_id] = uuid
                return {"ok": True, "patched": True, "via": "cardkit", "uuid": uuid,
                        "card_id": handle.card_id, "final": final}
            except FeishuError as err:
                reason = str(err)
                if "230020" in reason or "expired" in reason.lower():
                    return {"ok": False, "expired": True,
                            "reason": "card_update_window_expired_%sd" % CARD_KIT_UPDATE_WINDOW_DAYS,
                            "card_id": handle.card_id}
                # Fall through to message PATCH / text.
        # Message content PATCH (in-place edit of the interactive message).
        if handle.message_id:
            try:
                self.transport._call(
                    "PATCH",
                    "/im/v1/messages/" + handle.message_id,
                    body={
                        "msg_type": "interactive",
                        "content": json.dumps(card, ensure_ascii=False),
                    },
                )
                self._last_uuid[handle.task_id] = uuid
                return {"ok": True, "patched": True, "via": "message_patch",
                        "uuid": uuid, "message_id": handle.message_id, "final": final}
            except FeishuError as err:
                reason = str(err)
                if "230020" in reason or "expired" in reason.lower():
                    return {"ok": False, "expired": True,
                            "reason": "card_update_window_expired_%sd" % CARD_KIT_UPDATE_WINDOW_DAYS,
                            "message_id": handle.message_id}
                return self._text_fallback(handle, view, reason=reason)
        return self._text_fallback(handle, view, reason="no_message_or_card_id")

    def _text_fallback(self, handle, view, *, reason: str) -> dict:
        """Existing plaintext path — never invent a second card writer."""
        rendered = view.render() if hasattr(view, "render") else dict(view)
        text = "%s\n%s" % (rendered.get("title") or "任务", rendered.get("note") or "")
        state = str(rendered.get("state") or "")
        if state in ("waiting_user", "awaiting_plan_approval"):
            from agentcore.hitl_bridge import hitl_text_fallback_instructions
            extra = hitl_text_fallback_instructions(task_id=handle.task_id)
            summary = rendered.get("hitl_summary") or ""
            text = (text + "\n" + str(summary) + "\n" + extra).strip()

        try:
            receipt = self.transport.send_plaintext(
                origin=self._origin_for(handle),
                delivery_key=("%s:fallback:%d" % (handle.task_id, handle.sequence))[:50],
                payload={"text": text[:8000]},
            )
        except (FeishuError, ValueError) as err:
            return {"ok": False, "fallback": True, "error": str(err), "reason": reason}
        if not handle.message_id:
            handle.message_id = receipt.get("receipt_id") or handle.message_id
            if self.store is not None and hasattr(self.store, "save"):
                self.store.save(handle)
        return {"ok": True, "fallback": True, "message_id": receipt.get("receipt_id"),
                "reason": reason}

    def emit(self, handle, view, *, final: bool = False) -> dict:
        """CardEmitter transport entrypoint — create once, then same-card patch."""
        try:
            rendered = view.render() if hasattr(view, "render") else dict(view)
            if "task_id" not in rendered or not rendered.get("task_id"):
                rendered["task_id"] = handle.task_id
                view = _ViewProxy(view, rendered)
        except Exception:
            pass
        if not handle.message_id:
            return self._create(handle, view)
        return self._patch(handle, view, final=final)


class CardNotifyBridge:
    """Turn-complete / approval-requested notify on the *same* card (Codex §6-1).

    Does not open a model turn. Callers pass a CardEmitter (or compatible) and a
    target CardState; the bridge only patches the existing card / completion path.

    Prefer-C2: optional ``pool`` + ``transport`` kick ``kick_deliver_once`` after the
    same-card terminal frame so the outbox receipt also drains (at-least-once honest).
    Gateway injection is never the default path here.
    """

    def __init__(self, emitter, *, pool=None, transport=None):
        self.emitter = emitter
        self.pool = pool
        self.transport = transport

    def notify(self, state, *, detail: str = "", phase: str = "") -> object:
        view = self.emitter.notify_terminal_or_wait(state, detail=detail, phase=phase)
        return view

    def notify_terminal(self, state, *, detail: str = "", phase: str = "",
                        kick_delivery: bool = True) -> dict:
        """Same-card terminal frame + optional completion_delivery kick (no LLM).

        Returns ``{"card": view_or_None, "delivery": outcome_or_None}``. When no pool
        or transport is bound, delivery is omitted (inspect_pending remains the dry-run).
        """
        view = self.notify(state, detail=detail, phase=phase)
        delivery = None
        if kick_delivery and self.pool is not None:
            from agentcore.completion_delivery import notify_on_terminal
            delivery = notify_on_terminal(self.pool, self.transport, status=state)
        handle = getattr(self.emitter, "handle", None)
        card_id = getattr(handle, "card_id", "") if handle is not None else ""
        message_id = getattr(handle, "message_id", "") if handle is not None else ""
        return {
            "card": view,
            "delivery": delivery,
            "card_id": card_id,
            "message_id": message_id,
            "same_card": bool(message_id),
        }

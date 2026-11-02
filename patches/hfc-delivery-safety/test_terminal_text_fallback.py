from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_feishu_card.delivery import CardDeliveryResult, FeishuAPIError
from hermes_feishu_card.terminal_delivery import TerminalDeliveryPlan, TerminalTarget


def _plan(answer="complete final answer"):
    return TerminalDeliveryPlan(
        TerminalTarget(
            "turn-1", "om_card", "oc_chat", "bot-1", "omt_topic", "om_user", True,
            "profile-1", "message.completed", 9,
        ),
        "card",
        {"schema": "2.0", "body": {"elements": []}},
    )


class _Owner:
    def __init__(self):
        self.terminal_delivery_state = "pending"
        self.answer_text = "complete final answer"
        self.delivery_kind = "chat"
        self.reply_in_thread = True
        self.reply_to_message_id = "om_user"


async def test_text_fallback_posts_complete_answer_after_card_recovery_failure(monkeypatch):
    from hermes_feishu_card import server

    calls = []

    class Platform:
        async def send_card(self, *args, **kwargs):
            raise FeishuAPIError("card channel down", outcome="not_sent")

        async def send_text_message(self, chat_id, text, **kwargs):
            calls.append((chat_id, text, kwargs))
            return "om_text_fallback"

    app = server.create_app(Platform())
    owner = _Owner()
    app[server.SESSIONS_KEY]["turn-1"] = owner
    transport = server._AppTerminalTransport(app)
    result = await transport.recover_with_text(_plan(), owner)
    assert result.outcome == "delivered"
    assert result.message_id == "om_text_fallback"
    assert calls[0][0] == "oc_chat"
    assert calls[0][1] == owner.answer_text
    assert calls[0][2]["reply_to_message_id"] == "om_user"
    assert calls[0][2]["reply_in_thread"] is True


async def test_text_fallback_uses_stable_uuid_when_client_supports_it(monkeypatch):
    from hermes_feishu_card import server

    seen = []

    class Platform:
        async def send_card(self, *args, **kwargs):
            raise FeishuAPIError("card channel down", outcome="not_sent")

        async def send_text_message(self, chat_id, text, **kwargs):
            seen.append(kwargs)
            return "om_text_fallback"

    app = server.create_app(Platform())
    owner = _Owner()
    transport = server._AppTerminalTransport(app)
    await transport.recover_with_text(_plan(), owner)
    assert seen[0]["delivery_uuid"] == server.build_delivery_uuid(
        bot_id="bot-1", chat_id="oc_chat", reply_to_message_id="om_user",
        session_key=_plan().target.recovery_key, delivery_kind="terminal-text-recovery",
    )


async def test_text_fallback_records_partial_failure_without_claiming_delivery():
    from hermes_feishu_card import server

    class Platform:
        async def send_card(self, *args, **kwargs):
            raise FeishuAPIError("card channel down", outcome="not_sent")

        async def send_text_message(self, *args, **kwargs):
            raise FeishuAPIError("text channel down", outcome="not_sent")

    app = server.create_app(Platform())
    owner = _Owner()
    app[server.SESSIONS_KEY]["turn-1"] = owner
    transport = server._AppTerminalTransport(app)
    result = await transport.recover_with_text(_plan(), owner)
    assert result.outcome == "not_sent"
    assert owner.terminal_delivery_state == "pending" or owner.terminal_delivery_state == "not_sent"


async def test_text_fallback_is_idempotent_on_repeated_recovery():
    from hermes_feishu_card import server

    calls = []

    class Platform:
        async def send_card(self, *args, **kwargs):
            raise FeishuAPIError("card channel down", outcome="not_sent")

        async def send_text_message(self, chat_id, text, **kwargs):
            calls.append(text)
            return "om_text_fallback"

    app = server.create_app(Platform())
    owner = _Owner()
    owner.terminal_delivery_state = "delivered"
    app[server.SESSIONS_KEY]["turn-1"] = owner
    transport = server._AppTerminalTransport(app)
    result = await transport.recover_with_text(_plan(), owner)
    assert result.outcome == "delivered"
    assert calls == []

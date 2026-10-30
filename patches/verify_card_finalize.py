#!/usr/bin/env python3
"""Contract check for patches/patch_card_finalize.py against a host install tree.

Operational (not a pytest module): proves the two things the T5 delivery doc asks for
  * tool boundary does not change the message id (persistent stream message)
  * only the turn-final edit turns the card green / drops the card registry

Run against the host install tree:
  HERMES_AGENT_DIR=~/.hermes/hermes-agent python3 patches/verify_card_finalize.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

AGENT_DIR = os.environ.get("HERMES_AGENT_DIR") or os.path.expanduser("~/.hermes/hermes-agent")
sys.path.insert(0, AGENT_DIR)

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig, _Tick  # noqa: E402
from gateway.platforms.base import SendResult  # noqa: E402
from plugins.platforms.feishu.adapter import FeishuAdapter  # noqa: E402

FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAIL.append(name)


# ── A. consumer: one message across a tool boundary ───────────────────────
async def consumer_case() -> None:
    print("A. consumer / persistent stream message")
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = True
    adapter.PERSISTENT_STREAM_MESSAGE = True
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1"))
    c = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(cursor=""),
                              metadata={"thread_id": "t1"})

    check("capability is cumulative", c._cumulative_transport() is True)

    await c._send_or_edit("先查一下", finalize=False)
    check("first send created one message", adapter.send.call_count == 1)

    # tool boundary: gateway finalizes the preamble with is_turn_final=False
    await c._send_or_edit("先查一下", finalize=True, is_turn_final=False)
    seg_kwargs = adapter.edit_message.await_args.kwargs
    check("segment edit targets same id", seg_kwargs.get("message_id") == "m1", str(seg_kwargs))
    check("segment edit says is_turn_final=False",
          (seg_kwargs.get("metadata") or {}).get("is_turn_final") is False, str(seg_kwargs))

    tick = _Tick(got_segment_break=True)
    tick.update_visible = True
    await c._end_segment(tick)
    check("message id survives tool boundary", c._message_id == "m1", repr(c._message_id))

    # next segment keeps editing the same handle
    await c._send_or_edit("先查一下，接着回答", finalize=False)
    check("post-boundary edit same id", adapter.edit_message.await_args.kwargs.get("message_id") == "m1")
    check("no second send", adapter.send.call_count == 1, str(adapter.send.call_count))

    # turn final
    await c._send_or_edit("最终答案", finalize=True, is_turn_final=True)
    fin_kwargs = adapter.edit_message.await_args.kwargs
    check("turn final same id", fin_kwargs.get("message_id") == "m1", str(fin_kwargs))
    check("turn final says is_turn_final=True",
          (fin_kwargs.get("metadata") or {}).get("is_turn_final") is True, str(fin_kwargs))

    # control: without the capability the old reset still happens
    plain = MagicMock()
    plain.REQUIRES_EDIT_FINALIZE = False
    plain.MAX_MESSAGE_LENGTH = 4096
    plain.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m2"))
    plain.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m2"))
    c2 = GatewayStreamConsumer(plain, "chat", StreamConsumerConfig(cursor=""))
    check("default adapter is not cumulative", c2._cumulative_transport() is False)
    await c2._send_or_edit("x", finalize=False)
    t2 = _Tick(got_segment_break=True)
    t2.update_visible = True
    await c2._end_segment(t2)
    check("default adapter still resets at boundary", c2._message_id is None, repr(c2._message_id))


# ── B. Feishu adapter: card stays blue until turn final ───────────────────
class _Resp:
    code = 0
    msg = "ok"


async def feishu_case() -> None:
    print("B. feishu adapter / card colour + registry")
    a = object.__new__(FeishuAdapter)
    cards: list[dict] = []

    async def _patch(request):
        cards.append(request)
        return _Resp()

    a._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(patch=_patch))))
    a._card_replies = True
    a._card_message_ids = OrderedDict([("om_card", time.time())])
    a._bot_name = "BaiZe"

    async def _run_blocking(fn, request):
        return await fn(request)

    a._run_blocking = _run_blocking
    a.format_message = lambda s: s

    def _body(**kw):
        cards.append(kw)
        return kw

    a._build_patch_message_body = _body
    a._build_patch_message_request = lambda **kw: kw
    a._finalize_send_result = lambda resp, what: SendResult(success=True)

    async def _edit(*, finalize, metadata):
        cards.clear()
        r = await a.edit_message("chat", "om_card", "正文", finalize=finalize, metadata=metadata)
        payload = cards[0]["content"]
        card = json.loads(payload)
        return r, card

    r, card = await _edit(finalize=False, metadata=None)
    check("streaming edit patches the card", r.success and card["header"]["template"] == "blue")
    check("streaming edit keeps registry", "om_card" in a._card_message_ids)

    r, card = await _edit(finalize=True, metadata={"is_turn_final": False})
    check("tool-boundary finalize stays blue", r.success and card["header"]["template"] == "blue",
          card["header"]["template"])
    check("tool-boundary finalize keeps registry", "om_card" in a._card_message_ids)

    r, card = await _edit(finalize=True, metadata={"is_turn_final": True})
    check("turn final turns green", r.success and card["header"]["template"] == "green",
          card["header"]["template"])
    check("turn final drops registry", "om_card" not in a._card_message_ids)

    # no is_turn_final key (legacy callers) keeps the old behaviour: finalize == final
    a._card_message_ids["om_card"] = time.time()
    r, card = await _edit(finalize=True, metadata=None)
    check("legacy metadata-less final still turns green",
          r.success and card["header"]["template"] == "green" and
          "om_card" not in a._card_message_ids)


async def main() -> None:
    await consumer_case()
    await feishu_case()


if __name__ == "__main__":
    asyncio.run(main())
    print("\n" + ("ALL PASS" if not FAIL else f"FAILED: {FAIL}"))
    raise SystemExit(1 if FAIL else 0)

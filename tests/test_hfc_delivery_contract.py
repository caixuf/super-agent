"""Opt-in safety regressions against external HFC, never a live Hermes HOME.

Run with HFC_SOURCE_ROOT=/trusted/hfc python -m pytest -q
 tests/test_hfc_delivery_contract.py. Failures are release blockers, not xfails.
The default suite skips these when the external source is unavailable.
"""
from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def hfc(tmp_path, monkeypatch):
    root = os.environ.get("HFC_SOURCE_ROOT")
    if not root:
        pytest.skip("opt-in: requires trusted external HFC_SOURCE_ROOT")
    source = Path(root).resolve()
    if not (source / "hermes_feishu_card" / "server.py").is_file():
        pytest.fail("HFC_SOURCE_ROOT is not an HFC source directory")
    # External code runs only against a private HOME/state, even if the caller
    # forgot isolation flags or inherited live transport configuration.
    private_home = tmp_path / "home"
    private_home.mkdir(mode=0o700)
    for name in tuple(os.environ):
        if name.startswith(("HERMES_FEISHU_CARD_", "HFC_")):
            monkeypatch.delenv(name)
    for name in ("HOME", "HERMES_HOME", "HERMES_FEISHU_CARD_STATE_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(private_home))
    sys.path.insert(0, str(source))
    try:
        server = importlib.import_module("hermes_feishu_card.server")
        hooks = importlib.import_module("hermes_feishu_card.hook_runtime")
        if Path(server.__file__).resolve().parent.parent != source:
            pytest.fail("HFC import did not resolve to the requested source")
        yield server, hooks
    finally:
        sys.path.remove(str(source))


def _event(kind, sequence, answer="", *, message="probe-a"):
    import time
    return {
        "schema_version": "1", "event": kind,
        "conversation_id": "probe-conversation", "message_id": message,
        "chat_id": "oc_probe", "platform": "feishu", "sequence": sequence,
        "created_at": time.time(),
        "data": {"answer": answer} if answer else {},
    }


class _Platform:
    """In-memory Feishu substitute: no SDK, credentials or external requests."""

    def __init__(self):
        self.sent = []
        self.updated = []
        self.block_patch = None
        self.reject_delivery = False

    async def send_card(self, chat_id, card, **kwargs):
        if self.reject_delivery:
            from hermes_feishu_card.delivery import FeishuAPIError
            raise FeishuAPIError("injected rejection", outcome="not_sent")
        self.sent.append(card)
        return f"om_probe_{len(self.sent)}"

    async def update_card_message(self, message_id, card):
        if self.block_patch is not None:
            await self.block_patch.wait()
        if self.reject_delivery:
            raise RuntimeError("injected PATCH rejection")
        self.updated.append(card)


async def _client(server, tmp_path, platform):
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_feishu_card.native_handoff import NativeHandoffStore
    app = server.create_app(
        platform,
        card_config={"flush_interval_ms": 0},
        native_handoff_store=NativeHandoffStore(tmp_path / "handoff"),
        session_store_directory=tmp_path / "display",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _post(client, payload):
    async with client.post("/events", json=payload) as response:
        return await response.json()


def test_terminal_ack_must_not_claim_delivery_before_patch(hfc, tmp_path):
    server, _ = hfc

    async def run():
        platform = _Platform()
        client = await _client(server, tmp_path, platform)
        platform.block_patch = asyncio.Event()
        request = None
        try:
            assert (await _post(client, _event("message.started", 0)))["applied"]
            request = asyncio.create_task(_post(client, _event("message.completed", 1, "final-marker")))
            done, _ = await asyncio.wait({request}, timeout=0.1)
            early = request.result() if done else None
            assert platform.updated == []  # PATCH is deterministically blocked.
            platform.block_patch.set()
            result = await asyncio.wait_for(request, 3)
            assert early is None or early.get("applied") is not True, (
                "HFC acknowledged delivered ownership before any final PATCH; "
                "Hermes can suppress the only fallback while the answer is still absent"
            )
            assert result.get("ok") is True
        finally:
            platform.block_patch.set()
            if request is not None and not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            await client.close()

    asyncio.run(run())


def test_definite_terminal_delivery_failure_must_not_be_success(hfc, tmp_path, monkeypatch):
    server, hooks = hfc

    async def run():
        platform = _Platform()
        client = await _client(server, tmp_path, platform)
        native_calls = []

        async def no_more_retries(*args, **kwargs):
            return False

        async def native_send(**kwargs):
            native_calls.append(kwargs)
            return SimpleNamespace(success=True)

        async def local_post(_url, payload, _timeout):
            return await _post(client, payload)

        monkeypatch.setattr(server, "_retry_terminal_update", no_more_retries)
        monkeypatch.setattr(hooks, "_post_json_ordered_response", local_post)
        token = None
        try:
            await _post(client, _event("message.started", 0))
            platform.reject_delivery = True
            answer = "complete-final-marker"
            token = hooks._HFC_EXACT_COMPLETION_STAGE.set({
                "payload": _event("message.completed", 1, answer),
                "event_url": "http://127.0.0.1/events", "timeout_seconds": 3,
                "task_id": id(asyncio.current_task()),
            })
            adapter = SimpleNamespace(name="feishu", _send_with_retry=native_send)
            selected, content, reply_to, metadata = await hooks.prepare_exact_base_final_delivery({
                "delivery_adapter": adapter, "content": answer,
                "reply_to": None, "metadata": None, "obligation_id": None,
            })
            result = await selected._send_with_retry(content=content, reply_to=reply_to, metadata=metadata)
            session = next(iter(client.app[server.SESSIONS_KEY].values()))
            for _ in range(200):
                if session.terminal_delivery_state == "not_sent":
                    break
                await asyncio.sleep(0.005)
            assert session.terminal_delivery_state == "not_sent"
            assert platform.updated == [] and len(platform.sent) == 1
            assert native_calls or not result.success, (
                "Hermes received success=True, but every terminal PATCH and recovery "
                "failed definitively and the native final answer was never sent"
            )
        finally:
            if token is not None:
                hooks._HFC_EXACT_COMPLETION_STAGE.reset(token)
            await client.close()

    asyncio.run(run())


def test_superseded_card_must_not_swallow_unseen_late_answer(hfc, tmp_path):
    server, _ = hfc

    async def run():
        platform = _Platform()
        client = await _client(server, tmp_path, platform)
        try:
            await _post(client, _event("message.started", 0))
            await _post(client, _event("message.started", 0, message="probe-b"))
            old = client.app[server.SESSIONS_KEY]["probe-a"]
            assert old.status == "failed"  # New ingress abandoned the previous display.
            answer = "late-answer-never-previously-delivered"
            reply = await _post(client, _event("message.completed", 1, answer))
            for task in list(client.app[server.FLUSH_CONTROLLERS_KEY].values()):
                await task.drain(2)
            delivered = any(answer in str(card) for card in platform.sent + platform.updated)
            assert reply.get("applied") is not True or delivered, (
                "A late final answer was acknowledged/suppressed solely because the old "
                "display was failed; that answer was never delivered"
            )
        finally:
            await client.close()

    asyncio.run(run())


def test_successful_terminal_keeps_full_answer_and_duplicate_is_idempotent(hfc, tmp_path):
    server, _ = hfc

    async def run():
        platform = _Platform()
        client = await _client(server, tmp_path, platform)
        try:
            await _post(client, _event("message.started", 0))
            answer = "normal-complete-final-marker"
            result = await _post(client, _event("message.completed", 1, answer))
            for _ in range(200):
                if any(answer in str(card) for card in platform.updated):
                    break
                await asyncio.sleep(0.005)
            assert result.get("applied") is True
            assert any(answer in str(card) for card in platform.updated)
            count = len(platform.sent), len(platform.updated)
            await _post(client, _event("message.completed", 2, answer))
            await asyncio.sleep(0.02)
            assert count == (len(platform.sent), len(platform.updated))
        finally:
            await client.close()

    asyncio.run(run())


def test_rejected_first_card_never_acknowledges_delivery(hfc, tmp_path):
    server, _ = hfc

    async def run():
        platform = _Platform()
        platform.reject_delivery = True
        client = await _client(server, tmp_path, platform)
        try:
            result = await _post(client, _event("message.started", 0))
            assert result.get("applied") is not True
            assert result.get("ok") is False
            assert platform.sent == []
            assert not client.app[server.SESSIONS_KEY]
        finally:
            await client.close()

    asyncio.run(run())

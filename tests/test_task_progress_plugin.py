"""task-progress plugin: one entry per long task, edits in place, terminal write-once.

The fake host below is deliberately minimal: a ``post_message`` handle that records edits,
a durable plugin-state dict, and the real SQLite liveness ledger in a tmp dir.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "task_progress_plugin", ROOT / "plugins" / "task_progress" / "__init__.py")
assert _spec and _spec.loader
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

from agentcore.liveness import LivenessLedger  # noqa: E402


_LIVE: list = []


@pytest.fixture(autouse=True)
def _always_unload():
    """A leaked poller thread would poll into the next test's fake registry."""
    yield
    for ctx in _LIVE:
        for callback in ctx.unload_callbacks:
            try:
                callback()
            except Exception:
                pass
    _LIVE.clear()


class FakeMessage:
    def __init__(self, text, message_id):
        self.ok = True
        self.error = None
        self.message_id = message_id
        self.edits = []
        self.text = text

    def edit(self, text):
        self.edits.append(text)
        self.text = text
        return {"ok": True, "message_id": self.message_id, "edited": True}


class FakeState:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class Ctx:
    plugin_id = "task-progress"
    profile_name = "p"

    def __init__(self, tmp_path, settings, *, refuse=False):
        self.settings = settings
        self.messages = []
        self.refuse = refuse
        self.hooks = {}
        self.unload_callbacks = []
        self.state = FakeState(tmp_path / "state")
        _LIVE.append(self)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def post_message(self, text):
        if self.refuse:
            message = FakeMessage(text, None)
            message.ok = False
            message.error = "no_conversation"
            return message
        message = FakeMessage(text, "om_%d" % (len(self.messages) + 1))
        self.messages.append(message)
        return message

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def on_unload(self, callback):
        self.unload_callbacks.append(callback)


def _settings(tmp_path, **extra):
    base = {"enabled": True, "engine_root": str(ROOT),
            "liveness_db": str(tmp_path / "liveness.db"),
            "poll_seconds": 2, "min_interval": 0, "max_tracked": 5}
    base.update(extra)
    return base


def _wait(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _row(db, task, **fields):
    ledger = LivenessLedger(db)
    owner = fields.pop("owner", "p:s")
    label = fields.pop("label", "pytest tests")
    ledger.register(task, kind="terminal", owner=owner, label=label,
                    pid=999999, pid_start="dead:1", state="running", **fields)
    return ledger



@pytest.mark.parametrize("terminal", [False, True])
def test_failed_edit_retries_same_card_until_host_confirms(tmp_path, terminal):
    class FlakyMessage(FakeMessage):
        def __init__(self, text, message_id):
            super().__init__(text, message_id)
            self.attempts = 0
        def edit(self, text):
            self.attempts += 1
            self.edits.append(text)
            if self.attempts == 1:
                return {"ok": False, "error": "simulated_edit_failure", "retry_safe": True}
            self.text = text
            return {"ok": True, "message_id": self.message_id, "edited": True}

    class FlakyCtx(Ctx):
        def post_message(self, text):
            message = FlakyMessage(text, "one-stable-message")
            self.messages.append(message)
            return message

    ctx = FlakyCtx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    plugin.register(ctx)
    ledger = _row(ctx.settings["liveness_db"], "proc_retry", phase="initial")
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                result={"session_id": "proc_retry"},
                                session_id="s", turn_id="t")
    assert len(ctx.messages) == 1
    if terminal:
        ledger.finish("proc_retry", "done")
    else:
        ledger.beat("proc_retry", phase="new phase")
    if terminal:
        from agentcore.card_store import CardStore
        card_store = CardStore(tmp_path / "state" / "cards.db")
        assert _wait(lambda: (ctx.messages[0].attempts >= 2
                              and card_store.load("turn:s:t").state.value == "done"), timeout=8)
    else:
        assert _wait(lambda: len(ctx.messages[0].edits) >= 2, timeout=8)
    message = ctx.messages[0]
    assert message.message_id == "one-stable-message"
    assert message.attempts >= 2
    if terminal:
        assert card_store.load("turn:s:t").state.value == "done"
    else:
        assert "new phase" in message.edits[-1]
    for callback in ctx.unload_callbacks:
        callback()

@pytest.mark.parametrize("mode", ["unsupported", "rejected", "raised"])
def test_edit_only_never_falls_back_to_send(tmp_path, monkeypatch, mode):
    import asyncio
    import types

    gateway = types.ModuleType("gateway")
    messages = types.ModuleType("gateway.plugin_messages")
    sends = []
    feishu_module = types.ModuleType("hermes_plugins.feishu_platform__home_abcd.adapter")
    feishu_module.__file__ = "/opt/hermes/plugins/platforms/feishu/adapter.py"
    feishu_module.card_renders_markdown = lambda content: True
    class FeishuAdapter:
        def __init__(self):
            self.edits = []
        def _is_card_message(self, message_id):
            return False
        async def edit_message(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))
            if mode == "raised":
                raise RuntimeError("secret transport detail")
            return types.SimpleNamespace(success=mode != "rejected", message_id=message_id)
        async def send(self, *args, **kwargs):
            sends.append((args, kwargs))
            raise AssertionError("edit-only path called send")
    FeishuAdapter.__module__ = feishu_module.__name__
    adapter = FeishuAdapter()
    messages._supports_edit = lambda value: mode != "unsupported"
    messages._fit = lambda adapter, chat, text: text
    gateway.plugin_messages = messages
    monkeypatch.setitem(__import__("sys").modules, "gateway", gateway)
    monkeypatch.setitem(__import__("sys").modules, "gateway.plugin_messages", messages)
    monkeypatch.setitem(__import__("sys").modules, feishu_module.__name__, feishu_module)

    target = types.SimpleNamespace(platform="lark", live=None, adapter=adapter, chat_id="chat")
    result = asyncio.run(plugin._edit_only(target, "original-id", "updated", plugin_id="task-progress"))
    assert result["ok"] is False
    assert result["error"] in {"edit_unsupported", "edit_rejected", "edit_failed", "edit_outcome_unknown"}
    assert sends == []



@pytest.mark.parametrize("renderer_available", [False, True])
def test_feishu_card_edit_fails_closed_when_replacement_send_branch_possible(
        monkeypatch, renderer_available):
    import asyncio
    import sys
    import types

    gateway = types.ModuleType("gateway")
    plugin_messages = types.ModuleType("gateway.plugin_messages")
    feishu_module = types.ModuleType("hermes_plugins.feishu_platform__home_abcd.adapter")
    feishu_module.__file__ = "/opt/hermes/plugins/platforms/feishu/adapter.py"
    edits = []
    class FeishuAdapter:
        def format_message(self, text):
            return text
        async def edit_message(self, chat_id, message_id, content):
            edits.append((chat_id, message_id, content))
            return types.SimpleNamespace(success=True, message_id=message_id)
    FeishuAdapter.__module__ = feishu_module.__name__
    adapter = FeishuAdapter()
    adapter._is_card_message = lambda message_id: True
    plugin_messages._supports_edit = lambda adapter: True
    plugin_messages._fit = lambda adapter, chat, text: text
    if renderer_available:
        feishu_module.card_renders_markdown = lambda content: False
    gateway.plugin_messages = plugin_messages
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.plugin_messages", plugin_messages)
    monkeypatch.setitem(sys.modules, feishu_module.__name__, feishu_module)

    target = types.SimpleNamespace(platform="feishu", live=None, adapter=adapter, chat_id="chat")
    result = asyncio.run(plugin._edit_only(target, "existing-card", "unsupported", plugin_id="task-progress"))
    assert result == {"ok": False, "error": "edit_unsupported"}
    assert edits == []



def test_unverified_platform_adapter_edit_is_frozen(monkeypatch):
    import asyncio
    import sys
    import types

    gateway = types.ModuleType("gateway")
    messages = types.ModuleType("gateway.plugin_messages")
    class Adapter:
        async def edit_message(self, *args):
            raise AssertionError("unverified adapter must not be called")
    messages._supports_edit = lambda adapter: True
    messages._fit = lambda adapter, chat, text: text
    gateway.plugin_messages = messages
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.plugin_messages", messages)
    target = types.SimpleNamespace(platform="telegram", live=None, adapter=Adapter(), chat_id="chat")
    result = asyncio.run(plugin._edit_only(target, "id", "updated", plugin_id="task-progress"))
    assert result == {"ok": False, "error": "edit_unsupported"}


def test_edit_only_reuses_original_local_notification_key(monkeypatch):
    import asyncio
    import types

    gateway = types.ModuleType("gateway")
    messages = types.ModuleType("gateway.plugin_messages")
    published = []
    def publish(target, text):
        published.append((target.notice_key, text))
        return {"ok": True, "message_id": target.notice_key, "delivered": True}
    messages._publish_local = publish
    gateway.plugin_messages = messages
    monkeypatch.setitem(__import__("sys").modules, "gateway", gateway)
    monkeypatch.setitem(__import__("sys").modules, "gateway.plugin_messages", messages)

    target = types.SimpleNamespace(live=object(), notice_key="newly-resolved-key")
    result = asyncio.run(plugin._edit_only(target, "original-sticky-key", "updated", plugin_id="task-progress"))
    assert result["ok"] is True
    assert published == [("original-sticky-key", "updated")]




def test_owner_change_during_registry_poll_cannot_reconcile_foreign_row(tmp_path, monkeypatch):
    from agentcore.liveness import LivenessLedger
    ledger = None
    polled = []
    registered = threading.Event()
    class ProcessRegistry:
        def poll(self, task_id):
            polled.append(task_id)
            ledger.register(task_id, kind="terminal", owner="p:session-b",
                            label="foreign label", state="running",
                            phase="foreign phase", detail="foreign detail")
            registered.set()
            return {"status": "completed", "exit_code": 1,
                    "output_preview": "foreign secret output"}

    ctx = Ctx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    plugin.register(ctx)
    ledger = LivenessLedger(ctx.settings["liveness_db"])
    ledger.register("proc_owner_race", kind="terminal", owner="p:s",
                    label="own label", state="running", phase="own phase")
    monkeypatch.setattr(plugin, "_registry", lambda: ProcessRegistry())
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                result={"session_id": "proc_owner_race"},
                                session_id="s", turn_id="turn")
    assert registered.wait(6), polled
    row = ledger.get("proc_owner_race")
    assert row["owner"] == "p:session-b"
    assert row["state"] == "running"
    assert row["tail"] == ""
    assert all("foreign" not in text for text in [ctx.messages[0].text, *ctx.messages[0].edits])
    for callback in ctx.unload_callbacks:
        callback()


def test_unknown_task_owner_is_pinned_before_later_foreign_registration(tmp_path, monkeypatch):
    import time
    polled = []
    class ProcessRegistry:
        def poll(self, task_id):
            polled.append(task_id)
            return {"status": "running"}
    monkeypatch.setattr(plugin, "_registry", lambda: ProcessRegistry())
    ctx = Ctx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    plugin.register(ctx)
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                result={"session_id": "proc_late"},
                                session_id="session-a", turn_id="turn-a")
    record = ctx.state.data["entries"]["turn:session-a:turn-a"]
    assert record["owner"] == "p:session-a"
    assert len(ctx.messages) == 1
    _row(ctx.settings["liveness_db"], "proc_late", owner="p:session-b",
         label="foreign secret label", phase="foreign secret phase",
         detail="foreign secret detail")
    time.sleep(2.5)
    assert polled == []
    assert all("foreign secret" not in ctx.messages[0].text for _ in [0])
    for callback in ctx.unload_callbacks:
        callback()


def test_restart_unknown_task_keeps_foreign_owner_data_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-owner")
    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                result={"session_id": "proc_restart_late"},
                                session_id="session-a", turn_id="turn-a")
    checkpoint = dict(ctx.state.data)
    assert checkpoint["entries"]["turn:session-a:turn-a"]["owner"] == "p:session-a"
    from agentcore.card_store import CardStore
    initial_card = CardStore(tmp_path / "state" / "cards.db").load("turn:session-a:turn-a")
    assert initial_card is not None
    for callback in ctx.unload_callbacks:
        callback()

    _row(ctx.settings["liveness_db"], "proc_restart_late", owner="p:session-b",
         label="restart foreign secret", phase="restart secret phase",
         detail="restart secret detail")
    resumed = []
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda ctx, route, message_id, text:
                        (resumed.append(text) or {"ok": True}))
    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = checkpoint
    plugin.register(second)
    assert _wait(lambda: bool(resumed))
    assert "restart foreign secret" not in resumed[0]
    assert "restart secret phase" not in resumed[0]
    assert "restart secret detail" not in resumed[0]
    for callback in second.unload_callbacks:
        callback()


def test_background_task_from_another_session_is_not_projected(tmp_path):
    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    ledger = LivenessLedger(ctx.settings["liveness_db"])
    ledger.register("proc_private", kind="terminal", owner="p:session-b",
                    label="private task label", pid=999999, pid_start="dead:1",
                    state="running", phase="private phase", detail="private detail")
    ctx.hooks["post_tool_call"](
        tool_name="terminal", result={"session_id": "proc_private"},
        session_id="session-a", turn_id="turn-a")
    assert ctx.messages == []
    assert ctx.state.data.get("entries", {}) == {}
    for callback in ctx.unload_callbacks:
        callback()


def test_one_message_per_task_edited_in_place_until_terminal(tmp_path):
    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    ledger = _row(ctx.settings["liveness_db"], "proc_aaa", phase="collect")
    spawn = json.dumps({"output": "Background process started", "session_id": "proc_aaa",
                        "pid": 999999, "exit_code": 0, "notify_on_complete": True})
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                args={"command": "pytest tests", "timeout": 300},
                                result=spawn, session_id="s")
    assert len(ctx.messages) == 1, "the first observation must open exactly one entry"
    assert "已接收" in ctx.messages[0].text or "进行中" in ctx.messages[0].text

    ledger.beat("proc_aaa", phase="link", progress=(2, 4))
    assert _wait(lambda: any("link" in edit for edit in ctx.messages[0].edits)), ctx.messages[0].edits
    edits_after_change = len(ctx.messages[0].edits)

    # A poll with no new evidence must not edit again (no per-tick chatter).
    time.sleep(2.5)
    assert len(ctx.messages[0].edits) == edits_after_change, ctx.messages[0].edits

    ledger.finish("proc_aaa", "done", tail="ok")
    assert _wait(lambda: any("已完成" in edit for edit in ctx.messages[0].edits)), ctx.messages[0].edits
    assert len(ctx.messages) == 1, "a terminal update must edit, never post a second entry"

    edits = len(ctx.messages[0].edits)
    time.sleep(2.5)
    assert len(ctx.messages[0].edits) == edits, "terminal is write-once"
    for callback in ctx.unload_callbacks:
        callback()


def test_a_lost_process_is_reported_as_unfinished_not_running(tmp_path):
    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    ledger = _row(ctx.settings["liveness_db"], "proc_bbb", phase="run")
    ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "sleep 30"},
                                result={"yielded_session_id": "proc_bbb", "pid": 999999},
                                session_id="s")
    assert len(ctx.messages) == 1
    ledger.sweep()  # the pid is gone → state becomes lost
    assert _wait(lambda: any("未完成" in edit for edit in ctx.messages[0].edits)), ctx.messages[0].edits
    assert any("进程已不在" in edit or "未完成" in edit for edit in ctx.messages[0].edits)
    for callback in ctx.unload_callbacks:
        callback()


def test_no_conversation_means_no_entry_and_no_crash(tmp_path):
    ctx = Ctx(tmp_path, _settings(tmp_path), refuse=True)
    plugin.register(ctx)
    _row(ctx.settings["liveness_db"], "proc_ccc")
    ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "pytest"},
                                result={"session_id": "proc_ccc"}, session_id="s")
    assert ctx.messages == []
    for callback in ctx.unload_callbacks:
        callback()


def test_restart_freezes_the_recorded_entry_instead_of_opening_a_second(tmp_path):
    db = tmp_path / "liveness.db"
    first = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(first)
    _row(db, "proc_ddd", phase="run")
    first.hooks["post_tool_call"](tool_name="terminal", args={"command": "pytest"},
                                  result={"session_id": "proc_ddd"}, session_id="s")
    assert len(first.messages) == 1
    for callback in first.unload_callbacks:
        callback()
    routes = (first.state.data.get("entries") or {}).get("turn:s:proc_ddd") or {}
    assert ((first.state.data.get("entries") or {}).get("turn:s:proc_ddd") or {}).get("session_key") == ""

    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = dict(first.state.data)
    plugin.register(second)
    time.sleep(2.5)
    assert second.messages == [], "a restart must not post a duplicate entry"
    for callback in second.unload_callbacks:
        callback()

def test_a_bound_route_is_persisted_and_a_restart_freezes_without_duplicates(tmp_path, monkeypatch):
    """With a bound session key the route is recorded; resume without a gateway freezes."""
    import sys
    import types as _types

    gateway = _types.ModuleType("gateway")
    gateway.__path__ = []
    session_context = _types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda key, default="": (
        "agent:main:feishu:dm:chat-1" if key == "HERMES_SESSION_KEY" else default)
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)

    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    _row(ctx.settings["liveness_db"], "proc_eee", phase="run")
    ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "pytest"},
                                result={"session_id": "proc_eee"}, session_id="s")
    assert ctx.state.data["entries"]["turn:s:proc_eee"]["session_key"] == "agent:main:feishu:dm:chat-1"
    for callback in ctx.unload_callbacks:
        callback()

    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = dict(ctx.state.data)
    # Keep the fake gateway module installed so resume *attempts* and fails cleanly.
    plugin.register(second)
    assert _wait(lambda: (second.state.data.get("entry_frozen") or {}).get("entry") == "turn:s:proc_eee")
    assert second.messages == []
    for callback in second.unload_callbacks:
        callback()


def test_a_task_that_finished_before_the_entry_opened_is_terminal_immediately(tmp_path):
    """No stale "已接收" when the spawn receipt arrives after the process already exited."""
    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    ledger = _row(ctx.settings["liveness_db"], "proc_fff", phase="run")
    ledger.finish("proc_fff", "done", tail="ok")
    ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "pytest"},
                                result={"session_id": "proc_fff"}, session_id="s")
    assert len(ctx.messages) == 1
    assert "已完成" in ctx.messages[0].text, ctx.messages[0].text
    time.sleep(2.5)
    assert ctx.messages[0].edits == [], "an already-terminal entry is never edited again"
    for callback in ctx.unload_callbacks:
        callback()


def test_the_real_exit_from_the_host_registry_is_recorded_and_shown(tmp_path, monkeypatch):
    """With notify off nothing else closes the row; the registry's exit is the evidence."""
    class Registry:
        def __init__(self):
            self.calls = []

        def poll(self, task_id):
            self.calls.append(task_id)
            return {"status": "exited", "exit_code": 0, "output_preview": "ok"}

    registry = Registry()
    monkeypatch.setattr(plugin, "_registry", lambda: registry)

    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    _row(ctx.settings["liveness_db"], "proc_ggg", phase="run")
    ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "pytest"},
                                result={"session_id": "proc_ggg"}, session_id="s")
    assert len(ctx.messages) == 1

    assert _wait(lambda: any("已完成" in edit for edit in ctx.messages[0].edits)), \
        (ctx.messages[0].edits, registry.calls)
    assert "proc_ggg" in registry.calls
    assert LivenessLedger(ctx.settings["liveness_db"]).get("proc_ggg")["state"] == "done"
    for callback in ctx.unload_callbacks:
        callback()


def test_an_unroutable_finished_entry_is_closed_without_an_edit(tmp_path):
    """No route to edit through, but a finished task must not stay active in the store."""
    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    ledger = _row(ctx.settings["liveness_db"], "proc_hhh", phase="run")
    spawn = {"session_id": "proc_hhh"}
    ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "pytest"},
                                result=spawn, session_id="s")
    assert len(ctx.messages) == 1
    for callback in ctx.unload_callbacks:
        callback()
    ledger.finish("proc_hhh", "done", tail="ok")

    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = dict(ctx.state.data)
    plugin.register(second)
    store = plugin._store(second, plugin._engine_modules(str(ROOT)), _settings(tmp_path))
    assert _wait(lambda: (store.load("turn:s:proc_hhh") or None) is not None
                 and store.load("turn:s:proc_hhh").state.value == "done")
    assert second.messages == []
    for callback in second.unload_callbacks:
        callback()


def test_slash_commands_answer_from_the_ledger_without_a_model_turn(tmp_path, monkeypatch):
    """`/tasks` and `/task` are the reliable, non-model progress path."""
    import sys
    import types as _types

    gateway = _types.ModuleType("gateway")
    gateway.__path__ = []
    session_context = _types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda key, default="": (
        "sess-1" if key == "HERMES_SESSION_ID" else "route-1" if key == "HERMES_SESSION_KEY" else default)
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)

    class CommandCtx(Ctx):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.commands = {}

        def register_command(self, name, handler, description, **kwargs):
            self.commands[name] = handler

    ctx = CommandCtx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    assert set(ctx.commands) == {"longtasks", "longtask", "status", "pause", "cancel"}

    ledger = LivenessLedger(ctx.settings["liveness_db"])
    ledger.register("proc_zzz", kind="terminal", owner="p:sess-1", label="pytest tests",
                    pid=999999, pid_start="dead:1", state="running", phase="compile")
    ledger.register("proc_other", kind="terminal", owner="p:another", state="running")
    long_id = "proc_" + "a" * 40
    ledger.register(long_id, kind="terminal", owner="p:sess-1", label="long id", state="running")
    ctx.hooks["pre_llm_call"](session_id="sess-1", turn_id="visible-turn")
    ctx.hooks["pre_llm_call"](session_id="another", turn_id="private-turn")
    listed = ctx.commands["longtasks"]()
    assert "proc_zzz" in listed and "compile" in listed
    assert long_id in listed
    import re
    foreground_id = re.search(r"`(turn_[0-9a-f]{24})`", listed).group(1)
    assert "等待模型响应" in ctx.commands["longtask"](foreground_id)
    assert "long id" in ctx.commands["longtask"](long_id)
    assert len(re.findall(r"`turn_[0-9a-f]{24}`", listed)) == 1
    assert "proc_other" not in listed, "a session must not see another session's tasks"

    detail = ctx.commands["longtask"]("proc_zzz")
    assert "状态：进行中" in detail and "proc_zzz" in detail and "pytest tests" in detail
    assert "unknown_task" in ctx.commands["longtask"]("proc_other")
    assert "用法" in ctx.commands["longtask"]("")
    session_context.get_session_env = lambda key, default="": default
    assert "无法确认当前会话" in ctx.commands["longtasks"]()
    assert "无法确认当前会话" in ctx.commands["longtask"]("proc_zzz")
    for callback in ctx.unload_callbacks:
        callback()

def test_concurrent_same_turn_spawn_opens_only_one_card(tmp_path):
    import threading
    ctx = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    _row(ctx.settings["liveness_db"], "proc_race", phase="run")
    barrier = threading.Barrier(3)

    def spawn():
        barrier.wait()
        ctx.hooks["post_tool_call"](tool_name="terminal", result={"session_id": "proc_race"},
                                    session_id="s", turn_id="t")

    workers = [threading.Thread(target=spawn) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    assert len(ctx.messages) == 1
    assert ctx.state.data["entries"]["turn:s:t"]["send_state"] == "sent"
    for callback in ctx.unload_callbacks:
        callback()


def test_foreground_threshold_captures_route_per_session(tmp_path, monkeypatch):
    routes = {"current": "route-a"}
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: routes["current"])
    posted, edited = [], []

    def post(ctx, route, text, *, allow_bound_context=False):
        posted.append((route, text))
        return {"ok": True, "message_id": "msg-" + route}

    def edit(ctx, route, message_id, text):
        edited.append((route, message_id, text))
        return {"ok": True, "edited": True, "message_id": message_id}

    monkeypatch.setattr(plugin, "_post_to_session", post)
    monkeypatch.setattr(plugin, "_edit_to_session", edit)
    ctx = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    plugin.register(ctx)
    ctx.hooks["pre_llm_call"](session_id="s1", turn_id="t1")
    routes["current"] = "route-b"
    ctx.hooks["pre_llm_call"](session_id="s2", turn_id="t2")
    assert _wait(lambda: {row[0] for row in posted} == {"route-a", "route-b"})
    assert all("user" not in row[1].lower() for row in posted)

    ctx.hooks["on_session_end"](session_id="s1", turn_id="t1", completed=True)
    assert _wait(lambda: any(row[0] == "route-a" for row in edited))
    assert all(message_id == "msg-" + route for route, message_id, _ in edited)
    assert len(posted) == 2, "threshold emits one card per long foreground turn"
    for callback in ctx.unload_callbacks:
        callback()


def test_background_tasks_arriving_during_first_send_are_merged(tmp_path):
    import threading
    entered, release = threading.Event(), threading.Event()

    class BlockingCtx(Ctx):
        def post_message(self, text):
            entered.set()
            assert release.wait(5)
            return super().post_message(text)

    ctx = BlockingCtx(tmp_path, _settings(tmp_path))
    plugin.register(ctx)
    _row(ctx.settings["liveness_db"], "proc_first", phase="run")
    _row(ctx.settings["liveness_db"], "proc_second", phase="run")
    first = threading.Thread(target=lambda: ctx.hooks["post_tool_call"](
        tool_name="terminal", result={"session_id": "proc_first"}, session_id="s", turn_id="t"))
    first.start()
    assert entered.wait(3)
    ctx.hooks["post_tool_call"](tool_name="terminal", result={"session_id": "proc_second"},
                                session_id="s", turn_id="t")
    assert ctx.state.data["entries"]["turn:s:t"]["pending_tasks"] == ["proc_second"]
    release.set()
    first.join()
    assert len(ctx.messages) == 1
    assert _wait(lambda: set(ctx.state.data["entries"]["turn:s:t"]["tasks"])
                 == {"proc_first", "proc_second"})
    for callback in ctx.unload_callbacks:
        callback()


@pytest.mark.parametrize(("terminal", "expected"), [( {"completed": True}, "已完成"), ({"failed": True}, "未完成")])
def test_foreground_terminal_arriving_during_blocked_send_is_applied(tmp_path, monkeypatch,
                                                                      terminal, expected):
    import threading
    entered, release = threading.Event(), threading.Event()
    posted, edited = [], []
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-a")

    def post(ctx, route, text, *, allow_bound_context=False):
        entered.set()
        assert release.wait(5)
        posted.append(text)
        return {"ok": True, "message_id": "msg-a"}

    def edit(ctx, route, message_id, text):
        edited.append(text)
        return {"ok": True, "edited": True, "message_id": message_id}

    monkeypatch.setattr(plugin, "_post_to_session", post)
    monkeypatch.setattr(plugin, "_edit_to_session", edit)
    ctx = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    plugin.register(ctx)
    ctx.hooks["pre_llm_call"](session_id="s", turn_id="t")
    assert entered.wait(4)
    ctx.hooks["on_session_end"](session_id="s", turn_id="t", **terminal)
    release.set()
    assert _wait(lambda: any(expected in text for text in edited))
    assert len(posted) == 1
    assert all("进行中" not in text or expected not in text for text in [posted[0]])
    for callback in ctx.unload_callbacks:
        callback()


def test_terminal_hook_does_not_race_or_block_slow_running_edit(tmp_path, monkeypatch):
    """The sole poller finishes the in-flight edit before it publishes terminal state."""
    entered, release = threading.Event(), threading.Event()
    posted, edits = [], []
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-serial")

    def post(ctx, route, text, *, allow_bound_context=False):
        posted.append(text)
        return {"ok": True, "message_id": "msg-serial"}

    def edit(ctx, route, message_id, text):
        assert message_id == "msg-serial"
        if not edits:
            entered.set()
            assert release.wait(5), "controlled edit release timed out"
        edits.append(text)
        return {"ok": True, "edited": True, "message_id": message_id}

    monkeypatch.setattr(plugin, "_post_to_session", post)
    monkeypatch.setattr(plugin, "_edit_to_session", edit)
    ctx = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    plugin.register(ctx)
    ctx.hooks["pre_llm_call"](session_id="s", turn_id="t")
    assert _wait(lambda: bool(ctx.state.data.get("entries", {}).get("turn:s:t", {}).get("message_id")))
    _row(ctx.settings["liveness_db"], "proc_serial", owner="p:s", phase="running")
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                result={"session_id": "proc_serial"},
                                session_id="s", turn_id="t")
    assert entered.wait(5), "running edit did not start"

    start = time.monotonic()
    ctx.hooks["on_session_end"](session_id="s", turn_id="t", completed=True)
    assert time.monotonic() - start < 0.5, "session-end hook waited for transport"
    assert edits == [], "terminal state must wait behind the in-flight edit"

    release.set()
    assert _wait(lambda: len(edits) >= 2), edits
    assert "已完成" not in edits[0]
    assert "已完成" in edits[-1]
    assert "等待工具结果" not in edits[-1]
    for callback in ctx.unload_callbacks:
        callback()


def test_late_edit_coroutine_is_serialized_before_same_id_terminal_edit(monkeypatch):
    """A timed-out old coroutine keeps the per-message lock until it really returns."""
    import asyncio
    import sys
    import types

    entered, release = threading.Event(), threading.Event()
    completed = []
    loop = asyncio.new_event_loop()
    runner = types.SimpleNamespace(_gateway_loop=loop)
    gateway = types.ModuleType("gateway")
    run = types.ModuleType("gateway.run")
    run._gateway_runner_ref = lambda: runner
    messages = types.ModuleType("gateway.plugin_messages")
    messages._supports_edit = lambda adapter: True
    messages._fit = lambda adapter, chat, text: text
    gateway.run = run
    gateway.plugin_messages = messages

    module = types.ModuleType("hermes_plugins.feishu_platform__home_probe.adapter")
    module.__file__ = "/opt/hermes/plugins/platforms/feishu/adapter.py"
    module.card_renders_markdown = lambda content: True

    class FeishuAdapter:
        calls = 0

        def _is_card_message(self, message_id):
            return False

        async def edit_message(self, chat_id, message_id, text):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            completed.append(text)
            return types.SimpleNamespace(success=True, message_id=message_id)

    FeishuAdapter.__module__ = module.__name__
    adapter = FeishuAdapter()
    target = types.SimpleNamespace(platform="lark", chat_id="chat", live=None,
                                   adapter=adapter)
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.run", run)
    monkeypatch.setitem(sys.modules, "gateway.plugin_messages", messages)
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def loop_main():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=loop_main, daemon=True)
    thread.start()
    try:
        with pytest.raises(plugin._EditOutcomeUnknown):
            plugin._run_edit_on_loop(plugin._edit_only(
                target, "stable-id", "running", plugin_id="task-progress"), timeout=0.05)
        assert entered.wait(2)
        with pytest.raises(plugin._EditOutcomeUnknown):
            plugin._run_edit_on_loop(plugin._edit_only(
                target, "stable-id", "terminal", plugin_id="task-progress"), timeout=0.05)
        assert adapter.calls == 1, "second edit must remain behind the unknown first edit"
        release.set()
        assert plugin._run_edit_on_loop(asyncio.sleep(0.15, result="settled")) == "settled"
        assert completed == ["running", "terminal"]
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_unknown_edit_outcome_freezes_entry_and_does_not_retry(tmp_path, monkeypatch):
    """An ambiguous edit is persisted as unknown and never blindly retried."""
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-unknown")
    monkeypatch.setattr(plugin, "_post_to_session",
                        lambda ctx, route, text, **kwargs:
                        {"ok": True, "message_id": "msg-unknown"})
    attempts = []

    def ambiguous(ctx, route, message_id, text):
        attempts.append(text)
        raise plugin._EditOutcomeUnknown()

    monkeypatch.setattr(plugin, "_edit_to_session", ambiguous)
    ctx = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    plugin.register(ctx)
    ctx.hooks["pre_llm_call"](session_id="s", turn_id="t")
    assert _wait(lambda: bool(ctx.state.data.get("entries", {}).get("turn:s:t", {}).get("message_id")))
    _row(ctx.settings["liveness_db"], "proc_unknown", owner="p:s", phase="running")
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                result={"session_id": "proc_unknown"},
                                session_id="s", turn_id="t")
    assert _wait(lambda: ctx.state.data.get("entries", {}).get("turn:s:t", {}).get("edit_state") == "unknown")
    count = len(attempts)
    time.sleep(2.2)
    assert len(attempts) == count
    for callback in ctx.unload_callbacks:
        callback()


def test_active_edit_requires_persisted_sending_intent_before_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-intent")
    attempts = []

    class IntentFailState(FakeState):
        def set(self, key, value):
            if key == "entries" and any(
                    isinstance(record, dict) and record.get("edit_state") == "sending"
                    for record in value.values()):
                raise OSError("intent store unavailable")
            super().set(key, value)

    ctx = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    ctx.state = IntentFailState(tmp_path / "state")
    _LIVE.append(ctx)
    monkeypatch.setattr(plugin, "_post_to_session",
                        lambda ctx, route, text, **kwargs:
                        {"ok": True, "message_id": "msg-intent"})
    monkeypatch.setattr(plugin, "_edit_to_session",
                        lambda *args, **kwargs: attempts.append(args) or {"ok": True})
    plugin.register(ctx)
    ctx.hooks["pre_llm_call"](session_id="s", turn_id="t")
    assert _wait(lambda: bool(ctx.state.data.get("entries", {}).get("turn:s:t", {}).get("message_id")))
    _row(ctx.settings["liveness_db"], "proc_intent", owner="p:s", phase="running")
    ctx.hooks["post_tool_call"](tool_name="terminal", result={"session_id": "proc_intent"},
                                 session_id="s", turn_id="t")
    time.sleep(2.3)
    assert attempts == []
    assert ctx.state.data["entries"]["turn:s:t"]["edit_state"] == "confirmed"
    for callback in ctx.unload_callbacks:
        callback()


def test_edit_crash_after_write_ahead_is_unknown_on_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-crash-edit")
    monkeypatch.setattr(plugin, "_post_to_session",
                        lambda ctx, route, text, **kwargs:
                        {"ok": True, "message_id": "msg-crash-edit"})
    entered, release = threading.Event(), threading.Event()

    def block_after_dispatch(ctx, route, message_id, text):
        assert ctx.state.data["entries"]["turn:s:t"]["edit_state"] == "sending"
        entered.set()
        assert release.wait(5)
        return {"ok": True, "message_id": message_id}

    monkeypatch.setattr(plugin, "_edit_to_session", block_after_dispatch)
    first = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    plugin.register(first)
    first.hooks["pre_llm_call"](session_id="s", turn_id="t")
    assert _wait(lambda: bool(first.state.data.get("entries", {}).get("turn:s:t", {}).get("message_id")))
    _row(first.settings["liveness_db"], "proc_crash_edit", owner="p:s", phase="running")
    first.hooks["post_tool_call"](tool_name="terminal", result={"session_id": "proc_crash_edit"},
                                  session_id="s", turn_id="t")
    assert entered.wait(5), "edit did not reach the post-intent dispatch window"
    assert first.state.data["entries"]["turn:s:t"]["edit_state"] == "sending"
    # This durable snapshot represents a crash at the point after dispatch intent
    # and before a response/confirmation is written.
    snapshot = dict(first.state.data)
    release.set()
    for callback in first.unload_callbacks:
        callback()

    attempts = []
    second = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    second.state.data = snapshot
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda *args, **kwargs: attempts.append(args) or {"ok": True})
    plugin.register(second)
    assert _wait(lambda: second.state.data["entries"]["turn:s:t"].get("edit_state") == "unknown")
    assert attempts == []
    assert second.state.data["entry_frozen"]["entry"] == "turn:s:t"
    for callback in second.unload_callbacks:
        callback()


def test_resume_writes_sending_before_edit_and_retries_explicit_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-resume-intent")
    first = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(first)
    _row(first.settings["liveness_db"], "proc_resume_intent", owner="p:s", phase="running")
    first.hooks["post_tool_call"](tool_name="terminal",
                                  result={"session_id": "proc_resume_intent"},
                                  session_id="s", turn_id="t")
    snapshot = dict(first.state.data)
    for callback in first.unload_callbacks:
        callback()

    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = snapshot
    seen = []

    def explicit_rejection(ctx, route, message_id, text):
        seen.append(ctx.state.data["entries"]["turn:s:t"]["edit_state"])
        return {"ok": False, "error": "known_rejection", "retry_safe": True}

    monkeypatch.setattr(plugin, "_resume_edit", explicit_rejection)
    plugin.register(second)
    assert _wait(lambda: bool(seen))
    assert seen == ["sending"]
    assert second.state.data["entries"]["turn:s:t"]["edit_state"] == "confirmed"
    for callback in second.unload_callbacks:
        callback()

    third = Ctx(tmp_path, _settings(tmp_path))
    third.state.data = dict(second.state.data)
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda ctx, route, message_id, text:
                        (seen.append(ctx.state.data["entries"]["turn:s:t"]["edit_state"])
                         or {"ok": True}))
    plugin.register(third)
    assert _wait(lambda: len(seen) == 2)
    assert seen[-1] == "sending"
    assert third.state.data["entries"]["turn:s:t"]["edit_state"] == "confirmed"
    for callback in third.unload_callbacks:
        callback()


def test_restart_rehydrates_active_entry_until_same_id_terminal_update(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-rehydrate")
    first = Ctx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    plugin.register(first)
    _row(first.settings["liveness_db"], "proc_rehydrate", owner="p:s", phase="compile")
    first.hooks["post_tool_call"](tool_name="terminal",
                                  result={"session_id": "proc_rehydrate"},
                                  session_id="s", turn_id="t")
    record = first.state.data["entries"]["turn:s:t"]
    assert record["message_id"] == "om_1"
    assert record["owner"] == "p:s"
    for callback in first.unload_callbacks:
        callback()

    second = Ctx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    second.state.data = dict(first.state.data)
    resumed = []
    active_edits = []
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda ctx, route, message_id, text:
                        (resumed.append((route, message_id, text)) or {"ok": True}))
    monkeypatch.setattr(plugin, "_edit_to_session",
                        lambda ctx, route, message_id, text:
                        (active_edits.append((route, message_id, text))
                         or {"ok": True, "message_id": message_id, "edited": True}))
    plugin.register(second)
    assert _wait(lambda: bool(resumed))
    assert resumed[0][:2] == ("route-rehydrate", "om_1")
    assert second.state.data["entries"]["turn:s:t"]["edit_state"] == "confirmed"

    LivenessLedger(second.settings["liveness_db"]).finish("proc_rehydrate", "done")
    assert _wait(lambda: any(route == "route-rehydrate" and message_id == "om_1"
                             and "已完成" in text
                             for route, message_id, text in active_edits), timeout=6)
    assert all(message_id == "om_1" for _, message_id, _ in active_edits)
    assert "foreign" not in "".join(text for _, _, text in active_edits)
    for callback in second.unload_callbacks:
        callback()


def test_restart_rehydration_keeps_foreign_owner_snapshot_private(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-private-resume")
    first = Ctx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    plugin.register(first)
    _row(first.settings["liveness_db"], "proc_private_resume", owner="p:s",
         label="initial private task", phase="compile")
    first.hooks["post_tool_call"](tool_name="terminal",
                                  result={"session_id": "proc_private_resume"},
                                  session_id="s", turn_id="t")
    snapshot = dict(first.state.data)
    LivenessLedger(first.settings["liveness_db"]).register(
        "proc_private_resume", kind="terminal", owner="other:session",
        label="FOREIGN SECRET LABEL", state="running", pid=999999,
        pid_start="foreign:1", phase="FOREIGN SECRET PHASE")
    for callback in first.unload_callbacks:
        callback()

    second = Ctx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    second.state.data = snapshot
    texts = []
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda ctx, route, message_id, text:
                        (texts.append(text) or {"ok": True}))
    monkeypatch.setattr(plugin, "_edit_to_session",
                        lambda ctx, route, message_id, text:
                        (texts.append(text) or {"ok": True, "message_id": message_id, "edited": True}))
    plugin.register(second)
    assert _wait(lambda: bool(texts))
    assert all("FOREIGN SECRET" not in text for text in texts)
    for callback in second.unload_callbacks:
        callback()



def test_completed_first_task_does_not_terminal_lock_card_before_pending_running_task(tmp_path):
    import threading
    entered, release = threading.Event(), threading.Event()

    class BlockingCtx(Ctx):
        def post_message(self, text):
            entered.set()
            assert release.wait(5)
            return super().post_message(text)

    ctx = BlockingCtx(tmp_path, _settings(tmp_path, poll_seconds=2, min_interval=0))
    plugin.register(ctx)
    first = _row(ctx.settings["liveness_db"], "proc_complete", phase="run")
    first.finish("proc_complete", "done")
    _row(ctx.settings["liveness_db"], "proc_still_running", phase="compile")
    worker = threading.Thread(target=lambda: ctx.hooks["post_tool_call"](
        tool_name="terminal", result={"session_id": "proc_complete"},
        session_id="s", turn_id="t"))
    worker.start()
    assert entered.wait(3)
    ctx.hooks["post_tool_call"](tool_name="terminal",
                                result={"session_id": "proc_still_running"},
                                session_id="s", turn_id="t")
    release.set()
    worker.join()
    assert len(ctx.messages) == 1
    assert _wait(lambda: any("进行中" in edit and "2 个后台任务" in edit
                             for edit in ctx.messages[0].edits))
    record = ctx.state.data["entries"]["turn:s:t"]
    assert record["tasks"] == ["proc_complete", "proc_still_running"]
    assert record["send_state"] == "sent"
    for callback in ctx.unload_callbacks:
        callback()


def test_re_register_restores_foreground_as_pending_confirmation(tmp_path, monkeypatch):
    routes = {"current": "route-persist"}
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: routes["current"])
    posted = []
    def post(ctx, route, text, *, allow_bound_context=False):
        posted.append((route, text))
        return {"ok": True, "message_id": "msg-persist"}
    monkeypatch.setattr(plugin, "_post_to_session", post)

    first = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    plugin.register(first)
    first.hooks["pre_llm_call"](session_id="s-persist", turn_id="t-persist")
    assert _wait(lambda: bool(posted))
    record = first.state.data["entries"]["turn:s-persist:t-persist"]
    assert record["foreground"]["state"] == "running"
    assert record["session_key"] == "route-persist"
    for callback in first.unload_callbacks:
        callback()

    second = Ctx(tmp_path, _settings(tmp_path, foreground_min_seconds=0, poll_seconds=2))
    second.state.data = dict(first.state.data)
    resumed = []
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda ctx, route, message_id, text:
                        (resumed.append((route, message_id, text)) or {"ok": True}))
    plugin.register(second)
    assert _wait(lambda: bool(resumed))
    assert resumed[0][0:2] == ("route-persist", "msg-persist")
    assert "状态待确认" in resumed[0][2] and "已完成" not in resumed[0][2]
    assert second.messages == []
    for callback in second.unload_callbacks:
        callback()


def test_posted_message_id_recovers_card_bind_after_bind_failure(tmp_path, monkeypatch):
    from agentcore.card_store import CardStore

    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-recover")
    original_bind = CardStore.bind
    CardStore.bind = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated bind crash"))
    first = Ctx(tmp_path, _settings(tmp_path))
    plugin.register(first)
    _row(first.settings["liveness_db"], "proc_recover", owner="p:s-recover", phase="compile")
    first.hooks["post_tool_call"](tool_name="terminal", result={"session_id": "proc_recover"},
                                  session_id="s-recover", turn_id="t-recover")
    saved = first.state.data["entries"]["turn:s-recover:t-recover"]
    assert saved["send_state"] == "posted"
    assert saved["message_id"] == "om_1"
    assert saved["session_key"] == "route-recover"
    assert saved["tasks"] == ["proc_recover"]
    assert CardStore(tmp_path / "state" / "cards.db").load("turn:s-recover:t-recover") is None
    for callback in first.unload_callbacks:
        callback()

    CardStore.bind = original_bind
    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = dict(first.state.data)
    resumed = []
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda ctx, route, message_id, text:
                        (resumed.append((route, message_id, text)) or {"ok": True}))
    plugin.register(second)
    assert _wait(lambda: bool(resumed))
    assert resumed[0][:2] == ("route-recover", "om_1")
    assert "pytest tests" in resumed[0][2]
    recovered = CardStore(tmp_path / "state" / "cards.db").load("turn:s-recover:t-recover")
    assert recovered is not None and recovered.message_id == "om_1"
    assert second.messages == []
    for callback in second.unload_callbacks:
        callback()


def test_re_register_recovers_after_bind_before_final_state_write_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_capture_session_key", lambda: "route-window")
    first = Ctx(tmp_path, _settings(tmp_path))
    class FailingState(FakeState):
        def __init__(self, path):
            super().__init__(path)
            self.entry_writes = 0
            self.failed = False
        def set(self, key, value):
            if key == "entries":
                self.entry_writes += 1
                if self.entry_writes >= 3:
                    self.failed = True
                    raise OSError("simulated crash after card bind")
            if not self.failed:
                super().set(key, value)
    first.state = FailingState(tmp_path / "state")
    plugin.register(first)
    _row(first.settings["liveness_db"], "proc_window", owner="p:s-window", phase="run")
    first.hooks["post_tool_call"](tool_name="terminal", result={"session_id": "proc_window"},
                                  session_id="s-window", turn_id="t-window")
    assert first.state.failed
    checkpoint = first.state.data["entries"]["turn:s-window:t-window"]
    assert checkpoint["message_id"] == "om_1"
    assert checkpoint["tasks"] == ["proc_window"]
    for callback in first.unload_callbacks:
        callback()

    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = dict(first.state.data)
    resumed = []
    monkeypatch.setattr(plugin, "_resume_edit",
                        lambda ctx, route, message_id, text:
                        (resumed.append((route, message_id, text)) or {"ok": True}))
    plugin.register(second)
    assert _wait(lambda: bool(resumed))
    assert resumed[0][:2] == ("route-window", "om_1")
    assert "pytest tests" in resumed[0][2]
    assert second.messages == []
    for callback in second.unload_callbacks:
        callback()

def test_unknown_post_before_message_id_checkpoint_freezes_without_reposting(tmp_path, monkeypatch):
    from agentcore.card_store import CardStore
    original_bind = CardStore.bind
    monkeypatch.setattr(CardStore, "bind",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated bind failure")))
    first = Ctx(tmp_path, _settings(tmp_path))
    class ReceiptFailState(FakeState):
        def __init__(self, path):
            super().__init__(path)
            self.entry_writes = 0
        def set(self, key, value):
            if key == "entries":
                self.entry_writes += 1
                if self.entry_writes == 2:
                    raise OSError("simulated crash before receipt checkpoint")
            super().set(key, value)
    first.state = ReceiptFailState(tmp_path / "state")
    plugin.register(first)
    _row(first.settings["liveness_db"], "proc_unknown", owner="p:s-unknown", phase="run")
    first.hooks["post_tool_call"](tool_name="terminal", result={"session_id": "proc_unknown"},
                                  session_id="s-unknown", turn_id="t-unknown")
    record = first.state.data["entries"]["turn:s-unknown:t-unknown"]
    assert record["send_state"] == "unknown", (record, first.state.entry_writes)
    assert "message_id" not in record
    assert len(first.messages) == 1  # transport succeeded but both durable receipt paths failed
    for callback in first.unload_callbacks:
        callback()

    monkeypatch.setattr(CardStore, "bind", original_bind)
    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = dict(first.state.data)
    plugin.register(second)
    assert _wait(lambda: (second.state.data.get("entry_frozen") or {}).get("entry")
                 == "turn:s-unknown:t-unknown")
    assert (second.state.data["entry_frozen"]["error"]
            == "send_outcome_unknown_no_message_id")
    assert second.messages == []
    for callback in second.unload_callbacks:
        callback()


def test_edit_lock_table_stays_bounded():
    """编辑锁表必须有上限。

    key 里带 message_id，长跑网关会为每条消息留一把锁且从不回收 ——
    这是常驻内存里一处确定的单调增长。
    """
    import asyncio

    async def exercise():
        class _Target:
            platform = 'p'
            chat_id = 'c'

        target = _Target()
        loop = asyncio.get_running_loop()
        for index in range(plugin._MAX_EDIT_LOCKS + 50):
            plugin._edit_lock_for(target, 'm%d' % index)
        with plugin._EDIT_LOCKS_GUARD:
            per_loop = plugin._EDIT_LOCKS.setdefault(loop, {})
            assert len(per_loop) <= plugin._MAX_EDIT_LOCKS

    asyncio.run(exercise())


def test_a_held_edit_lock_is_never_evicted():
    """正在被持有的锁不能被回收 —— 换掉它等于失去互斥。"""
    import asyncio

    async def exercise():
        class _Target:
            platform = 'p'
            chat_id = 'c'

        target = _Target()
        lock = plugin._edit_lock_for(target, 'held')
        await lock.acquire()
        try:
            with plugin._EDIT_LOCKS_GUARD:
                per_loop = plugin._EDIT_LOCKS.setdefault(asyncio.get_running_loop(), {})
                plugin._trim_edit_locks_locked(per_loop)
                assert per_loop.get(('p', 'c', 'held')) is lock
        finally:
            lock.release()

    asyncio.run(exercise())

"""task-progress plugin: one entry per long task, edits in place, terminal write-once.

The fake host below is deliberately minimal: a ``post_message`` handle that records edits,
a durable plugin-state dict, and the real SQLite liveness ledger in a tmp dir.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "task_progress_plugin", ROOT / "plugins" / "task_progress" / "__init__.py")
assert _spec and _spec.loader
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

from agentcore.liveness import LivenessLedger  # noqa: E402


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
    ledger.register(task, kind="terminal", owner="p:s", label="pytest tests",
                    pid=999999, pid_start="dead:1", state="running", **fields)
    return ledger


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
    routes = first.state.data.get("routes") or {}
    assert routes.get("proc_ddd") is None  # no bound session env in this fake host

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
    assert ctx.state.data["routes"]["proc_eee"] == "agent:main:feishu:dm:chat-1"
    for callback in ctx.unload_callbacks:
        callback()

    second = Ctx(tmp_path, _settings(tmp_path))
    second.state.data = dict(ctx.state.data)
    # Keep the fake gateway module installed so resume *attempts* and fails cleanly.
    plugin.register(second)
    assert _wait(lambda: (second.state.data.get("entry_frozen") or {}).get("task_id") == "proc_eee")
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
    assert registry.calls and registry.calls[0] == "proc_ggg"
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
    assert _wait(lambda: plugin._store(second, plugin._engine_modules(str(ROOT)),
                                       _settings(tmp_path)).load("proc_hhh").state.value == "done")
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
        "sess-1" if key == "HERMES_SESSION_ID" else default)
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
    assert set(ctx.commands) == {"tasks", "task"}

    ledger = LivenessLedger(ctx.settings["liveness_db"])
    ledger.register("proc_zzz", kind="terminal", owner="p:sess-1", label="pytest tests",
                    pid=999999, pid_start="dead:1", state="running", phase="compile")
    ledger.register("proc_other", kind="terminal", owner="p:another", state="running")
    listed = ctx.commands["tasks"]()
    assert "proc_zzz" in listed and "compile" in listed
    assert "proc_other" not in listed, "a session must not see another session's tasks"

    detail = ctx.commands["task"]("proc_zzz")
    assert "状态：进行中" in detail and "proc_zzz" in detail and "pytest tests" in detail
    assert "unknown_task" in ctx.commands["task"]("proc_other")
    assert "用法" in ctx.commands["task"]("")
    for callback in ctx.unload_callbacks:
        callback()

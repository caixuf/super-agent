"""task_status plugin: read-only snapshots, owner-scoped, no fabricated success."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "task_status_plugin", ROOT / "plugins" / "task_status" / "__init__.py")
assert _spec and _spec.loader
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

from agentcore.liveness import LivenessLedger  # noqa: E402


class Ctx:
    profile_name = "p"

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.tools = []
        self.sections = []

    def get_config(self, key, default=None):
        if key in ("db_path", "liveness_db"):
            return self.db_path
        if key == "enabled":
            return True
        if key == "engine_root":
            return str(ROOT)
        return default

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_system_prompt_section(self, *args, **kwargs):
        self.sections.append((args, kwargs))


def _handler(ctx):
    plugin._CTX = ctx
    plugin._CACHE.clear()
    return plugin._handle


def test_inspect_reports_state_for_the_owning_session(tmp_path):
    ctx = Ctx(tmp_path / "liveness.db")
    store = LivenessLedger(ctx.db_path, stall_seconds=60)
    store.register("t1", kind="terminal", owner="p:s", label="suite", state="running")
    store.beat("t1", phase="compile", progress=(1, 4))
    out = json.loads(_handler(ctx)({"action": "inspect", "task_id": "t1"}, session_id="s"))
    assert out["ok"] is True
    assert out["verdict"] == "running"
    assert out["phase"] == "compile"
    assert out["progress"]["percent"] == 25.0


def test_cross_session_task_is_unknown(tmp_path):
    ctx = Ctx(tmp_path / "liveness.db")
    store = LivenessLedger(ctx.db_path)
    store.register("t1", kind="terminal", owner="p:other", state="running")
    out = json.loads(_handler(ctx)({"action": "inspect", "task_id": "t1"}, session_id="s"))
    assert out == {"ok": False, "error": "unknown_task", "task_id": "t1"}


def test_list_and_tail_are_bounded(tmp_path):
    ctx = Ctx(tmp_path / "liveness.db")
    store = LivenessLedger(ctx.db_path)
    store.register("t1", kind="terminal", owner="p:s", state="running")
    store.record_tail("t1", "x" * 5000, limit=80)
    handle = _handler(ctx)
    listed = json.loads(handle({"action": "list"}, session_id="s"))
    assert listed["ok"] is True and [t["task_id"] for t in listed["tasks"]] == ["t1"]
    tail = json.loads(handle({"action": "tail", "task_id": "t1"}, session_id="s"))
    assert tail["ok"] is True and len(tail["output"]) == 80


def test_register_is_opt_in_and_exposes_prompt_discipline(tmp_path):
    ctx = Ctx(tmp_path / "liveness.db")
    plugin.register(ctx)
    assert ctx.tools and ctx.tools[0]["name"] == "task_status"
    assert ctx.sections
    assert "Do not loop" in ctx.sections[0][0][1]

    class Disabled(Ctx):
        def get_config(self, key, default=None):
            if key == "enabled":
                return False
            return super().get_config(key, default)

    disabled = Disabled(tmp_path / "x.db")
    plugin.register(disabled)
    assert disabled.tools == []


def test_reconcile_persists_real_exit_once(tmp_path, monkeypatch):
    store = LivenessLedger(tmp_path / "liveness.db")
    store.register("proc_1", kind="terminal", owner="p:s", pid=123, state="running")
    task = store.get("proc_1")

    class Registry:
        calls = 0

        def poll(self, session_id):
            self.calls += 1
            return {"status": "exited", "exit_code": 0, "output_preview": "all green"}

    registry = Registry()
    monkeypatch.setattr(plugin, "_process_registry", lambda: registry)
    task = plugin._reconcile(store, task)
    assert task["state"] == "done"
    assert task["exit_code"] == 0
    assert task["tail"] == "all green"
    assert registry.calls == 1
    # A second reconcile cannot reopen the terminal row.
    again = plugin._reconcile(store, store.get("proc_1"))
    assert again["state"] == "done"


def test_list_classifies_gone_processes_before_reporting(tmp_path):
    """Asking "what am I running" must not return a dead process as `running`."""
    import os

    from agentcore.liveness import pid_start_time

    ctx = Ctx(tmp_path / "liveness.db")
    store = LivenessLedger(ctx.db_path, stall_seconds=60)
    store.register("gone", kind="terminal", owner="p:s", pid=999999,
                   pid_start="deadbeef:1", state="running")
    me = os.getpid()
    store.register("live", kind="terminal", owner="p:s", pid=me,
                   pid_start=pid_start_time(me), state="running")

    listed = json.loads(_handler(ctx)({"action": "list"}, session_id="s"))
    by_id = {task["task_id"]: task for task in listed["tasks"]}
    assert by_id["gone"]["state"] == "lost" and by_id["gone"]["verdict"] == "lost"
    assert by_id["live"]["state"] == "running" and by_id["live"]["verdict"] == "running"

    # The classification is persisted: the next reader does not have to redo it.
    persisted = LivenessLedger(ctx.db_path, stall_seconds=60).get("gone")
    assert persisted["state"] == "lost" and persisted["seq"] > 1
    assert persisted["detail"] == "process is gone"


def test_list_does_not_sweep_other_sessions(tmp_path):
    ctx = Ctx(tmp_path / 'liveness.db')
    store = LivenessLedger(ctx.db_path)
    store.register('mine', kind='terminal', owner='p:s', pid=999999,
                   pid_start='deadbeef:1', state='running')
    store.register('other', kind='terminal', owner='p:other', pid=999999,
                   pid_start='deadbeef:1', state='running')
    result = json.loads(_handler(ctx)({'action': 'list'}, session_id='s'))
    assert result['ok'] and [task['task_id'] for task in result['tasks']] == ['mine']
    assert store.get('mine')['state'] == 'lost'
    assert store.get('other')['state'] == 'running'


def test_list_marks_a_silent_pid_less_row_stalled(tmp_path):
    """A queued row with no pid and an old heartbeat is `stalled`, not silently `running`."""
    ctx = Ctx(tmp_path / "liveness.db")
    store = LivenessLedger(ctx.db_path, stall_seconds=60)
    store.register("quiet", kind="terminal", owner="p:s", state="running",
                   started_at=store._now() - 600)
    with store.db() as db:
        db.execute("UPDATE task_liveness SET heartbeat_at=? WHERE task_id=?",
                   (store._now() - 600, "quiet"))

    listed = json.loads(_handler(ctx)({"action": "list"}, session_id="s"))
    row = next(task for task in listed["tasks"] if task["task_id"] == "quiet")
    assert row["state"] == "stalled" and row["verdict"] == "stalled"

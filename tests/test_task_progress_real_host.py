"""Hermes-host integration for task-progress hooks with a temporary profile and fake transport.

The real PluginManager registers and dispatches callbacks. PluginContext.post_message is replaced
only at the transport boundary; this verifies bound session identity without contacting a platform.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = os.environ.get("HERMES_SOURCE")


class RealHermesTaskProgress(unittest.TestCase):
    @unittest.skipUnless(HERMES_SOURCE, "set HERMES_SOURCE to exercise the real Hermes host")
    def test_manifest_hooks_dispatch_with_bound_route_using_fake_transport(self):
        with tempfile.TemporaryDirectory(prefix="task-progress-real-host-") as temp:
            home = Path(temp) / ".hermes"
            home.mkdir(mode=0o700)
            (home / "config.yaml").write_text("{}")
            env = os.environ.copy()
            # Hermes resolves the profile root from both HOME and HERMES_HOME. Inherit neither
            # the invoking account's profile root nor process-scoped ledger overrides: a full
            # suite may run after tests that changed these values in the parent process.
            env["HOME"] = str(Path(temp).resolve())
            env["HERMES_HOME"] = str(home)
            for key in ("HERMES_PROFILE", "HERMES_TIMEZONE", "AGENTCORE_LIVENESS_DB",
                        "AGENTCORE_STATE_DIR"):
                env.pop(key, None)
            env["HERMES_SOURCE"] = str(Path(HERMES_SOURCE).resolve())
            env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
            env["PYTHONPATH"] = os.pathsep.join([env["HERMES_SOURCE"], str(ROOT)])
            env["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
            hermes_python = Path(HERMES_SOURCE) / "venv" / "bin" / "python"
            python_bin = str(hermes_python) if hermes_python.is_file() else sys.executable
            result = subprocess.run(
                [python_bin, str(Path(__file__).resolve()), "--worker"],
                cwd=str(home), env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("task-progress-real-host-passed", result.stdout)


class FakeMessage:
    ok = True
    error = None
    message_id = "fake-message-1"

    def __init__(self, outbox):
        self.outbox = outbox

    def edit(self, text):
        self.outbox["edits"].append(text)
        return {"ok": True, "message_id": self.message_id, "edited": True}


def worker():
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from gateway.session_context import scoped_session_key
    from agentcore.liveness import LivenessLedger

    home = Path(os.environ["HERMES_HOME"])
    profile = home / "profiles" / "canary"
    profile.mkdir(parents=True)
    db_path = profile / "liveness.db"
    settings = {"enabled": True, "engine_root": str(ROOT),
                "liveness_db": str(db_path), "store_path": str(profile / "cards.db"),
                "foreground_min_seconds": 0, "poll_seconds": 2, "min_interval": 0}
    (profile / "config.yaml").write_text(json.dumps(
        {"plugins": {"load_timeout_seconds": 45,
                     "entries": {"task-progress": {"settings": settings}}}}))

    outbox = {"routes": [], "posts": [], "edits": []}
    def fake_post_message(self, text):
        from gateway.session_context import get_session_env
        route = get_session_env("HERMES_SESSION_KEY", "")
        outbox["routes"].append(route)
        outbox["posts"].append(text)
        return FakeMessage(outbox)

    # Isolate the transport only. Hook registration, callback invocation, session context,
    # PluginContext construction, manifest loading and plugin code are the real Hermes host.
    PluginContext.post_message = fake_post_message
    manager = PluginManager(scope_key=str(profile))
    manifest = PluginManifest(name="task-progress", key="task-progress",
                              source="user", path=str(ROOT / "plugins" / "task_progress"))
    manager._load_plugin(manifest)
    record = manager._plugins["task-progress"]
    # Load the bundled Feishu platform through Hermes' actual directory-plugin loader
    # in this temporary profile; no adapter connection or credentials are created.
    import asyncio
    import importlib
    import types
    feishu_path = Path(os.environ["HERMES_SOURCE"]) / "plugins" / "platforms" / "feishu"
    feishu_manifest = PluginManifest(name="feishu-platform", key="feishu-platform",
                                     source="bundled", path=str(feishu_path))
    feishu_package = manager._load_directory_module(feishu_manifest)
    feishu_adapter_module = importlib.import_module(feishu_package.__name__ + ".adapter")
    FeishuAdapter = feishu_adapter_module.FeishuAdapter
    assert FeishuAdapter.__module__.startswith("hermes_plugins.feishu_platform")
    assert Path(feishu_adapter_module.__file__).resolve().as_posix().endswith(
        "/plugins/platforms/feishu/adapter.py")

    # Exercise the actual task-progress guard against a Feishu tracked card whose
    # content would enter edit_message's replacement-send branch.
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._card_message_ids = {"tracked-card": time.time()}
    target = types.SimpleNamespace(platform="lark", live=None, adapter=adapter, chat_id="chat")
    rejected = asyncio.run(record.module._edit_only(
        target, "tracked-card", "```\\nunsupported card content\\n```", plugin_id="task-progress"))
    assert rejected == {"ok": False, "error": "edit_unsupported"}

    # The local path uses Hermes' actual keyed notification publisher and must
    # reuse the original key generated by the first post.
    local_events = []
    class EventStream:
        def publish(self, chat_id, payload, *, event_type):
            local_events.append((chat_id, payload, event_type))
            return True
    local_target = types.SimpleNamespace(
        platform="local", chat_id="local-session", live=types.SimpleNamespace(event_stream=EventStream()),
        notice_key="new-resolution-key")
    local_result = asyncio.run(record.module._edit_only(
        local_target, "original-notification-key", "updated", plugin_id="task-progress"))
    assert local_result["ok"] is True
    assert local_events[0][1]["key"] == "original-notification-key"
    assert local_events[0][1]["id"] == "original-notification-key"
    assert not record.error, record.error
    expected = {"pre_llm_call", "pre_tool_call", "post_tool_call", "on_session_end"}
    registered = set(record.hooks_registered)
    assert expected <= registered, (expected, registered)
    assert expected <= set(manager._hooks), manager._hooks.keys()

    ledger = LivenessLedger(db_path, stall_seconds=60)
    ledger.register("proc_host", kind="terminal", owner="canary:sess-a",
                    label="isolated host probe", state="running", pid=999999,
                    pid_start="never:1")
    token = set_hermes_home_override(manager.home_path)
    try:
        with scoped_session_key("fake-route-a"):
            manager.invoke_hook("pre_llm_call", session_id="sess-a", turn_id="turn-a")
            manager.invoke_hook("pre_tool_call", tool_name="terminal", args={"secret": "omit"},
                                session_id="sess-a", turn_id="turn-a")
            manager.invoke_hook("post_tool_call", tool_name="terminal",
                                args={"command": "must not be displayed"},
                                result={"session_id": "proc_host"},
                                session_id="sess-a", turn_id="turn-a")
            ledger.finish("proc_host", "done")
            manager.invoke_hook("on_session_end", session_id="sess-a",
                                turn_id="turn-a", completed=True)
    finally:
        reset_hermes_home_override(token)

    assert len(outbox["posts"]) == 1, outbox
    assert outbox["routes"] == ["fake-route-a"], outbox["routes"]
    assert "isolated host probe" in outbox["posts"][0]
    assert "secret" not in outbox["posts"][0]
    assert "must not be displayed" not in outbox["posts"][0]
    # Session-end only wakes the background poller; do not assume that the edit is
    # synchronous with hook dispatch. Keep the terminal assertion, with a bounded wait.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not any(
            "已完成" in text for text in outbox["edits"]):
        time.sleep(0.05)
    assert outbox["edits"] and any("已完成" in text for text in outbox["edits"]), outbox["edits"]

    # Exercise a foreground turn whose threshold is checked later by the plugin
    # poller, after the active-profile ContextVar has been reset.
    module = record.module
    routed = []
    class RouteMessage:
        ok = True
        error = None
        message_id = "foreground-sticky"
        def edit(self, text):
            outbox["edits"].append(text)
            return {"ok": True, "edited": True, "message_id": self.message_id}
    def fake_post_to_session(ctx, session_key, text, *, allow_bound_context=False):
        routed.append((session_key, text))
        message = RouteMessage()
        return {"ok": True, "message_id": message.message_id, "handle": message}
    module._post_to_session = fake_post_to_session
    profile_token = set_hermes_home_override(manager.home_path)
    try:
        with scoped_session_key("fake-route-foreground"):
            manager.invoke_hook("pre_llm_call", session_id="sess-fg", turn_id="turn-fg")
    finally:
        reset_hermes_home_override(profile_token)
    deadline = time.monotonic() + 8
    foreground_record = None
    while time.monotonic() < deadline:
        state_entries = module._CTX.state.get("entries", {})
        foreground_record = state_entries.get("turn:sess-fg:turn-fg")
        if foreground_record and foreground_record.get("message_id"):
            break
        time.sleep(0.05)
    assert foreground_record is not None, module._CTX.state.get("entries", {})
    assert foreground_record["owner"] == "canary:sess-fg", foreground_record
    assert routed and routed[0][0] == "fake-route-foreground", routed
    manager.unload()
    print("task-progress-real-host-passed")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker()

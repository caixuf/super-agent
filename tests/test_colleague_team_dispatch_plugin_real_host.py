"""Real Hermes PluginManager dispatch binding with only the Gateway transport faked."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = os.environ.get("HERMES_SOURCE")


class ColleaguePluginRealHost(unittest.TestCase):
    @unittest.skipUnless(HERMES_SOURCE, "set HERMES_SOURCE to exercise Hermes PluginManager")
    def test_real_manager_owner_bound_dispatch_and_fail_closed_host(self):
        with tempfile.TemporaryDirectory(prefix="colleague-plugin-host-") as temp:
            home = Path(temp) / ".hermes" / "profiles" / "leader"
            home.mkdir(parents=True, mode=0o700)
            settings = {"enabled": True, "team_id": "team", "primary_id": "leader",
                        "primary_name": "Leader", "registry_path": str(home / "team.sqlite"),
                        "gateway_dispatch": {"enabled": True}}
            (home / "config.yaml").write_text(json.dumps({"plugins": {"load_timeout_seconds": 45,
                "entries": {"colleague-team": {"settings": settings}}}}))
            env = os.environ.copy()
            env.update(HERMES_HOME=str(home), HERMES_SOURCE=str(Path(HERMES_SOURCE).resolve()),
                       HERMES_DISABLE_LAZY_INSTALLS="1", HERMES_ENABLE_PROJECT_PLUGINS="0",
                       PYTHONPATH=os.pathsep.join([str(Path(HERMES_SOURCE).resolve()), str(ROOT)]))
            hermes_python = Path(HERMES_SOURCE) / "venv" / "bin" / "python"
            python_bin = str(hermes_python) if hermes_python.is_file() else sys.executable
            result = subprocess.run([python_bin, str(Path(__file__).resolve()), "--worker"], cwd=home,
                                    env=env, text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("colleague-plugin-real-host-passed", result.stdout)


def worker():
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools.registry import registry as tool_registry
    from agentcore.colleague_registry import Actor, Registry, Supervisor
    from agentcore.colleague_hermes_runner import ProfileBinding
    from agentcore.colleague_gateway_dispatch import GatewayColleagueDispatcher
    from agentcore.colleague_gateway_service import (
        register_gateway_dispatch_service, unregister_gateway_dispatch_service,
    )

    home = Path(os.environ["HERMES_HOME"])
    profile = home.name
    config_path = home / "config.yaml"
    config = json.loads(config_path.read_text())
    entry_settings = config["plugins"]["entries"]["colleague-team"]["settings"]
    manager = PluginManager(scope_key=str(home))
    manifest = PluginManifest(name="colleague-team", key="colleague-team", source="user",
                              path=str(ROOT / "plugins" / "colleague_team"))
    entry_settings["gateway_dispatch"]["enabled"] = False
    config_path.write_text(json.dumps(config))
    manager._load_plugin(manifest)
    assert not manager._plugins["colleague-team"].error
    assert tool_registry.get_entry("colleague_assign", scope=manager.scope_key) is None
    manager.unload()

    entry_settings["gateway_dispatch"]["enabled"] = True
    config_path.write_text(json.dumps(config))
    manager = PluginManager(scope_key=str(home))
    manager._load_plugin(manifest)
    loaded = manager._plugins["colleague-team"]
    assert not loaded.error, loaded.error
    for tool in ("colleague_assign", "colleague_task_status"):
        assert tool_registry.get_entry(tool, scope=manager.scope_key) is not None

    # No trusted service: tools are present only because operator enabled the extra
    # switch, and still fail closed instead of treating profile/Gateway config as ready.
    tokens = set_session_vars(session_id="owner-a", profile=profile)
    try:
        absent = json.loads(tool_registry.dispatch("colleague_assign", {
            "colleague": "Alice", "request_id": "task-a", "task": "Review this"},
            scope=manager.scope_key, session_id="owner-a"))
        assert absent == {"state": "unavailable", "error": "gateway_dispatch_host_unavailable"}, absent
    finally:
        clear_session_vars(tokens)

    registry = Registry(home / "team.sqlite", profile_id="team", primary_id="leader", primary_name="Leader")
    actor, supervisor = Actor("team", "leader"), Supervisor("team")
    registry.define_role(actor=actor, role_id="review", label="Reviewer", duty="Review tasks",
                         boundaries=["Keep the current permissions"], request_id="role")
    registry.hire(actor=actor, member_id="alice", role_id="review", display_name="Alice", request_id="hire")
    state_dir = home / "dispatch-state"
    state_dir.mkdir(mode=0o700)
    bound_session = "local-" + "a" * 64

    class Provisioner:
        def status(self, member_id, generation):
            return {"ready": True}

    class Bindings:
        def resolve(self, member_id, generation, owner_profile, owner_session, **kwargs):
            return ProfileBinding(member_id, generation, owner_profile, owner_session,
                                  "alice_profile", bound_session)

    class Connection:
        hermes_profile = "alice_profile"
        gateway_profile_id = "/isolated/profiles/alice"
        principal_id = "uid:isolated-test"

        def __init__(self):
            self.calls = []
            self.terminal = False

        def call(self, method, **params):
            self.calls.append((method, params))
            if method == "session.resume":
                return {"stored_session_id": bound_session, "execution_generation": 0}
            ref = {"profile_id": self.gateway_profile_id, "session_id": bound_session}
            if method == "prompt.submit":
                return {"admission_id": "admission-a", "status": "queued",
                        "execution_generation": None, "ref": ref}
            if method == "prompt.receipt":
                if self.terminal:
                    return {"admission_id": "admission-a", "status": "terminal", "outcome": "completed",
                            "execution_generation": None, "ref": ref,
                            "result": {"session_id": bound_session, "completed": True, "failed": False,
                                       "interrupted": False, "final_response": "甲" * 12000 + "结尾"}}
                return {"admission_id": "admission-a", "status": "queued",
                        "execution_generation": None, "ref": ref}
            raise AssertionError(method)

    connection = Connection()
    dispatcher = GatewayColleagueDispatcher(registry=registry, supervisor=supervisor,
        provisioner=Provisioner(), bindings=Bindings(),
        connection_for_profile=lambda name: connection, state_dir=state_dir)
    register_gateway_dispatch_service(profile, dispatcher)
    try:
        tokens = set_session_vars(session_id="owner-a", profile=profile)
        try:
            assigned = json.loads(tool_registry.dispatch("colleague_assign", {
                "colleague": "Alice", "request_id": "task-a", "task": "Review this diff"},
                scope=manager.scope_key, session_id="owner-a"))
            assert assigned["state"] == "queued" and assigned["member"]["member_id"] == "alice", assigned
            assert [call[0] for call in connection.calls] == [
                "session.resume", "prompt.submit"], connection.calls

            status = json.loads(tool_registry.dispatch("colleague_task_status", {"request_id": "task-a"},
                scope=manager.scope_key, session_id="owner-a"))
            assert status["state"] == "queued", status
            assert "等待执行" in status["status_message"], status
            connection.terminal = True
            first = json.loads(tool_registry.dispatch("colleague_task_status", {"request_id": "task-a"},
                scope=manager.scope_key, session_id="owner-a"))
            assert first["result"]["response_next_offset"] == 12000, first
            second = json.loads(tool_registry.dispatch("colleague_task_status", {
                "request_id": "task-a", "response_offset": 12000,
                "response_revision": first["result"]["response_revision"]},
                scope=manager.scope_key, session_id="owner-a"))
            assert second["result"]["final_response"] == "结尾", second
            assert second["result"]["response_next_offset"] is None, second
            cross_session = json.loads(tool_registry.dispatch("colleague_task_status", {"request_id": "task-a"},
                scope=manager.scope_key, session_id="owner-b"))
            assert cross_session["error"] == "session_context_mismatch", cross_session
            spoof = json.loads(tool_registry.dispatch("colleague_assign", {
                "colleague": "Alice", "request_id": "task-b", "task": "Review",
                "owner": {"profile": profile, "session": "owner-a"}},
                scope=manager.scope_key, session_id="owner-a"))
            assert spoof["error"] == "invalid_arguments", spoof
            assert len([method for method, _ in connection.calls if method == "prompt.submit"]) == 1

            tokens2 = set_session_vars(session_id="owner-a", profile="someone-else")
            try:
                mismatch = json.loads(tool_registry.dispatch("colleague_task_status", {"request_id": "task-a"},
                    scope=manager.scope_key, session_id="owner-a"))
                assert mismatch["error"] == "profile_context_mismatch", mismatch
            finally:
                clear_session_vars(tokens2)
        finally:
            clear_session_vars(tokens)
    finally:
        unregister_gateway_dispatch_service(profile, dispatcher)
        manager.unload()
    print("colleague-plugin-real-host-passed")


if __name__ == "__main__" and "--worker" in sys.argv:
    worker()

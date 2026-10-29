"""Real Hermes integration for task_status. Set HERMES_SOURCE and use its interpreter."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = os.environ.get("HERMES_SOURCE")


class RealHermesTaskStatus(unittest.TestCase):
    @unittest.skipUnless(HERMES_SOURCE, "set HERMES_SOURCE to exercise the real Hermes host")
    def test_tool_is_registered_and_owner_scoped(self):
        with tempfile.TemporaryDirectory(prefix="task-status-real-host-") as temp:
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
            env["PYTHONPATH"] = os.pathsep.join([env["HERMES_SOURCE"], str(ROOT)])
            env["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
            hermes_python = Path(HERMES_SOURCE) / "venv" / "bin" / "python"
            python_bin = str(hermes_python) if hermes_python.is_file() else sys.executable
            result = subprocess.run(
                [python_bin, str(Path(__file__).resolve()), "--worker"],
                cwd=str(home), env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("task-status-real-host-passed", result.stdout)


def worker():
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.registry import registry
    from agentcore.liveness import LivenessLedger

    home = Path(os.environ["HERMES_HOME"])
    profile = home / "profiles" / "canary"
    profile.mkdir(parents=True)
    db_path = profile / "liveness.db"
    settings = {"enabled": True, "engine_root": str(ROOT), "db_path": str(db_path)}
    (profile / "config.yaml").write_text(json.dumps(
        {"plugins": {"load_timeout_seconds": 45,
                     "entries": {"task-status": {"settings": settings}}}}))
    plugin = ROOT / "plugins" / "task_status"
    manager = PluginManager(scope_key=str(profile))
    manifest = PluginManifest(name="task-status", key="task-status",
                              source="user", path=str(plugin))
    manager._load_plugin(manifest)
    record = manager._plugins["task-status"]
    assert not record.error, record.error

    store = LivenessLedger(db_path, stall_seconds=60)
    store.register("t1", kind="terminal", owner="canary:session", label="suite",
                   state="running")
    store.beat("t1", phase="compile", progress=(1, 2))

    token = set_hermes_home_override(manager.home_path)
    try:
        result = registry.dispatch("task_status", {"action": "inspect", "task_id": "t1"},
                                   scope=manager.scope_key, session_id="session")
        data = json.loads(result) if isinstance(result, str) else result
        assert data["ok"] is True, data
        assert data["owner"] == "canary:session", data
        assert data["verdict"] == "running", data
        assert data["progress"]["percent"] == 50.0, data
        denied = registry.dispatch("task_status", {"action": "inspect", "task_id": "t1"},
                                   scope=manager.scope_key, session_id="other")
        denied = json.loads(denied) if isinstance(denied, str) else denied
        assert denied["error"] == "unknown_task", denied
    finally:
        reset_hermes_home_override(token)
    print("task-status-real-host-passed")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker()

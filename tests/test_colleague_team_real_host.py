"""Real Hermes integration. Set HERMES_SOURCE and use the Hermes interpreter."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = os.environ.get("HERMES_SOURCE")


class RealHermesIntegration(unittest.TestCase):
    @unittest.skipUnless(HERMES_SOURCE, "set HERMES_SOURCE to exercise the real Hermes host")
    def test_isolated_profiles_and_registration(self):
        with tempfile.TemporaryDirectory(prefix="colleague-real-host-") as temp:
            home = Path(temp) / ".hermes"
            home.mkdir(mode=0o700)
            (home / "config.yaml").write_text("{}")
            env = os.environ.copy()
            env["HERMES_HOME"] = str(home)
            env["HERMES_SOURCE"] = str(Path(HERMES_SOURCE).resolve())
            env["PYTHONPATH"] = os.pathsep.join([env["HERMES_SOURCE"], str(ROOT)])
            env["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker"],
                cwd=str(home), env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("real-host-checks-passed", result.stdout)


def worker():
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.registry import registry

    home = Path(os.environ["HERMES_HOME"])
    private = home / "private"
    private.mkdir(mode=0o700)
    plugin = ROOT / "plugins" / "colleague_team"
    names = ["colleague_list", "colleague_define_role", "colleague_hire",
             "colleague_grant", "colleague_retire"]

    def setup(profile, **overrides):
        path = home / "profiles" / profile
        path.mkdir(parents=True)
        settings = {"enabled": True, "registry_path": str(private / "team.sqlite"),
                    "team_id": "team", "primary_id": "leader", "engine_root": str(ROOT)}
        settings.update(overrides)
        (path / "config.yaml").write_text(json.dumps(
            {"plugins": {"load_timeout_seconds": 45, "entries": {"colleague-team": {"settings": settings}}}}))
        manager = PluginManager(scope_key=str(path))
        manifest = PluginManifest(name="colleague-team", key="colleague-team",
                                  source="user", path=str(plugin))
        manager._load_plugin(manifest)
        return manager

    def call(manager, name, args, session="session"):
        token = set_hermes_home_override(manager.home_path)
        try:
            result = registry.dispatch("colleague_" + name, args, scope=manager.scope_key,
                                       session_id=session)
            return json.loads(result) if isinstance(result, str) else result
        finally:
            reset_hermes_home_override(token)

    leader = setup("leader")
    assert not leader._plugins["colleague-team"].error, leader._plugins["colleague-team"].error
    assert all(registry.snapshot_registration(name, scope=leader.scope_key) for name in names)
    role = {"role_id": "people", "label": "People", "duty": "Build the team",
            "boundaries": ["Host policy applies"], "request_id": "role-1"}
    assert call(leader, "define_role", role)["ok"]
    assert call(leader, "hire", {"member_id": "recruiter", "role_id": "people",
        "display_name": "Recruiter", "request_id": "hire-1"})["provisioning_required"]
    assert call(leader, "grant", {"member_id": "recruiter", "enabled": True,
                                   "request_id": "grant-1"})["ok"]
    assert call(leader, "hire", {"member_id": "reviewer", "role_id": "people",
        "display_name": "Reviewer", "request_id": "hire-2"})["ok"]

    recruiter = setup("recruiter")
    reviewer = setup("reviewer")
    assert not recruiter._plugins["colleague-team"].error
    assert all(registry.snapshot_registration(name, scope=recruiter.scope_key) for name in names)
    assert registry.snapshot_registration("colleague_list", scope=leader.scope_key).handler is not (
        registry.snapshot_registration("colleague_list", scope=recruiter.scope_key).handler)
    assert call(recruiter, "hire", {"member_id": "recruiter_two", "role_id": "people",
        "display_name": "Recruiter two", "request_id": "hire-3"})["ok"]
    assert call(recruiter, "grant", {"member_id": "recruiter_two", "enabled": True,
                                       "request_id": "grant-2"})["ok"]
    assert call(recruiter, "retire", {"member_id": "leader",
        "request_id": "retire-1"}, session="leader")["error"] == "primary_protected"
    assert call(leader, "retire", {"member_id": "leader",
        "request_id": "retire-2"})["error"] == "primary_protected"
    assert call(reviewer, "list", {}, session="leader")["ok"]
    hire = {"member_id": "other", "role_id": "people", "display_name": "Other",
            "request_id": "hire-4"}
    assert not call(reviewer, "hire", hire, session="leader")["ok"]
    assert call(reviewer, "hire", {**hire, "actor": "leader"})["error"] == "invalid_arguments"
    assert call(reviewer, "list", {}, session=None)["error"] == "team_session_required"

    disabled = setup("disabled", enabled=False)
    assert all(registry.snapshot_registration(name, scope=disabled.scope_key) is None for name in names)
    invalid = setup("invalid", registry_path=123)
    assert invalid._plugins["colleague-team"].error
    assert all(registry.snapshot_registration(name, scope=invalid.scope_key) is None for name in names)
    print("real-host-checks-passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        worker()
    else:
        unittest.main()

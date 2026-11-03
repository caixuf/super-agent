"""Contract tests for the single-root Super Agent installer.

All mutation happens under pytest's temporary HOME. The tests never enable a
real service, change the live profile, edit Hermes source or contact Feishu.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools/superagent.py"


def _load_installer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("superagent_installer_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seal_test_release(release, version):
    module = _load_installer()
    (release / "release.json").write_text(json.dumps({
        "schema_version": 1, "version": version, "identity": version,
        "tree_sha256": module._release_tree_hash(release),
        "files": module._release_files(release),
    }))


def _run(home: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(home))
    env.pop("HERMES_HOME", None)
    command = [
        sys.executable, str(TOOL),
        "--profile", "demo",
        "--profile-home", str(home / ".hermes" / "profiles" / "demo"),
        "--root", str(home / ".hermes" / "superagent"),
        *args,
    ]
    proc = subprocess.run(command, env=env, text=True, capture_output=True)
    if check:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def _seed_profile(home: Path) -> Path:
    profile = home / ".hermes/profiles/demo"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "# preserved\ntelegram:\n  enabled: false\nplugins:\n  enabled: []\n  disabled: []\n",
        encoding="utf-8",
    )
    (profile / ".env").write_text("TOKEN=local-test-only\n", encoding="utf-8")
    hermes = home / ".hermes/hermes-agent"
    (hermes / "plugins/platforms/feishu").mkdir(parents=True)
    (hermes / "plugins/platforms/feishu/adapter.py").write_text("# fake adapter\n", encoding="utf-8")
    return profile


def test_external_snapshot_cannot_override_repo_owned_memory_provider(tmp_path, monkeypatch):
    module = _load_installer()
    stale = tmp_path / 'external/localvec'
    stale.mkdir(parents=True)
    (stale / '__init__.py').write_text('# stale unguarded provider\n')
    plan = {'release': str(tmp_path / 'releases/test'), 'version': 'test', 'identity': 'test',
            'external_plugins': [str(stale)]}
    release = module._stage_release(plan)
    assert (release / 'plugins/localvec/__init__.py').read_bytes() == (REPO / 'plugins/localvec/__init__.py').read_bytes()


def test_plan_is_read_only_and_identifies_managed_layout(tmp_path):
    profile = _seed_profile(tmp_path)
    before = (profile / "config.yaml").read_bytes()
    proc = _run(tmp_path, "plan", check=True)
    plan = json.loads(proc.stdout)
    assert plan["managed_root"] == str(tmp_path / ".hermes/superagent/demo")
    assert plan["release"].startswith(plan["managed_root"] + "/releases/")
    assert plan["plugins"]
    assert (profile / "config.yaml").read_bytes() == before
    assert not (tmp_path / ".hermes/superagent").exists()


def test_install_uses_one_root_and_idempotent_plugin_links(tmp_path):
    profile = _seed_profile(tmp_path)
    result = _run(tmp_path, "install", check=True)
    manifest = json.loads(result.stdout)
    root = Path(manifest["managed_root"])
    release = Path(manifest["release"])
    assert (root / "current").resolve() == release.resolve()
    assert (root / "install-manifest.json").is_file()
    assert (release / "agentcore" / "liveness.py").is_file()
    assert (release / "plugins" / "task_status" / "__init__.py").is_file()
    assert (release / "skills" / "im-card-markdown" / "SKILL.md").is_file()
    assert (release / "patches/patch_frontdesk_boundary.py").is_file()
    assert (release / "runtime/hermes_frontdesk_policy.py").is_file()
    assert (release / "runtime/hermes_frontdesk_turn.py").is_file()
    plugins = yaml.safe_load((profile / "config.yaml").read_text())["plugins"]["enabled"]
    assert "task-status" in plugins
    assert (profile / "plugins/task-status").is_symlink()
    assert "localvec" not in plugins
    assert manifest['memory_providers'] == ['localvec']
    assert (profile / 'plugins/localvec').resolve() == release / 'plugins/localvec'
    assert (release / 'runtime/hermes_memory_identity.py').is_file()

    before = (profile / "config.yaml").read_bytes()
    second = _run(tmp_path, "install", check=True)
    assert json.loads(second.stdout)["tree_sha256"] == manifest["tree_sha256"]
    assert (profile / "config.yaml").read_bytes() == before


def test_doctor_detects_missing_managed_memory_provider(tmp_path):
    profile = _seed_profile(tmp_path)
    _run(tmp_path, 'install', check=True)
    (profile / 'plugins/localvec').unlink()
    result = _run(tmp_path, 'doctor')
    assert result.returncode != 0
    assert json.loads(result.stdout)['plugin_links_valid'] is False


def test_uninstall_unlinks_memory_provider_without_enabling_it(tmp_path):
    profile = _seed_profile(tmp_path)
    manifest = json.loads(_run(tmp_path, 'install', check=True).stdout)
    result = _run(tmp_path, 'uninstall', '--keep-data', check=True)
    assert not (profile / 'plugins/localvec').is_symlink()
    assert Path(manifest['managed_root']).is_dir()
    assert 'localvec' not in (yaml.safe_load((profile / 'config.yaml').read_text())['plugins'].get('enabled') or [])


def test_reinstall_does_not_delete_or_repair_active_release_in_place(tmp_path):
    _seed_profile(tmp_path)
    manifest = json.loads(_run(tmp_path, "install", check=True).stdout)
    release = Path(manifest["release"])
    engine = release / "agentcore/liveness.py"
    engine.write_text("# drift must be preserved for recovery\n", encoding="utf-8")
    before = engine.read_bytes()
    proc = _run(tmp_path, "install")
    assert proc.returncode != 0
    assert engine.read_bytes() == before
    assert (Path(manifest["managed_root"]) / "current").resolve() == release.resolve()


@pytest.fixture
def staged_installer(tmp_path, monkeypatch):
    module = _load_installer()
    source = tmp_path / "source"
    for name in ("agentcore/liveness.py", "plugins/demo/plugin.yaml",
                 "skills/demo/SKILL.md", "tools/demo.py"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# synthetic payload\n")
    monkeypatch.setattr(module, "REPO", source)
    monkeypatch.setattr(module, "PLUGIN_SOURCES", (source / "plugins/demo",))
    plan = {"release": str(tmp_path / "managed/releases/test"),
            "version": "test", "identity": "test", "external_plugins": []}
    return module, plan, source


def test_staging_identical_release_reuses_files_without_rewriting(staged_installer):
    module, plan, _ = staged_installer
    release = module._stage_release(plan)
    payload = release / "agentcore/liveness.py"
    before = payload.stat()
    assert module._stage_release(plan) == release
    assert payload.stat().st_ino == before.st_ino
    assert payload.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("drift", ("source", "mode", "extra_file", "descriptor"))
def test_staging_rejects_same_version_drift_without_replacing_files(staged_installer, drift):
    module, plan, source = staged_installer
    release = module._stage_release(plan)
    payload = release / "agentcore/liveness.py"
    if drift == "source":
        (source / "agentcore/liveness.py").write_text("# new version required\n")
    elif drift == "mode":
        payload.chmod(0o755)
    elif drift == "extra_file":
        (release / "extra.py").write_text("# unexplained file\n")
    else:
        (release / "release.json").write_text("{}\n")
    before = {p.relative_to(release): (p.read_bytes(), p.stat().st_mode)
              for p in release.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError, match="refusing in-place"):
        module._stage_release(plan)
    after = {p.relative_to(release): (p.read_bytes(), p.stat().st_mode)
             for p in release.rglob("*") if p.is_file()}
    assert after == before
    assert not list(release.parent.glob(".staging-*"))


def test_failed_staging_leaves_active_release_and_no_partial_version(staged_installer, monkeypatch):
    module, plan, _ = staged_installer
    old = module._stage_release(plan)
    current = old.parents[1] / "current"
    current.symlink_to(old)
    next_release = old.with_name("next")
    next_plan = {**plan, "release": str(next_release), "version": "next", "identity": "next"}
    original_copy = module._copy_tree

    def broken_copy(src, dst):
        original_copy(src, dst)
        raise OSError("synthetic interrupted copy")

    monkeypatch.setattr(module, "_copy_tree", broken_copy)
    with pytest.raises(OSError, match="interrupted copy"):
        module._stage_release(next_plan)
    assert current.resolve() == old
    assert (old / "agentcore/liveness.py").is_file()
    assert not next_release.exists()
    assert not list(old.parent.glob(".staging-*"))


def test_staging_refuses_existing_release_symlink(staged_installer):
    module, plan, _ = staged_installer
    release = module._stage_release(plan)
    link = release.with_name("linked")
    link.symlink_to(release)
    with pytest.raises(RuntimeError, match="refusing in-place"):
        module._stage_release({**plan, "release": str(link)})
    assert link.is_symlink()
    assert (release / "agentcore/liveness.py").is_file()


@pytest.mark.parametrize("drift", ("bytes", "mode", "missing_file", "extra_file", "symlink"))
def test_managed_release_validation_rejects_drift(staged_installer, drift):
    module, plan, _ = staged_installer
    release = module._stage_release(plan)
    payload = release / "agentcore/liveness.py"
    assert module._release_valid(release)
    if drift == "bytes":
        payload.write_text("# drift\n")
    elif drift == "mode":
        payload.chmod(0o755)
    elif drift == "missing_file":
        payload.unlink()
    elif drift == "extra_file":
        (release / "extra.py").write_text("# drift\n")
    else:
        payload.unlink()
        payload.symlink_to(REPO / "agentcore/liveness.py")
    assert not module._release_valid(release)


def test_install_manifest_is_the_uninstall_source_of_truth(tmp_path):
    profile = _seed_profile(tmp_path)
    manifest = json.loads(_run(tmp_path, "install", check=True).stdout)
    root = Path(manifest["managed_root"])
    _run(tmp_path, "uninstall", check=True)
    assert not root.exists()
    assert not (profile / "plugins/task-status").exists()
    assert "telegram:" in (profile / "config.yaml").read_text()


def test_uninstall_can_keep_state_without_keeping_code(tmp_path):
    _seed_profile(tmp_path)
    manifest = json.loads(_run(tmp_path, "install", check=True).stdout)
    root = Path(manifest["managed_root"])
    _run(tmp_path, "uninstall", "--keep-data", check=True)
    assert root.exists()
    assert not (root / "current").exists() or not (root / "current").is_symlink()
    assert (root / "install-manifest.json").is_file()


def test_doctor_reports_broken_managed_current(tmp_path):
    _seed_profile(tmp_path)
    manifest = json.loads(_run(tmp_path, "install", check=True).stdout)
    current = Path(manifest["managed_root"]) / "current"
    current.unlink()
    doctor = _run(tmp_path, "doctor")
    assert doctor.returncode == 1
    assert json.loads(doctor.stdout)["status"] == "broken"


def test_doctor_reports_plugin_link_to_wrong_release(tmp_path):
    profile = _seed_profile(tmp_path)
    _run(tmp_path, "install", check=True)
    link = profile / "plugins/task-status"
    link.unlink()
    link.symlink_to(REPO / "plugins/task_status")
    proc = _run(tmp_path, "doctor")
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["plugin_links_valid"] is False


def test_upgrade_keeps_previous_release_and_rollback_switches_current(tmp_path):
    _seed_profile(tmp_path)
    first = json.loads(_run(tmp_path, "install", check=True).stdout)
    old_release = Path(first["release"])
    root = Path(first["managed_root"])
    # Simulate a separately built next release without touching the live one.
    next_release = root / "releases" / "next-test"
    next_release.mkdir(parents=True)
    (next_release / "agentcore").mkdir()
    (next_release / "agentcore" / "liveness.py").write_text("# next\n")
    shutil.copytree(old_release / "plugins", next_release / "plugins")
    (next_release / "skills").mkdir()
    _seal_test_release(next_release, "next-test")
    result = _run(tmp_path, "activate-release", "--release", str(next_release))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "current").resolve() == next_release.resolve()
    assert old_release.is_dir()
    rollback = _run(tmp_path, "rollback")
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    assert (root / "current").resolve() == old_release.resolve()


def test_rollback_refuses_corrupted_previous_release_without_switching(tmp_path):
    _seed_profile(tmp_path)
    first = json.loads(_run(tmp_path, "install", check=True).stdout)
    root = Path(first["managed_root"])
    old_release = Path(first["release"])
    next_release = root / "releases/next-test"
    (next_release / "agentcore").mkdir(parents=True)
    (next_release / "agentcore/liveness.py").write_text("# next\n")
    shutil.copytree(old_release / "plugins", next_release / "plugins")
    (next_release / "skills").mkdir()
    _seal_test_release(next_release, "next-test")
    _run(tmp_path, "activate-release", "--release", str(next_release), check=True)
    (old_release / "agentcore/liveness.py").write_text("# corrupted old release\n")
    result = _run(tmp_path, "rollback")
    assert result.returncode != 0
    assert (root / "current").resolve() == next_release.resolve()


def test_activate_sealed_release_missing_memory_provider_preserves_all_links(tmp_path):
    profile = _seed_profile(tmp_path)
    first = json.loads(_run(tmp_path, 'install', check=True).stdout)
    root, old = Path(first['managed_root']), Path(first['release'])
    candidate = root / 'releases/no-provider'
    shutil.copytree(old, candidate)
    shutil.rmtree(candidate / 'plugins/localvec')
    _seal_test_release(candidate, 'no-provider')
    before = {link.name: link.resolve() for link in (profile / 'plugins').iterdir() if link.is_symlink()}
    result = _run(tmp_path, 'activate-release', '--release', str(candidate))
    assert result.returncode != 0
    assert (root / 'current').resolve() == old
    assert {link.name: link.resolve() for link in (profile / 'plugins').iterdir() if link.is_symlink()} == before


def test_upgrade_refuses_incomplete_release_before_switching(tmp_path):
    _seed_profile(tmp_path)
    first = json.loads(_run(tmp_path, "install", check=True).stdout)
    root = Path(first["managed_root"])
    bad = root / "releases" / "bad"
    bad.mkdir(parents=True)
    before = (root / "current").resolve()
    result = _run(tmp_path, "activate-release", "--release", str(bad))
    assert result.returncode != 0
    assert (root / "current").resolve() == before


def test_migrate_config_moves_only_owned_paths_and_preserves_secrets(tmp_path):
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    config.write_text(
        "# keep comment\nmodel:\n  api_key: sk-secret\n"
        "plugins:\n  entries:\n    task-status:\n      settings:\n"
        "        enabled: true\n        engine_root: /home/caixuf/code/super-agent\n"
        "        liveness_db: /home/caixuf/.local/state/agentcore/liveness.db\n",
        encoding="utf-8",
    )
    migrated = _run(tmp_path, "migrate-config", check=True)
    data = json.loads(migrated.stdout)
    assert data["status"] == "migrated"
    text = config.read_text()
    root = tmp_path / ".hermes/superagent/demo"
    assert f"engine_root: {root}/current" in text
    assert f"liveness_db: {root}/state/liveness.db" in text
    assert "sk-secret" in text and "# keep comment" in text
    before = config.read_bytes()
    again = _run(tmp_path, "migrate-config", check=True)
    assert json.loads(again.stdout)["status"] == "unchanged"
    assert config.read_bytes() == before


def test_update_stops_enabled_services_and_restarts_them(tmp_path):
    _seed_profile(tmp_path)
    calls = tmp_path / "systemctl-calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$TEST_SYSTEMCTL_CALLS"
if [ "$2" = is-active ]; then exit 0; fi
exit 0
""", encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path), TEST_SYSTEMCTL_CALLS=str(calls),
               PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    command = [
        sys.executable, str(TOOL), "--profile", "demo",
        "--profile-home", str(tmp_path / ".hermes/profiles/demo"),
        "--root", str(tmp_path / ".hermes/superagent"), "update",
    ]
    proc = subprocess.run(command, env=env, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = calls.read_text().splitlines()
    assert any("stop hermes-gateway-demo.service" in line for line in lines)
    assert any("start hermes-gateway-demo.service" in line for line in lines)


def test_shell_entrypoint_is_plan_only_without_apply(tmp_path):
    profile = _seed_profile(tmp_path)
    wrapper = REPO / "install/superagent.sh"
    env = dict(os.environ, HOME=str(tmp_path))
    before = (profile / "config.yaml").read_bytes()
    proc = subprocess.run(
        [str(wrapper), "install", "--profile", "demo",
         "--profile-home", str(profile),
         "--root", str(tmp_path / ".hermes/superagent")],
        env=env, text=True, capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Plan only" in proc.stdout
    assert (profile / "config.yaml").read_bytes() == before
    assert not (tmp_path / ".hermes/superagent").exists()


def test_shell_entrypoint_apply_installs_once(tmp_path):
    profile = _seed_profile(tmp_path)
    wrapper = REPO / "install/superagent.sh"
    env = dict(os.environ, HOME=str(tmp_path))
    proc = subprocess.run(
        [str(wrapper), "install", "--profile", "demo",
         "--profile-home", str(profile),
         "--root", str(tmp_path / ".hermes/superagent"), "--apply"],
        env=env, text=True, capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / ".hermes/superagent/demo/current").is_symlink()


def test_uninstall_removes_managed_enable_entries_but_keeps_comments_and_unrelated_plugins(tmp_path):
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    config.write_text(
        "# keep\nplugins:\n  enabled:\n  - existing-plugin\n  - task-status\n"
        "  - write-guard\n  disabled:\n  - keep-disabled\nmodel:\n  api_key: keep\n",
        encoding="utf-8",
    )
    manifest = json.loads(_run(tmp_path, "install", check=True).stdout)
    _run(tmp_path, "uninstall", check=True)
    text = config.read_text()
    assert "# keep" in text and "api_key: keep" in text
    assert "- existing-plugin" in text and "- keep-disabled" in text
    assert "- task-status" not in text
    assert "- write-guard" not in text
    assert not Path(manifest["managed_root"]).exists()


def test_candidate_builder_is_separate_and_does_not_touch_live_hermes(tmp_path):
    """Candidate generation must not inspect or mutate the live Hermes checkout."""
    profile = _seed_profile(tmp_path)
    live_adapter = tmp_path / ".hermes/hermes-agent/plugins/platforms/feishu/adapter.py"
    before = live_adapter.read_bytes()
    stale = tmp_path / ".hermes/superagent/demo/hermes/v0.22.5-bf0203a794a0"
    stale.mkdir(parents=True)
    (stale / "sentinel").write_text("old")
    proc = _run(tmp_path, "candidate-hermes", check=False)
    # In an offline test this may fail to clone; either way the live tree and
    # profile are untouched and no stale candidate is silently reused.
    assert live_adapter.read_bytes() == before
    assert not (stale / "sentinel").exists()


def test_candidate_patch_probe_targets_the_candidate_not_live_hermes(tmp_path):
    """A clean candidate must not be judged against the live Hermes checkout."""
    import importlib.util
    from unittest.mock import patch
    spec = importlib.util.spec_from_file_location("superagent_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate = tmp_path / "candidate"
    live = tmp_path / "live"
    for tree in (candidate, live):
        (tree / "plugins/platforms/feishu").mkdir(parents=True)
        (tree / "plugins/platforms/feishu/adapter.py").write_text("# no card patch\n")
    observed = []
    def fake_run(command, *, cwd=None, check=False, env=None):
        observed.append((command, (env or {}).get("HERMES_AGENT_DIR")))
        return subprocess.CompletedProcess(command, 0, "", "")
    with patch.object(module, "_run", fake_run):
        module._patch_state(candidate, module.CARD_PATCH_TOOLS[0])
    assert observed
    assert observed[-1][1] == str(candidate)


def test_candidate_cannot_silently_omit_required_frontdesk_patch(tmp_path, monkeypatch):
    from unittest.mock import Mock

    module = _load_installer()
    selection = tuple(item for item in module.KEEP_HOST_PATCH_TOOLS if item[1] == "frontdesk-boundary")
    assert len(selection) == 1
    monkeypatch.setattr(module, "KEEP_HOST_PATCH_TOOLS", selection)
    monkeypatch.setattr(module, "REPO", tmp_path / "incomplete-bundle")
    run = Mock()
    monkeypatch.setattr(module, "_run", run)
    with pytest.raises(RuntimeError, match="required preserved patch missing: frontdesk-boundary"):
        module._apply_keep_patches(tmp_path / "candidate")
    run.assert_not_called()


def test_candidate_smoke_requires_version_and_committed_pm_environment(tmp_path):
    import importlib.util
    from unittest.mock import patch
    spec = importlib.util.spec_from_file_location("superagent_smoke", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    work = tmp_path / "candidate"
    work.mkdir()

    def fake_run_candidate(_work, args, timeout=0):
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, "Hermes Agent v0.22.5\n", "")
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"outcome": "ok", "kind": "sync"}) + "\n", "")

    with patch.object(module, "_run_candidate", fake_run_candidate):
        assert module._smoke_candidate(work)["ok"] is True

    def failed_status(_work, args, timeout=0):
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, "Hermes Agent v0.22.5\n", "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"outcome": "failed"}), "")

    with patch.object(module, "_run_candidate", failed_status):
        result = module._smoke_candidate(work)
        assert result["ok"] is False
        assert result["reason"] == "candidate_pm_sync_not_ok"


def test_switch_uses_pm_bound_launcher_and_refuses_failed_smoke(tmp_path):
    import importlib.util
    from unittest.mock import patch
    spec = importlib.util.spec_from_file_location("superagent_switch", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / ".hermes/superagent/demo"
    candidate = root / "hermes" / "v0.22.5-bf0203a794a0"
    (candidate / "plugins/platforms/feishu").mkdir(parents=True)
    (candidate / "plugins/platforms/feishu/adapter.py").write_text("# candidate\n")
    (candidate / ".hermes/bin").mkdir(parents=True)
    launcher = candidate / ".hermes/bin/hermes"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    root.mkdir(exist_ok=True)
    (root / "hermes-current").symlink_to(candidate, target_is_directory=True)
    unit = tmp_path / ".config/systemd/user/hermes-gateway-demo.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/old/hermes --profile demo gateway run\n")
    args = type("Args", (), {"profile": "demo", "profile_home": tmp_path / ".hermes/profiles/demo", "root": tmp_path / ".hermes/superagent", "hermes_root": tmp_path / ".hermes/hermes-agent", "keep_data": False})()
    with patch.object(module, "_legacy_patch_state", return_value=[{"in_place": False}]), \
         patch.object(module, "_smoke_candidate", return_value={"ok": True}), \
         patch.object(module, "_gateway_dependency_smoke", return_value={"ok": False, "reason": "candidate_gateway_status_failed"}):
        assert module.switch_gateway_source(args) == 1
    assert "ExecStart=/old/hermes" in unit.read_text()
    class FakeHome(type(tmp_path)):
        pass
    with patch.object(module, "_legacy_patch_state", return_value=[{"in_place": False}]), \
         patch.object(module, "_smoke_candidate", return_value={"ok": True}), \
         patch.object(module, "_gateway_dependency_smoke", return_value={"ok": True}), \
         patch.object(module.Path, "home", return_value=tmp_path):
        assert module.switch_gateway_source(args) == 0
    assert str(launcher) in unit.read_text()
    assert (unit.parent / "hermes-gateway-demo.service.superagent-before").is_file()


def test_release_hash_excludes_self_referential_manifest(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("superagent_hash", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    release = tmp_path / "release"
    release.mkdir()
    (release / "payload.txt").write_text("one")
    first = module._release_tree_hash(release)
    (release / "release.json").write_text('{"version":"one"}')
    assert module._release_tree_hash(release) == first
    (release / "release.json").write_text('{"version":"two"}')
    assert module._release_tree_hash(release) == first
    (release / "payload.txt").write_text("two")
    assert module._release_tree_hash(release) != first


def test_install_same_identity_records_release_hash_and_rollback_capability(tmp_path):
    _seed_profile(tmp_path)
    first = json.loads(_run(tmp_path, "install", check=True).stdout)
    root = Path(first["managed_root"])
    manifest = json.loads((root / "install-manifest.json").read_text())
    release = Path(first["release"])
    assert manifest["tree_sha256"] == json.loads((release / "release.json").read_text())["tree_sha256"]
    assert "release_history" in manifest
    doctor = json.loads(_run(tmp_path, "doctor", check=True).stdout)
    assert doctor["release_hash_valid"] is True


def test_current_cardkit_is_not_misclassified_as_legacy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _load_installer()
    _seed_profile(tmp_path)
    monkeypatch.setattr(module, "_git_identity", lambda: ("candidate", "candidate"))
    monkeypatch.setattr(module, "_plugin_names", lambda args: ["example"])
    monkeypatch.setattr(module, "_external_plugin_sources", lambda args: [])
    monkeypatch.setattr(module, "_keep_patch_state", lambda root: [])
    entries = [dict(id=name, in_place=present, state="in-place" if present else "absent") for name, present in (("card-replies", True), ("card-finalize", True), ("card-native-markdown", False), ("card-update-retry", True), ("delivery-uuid", True))]
    monkeypatch.setattr(module, "_legacy_patch_state", lambda root: entries)
    monkeypatch.setattr(module, "_profile_home", lambda args: tmp_path / ".hermes/profiles/demo")
    monkeypatch.setattr(module, "_managed_root", lambda args: tmp_path / ".hermes/superagent/demo")
    args = SimpleNamespace(hermes_root=tmp_path / ".hermes/hermes-agent", runtime_manifest=None, profile="demo")
    plan = module.build_plan(args)
    assert plan["card_implementation"] == "release-cardkit"
    assert "legacy_card_patches_require_clean_candidate" not in plan["blockers"]
    entries[2]["in_place"] = True
    plan = module.build_plan(args)
    assert "legacy_card_patches_require_clean_candidate" in plan["blockers"]


def test_retired_progress_state_is_preserved_for_rollback(tmp_path):
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    data = profile / "plugin-data/agent-plugin-task-progress-b1e89459"
    data.mkdir(parents=True)
    (data / "history.json").write_text("existing task history")
    module._purge_legacy_plugins(profile)
    assert (data / "history.json").read_text() == "existing task history"


def test_failed_update_restores_configuration_and_declared_dropins(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    root.mkdir(parents=True)
    prior_manifest = root / "install-manifest.json"
    prior_manifest.write_text("prior manifest")
    manifest = tmp_path / "runtime.yaml"
    manifest.write_text("profile: demo\nruntime:\n  drop_ins:\n    10-card.conf:\n      TEST_FLAG: 1\n    20-new.conf:\n      OTHER_FLAG: 1\n")
    drop = tmp_path / ".config/systemd/user/hermes-gateway-demo.service.d"
    drop.mkdir(parents=True)
    existing = drop / "10-card.conf"
    existing.write_text("old runtime config")
    original = (profile / "config.yaml").read_bytes()
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_run", lambda command, **kw: subprocess.CompletedProcess(command, 1 if "is-active" in command else 0, "", ""))
    def failed_install(args, plan):
        (profile / "config.yaml").write_text("partially changed")
        prior_manifest.write_text("partially changed")
        existing.write_text("new runtime config")
        (drop / "20-new.conf").write_text("new file")
        raise RuntimeError("injected failure")
    monkeypatch.setattr(module, "install", failed_install)
    args = SimpleNamespace(profile="demo", runtime_manifest=manifest)
    assert module.update(args, {"profile_home": str(profile)}) == 1
    assert (profile / "config.yaml").read_bytes() == original
    assert prior_manifest.read_text() == "prior manifest"
    assert existing.read_text() == "old runtime config"
    assert not (drop / "20-new.conf").exists()


def test_update_reports_incomplete_rollback_when_plugin_links_drift(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    root.mkdir(parents=True)
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_run", lambda command, **kw: subprocess.CompletedProcess(command, 1 if "is-active" in command else 0, "", ""))
    def failed_install(args, plan):
        plugins = profile / "plugins"
        plugins.mkdir(exist_ok=True)
        (plugins / "new-plugin").symlink_to(tmp_path / "new-release")
        raise RuntimeError("injected failure")
    monkeypatch.setattr(module, "install", failed_install)
    args = SimpleNamespace(profile="demo", runtime_manifest=None)
    assert module.update(args, {"profile_home": str(profile)}) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "rollback-incomplete"
    assert "plugin-links" in report["rollback_errors"]


def test_update_controls_gateway_declared_in_runtime_manifest(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    root.mkdir(parents=True)
    manifest = tmp_path / "runtime.yaml"
    manifest.write_text("profile: demo\ngateway_service: custom-demo.service\nruntime:\n  drop_ins: {}\n")
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        active = "is-active" in command and "custom-demo.service" in command
        return subprocess.CompletedProcess(command, 0 if active or "is-active" not in command else 1, "", "")
    monkeypatch.setattr(module, "_run", run)
    def fail(args, plan):
        raise RuntimeError("injected")
    monkeypatch.setattr(module, "install", fail)
    args = SimpleNamespace(profile="demo", runtime_manifest=manifest)
    assert module.update(args, {"profile_home": str(profile)}) == 1
    assert any("stop" in cmd and "custom-demo.service" in cmd for cmd in calls)
    assert any("start" in cmd and "custom-demo.service" in cmd for cmd in calls)
    assert not any("hermes-gateway-demo.service" in cmd for cmd in calls)


def test_failed_update_restores_managed_plugin_links(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    root.mkdir(parents=True)
    plugins = profile / "plugins"
    plugins.mkdir()
    old_target = root / "releases/old/plugins/existing"
    new_target = root / "releases/new/plugins/existing"
    old_target.mkdir(parents=True)
    new_target.mkdir(parents=True)
    link = plugins / "existing"
    link.symlink_to(old_target)
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_run", lambda command, **kw: subprocess.CompletedProcess(command, 1 if "is-active" in command else 0, "", ""))
    def failed_install(args, plan):
        link.unlink()
        link.symlink_to(new_target)
        (plugins / "new").symlink_to(root / "releases/new/plugins/new")
        raise RuntimeError("injected failure")
    monkeypatch.setattr(module, "install", failed_install)
    assert module.update(SimpleNamespace(profile="demo", runtime_manifest=None), {"profile_home": str(profile)}) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "rolled-back"
    assert link.resolve() == old_target and not (plugins / "new").is_symlink()


def test_update_stops_started_services_before_restoring_release(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    old, new = root / "releases/old", root / "releases/new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (root / "current").symlink_to(old)
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_release_valid", lambda release: True)
    monkeypatch.setattr(module, "_manifest", lambda args: (root / "install-manifest.json", {"release": str(new)}))
    calls = []
    worker_failed = False
    def run(command, **kwargs):
        nonlocal worker_failed
        calls.append(tuple(command))
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 1 if "superagent-memory-demo.service" in command else 0, "", "")
        if "start" in command and "superagent-worker-demo.service" in command and not worker_failed:
            worker_failed = True
            raise RuntimeError("injected startup failure")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(module, "_run", run)
    def install(args, plan):
        (root / "current").unlink()
        (root / "current").symlink_to(new)
        return 0
    monkeypatch.setattr(module, "install", install)
    def activate(args):
        calls.append(("restore-release",))
        (root / "current").unlink()
        (root / "current").symlink_to(old)
        return 0
    monkeypatch.setattr(module, "activate_release", activate)
    assert module.update(SimpleNamespace(profile="demo", runtime_manifest=None), {"profile_home": str(profile)}) == 1
    rollback = calls.index(("restore-release",))
    assert calls[:rollback].count(("systemctl", "--user", "stop", "hermes-gateway-demo.service")) == 2
    assert calls[:rollback].count(("systemctl", "--user", "stop", "superagent-worker-demo.service")) == 2
    assert (root / "current").resolve() == old
    assert json.loads(capsys.readouterr().out)["status"] == "rolled-back"


def test_update_refuses_release_rollback_if_new_gateway_cannot_stop(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    old, new = root / "releases/old", root / "releases/new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (root / "current").symlink_to(old)
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_release_valid", lambda release: True)
    monkeypatch.setattr(module, "_manifest", lambda args: (root / "install-manifest.json", {"release": str(new)}))
    count = {"stops": 0}
    def run(command, **kwargs):
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0 if "hermes-gateway-demo.service" in command else 1, "", "")
        if "stop" in command and "hermes-gateway-demo.service" in command:
            count["stops"] += 1
            if count["stops"] == 2:
                raise RuntimeError("gateway stop not proven")
        if "start" in command and "hermes-gateway-demo.service" in command:
            raise RuntimeError("start failed")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(module, "_run", run)
    def install(args, plan):
        (root / "current").unlink()
        (root / "current").symlink_to(new)
        return 0
    monkeypatch.setattr(module, "install", install)
    monkeypatch.setattr(module, "activate_release", lambda args: pytest.fail("must not switch release while gateway may run"))
    assert module.update(SimpleNamespace(profile="demo", runtime_manifest=None), {"profile_home": str(profile)}) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "rollback-incomplete" and "stop-gateway" in report["rollback_errors"]
    assert (root / "current").resolve() == new


def test_candidate_command_enforces_its_timeout(tmp_path, monkeypatch):
    module = _load_installer()
    observed = []
    monkeypatch.setattr(module, "_candidate_bootstrap_command", lambda work: ["fake-hermes"])
    monkeypatch.setattr(module, "_candidate_env", lambda work: {})
    def timed_run(command, **kwargs):
        observed.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(module, "_run", timed_run)
    with pytest.raises(RuntimeError, match="candidate_command_timeout"):
        module._run_candidate(tmp_path, ["--version"], timeout=7)
    assert observed == [7]


def test_staged_clean_marker_cannot_authorize_different_patched_host(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    host = tmp_path / ".hermes/hermes-agent"
    (host / "plugins/platforms/feishu/adapter.py").write_text("def _build_reply_card(): pass\n# old card implementation\n")
    root = tmp_path / ".hermes/superagent/demo"
    marker = root / "hermes" / (module.CANDIDATE_TAG + "-" + module.CANDIDATE_COMMIT[:12]) / "adapter-is-clean"
    marker.parent.mkdir(parents=True)
    marker.write_text(module.CANDIDATE_COMMIT)
    monkeypatch.setattr(module, "_git_identity", lambda: ("candidate", "candidate"))
    monkeypatch.setattr(module, "_plugin_names", lambda args: ["example"])
    monkeypatch.setattr(module, "_external_plugin_sources", lambda args: [])
    monkeypatch.setattr(module, "_keep_patch_state", lambda root: [])
    monkeypatch.setattr(module, "_legacy_patch_state", lambda root: [{"id": "card-replies", "in_place": False}])
    monkeypatch.setattr(module, "_profile_home", lambda args: profile)
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    plan = module.build_plan(SimpleNamespace(hermes_root=host, runtime_manifest=None, profile="demo"))
    assert "legacy_card_patches_require_clean_candidate" in plan["blockers"]


def test_migration_preserves_profile_model_settings_and_comments(tmp_path):
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    original = ("# custom profile\nmodel:\n  default: custom-model  # keep provider choice\n"
                "streaming:\n  enabled: false  # intentionally disabled\n"
                "plugins:\n  entries:\n    task-status:\n      settings:\n"
                "        engine_root: /home/caixuf/code/super-agent\n")
    config.write_text(original)
    proc = _run(tmp_path, "migrate-config", check=True)
    migrated = config.read_text()
    expected = original.replace("/home/caixuf/code/super-agent", str(tmp_path / ".hermes/superagent/demo/current"))
    assert migrated == expected
    assert json.loads(proc.stdout)["status"] == "migrated"


def test_explicit_runtime_invariants_fail_closed_without_exposing_values(tmp_path):
    profile = _seed_profile(tmp_path)
    (profile / "config.yaml").write_text("model:\n  default: private-model-name\nplatforms:\n  feishu:\n    card_replies: 1\n")
    manifest = tmp_path / "runtime.yaml"
    manifest.write_text("profile: demo\nruntime:\n  config_invariants:\n    platforms.feishu.card_replies: true\n")
    proc = _run(tmp_path, "plan", "--runtime-manifest", str(manifest))
    assert proc.returncode != 0
    report = json.loads(proc.stdout)
    assert "runtime_invariants_mismatch" in report["blockers"]
    assert report["config_invariants"] == [{"path": "platforms.feishu.card_replies", "status": "mismatch"}]
    assert "private-model-name" not in proc.stdout + proc.stderr
    (profile / "config.yaml").write_text("platforms:\n  feishu:\n    card_replies: true\n")
    proc = _run(tmp_path, "plan", "--runtime-manifest", str(manifest))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["config_invariants"][0]["status"] == "matched"


def test_missing_invariant_does_not_match_expected_null(tmp_path):
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    manifest = tmp_path / "runtime.yaml"
    manifest.write_text("profile: demo\nruntime:\n  config_invariants:\n    missing.path: null\n")
    assert module._check_runtime_invariants(manifest, profile, "demo") == [{"path": "missing.path", "status": "mismatch"}]


def test_native_card_disables_old_producer_but_keeps_its_data(tmp_path):
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    config.write_text("plugins:\n  enabled:\n  - hermes-feishu-card\n  - hfc-runtime-dependency\n  - useful-plugin\n")
    data = profile / "plugin-data/hfc/history"
    data.parent.mkdir(parents=True)
    data.write_text("preserve")
    module._disable_legacy_card_producer(profile)
    assert yaml.safe_load(config.read_text())["plugins"]["enabled"] == ["useful-plugin"]
    assert data.read_text() == "preserve"


def test_unhandled_legacy_plugin_config_fails_closed(tmp_path):
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    (profile / "config.yaml").write_text("plugins:\n  enabled: [hermes-feishu-card, useful-plugin]\n")
    with pytest.raises(ValueError, match="legacy card producer"):
        module._disable_legacy_card_producer(profile)


def test_migration_does_not_rewrite_comments_or_secret_values(tmp_path):
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    old = "/home/caixuf/code/super-agent"
    original = (f"# historical path {old}\nmodel:\n  api_key: '{old}/credential'\n"
                f"plugins:\n  entries:\n    durable-dispatch:\n      settings:\n"
                f"        engine_root: '{old}' # unchanged comment {old}\n"
                f"        allowed_roots: ['{old}/src']\n"
                f"        note: 'The source used to be {old}'\n")
    config.write_text(original)
    _run(tmp_path, "migrate-config", check=True)
    got = config.read_text()
    new = str(tmp_path / ".hermes/superagent/demo/current")
    assert f"# historical path {old}" in got
    assert f"api_key: '{old}/credential'" in got
    assert f"engine_root: '{new}' # unchanged comment {old}" in got
    assert f"allowed_roots: ['{new}/src']" in got
    assert f"note: 'The source used to be {old}'" in got


def test_disabling_retired_progress_does_not_modify_other_sections(tmp_path):
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    config.write_text("other:\n  enabled:\n    - task-progress\nplugins:\n  enabled:\n    - task-progress\n    - retained\n")
    module._purge_legacy_plugins(profile)
    text = config.read_text()
    assert "other:\n  enabled:\n    - task-progress\n" in text
    import yaml
    assert yaml.safe_load(text)["plugins"]["enabled"] == ["retained"]


def test_retired_progress_inline_list_fails_closed(tmp_path):
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    config.write_text("plugins:\n  enabled: [task-progress, retained]\n")
    import pytest
    with pytest.raises(ValueError, match="retired progress producer"):
        module._purge_legacy_plugins(profile)
    assert "task-progress" in config.read_text()


def test_missing_plugin_allowlist_cannot_prove_retired_producers_disabled(tmp_path):
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    (profile / "config.yaml").write_text("plugins:\n  entries: {}\n")
    import pytest
    with pytest.raises(ValueError, match="retired progress producer"):
        module._purge_legacy_plugins(profile)
    with pytest.raises(ValueError, match="legacy card producer"):
        module._disable_legacy_card_producer(profile)


def test_service_quiescence_checks_populated_control_group(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pytest
    module = _load_installer()
    monkeypatch.setattr(module, "_run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="/user.slice/demo.service\n"))
    group = tmp_path / "user.slice/demo.service"
    group.mkdir(parents=True)
    events = group / "cgroup.events"
    events.write_text("populated 1\nfrozen 0\n")
    with pytest.raises(RuntimeError, match="not empty"):
        module._assert_service_quiescent("demo.service", cgroup_root=tmp_path)
    events.write_text("populated 0\nfrozen 0\n")
    module._assert_service_quiescent("demo.service", cgroup_root=tmp_path)
    events.unlink()
    with pytest.raises(RuntimeError, match="state unavailable"):
        module._assert_service_quiescent("demo.service", cgroup_root=tmp_path)


def test_update_refuses_inactive_service_with_unproven_stop(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / "managed"
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_update_config_snapshot", lambda *a: ({}, "hermes-gateway-demo.service"))
    monkeypatch.setattr(module, "_plugin_link_snapshot", lambda *a: {})
    monkeypatch.setattr(module, "_run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""))
    def blocked(name):
        raise RuntimeError("service control group is not empty")
    monkeypatch.setattr(module, "_assert_service_quiescent", blocked)
    monkeypatch.setattr(module, "install", lambda *a: pytest.fail("must not install"))
    args = SimpleNamespace(profile="demo")
    assert module.update(args, {"profile_home": str(profile)}) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "service_stop_unproven"


def test_rollback_refuses_switch_if_stopped_gateway_cgroup_remains_populated(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / "managed"
    old, new = root / "releases/old", root / "releases/new"
    old.mkdir(parents=True); new.mkdir(parents=True)
    (root / "current").symlink_to(old)
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_release_valid", lambda release: True)
    monkeypatch.setattr(module, "_manifest", lambda args: (root / "install-manifest.json", {"release": str(new)}))
    checks = {"gateway": 0}
    def quiescent(name):
        if name == "hermes-gateway-demo.service":
            checks["gateway"] += 1
            if checks["gateway"] == 2: raise RuntimeError("service control group is not empty")
    monkeypatch.setattr(module, "_assert_service_quiescent", quiescent)
    def run(command, **kwargs):
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0 if "hermes-gateway-demo.service" in command else 1, "", "")
        if "start" in command and "hermes-gateway-demo.service" in command:
            raise RuntimeError("start failed")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(module, "_run", run)
    def install(args, plan):
        (root / "current").unlink(); (root / "current").symlink_to(new)
        return 0
    monkeypatch.setattr(module, "install", install)
    monkeypatch.setattr(module, "activate_release", lambda args: pytest.fail("must not switch under remaining process"))
    assert module.update(SimpleNamespace(profile="demo", runtime_manifest=None), {"profile_home": str(profile)}) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "rollback-incomplete" and "stop-gateway" in report["rollback_errors"]
    assert (root / "current").resolve() == new

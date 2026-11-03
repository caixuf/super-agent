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

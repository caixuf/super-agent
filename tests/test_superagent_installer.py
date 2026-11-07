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


def test_candidate_python_uses_current_home_or_path_not_another_user(tmp_path, monkeypatch):
    module = _load_installer()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/python3')
    assert module._hermes_python() == Path('/usr/bin/python3')


@pytest.mark.parametrize('tools_exit', [0, 1])
def test_candidate_installs_tools_before_python_dependencies(tmp_path, monkeypatch, tools_exit):
    module = _load_installer()
    calls = []
    def run(work, args, timeout):
        calls.append(args)
        code = tools_exit if len(calls) == 1 else 0
        return subprocess.CompletedProcess(args, code, 'output', '')
    monkeypatch.setattr(module, '_run_candidate', run)
    result = module._sync_candidate_dependencies(tmp_path)
    assert calls[0] == ['pm', 'install', '--tools-only']
    if tools_exit:
        assert len(calls) == 1 and result.returncode == tools_exit
    else:
        assert calls[1] == ['pm', 'install', '--extra', 'all']
        assert result.returncode == 0 and result.stdout == 'outputoutput'


@pytest.mark.parametrize('timeout_call', [1, 2])
def test_candidate_launcher_timeout_is_bounded_and_reported(tmp_path, monkeypatch, timeout_call):
    module = _load_installer()
    launcher = tmp_path / '.hermes/bin/hermes'
    launcher.parent.mkdir(parents=True)
    launcher.write_text(str(tmp_path))
    monkeypatch.setattr(module, '_candidate_python', lambda work: Path('/usr/bin/python3'))
    calls = []
    def run(cmd, **kwargs):
        calls.append(kwargs)
        if len(calls) == timeout_call:
            raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])
        return subprocess.CompletedProcess(cmd, 0, '', '')
    monkeypatch.setattr(module, '_run', run)
    with pytest.raises(RuntimeError, match='candidate_launcher_.*_timeout'):
        module._publish_candidate_launcher(tmp_path)
    assert all(call['timeout'] == 120 for call in calls)


@pytest.mark.parametrize('phase', ['dependencies', 'smoke'])
def test_candidate_command_timeout_reports_blocked(tmp_path, monkeypatch, phase):
    module = _load_installer()
    monkeypatch.setattr(module, '_managed_root', lambda args: tmp_path)
    def run(cmd, **kwargs):
        if cmd[1] == 'clone':
            Path(cmd[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(cmd, 0, '', '')
        return subprocess.CompletedProcess(cmd, 0, module.CANDIDATE_COMMIT, '')
    monkeypatch.setattr(module, '_run', run)
    monkeypatch.setattr(module, '_apply_keep_patches', lambda work: [])
    monkeypatch.setattr(module, '_legacy_patch_state', lambda work: [])
    monkeypatch.setattr(module, '_hermes_python', lambda: Path('/usr/bin/python3'))
    def timeout(work):
        raise RuntimeError('candidate_command_timeout')
    if phase == 'dependencies':
        monkeypatch.setattr(module, '_sync_candidate_dependencies', timeout)
    else:
        monkeypatch.setattr(module, '_sync_candidate_dependencies',
                            lambda work: subprocess.CompletedProcess([], 0, '', ''))
        monkeypatch.setattr(module, '_publish_candidate_launcher', lambda work: None)
        monkeypatch.setattr(module, '_smoke_candidate', timeout)
    result = module.build_clean_hermes_candidate(None, {})
    assert result['status'] == 'blocked' and result['reason'] == 'candidate_command_timeout'


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


def test_candidate_builder_is_separate_and_does_not_touch_live_hermes(tmp_path, monkeypatch):
    """A failed clone must not reuse stale candidate data or touch the live checkout."""
    import shlex
    real_git = shutil.which('git')
    if not real_git:
        pytest.skip('git is required to build the installation plan')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    git = bin_dir / 'git'
    git.write_text('#!/bin/sh\nif [ "$1" = clone ]; then exit 128; fi\nexec '
                   + shlex.quote(real_git) + ' "$@"\n')
    git.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))
    profile = _seed_profile(tmp_path)
    profile_before = {name: (profile / name).read_bytes() for name in ('config.yaml', '.env')}
    live_adapter = tmp_path / ".hermes/hermes-agent/plugins/platforms/feishu/adapter.py"
    before = live_adapter.read_bytes()
    stale = tmp_path / ".hermes/superagent/demo/hermes/v0.22.5-bf0203a794a0"
    stale.mkdir(parents=True)
    (stale / "sentinel").write_text("old")
    proc = _run(tmp_path, "candidate-hermes", check=False)
    assert json.loads(proc.stdout)['reason'] == 'candidate_clone_failed'
    assert live_adapter.read_bytes() == before
    assert all((profile / name).read_bytes() == contents for name, contents in profile_before.items())
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


def test_plan_blocks_card_if_update_outcome_guard_is_missing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _load_installer()
    _seed_profile(tmp_path)
    monkeypatch.setattr(module, "_git_identity", lambda: ("candidate", "candidate"))
    monkeypatch.setattr(module, "_plugin_names", lambda args: ["example"])
    monkeypatch.setattr(module, "_external_plugin_sources", lambda args: [])
    monkeypatch.setattr(module, "_keep_patch_state", lambda root: [])
    monkeypatch.setattr(module, "_legacy_patch_state", lambda root: [{"id": "card-replies", "in_place": True}, {"id": "card-native-markdown", "in_place": False}])
    monkeypatch.setattr(module, "_patch_state", lambda root, spec: {"in_place": spec[1] == "card-lifecycle"})
    monkeypatch.setattr(module, "_profile_home", lambda args: tmp_path / ".hermes/profiles/demo")
    monkeypatch.setattr(module, "_managed_root", lambda args: tmp_path / ".hermes/superagent/demo")
    args = SimpleNamespace(hermes_root=tmp_path / ".hermes/hermes-agent", runtime_manifest=None, profile="demo")
    assert "card_delivery_guard_not_verified" in module.build_plan(args)["blockers"]


def test_plan_refuses_enabled_feishu_card_without_host_implementation(tmp_path):
    profile = _seed_profile(tmp_path)
    (profile / "config.yaml").write_text("platforms:\n  feishu:\n    enabled: true\n    card_replies: true\nplugins:\n  enabled: []\n  disabled: []\n")
    result = _run(tmp_path, "plan")
    assert result.returncode != 0
    plan = json.loads(result.stdout)
    assert "feishu_card_implementation_unverified" in plan["blockers"]


def test_doctor_reports_configured_card_missing_from_host(tmp_path):
    profile = _seed_profile(tmp_path)
    _run(tmp_path, "install", check=True)
    (profile / "config.yaml").write_text("platforms:\n  feishu:\n    enabled: true\n    card_replies: true\nplugins:\n  enabled: []\n  disabled: []\n")
    result = _run(tmp_path, "doctor")
    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["status"] == "broken"
    assert report["feishu_card_observation"]["status"] == "mismatch"
    assert not report["feishu_card_observation"]["checks"]["card-replies"]


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
    assert f"allowed_roots: ['{old}/src']" in got
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


def test_explicit_legacy_engine_migration_preserves_business_paths():
    module = _load_installer()
    original = ("plugins:\n  entries:\n    worker:\n      settings:\n"
                "        engine_root: /home/worker/code/agentcore\n"
                "        allowed_roots:\n        - /home/worker/code/agentcore\n"
                "        queue_root: /home/worker/.local/state/agentcore/analysis-queue\n"
                "business:\n  source: /home/worker/code/agentcore\n")
    result = module._migrate_plugin_engine_roots(original, ["/home/worker/code/agentcore"], "/managed/current")
    assert "engine_root: /managed/current" in result
    assert "- /home/worker/code/agentcore" in result
    assert "source: /home/worker/code/agentcore" in result
    assert "/home/worker/.local/state/agentcore/analysis-queue" in result


def test_explicit_legacy_engine_migration_rejects_shared_yaml_alias():
    module = _load_installer()
    original = ("business_path: &engine /home/worker/code/agentcore\n"
                "plugins:\n  entries:\n    worker:\n      settings:\n        engine_root: *engine\n")
    with pytest.raises(ValueError, match="aliased engine path"):
        module._migrate_plugin_engine_roots(original, ["/home/worker/code/agentcore"], "/managed/current")


def test_worker_engine_migration_preserves_business_pythonpath_and_is_idempotent(tmp_path, monkeypatch):
    module = _load_installer()
    old = "/home/worker/code/agentcore"
    desired = tmp_path / "managed" / "current"
    business = "/opt/business/app"
    environment = [f"PYTHONPATH={old}:{business}"]

    def run(command, **kwargs):
        assert command[:4] == ["systemctl", "--user", "show", "superagent-worker-sample.service"]
        output = "LoadState=loaded\nExecStart=python -m agentcore.pool\nEnvironment=" + environment[0] + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(module, "_run", run)
    first = module._planned_worker_pythonpath("sample", desired, [old])
    assert first["pythonpath"] == f"{desired}:{business}"
    environment[0] = f"PYTHONPATH={first['pythonpath']}"
    assert module._planned_worker_pythonpath("sample", desired, [old]) == first


def test_worker_engine_migration_rejects_unrelated_worker(tmp_path, monkeypatch):
    module = _load_installer()
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0,
            "LoadState=loaded\nExecStart=python -m other.service\nEnvironment=PYTHONPATH=/old\n", "")
    monkeypatch.setattr(module, "_run", run)
    with pytest.raises(ValueError, match="worker_unit_not_recognized"):
        module._planned_worker_pythonpath("sample", tmp_path / "current", ["/old"])


def test_worker_engine_detection_rejects_python_module_and_zip_shadow(tmp_path):
    import zipfile
    module = _load_installer()
    managed = tmp_path / "managed"
    (managed / "agentcore").mkdir(parents=True)
    (managed / "agentcore" / "__init__.py").write_text("")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "agentcore.py").write_text("")
    assert module._first_engine_path_matches(f"{shadow}:{managed}", managed) is False
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("agentcore/__init__.py", "")
    assert module._first_engine_path_matches(f"{archive}:{managed}", managed) is False
    assert module._first_engine_path_matches(str(managed), managed) is True


def test_first_update_failure_restores_old_plugin_directory_and_clears_current(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    release = root / "releases/new"
    new_plugin = release / "plugins/business"
    new_plugin.mkdir(parents=True)
    plugins = profile / "plugins"
    plugins.mkdir()
    old = plugins / "business"
    old.mkdir()
    (old / "business_data").write_text("original")
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_run", lambda command, **kw: subprocess.CompletedProcess(command, 1 if "is-active" in command else 0, "", ""))
    def failed_install(args, plan):
        old.rename(args._plugin_rollback_dir / "business")
        old.symlink_to(new_plugin)
        (root / "current").symlink_to(release)
        raise RuntimeError("injected failure")
    monkeypatch.setattr(module, "install", failed_install)
    result = module.update(SimpleNamespace(profile="demo", runtime_manifest=None),
                           {"profile_home": str(profile), "release": str(release)})
    assert result == 1
    assert json.loads(capsys.readouterr().out)["status"] == "rolled-back"
    assert old.is_dir() and not old.is_symlink()
    assert (old / "business_data").read_text() == "original"
    assert not (root / "current").exists() and not (root / "current").is_symlink()


def test_update_restarts_declared_services_and_checks_loaded_path(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    release = root / "releases/new"
    release.mkdir(parents=True)
    manifest = tmp_path / "runtime.yaml"
    manifest.write_text("profile: demo\nruntime:\n  drop_ins: {}\n  managed_services:\n    - agentcore-memory-demo.service\n")
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_assert_service_quiescent", lambda _: None)
    monkeypatch.setattr(module, "_release_valid", lambda _: True)
    monkeypatch.setattr(module, "_manifest", lambda args: (root / "manifest.json", {"release": str(release)}))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        active = "is-active" in command and "agentcore-memory-demo.service" in command
        return subprocess.CompletedProcess(command, 0 if active or "is-active" not in command else 1, "", "")
    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setattr(module, "install", lambda args, plan: 0)
    seen = []
    monkeypatch.setattr(module, "_managed_service_import_observation", lambda service, path: seen.append((service, path)) or True)
    args = SimpleNamespace(profile="demo", runtime_manifest=manifest)
    assert module.update(args, {"profile_home": str(profile)}) == 0
    assert any("stop" in cmd and "agentcore-memory-demo.service" in cmd for cmd in calls)
    assert any("start" in cmd and "agentcore-memory-demo.service" in cmd for cmd in calls)
    assert seen == [("agentcore-memory-demo.service", root / "current")]


def test_invalid_or_duplicate_declared_services_are_rejected(tmp_path):
    from types import SimpleNamespace
    module = _load_installer()
    manifest = tmp_path / "runtime.yaml"
    for entry in ("[bad/path.service]", "[same.service, same.service]", "[{}]"):
        manifest.write_text("profile: demo\nruntime:\n  managed_services: " + entry + "\n")
        with pytest.raises(ValueError, match="invalid_runtime_services"):
            module._declared_runtime_services(SimpleNamespace(profile="demo", runtime_manifest=manifest))


def test_consolidated_worker_does_not_create_an_override(tmp_path, monkeypatch):
    module = _load_installer()
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_planned_worker_pythonpath", lambda *_: {"service": "superagent-worker-demo.service", "pythonpath": "/managed/current"})
    monkeypatch.setattr(module, "_worker_runtime_observation", lambda *_: {"configured_pythonpath_matches": True})
    result = module._apply_worker_pythonpath("demo", tmp_path / "current", ["/legacy"])
    assert result["configured"] is True and result["override"] is None
    assert not (tmp_path / ".config/systemd/user/superagent-worker-demo.service.d/99-superagent-engine.conf").exists()


def test_declared_script_services_require_owned_service_and_absolute_path(tmp_path):
    from types import SimpleNamespace
    module = _load_installer()
    manifest = tmp_path / "runtime.yaml"
    manifest.write_text("profile: demo\nruntime:\n  managed_services: [alert.service]\n  managed_script_services:\n    alert.service: /opt/business/alert.py\n")
    args = SimpleNamespace(profile="demo", runtime_manifest=manifest)
    assert module._declared_runtime_scripts(args, ["alert.service"]) == {"alert.service": "/opt/business/alert.py"}
    with pytest.raises(ValueError, match="invalid_runtime_scripts"):
        module._declared_runtime_scripts(args, [])
    manifest.write_text("profile: demo\nruntime:\n  managed_services: [alert.service]\n  managed_script_services:\n    alert.service: ../alert.py\n")
    with pytest.raises(ValueError, match="invalid_runtime_scripts"):
        module._declared_runtime_scripts(args, ["alert.service"])


def test_declared_services_restart_in_reverse_stop_order(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    root = tmp_path / ".hermes/superagent/demo"
    release = root / "releases/new"
    release.mkdir(parents=True)
    manifest = tmp_path / "runtime.yaml"
    manifest.write_text("profile: demo\nruntime:\n  drop_ins: {}\n  managed_services: [monitor.service, alert.service]\n")
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_assert_service_quiescent", lambda _: None)
    monkeypatch.setattr(module, "_release_valid", lambda _: True)
    monkeypatch.setattr(module, "_manifest", lambda args: (root / "manifest.json", {"release": str(release)}))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        active = "is-active" in command and command[-1] in {"monitor.service", "alert.service"}
        return subprocess.CompletedProcess(command, 0 if active or "is-active" not in command else 1, "", "")
    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setattr(module, "install", lambda args, plan: 0)
    monkeypatch.setattr(module, "_managed_service_import_observation", lambda *_args, **_kwargs: True)
    assert module.update(SimpleNamespace(profile="demo", runtime_manifest=manifest), {"profile_home": str(profile)}) == 0
    assert [c[-1] for c in calls if "stop" in c] == ["monitor.service", "alert.service"]
    assert [c[-1] for c in calls if "start" in c] == ["alert.service", "monitor.service"]


def test_doctor_recall_observation_reports_missing_and_healthy_without_query(tmp_path):
    import time
    module = _load_installer()
    profiles = tmp_path / "profiles"
    main = profiles / "sample"
    member = profiles / "sample-architect"
    main.mkdir(parents=True)
    member.mkdir()
    report = tmp_path / "recall.json"
    report.write_text(json.dumps({"at": time.time(), "code": "hit", "count": 2, "query": "private text"}))
    report.chmod(0o600)
    main_config = {"memory": {"localvec": {"recall_cmd": ["x", "--diagnostic-file", str(report)]}},
                   "plugins": {"entries": {"colleague-team": {"settings": {"gateway_dispatch": {
                       "enabled": True, "tool_backend": {"members": [{"profile": "sample-architect", "home": str(member)}]}}}}}}}
    (main / "config.yaml").write_text(yaml.safe_dump(main_config))
    (member / "config.yaml").write_text(yaml.safe_dump({"memory": {"localvec": {
        "recall_cmd": ["x", "--diagnostic-file", str(tmp_path / "missing.json")]}}}))
    result = module._recall_diagnostics_observation(main)
    assert result["sources"]["sample"]["status"] == "healthy"
    assert result["sources"]["sample-architect"] == {"status": "unknown", "reason": "not_observed"}
    assert "private text" not in repr(result)


def test_doctor_recall_observation_refuses_foreign_profile_home(tmp_path):
    module = _load_installer()
    main = tmp_path / "profiles" / "sample"
    main.mkdir(parents=True)
    config = {"plugins": {"entries": {"colleague-team": {"settings": {"gateway_dispatch": {
        "enabled": True, "tool_backend": {"members": [{"profile": "sample-test", "home": str(tmp_path / "elsewhere" / "sample-test")}]}}}}}}}
    (main / "config.yaml").write_text(yaml.safe_dump(config))
    assert module._recall_diagnostics_observation(main)["status"] == "unknown"


def test_doctor_distinguishes_disk_binding_from_recall_health(tmp_path):
    module = _load_installer()
    home = tmp_path / "profiles" / "sample"
    home.mkdir(parents=True)
    release = tmp_path / "releases" / "v1"
    plugin = release / "plugins" / "localvec"
    plugin.mkdir(parents=True)
    (home / "plugins").mkdir()
    (home / "plugins" / "localvec").symlink_to(plugin)
    current = tmp_path / "current"
    current.symlink_to(release)
    config = {"memory": {"localvec": {"recall_cmd": ["/usr/bin/env", f"AGENTCORE_ROOT={current}", "python3"]}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    obs = module._recall_diagnostics_observation(home, current)
    assert obs["sources"]["sample"]["status"] == "unknown"
    assert obs["disk_bindings"]["sample"] == {"plugin": "matched", "engine": "matched"}
    assert obs["binding_scope"] == "configured_files_only"
    config["memory"]["localvec"]["recall_cmd"][1] = "AGENTCORE_ROOT=/old/release"
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    assert module._recall_diagnostics_observation(home, current)["disk_bindings"]["sample"]["engine"] == "mismatched"


def test_doctor_queue_observation_counts_blocked_without_exposing_content(tmp_path):
    import sqlite3
    module = _load_installer()
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE session_admissions (status TEXT, target_session_id TEXT, payload TEXT)")
        db.executemany("INSERT INTO session_admissions VALUES (?, ?, ?)", [
            ("unknown", "a", "private"), ("queued", "a", "secret"),
            ("queued", "a", "secret2"), ("queued", "b", "secret3"),
            ("started", "c", "secret4"), ("terminal", "d", "secret5")])
    report = module._gateway_queue_observation(tmp_path)
    assert report == {"state": "observed", "queued": 3, "started": 1, "unknown": 1,
                      "queued_blocked_by_unknown": 2}
    assert "secret" not in repr(report) and "private" not in repr(report)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM session_admissions").fetchone()[0] == 6


def test_doctor_queue_observation_does_not_claim_missing_or_invalid_database_is_empty(tmp_path):
    import sqlite3
    module = _load_installer()
    assert module._gateway_queue_observation(tmp_path)["state"] == "unknown"
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    assert module._gateway_queue_observation(tmp_path)["state"] == "unknown"


def test_doctor_cron_observation_reports_failure_without_sensitive_content(tmp_path):
    import sqlite3
    module = _load_installer()
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{"enabled": True, "prompt": "secret"}]}))
    with sqlite3.connect(cron / "executions.db") as db:
        db.execute("CREATE TABLE executions (status TEXT, delivery_outcome TEXT, started_at TEXT)")
        db.execute("CREATE TABLE cron_incidents (state TEXT)")
        db.execute("INSERT INTO executions VALUES ('failed', 'suppressed', '2026-11-04')")
        db.executemany("INSERT INTO cron_incidents VALUES (?)", [('detected',), ('closed',)])
    result = module._cron_observation(tmp_path)
    assert result == {"state": "observed", "enabled_jobs": 1, "latest_status": "failed",
                      "latest_delivery": "suppressed", "open_incidents": 1}
    assert "secret" not in repr(result)


def test_doctor_cron_observation_missing_or_corrupt_is_unknown(tmp_path):
    module = _load_installer()
    assert module._cron_observation(tmp_path)["state"] == "unknown"
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron/jobs.json").write_text("not-json")
    (tmp_path / "cron/executions.db").write_bytes(b"bad")
    assert module._cron_observation(tmp_path)["state"] == "unknown"


def test_migration_keeps_checkout_knowledge_sources_while_moving_engine(tmp_path):
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    old = "/home/caixuf/code/super-agent"
    original = ("plugins:\n  entries:\n    local-model-grounding:\n      settings:\n"
                f"        engine_root: {old}\n        docs_roots: [{old}/docs]\n"
                f"        index_dir: {old}/index  # operator data\n")
    config.write_text(original)
    _run(tmp_path, "migrate-config", check=True)
    updated = config.read_text()
    assert f"engine_root: {tmp_path}/.hermes/superagent/demo/current" in updated
    assert f"docs_roots: [{old}/docs]" in updated
    assert f"index_dir: {old}/index  # operator data" in updated


def test_update_rolls_back_post_migration_invariant_drift_before_start(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    config.write_text("plugins:\n  entries:\n    grounding:\n      settings:\n        enabled: true\n")
    original = config.read_bytes()
    root = tmp_path / ".hermes/superagent/demo"
    root.mkdir(parents=True)
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("profile: demo\nruntime:\n  config_invariants:\n"
                       "    plugins.entries.grounding.settings.enabled: true\n")
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_managed_root", lambda args: root)
    monkeypatch.setattr(module, "_assert_service_quiescent", lambda name: None)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if "is-active" in command else 0, "", "")
    monkeypatch.setattr(module, "_run", run)
    def install(args, plan):
        config.write_text("plugins:\n  entries:\n    grounding:\n      settings:\n        enabled: false\n")
        return 0
    monkeypatch.setattr(module, "install", install)
    monkeypatch.setattr(module, "_manifest", lambda args: (root / "install-manifest.json", {"release": str(root / "candidate")}))
    monkeypatch.setattr(module, "_release_valid", lambda path: True)
    args = SimpleNamespace(profile="demo", runtime_manifest=runtime)
    assert module.update(args, {"profile_home": str(profile)}) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "rolled-back"
    assert result["error"] == "runtime_invariants_changed_after_install"
    assert config.read_bytes() == original
    assert not any("start" in call for call in calls)


def test_install_blocks_predicted_invariant_drift_before_any_mutation(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = _load_installer()
    profile = _seed_profile(tmp_path)
    config = profile / "config.yaml"
    old = "/home/caixuf/.local/state/agentcore/analysis-queue"
    config.write_text("custom:\n  data_path: " + old + "\n")
    before = config.read_bytes()
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("profile: demo\nruntime:\n  config_invariants:\n    custom.data_path: " + old + "\n")
    root = tmp_path / ".hermes/superagent/demo"
    managed = {"engine_root": str(root / "current"), "queue_root": str(root / "state/queue"),
               "memory_spool": str(root / "state/memory"), "markdown_outbox": str(root / "state/outbox"),
               "liveness_db": str(root / "state/liveness.db"), "colleague_registry": str(root / "state/registry.db")}
    monkeypatch.setattr(module, "_snapshot_external_plugins", lambda args: pytest.fail("mutation before preview"))
    args = SimpleNamespace(profile="demo", runtime_manifest=runtime, legacy_engine_root=())
    plan = {"profile_home": str(profile), "managed_root": str(root), "managed_paths": managed}
    assert module.install(args, plan) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "migration_would_change_runtime_invariants"
    assert config.read_bytes() == before and not root.exists()


def test_staged_release_carries_the_managed_worker_runner(tmp_path, monkeypatch):
    """The dev worker must run the release's runner, not a profile-side copy."""
    module = _load_installer()
    source = tmp_path / "repo"
    for rel in ("agentcore/liveness.py", "plugins/demo/__init__.py", "skills/readme.md",
                "install/worker/dev_exec_runner.py", "install/superagent.sh"):
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# " + rel + chr(10), encoding="utf-8")
    monkeypatch.setattr(module, "REPO", source)
    monkeypatch.setattr(module, "PLUGIN_SOURCES", (source / "plugins/demo",))
    plan = {"release": str(tmp_path / "managed/releases/test"),
            "version": "test", "identity": "test", "external_plugins": []}
    release = module._stage_release(plan)
    runner = release / "install/worker/dev_exec_runner.py"
    assert runner.is_file()
    assert runner.read_text(encoding="utf-8") == "# install/worker/dev_exec_runner.py" + chr(10)


def test_prune_releases_removes_old_releases_and_tarballs_keeping_only_active(tmp_path):
    _seed_profile(tmp_path)
    first = json.loads(_run(tmp_path, "install", check=True).stdout)
    root = Path(first["managed_root"])
    active = Path(first["release"])
    stale_dir = root / "releases" / "old-release-001"
    stale_dir.mkdir(parents=True)
    (stale_dir / "dummy.txt").write_text("stale")
    stale_tar = root / "releases" / "old-bundle.tar.gz"
    stale_tar.write_bytes(b"tarball")
    pruned = json.loads(_run(tmp_path, "prune-releases", check=True).stdout)
    assert pruned["status"] == "pruned"
    assert set(pruned["removed"]) == {"old-release-001", "old-bundle.tar.gz"}
    remaining = [p.name for p in (root / "releases").iterdir()]
    assert remaining == [active.name]
    manifest = json.loads((root / "install-manifest.json").read_text())
    assert manifest["release_history"] == []


def test_sync_profile_skills_refreshes_existing_local_skill_copy(tmp_path):
    module = _load_installer()
    profile = tmp_path / "profile"
    local_skill = profile / "skills" / "illustrated-doc-rendering"
    local_skill.mkdir(parents=True)
    (local_skill / "SKILL.md").write_text("# stale 11-01 skill\n", encoding="utf-8")
    release = tmp_path / "release"
    rel_skill = release / "skills" / "illustrated-doc-rendering"
    rel_skill.mkdir(parents=True)
    (rel_skill / "SKILL.md").write_text("# updated skill with progressive delivery\n", encoding="utf-8")
    synced = module._sync_profile_skills(profile, release)
    assert synced == ["illustrated-doc-rendering"]
    assert (local_skill / "SKILL.md").read_text(encoding="utf-8") == "# updated skill with progressive delivery\n"



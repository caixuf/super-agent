"""Contract tests for deploy-source guard, release retention, and doctor version truth.

Tests cover:
1. Dirty source blocking and --allow-dirty bypass.
2. The exact incident: Clone A (commit C2) and Clone B (commit C1 with uncommitted changes)
   attempting update/install from B; blocked and state byte-for-byte unchanged.
3. Forward update allowed, downgrade blocked, --allow-downgrade bypass.
4. --expect-commit verification (match allowed, mismatch blocked).
5. Retention of past releases, non-cleared release_history, successful rollback,
   and --keep 0 explicit total pruning.
6. Doctor deploy_version_observation (clean/ready, ahead/drift, dirty/drift).
7. Legacy manifests without source_commit (ancestry resolution and unpinned warning).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REAL_REPO = Path(__file__).resolve().parents[1]
TOOL = REAL_REPO / "tools/superagent.py"


def _make_git_repo(path: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(REAL_REPO), str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Deploy Guard Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "guard@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "receive.denyCurrentBranch", "ignore"], check=True)
    return path


def _make_commit(repo: Path, message: str) -> str:
    marker = repo / ".test_guard_marker"
    marker.write_text(f"{message}\n{time.time()}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".test_guard_marker"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, capture_output=True)
    return proc.stdout.strip()


def _seed_test_env(tmp_path: Path):
    profile = tmp_path / ".hermes/profiles/demo"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text("plugins:\n  enabled: []\n  disabled: []\n", encoding="utf-8")
    hermes = tmp_path / ".hermes/hermes-agent"
    (hermes / "plugins/platforms/feishu").mkdir(parents=True, exist_ok=True)
    (hermes / "plugins/platforms/feishu/adapter.py").write_text("# adapter stub\n", encoding="utf-8")
    managed_root = tmp_path / ".hermes/superagent"
    return profile, hermes, managed_root


def _run_tool(repo: Path, profile: Path, hermes: Path, managed_root: Path, *args: str,
              env_override: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(profile.parents[2]), SUPERAGENT_REPO=str(repo))
    env.pop("SUPERAGENT_SOURCE_DIRTY", None)
    if env_override:
        env.update(env_override)
    command = [
        sys.executable, str(TOOL),
        "--profile", "demo",
        "--profile-home", str(profile),
        "--root", str(managed_root),
        "--hermes-root", str(hermes),
        *args,
    ]
    return subprocess.run(command, env=env, text=True, capture_output=True)


def test_guard_blocks_dirty_source_and_allows_bypass(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    # Introduce dirty change
    (repo / "README.md").write_text("# modified\n", encoding="utf-8")

    # Install without bypass -> blocked
    res_blocked = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res_blocked.returncode == 1, res_blocked.stdout + res_blocked.stderr
    data_blocked = json.loads(res_blocked.stdout)
    assert data_blocked["status"] == "blocked"
    assert "source_dirty" in data_blocked["blockers"]

    # Install with --allow-dirty -> succeeds
    res_allowed = _run_tool(repo, profile, hermes, managed_root, "install", "--allow-dirty")
    assert res_allowed.returncode == 0, res_allowed.stdout + res_allowed.stderr
    data_allowed = json.loads(res_allowed.stdout)
    assert data_allowed["status"] == "installed"
    assert data_allowed["source_dirty"] is True


def test_exact_incident_clone_a_and_clone_b(tmp_path):
    """Clone A installs at C2; Clone B at older C1 with uncommitted changes attempts install/update.

    Must block on source_repo_mismatch, source_dirty, and downgrade_or_divergent (or unpulled commit).
    State on disk must remain byte-for-byte identical.
    """
    upstream = _make_git_repo(tmp_path / "upstream")
    c1 = _make_commit(upstream, "commit 1")

    # Clone A and Clone B from upstream
    clone_a = tmp_path / "clone_a"
    clone_b = tmp_path / "clone_b"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone_a)], check=True)
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone_b)], check=True)
    subprocess.run(["git", "-C", str(clone_a), "config", "user.name", "Tester A"], check=True)
    subprocess.run(["git", "-C", str(clone_a), "config", "user.email", "a@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(clone_b), "config", "user.name", "Tester B"], check=True)
    subprocess.run(["git", "-C", str(clone_b), "config", "user.email", "b@example.invalid"], check=True)

    # In Clone A, make C2 and push to upstream
    c2 = _make_commit(clone_a, "commit 2")
    subprocess.run(["git", "-C", str(clone_a), "push", "-q", "origin", "main"], check=True)

    # Install from Clone A
    profile, hermes, managed_root = _seed_test_env(tmp_path)
    res_a = _run_tool(clone_a, profile, hermes, managed_root, "install")
    assert res_a.returncode == 0, res_a.stdout + res_a.stderr

    manifest_file = managed_root / "demo" / "install-manifest.json"
    manifest_bytes_before = manifest_file.read_bytes()
    current_symlink_before = (managed_root / "demo" / "current").resolve()
    releases_before = sorted(p.name for p in (managed_root / "demo" / "releases").iterdir())

    # In Clone B: at C1 (stale), introduce an uncommitted edit (dirty)
    (clone_b / "uncommitted.txt").write_text("dirty content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone_b), "add", "uncommitted.txt"], check=True)

    # Try install from Clone B
    res_b_install = _run_tool(clone_b, profile, hermes, managed_root, "install")
    assert res_b_install.returncode == 1
    data_b_install = json.loads(res_b_install.stdout)
    assert data_b_install["status"] == "blocked"
    assert "source_repo_mismatch" in data_b_install["blockers"]
    assert "source_dirty" in data_b_install["blockers"]

    # Try update from Clone B
    res_b_update = _run_tool(clone_b, profile, hermes, managed_root, "update")
    assert res_b_update.returncode == 1
    data_b_update = json.loads(res_b_update.stdout)
    assert data_b_update["status"] == "blocked"
    assert "source_repo_mismatch" in data_b_update["blockers"]
    assert "source_dirty" in data_b_update["blockers"]

    # Verify state is byte-for-byte unchanged
    assert manifest_file.read_bytes() == manifest_bytes_before
    assert (managed_root / "demo" / "current").resolve() == current_symlink_before
    assert sorted(p.name for p in (managed_root / "demo" / "releases").iterdir()) == releases_before


def test_forward_update_allowed_downgrade_blocked_and_bypass(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    c1 = _make_commit(repo, "commit 1")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    # Install C1
    res1 = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res1.returncode == 0

    # Advance repo to C2
    c2 = _make_commit(repo, "commit 2")

    # Forward install C1 -> C2 is allowed
    res2 = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res2.returncode == 0
    data2 = json.loads(res2.stdout)
    assert data2["source_commit"] == c2

    # Checkout older C1 in the same repo
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", c1], check=True)

    # Downgrade C2 -> C1 is blocked
    res_down_blocked = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res_down_blocked.returncode == 1
    data_down_blocked = json.loads(res_down_blocked.stdout)
    assert data_down_blocked["status"] == "blocked"
    assert "downgrade_or_divergent" in data_down_blocked["blockers"]

    # Downgrade with --allow-downgrade succeeds
    res_down_allowed = _run_tool(repo, profile, hermes, managed_root, "install", "--allow-downgrade")
    assert res_down_allowed.returncode == 0
    data_down_allowed = json.loads(res_down_allowed.stdout)
    assert data_down_allowed["source_commit"] == c1


def test_expect_commit_validation(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    c1 = _make_commit(repo, "commit 1")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    # Expecting different commit blocks
    res_bad = _run_tool(repo, profile, hermes, managed_root, "install", "--expect-commit", "1111222233334444555566667777888899990000")
    assert res_bad.returncode == 1
    data_bad = json.loads(res_bad.stdout)
    assert data_bad["status"] == "blocked"
    assert "unexpected_commit" in data_bad["blockers"]

    # Expecting short prefix of current commit matches
    res_good = _run_tool(repo, profile, hermes, managed_root, "install", "--expect-commit", c1[:7])
    assert res_good.returncode == 0
    data_good = json.loads(res_good.stdout)
    assert data_good["status"] == "installed"
    assert data_good["source_commit"] == c1


def test_retention_and_rollback_after_consecutive_installs(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    commits = []
    for i in range(1, 5):
        c = _make_commit(repo, f"version {i}")
        commits.append(c)
        res = _run_tool(repo, profile, hermes, managed_root, "install")
        assert res.returncode == 0

    manifest_file = managed_root / "demo" / "install-manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    history = manifest["release_history"]
    # With default keep=3, release_history has the last 3 previous releases
    assert len(history) == 3

    # All 3 history releases plus current active exist on disk
    releases_dir = managed_root / "demo" / "releases"
    existing_dirs = {p.name for p in releases_dir.iterdir() if p.is_dir()}
    assert len(existing_dirs) >= 4

    # Rollback succeeds to the immediately preceding release
    res_rb = _run_tool(repo, profile, hermes, managed_root, "rollback")
    assert res_rb.returncode == 0
    rb_data = json.loads(res_rb.stdout)
    assert rb_data["status"] == "rolled-back"

    # Prune with --keep 0 deletes all non-active releases
    res_prune_0 = _run_tool(repo, profile, hermes, managed_root, "prune-releases", "--keep", "0")
    assert res_prune_0.returncode == 0
    prune_data = json.loads(res_prune_0.stdout)
    assert prune_data["status"] == "pruned"

    remaining = [p.name for p in releases_dir.iterdir()]
    active = Path(rb_data["release"]).name
    assert remaining == [active]

    manifest_after = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_after["release_history"] == []


def test_doctor_deploy_version_observation(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    c1 = _make_commit(repo, "version 1")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    # 1. Clean installation -> doctor is ready
    res_install = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res_install.returncode == 0

    res_doc1 = _run_tool(repo, profile, hermes, managed_root, "doctor")
    assert res_doc1.returncode == 0, res_doc1.stdout + res_doc1.stderr
    doc1 = json.loads(res_doc1.stdout)
    assert doc1["status"] == "ready"
    obs1 = doc1["deploy_version_observation"]
    assert obs1["installed_commit"] == c1
    assert obs1["installed_dirty"] is False
    assert obs1["source_head"] == c1
    assert obs1["source_dirty"] is False
    assert obs1["reasons"] == []

    # 2. Advance repo head -> doctor reports drift (installed_behind_source)
    c2 = _make_commit(repo, "version 2")
    res_doc2 = _run_tool(repo, profile, hermes, managed_root, "doctor")
    assert res_doc2.returncode == 1
    doc2 = json.loads(res_doc2.stdout)
    assert doc2["status"] == "drift"
    obs2 = doc2["deploy_version_observation"]
    assert obs2["source_head"] == c2
    assert "installed_behind_source" in obs2["reasons"]

    # 3. Dirty install -> doctor reports drift (installed_dirty)
    (repo / "README.md").write_text("# dirty edit\n", encoding="utf-8")
    res_dirty_install = _run_tool(repo, profile, hermes, managed_root, "install", "--allow-dirty")
    assert res_dirty_install.returncode == 0

    res_doc3 = _run_tool(repo, profile, hermes, managed_root, "doctor")
    assert res_doc3.returncode == 1
    doc3 = json.loads(res_doc3.stdout)
    assert doc3["status"] == "drift"
    obs3 = doc3["deploy_version_observation"]
    assert "installed_dirty" in obs3["reasons"]


def test_legacy_manifest_without_source_commit(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    c1 = _make_commit(repo, "commit 1")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    # Perform normal install
    res = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res.returncode == 0

    # Rewrite manifest to simulate a legacy manifest (strip source_commit)
    manifest_path = managed_root / "demo" / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("source_commit", None)
    manifest["identity"] = c1[:7] + "-dirty"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Advance to C2 and install
    c2 = _make_commit(repo, "commit 2")
    res_leg = _run_tool(repo, profile, hermes, managed_root, "install")
    # c1[:7] resolves in repo, ancestry check passes (c1 is ancestor of c2)
    assert res_leg.returncode == 0
    data_leg = json.loads(res_leg.stdout)
    assert data_leg["source_commit"] == c2

    # Now simulate unresolvable legacy identity
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("source_commit", None)
    manifest["identity"] = "deadbeef-dirty"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    c3 = _make_commit(repo, "commit 3")
    res_unpinned = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res_unpinned.returncode == 0
    data_unpinned = json.loads(res_unpinned.stdout)
    assert "warnings" in data_unpinned
    assert "legacy_manifest_unpinned" in data_unpinned["warnings"]


def test_adopt_source_and_unknown_previous_commit(tmp_path):
    repo_a = _make_git_repo(tmp_path / "repo_a")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    # Install from Repo A
    res_a = _run_tool(repo_a, profile, hermes, managed_root, "install")
    assert res_a.returncode == 0

    # Clone B from Repo A
    clone_b = tmp_path / "clone_b"
    subprocess.run(["git", "clone", "-q", str(repo_a), str(clone_b)], check=True)
    subprocess.run(["git", "-C", str(clone_b), "config", "user.name", "Tester B"], check=True)
    subprocess.run(["git", "-C", str(clone_b), "config", "user.email", "b@example.invalid"], check=True)

    # Attempt install from Clone B without adopt-source -> blocked
    res_mismatch = _run_tool(clone_b, profile, hermes, managed_root, "install")
    assert res_mismatch.returncode == 1
    assert "source_repo_mismatch" in json.loads(res_mismatch.stdout)["blockers"]

    # Adopt source -> succeeds and records clone_b
    res_adopted = _run_tool(clone_b, profile, hermes, managed_root, "install", "--adopt-source")
    assert res_adopted.returncode == 0
    data_adopted = json.loads(res_adopted.stdout)
    assert data_adopted["source_repo"] == str(clone_b.resolve())

    # Create an independent repo without Repo A's commits
    repo_other = tmp_path / "repo_other"
    repo_other.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_other)], check=True)
    subprocess.run(["git", "-C", str(repo_other), "config", "user.name", "Other"], check=True)
    subprocess.run(["git", "-C", str(repo_other), "config", "user.email", "other@example.invalid"], check=True)
    # Copy essential release files from repo_a
    for item in ("agentcore", "skills", "plugins", "install"):
        shutil.copytree(repo_a / item, repo_other / item)
    subprocess.run(["git", "-C", str(repo_other), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_other), "commit", "-qm", "independent root"], check=True)

    # Install from repo_other with --adopt-source -> blocked on previous_commit_unknown_to_source
    res_unknown = _run_tool(repo_other, profile, hermes, managed_root, "install", "--adopt-source")
    assert res_unknown.returncode == 1
    data_unknown = json.loads(res_unknown.stdout)
    assert "previous_commit_unknown_to_source" in data_unknown["blockers"]

    # Bypass with --allow-downgrade -> succeeds
    res_bypassed = _run_tool(repo_other, profile, hermes, managed_root, "install", "--adopt-source", "--allow-downgrade")
    assert res_bypassed.returncode == 0
    data_bypassed = json.loads(res_bypassed.stdout)
    assert data_bypassed["status"] == "installed"


def test_update_post_check_detects_service_not_restarted(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    profile, hermes, managed_root = _seed_test_env(tmp_path)

    # First install
    res_init = _run_tool(repo, profile, hermes, managed_root, "install")
    assert res_init.returncode == 0

    # Create mock systemctl where show outputs MainPID=1 (system boot time, < installed_at)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text(
        """#!/bin/sh
if [ "$2" = "is-active" ]; then exit 0; fi
case "$*" in
    *"property=ControlGroup"*)
        exit 0
        ;;
    *"property=MainPID"*)
        printf 'ActiveState=active\\nMainPID=1\\n'
        exit 0
        ;;
esac
exit 0
""", encoding="utf-8")
    stub.chmod(0o755)

    c2 = _make_commit(repo, "commit 2")
    env_override = {
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
    }
    res_update = _run_tool(repo, profile, hermes, managed_root, "update", env_override=env_override)
    assert res_update.returncode == 1
    parts = res_update.stdout.strip().split("\n{\n")
    data_update = json.loads("{\n" + parts[-1] if len(parts) > 1 else parts[0])
    assert data_update["status"] in ("rolled-back", "rollback-incomplete")
    assert "service_not_restarted_on_new_release" in data_update["error"]


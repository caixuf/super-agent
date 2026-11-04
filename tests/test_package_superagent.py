"""Release archives use tracked input and work without Git after extraction."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

REPO = Path(__file__).resolve().parents[1]


def load_tool(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def package_repo(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    contents = {
        "agentcore/liveness.py": "# synthetic engine\n",
        "plugins/demo/plugin.yaml": "name: demo\n",
        "skills/demo/SKILL.md": "# synthetic skill\n",
        "toolsets/demo/__init__.py": "",
        "tools/superagent.py": "# synthetic installer\n",
        "runtime/demo.service": "# synthetic service\n",
        "install/superagent.sh": "#!/bin/sh\nexit 0\n",
        "identity/SOUL.generic.md": "# synthetic identity\n",
        "patches/demo.py": "# synthetic host patch\n",
        "integrations/example/lock.json": "{}\n",
        "README.md": "# synthetic distribution\n",
        "LICENSE": "synthetic license fixture\n",
        "pyproject.toml": "[project]\nname='synthetic'\nversion='0.1.0'\n",
        "verify.sh": "#!/bin/sh\nexit 0\n",
        ".gitignore": "dist/\n",
    }
    for name, text in contents.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (repo / "install/superagent.sh").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=Release Test",
        "-c", "user.email=release@example.invalid", "commit", "-qm", "fixture",
    ], check=True, env=dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+0000",
                           GIT_COMMITTER_DATE="2026-01-01T00:00:00+0000"))
    module = load_tool("package_superagent_test", REPO / "tools/package_superagent.py")
    monkeypatch.setattr(module, "REPO", repo)
    return module, repo


def archive_files(archive):
    with tarfile.open(archive) as bundle:
        return {
            member.name.removeprefix("./"): bundle.extractfile(member).read()
            for member in bundle.getmembers() if member.isfile()
        }


def test_package_contains_installer_dependencies_and_license(package_repo, tmp_path):
    module, _ = package_repo
    archive = module.build_package(tmp_path / "out")
    files = archive_files(archive)
    for name in ("patches/demo.py", "integrations/example/lock.json", "pyproject.toml",
                 "README.md", "LICENSE", "verify.sh", "identity/SOUL.generic.md"):
        assert name in files
    manifest = json.loads(files["release.json"])
    assert len(manifest["source_commit"]) == 40
    assert manifest["publishable"] is True
    assert manifest["files"]["agentcore/liveness.py"]["sha256"] == hashlib.sha256(
        files["agentcore/liveness.py"]).hexdigest()


def test_package_is_reproducible_despite_filesystem_mtime(package_repo, tmp_path):
    module, repo = package_repo
    first = module.build_package(tmp_path / "one")
    for path in repo.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (1700000000, 1700000000))
    second = module.build_package(tmp_path / "two")
    assert first.read_bytes() == second.read_bytes()


def test_package_does_not_overwrite_reviewed_archive(package_repo, tmp_path):
    module, _ = package_repo
    archive = module.build_package(tmp_path / "out")
    before = archive.read_bytes()
    with pytest.raises(FileExistsError):
        module.build_package(tmp_path / "out")
    assert archive.read_bytes() == before


def test_dirty_tree_requires_explicit_candidate_mode(package_repo, tmp_path):
    module, repo = package_repo
    (repo / "agentcore/liveness.py").write_text("# changed engine\n")
    with pytest.raises(ValueError, match="dirty"):
        module.build_package(tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_candidate_excludes_untracked_secrets(package_repo, tmp_path):
    module, repo = package_repo
    (repo / "agentcore/.env").write_text("TOKEN=synthetic-secret-sentinel\n")
    archive = module.build_package(tmp_path / "out", allow_dirty=True)
    files = archive_files(archive)
    assert "agentcore/.env" not in files
    assert all(b"synthetic-secret-sentinel" not in content for content in files.values())
    manifest = json.loads(files["release.json"])
    assert manifest["publishable"] is False
    assert "-dirty-" in manifest["identity"]


def test_candidate_identity_changes_when_tracked_content_changes(package_repo, tmp_path):
    module, repo = package_repo
    engine = repo / "agentcore/liveness.py"
    engine.write_text("# candidate one\n")
    first = module.build_package(tmp_path / "one", allow_dirty=True)
    engine.write_text("# candidate two\n")
    second = module.build_package(tmp_path / "two", allow_dirty=True)
    assert first.name != second.name


def test_package_rejects_tracked_symlink(package_repo, tmp_path):
    module, repo = package_repo
    link = repo / "agentcore/link"
    link.symlink_to(tmp_path / "outside")
    subprocess.run(["git", "-C", str(repo), "add", "agentcore/link"], check=True)
    with pytest.raises(ValueError, match="symlink"):
        module.build_package(tmp_path / "out", allow_dirty=True)


def test_package_rejects_tracked_credential_file(package_repo, tmp_path):
    module, repo = package_repo
    (repo / "runtime/.env").write_text("TOKEN=synthetic-secret-sentinel\n")
    subprocess.run(["git", "-C", str(repo), "add", "runtime/.env"], check=True)
    with pytest.raises(ValueError, match="credential"):
        module.build_package(tmp_path / "out", allow_dirty=True)


def test_exported_installer_identity_works_without_git(package_repo, tmp_path, monkeypatch):
    module, _ = package_repo
    archive = module.build_package(tmp_path / "out")
    exported = tmp_path / "exported"
    exported.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(exported, filter="data")
    installer = load_tool("exported_identity_test", REPO / "tools/superagent.py")
    monkeypatch.setattr(installer, "_run",
                        lambda *a, **kw: pytest.fail("exported identity must not invoke Git"))
    manifest = json.loads((exported / "release.json").read_text())
    assert installer._git_identity(exported) == (manifest["version"], manifest["identity"])
    (exported / "agentcore/liveness.py").write_text("# tampered after extraction\n")
    with pytest.raises(ValueError, match="integrity"):
        installer._git_identity(exported)


def test_exported_installer_rejects_changed_executable_mode(package_repo, tmp_path):
    module, _ = package_repo
    archive = module.build_package(tmp_path / "out")
    exported = tmp_path / "exported"
    exported.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(exported, filter="data")
    installer = load_tool("exported_mode_test", REPO / "tools/superagent.py")
    (exported / "install/superagent.sh").chmod(0o644)
    with pytest.raises(ValueError, match="integrity"):
        installer._git_identity(exported)


def test_exported_identity_survives_standard_build_and_test_metadata(package_repo, tmp_path):
    module, _ = package_repo
    archive = module.build_package(tmp_path / "out")
    exported = tmp_path / "exported"
    exported.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(exported, filter="data")
    for name in ("build/lib/agentcore/liveness.py", "agentcore.egg-info/PKG-INFO",
                 ".pytest_cache/v/cache/nodeids", ".venv/pyvenv.cfg"):
        generated = exported / name
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("# generated, not part of the source payload\n")
    installer = load_tool("exported_metadata_test", REPO / "tools/superagent.py")
    manifest = json.loads((exported / "release.json").read_text())
    assert installer._git_identity(exported) == (manifest["version"], manifest["identity"])


def test_exported_identity_rejects_unmanifested_source_file(package_repo, tmp_path):
    module, _ = package_repo
    archive = module.build_package(tmp_path / "out")
    exported = tmp_path / "exported"
    exported.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(exported, filter="data")
    (exported / "agentcore/unexpected.py").write_text("# not a build product\n")
    installer = load_tool("exported_extra_test", REPO / "tools/superagent.py")
    with pytest.raises(ValueError, match="integrity"):
        installer._git_identity(exported)


def test_clean_build_uses_pinned_commit_when_working_file_changes_during_build(
        package_repo, tmp_path, monkeypatch):
    module, repo = package_repo
    original_git = module._git

    def racing_git(*args):
        result = original_git(*args)
        if args[:1] == ("archive",):
            (repo / "agentcore/liveness.py").write_text("# concurrent edit must not publish\n")
        return result

    monkeypatch.setattr(module, "_git", racing_git)
    files = archive_files(module.build_package(tmp_path / "out"))
    assert files["agentcore/liveness.py"] == b"# synthetic engine\n"


def test_bundle_hash_uses_installer_path_order(package_repo, tmp_path):
    module, repo = package_repo
    (repo / "agentcore/z").mkdir()
    (repo / "agentcore/z/child.py").write_text("# nested\n")
    (repo / "agentcore/z.py").write_text("# sibling\n")
    subprocess.run(["git", "-C", str(repo), "add", "agentcore"], check=True)
    archive = module.build_package(tmp_path / "out", allow_dirty=True)
    exported = tmp_path / "exported"
    exported.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(exported, filter="data")
    installer = load_tool("exported_order_test", REPO / "tools/superagent.py")
    assert installer._git_identity(exported)[1].startswith(
        json.loads((exported / "release.json").read_text())["identity"])


def test_package_shell_help_routes_to_builder():
    proc = subprocess.run(["bash", str(REPO / "install/superagent.sh"), "package", "--help"],
                          env=dict(os.environ, PYTHON=sys.executable),
                          text=True, capture_output=True, check=True)
    assert "--allow-dirty" in proc.stdout


def test_package_with_profile_overlay(package_repo, tmp_path):
    module, _ = package_repo
    prof = tmp_path / "mock_expert_profile"
    prof.mkdir()
    (prof / "SOUL.md").write_text("# Expert Soul\n", encoding="utf-8")
    (prof / "memories").mkdir()
    (prof / "memories" / "MEMORY.md").write_text("Memory text\n", encoding="utf-8")

    archive = module.build_package(tmp_path / "out", profile_overlay=prof)
    assert "-overlay-mock_expert_profile.tar.gz" in archive.name
    files = archive_files(archive)
    assert "overlay/SOUL.md" in files
    assert "overlay/memories/MEMORY.md" in files
    assert "overlay/metadata.json" in files
    meta = json.loads(files["overlay/metadata.json"])
    assert meta["overlay_name"] == "mock_expert_profile"
    assert meta["components"]["has_soul"] is True

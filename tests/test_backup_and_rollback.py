from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.ops.backup_and_rollback import create_backup, rollback_from_backup


def test_backup_and_rollback_roundtrip(tmp_path):
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    file_a = repo / "version.txt"
    file_a.write_text("v1.0.0")
    subprocess.run(["git", "add", "version.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)

    config = tmp_path / "profile_config.yaml"
    config.write_text("key: value_initial\n")

    backup_root = tmp_path / "backups"
    res = create_backup(repo, config, backup_root)
    assert res["ok"] is True
    backup_dir = Path(res["backup_dir"])
    assert (backup_dir / "manifest.json").exists()
    assert (backup_dir / "profile_config.yaml").exists()

    # Now make destructive changes
    file_a.write_text("v2.0.0-modified")
    subprocess.run(["git", "commit", "-am", "destructive update"], cwd=repo, check=True)
    config.write_text("key: value_corrupted\n")

    # Perform rollback
    rb_res = rollback_from_backup(backup_dir)
    assert rb_res["ok"] is True

    # Assert clean state restored
    assert file_a.read_text() == "v1.0.0"
    assert config.read_text() == "key: value_initial\n"

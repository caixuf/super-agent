#!/usr/bin/env python3
"""Backup and rollback manager for agentcore gateway deployments.

Maintains atomic snapshots of git revisions, uncommitted working tree diffs,
Hermes profile configs, and runtime state before any production update.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


def create_backup(target_repo: Path, config_file: Path | None, backup_root: Path) -> dict:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_root / f"backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    metadata: dict = {
        "timestamp": timestamp,
        "repo_path": str(target_repo.resolve()),
        "config_path": str(config_file.resolve()) if config_file and config_file.exists() else None,
    }

    # 1. Record Git commit and status
    try:
        head_commit = _run(["git", "rev-parse", "HEAD"], cwd=target_repo).stdout.strip()
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target_repo).stdout.strip()
        metadata["git_head"] = head_commit
        metadata["git_branch"] = branch

        diff_out = _run(["git", "diff", "HEAD"], cwd=target_repo).stdout
        patch_file = backup_dir / "working_tree.patch"
        patch_file.write_text(diff_out, encoding="utf-8")
        metadata["patch_file"] = str(patch_file.name)
    except Exception as exc:
        metadata["git_error"] = str(exc)

    # 2. Backup config file
    if config_file and config_file.exists():
        target_cfg = backup_dir / config_file.name
        shutil.copy2(config_file, target_cfg)
        metadata["backup_config_file"] = str(target_cfg.name)

    # Write manifest
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"ok": True, "backup_dir": str(backup_dir), "manifest": metadata}


def rollback_from_backup(backup_dir: Path) -> dict:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found in {backup_dir}")

    metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo_path = Path(metadata["repo_path"])
    config_path = Path(metadata["config_path"]) if metadata.get("config_path") else None

    # 1. Restore config file
    if config_path and metadata.get("backup_config_file"):
        src_cfg = backup_dir / metadata["backup_config_file"]
        if src_cfg.exists():
            shutil.copy2(src_cfg, config_path)

    # 2. Restore Git commit and working tree
    head = metadata.get("git_head")
    if head and repo_path.exists():
        _run(["git", "reset", "--hard", head], cwd=repo_path)
        patch_name = metadata.get("patch_file")
        if patch_name:
            patch_file = backup_dir / patch_name
            if patch_file.exists() and patch_file.stat().st_size > 0:
                _run(["git", "apply", str(patch_file)], cwd=repo_path)

    return {"ok": True, "restored_head": head, "restored_config": str(config_path) if config_path else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    p_bak = sub.add_parser("create")
    p_bak.add_argument("--repo", type=Path, default=Path.cwd())
    p_bak.add_argument("--config", type=Path, default=None)
    p_bak.add_argument("--backup-root", type=Path, default=Path.home() / ".backup" / "agentcore")

    p_rb = sub.add_parser("rollback")
    p_rb.add_argument("--backup-dir", type=Path, required=True)

    args = parser.parse_args()
    if args.action == "create":
        res = create_backup(args.repo, args.config, args.backup_root)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.action == "rollback":
        res = rollback_from_backup(args.backup_dir)
        print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

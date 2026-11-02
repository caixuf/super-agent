"""Build a self-contained release tarball for Super Agent.

Creates a reproducible, single-file release:
  superagent-<version>.tar.gz

Contains:
  - agentcore/ (with native cardkit2_renderer, pool, memory, dev_runner)
  - plugins/ (all official plugins)
  - skills/ (standard and core skills)
  - toolsets/ (pi_executor)
  - tools/ (superagent CLI & runtime verification tools)
  - runtime/ (standard baseline & service templates)
  - install/ (standalone installer script)
  - release.json (exact git version & tree sha256)

Installation on any target machine requires zero git commands and zero monkey-patches:
  tar -xzf superagent-<version>.tar.gz -C ~/.hermes/superagent/releases/<version>
  ln -sfn ~/.hermes/superagent/releases/<version> ~/.hermes/superagent/current
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]


def get_git_version() -> tuple[str, str]:
    short = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO, capture_output=True, text=True
    ).stdout.strip() or "dev"
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO, capture_output=True, text=True
    ).stdout.strip())
    describe = subprocess.run(
        ["git", "describe", "--tags", "--always", "--dirty"],
        cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    version = describe or short
    return version, short + ("-dirty" if dirty else "")


def compute_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "release.json":
            continue
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(p.read_bytes())
    return digest.hexdigest()


def copy_clean_tree(src: Path, dst: Path) -> None:
    for item in sorted(src.rglob("*")):
        if not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def build_package(output_dir: Path | None = None) -> Path:
    version, identity = get_git_version()
    out_dir = output_dir or (REPO / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)

    stage_dir = out_dir / f"stage-superagent-{identity}"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy core components
    components = ("agentcore", "plugins", "skills", "toolsets", "tools", "runtime", "install", "identity")
    for comp in components:
        src = REPO / comp
        if src.is_dir():
            copy_clean_tree(src, stage_dir / comp)

    # 2. Write immutable release.json
    tree_hash = compute_tree_sha256(stage_dir)
    release_info: Dict[str, Any] = {
        "schema_version": 1,
        "package_type": "superagent-standalone-tarball",
        "version": version,
        "identity": identity,
        "tree_sha256": tree_hash,
    }
    (stage_dir / "release.json").write_text(
        json.dumps(release_info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    # 3. Create compressed tarball
    archive_name = f"superagent-{identity}.tar.gz"
    archive_path = out_dir / archive_name
    if archive_path.exists():
        archive_path.unlink()

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(stage_dir, arcname=".")

    # Cleanup stage dir
    shutil.rmtree(stage_dir)
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Super Agent standalone release package")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for tarball")
    args = parser.parse_args()

    pkg = build_package(args.out_dir)
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
    size_mb = pkg.stat().st_size / (1024 * 1024)

    result = {
        "status": "packaged",
        "tarball": str(pkg),
        "filename": pkg.name,
        "size_mb": f"{size_mb:.2f}MB",
        "sha256": sha,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

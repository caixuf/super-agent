"""Build a reproducible, tracked-source Super Agent release archive.

The bundle includes installer inputs, packaging metadata, license and checks.
It does not bundle Python, Hermes, Node, model credentials or optional tools;
extracting it or creating a symlink is not runtime installation/acceptance.
Dirty builds require --allow-dirty and are explicitly non-publishable.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentcore.profile_overlay import collect_profile_overlay, resolve_profile_dir

REPO = Path(__file__).resolve().parents[1]
REQUIRED = (
    "agentcore/liveness.py", "tools/superagent.py", "install/superagent.sh",
    "identity/SOUL.generic.md", "pyproject.toml", "README.md", "LICENSE",
)


def _git(*args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=True)
    return proc.stdout


def get_git_version() -> tuple[str, str]:
    short = _git("rev-parse", "--short", "HEAD").decode().strip()
    version = _git("describe", "--tags", "--always").decode().strip()
    dirty = bool(_git("status", "--porcelain", "--untracked-files=all"))
    return version, short + ("-dirty" if dirty else "")


def compute_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "release.json":
            continue
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_package(output_dir: Path | None = None, *, allow_dirty: bool = False,
                  profile_overlay: str | Path | None = None) -> Path:
    version, identity = get_git_version()
    dirty = identity.endswith("-dirty")
    if dirty and not allow_dirty:
        raise ValueError("dirty source tree; commit changes or use --allow-dirty for a candidate")
    commit = _git("rev-parse", "HEAD").decode().strip()
    epoch = int(_git("show", "-s", "--format=%ct", "HEAD").decode().strip())
    files: dict[str, tuple[bytes, int]] = {}
    committed = {}
    if not dirty:
        # Pin clean builds to immutable Git blobs, not files another process
        # could edit after the cleanliness check.
        with tarfile.open(fileobj=io.BytesIO(_git("archive", "--format=tar", commit))) as source:
            for member in source:
                if member.isfile():
                    committed[member.name] = source.extractfile(member).read()
    # The index bounds input names; untracked credentials and build products
    # cannot leak through a recursive directory copy, even in candidate mode.
    for record in _git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, _, stage = metadata.split()
        name = raw_name.decode("utf-8")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValueError("unsafe tracked source path")
        if stage != b"0":
            raise ValueError("unmerged source index")
        path = REPO / name
        inside = path
        while inside != REPO:
            if inside.is_symlink():
                raise ValueError("tracked symlink input is not supported")
            inside = inside.parent
        if mode == b"120000":
            raise ValueError("tracked symlink input is not supported")
        if mode not in (b"100644", b"100755"):
            raise ValueError("unsupported tracked source type")
        if (relative.name == ".env" or relative.name.startswith(".env.")
                or relative.suffix.lower() in (".pem", ".key", ".p12", ".pfx")):
            raise ValueError("credential file is not a release input")
        if relative.parts[0] in ("dist", ".git", ".venv", "node_modules"):
            raise ValueError("generated directory is not a release input")
        if name == "release.json":
            raise ValueError("release.json must be generated, not a source input")
        if not path.is_file():
            raise ValueError("tracked release input is missing")
        content = path.read_bytes() if dirty else committed.get(name)
        if content is None:
            raise ValueError("source index drifted from the pinned commit")
        files[name] = (content, 0o755 if mode == b"100755" else 0o644)
    if any(name not in files for name in REQUIRED):
        raise ValueError("required release input is missing")
    for directory in ("plugins", "skills", "patches", "runtime", "toolsets"):
        if not any(name.startswith(directory + "/") for name in files):
            raise ValueError("required release directory is missing: " + directory)

    overlay_meta = None
    overlay_name = None
    if profile_overlay:
        p_dir = resolve_profile_dir(profile_overlay)
        overlay_name = p_dir.name
        overlay_files, overlay_meta = collect_profile_overlay(p_dir, overlay_name)
        files.update(overlay_files)

    tree = hashlib.sha256()
    manifest = {}
    for name, (content, mode) in sorted(files.items(), key=lambda item: PurePosixPath(item[0])):
        tree.update(name.encode())
        tree.update(b"\0")
        tree.update(content)
        manifest[name] = {"sha256": hashlib.sha256(content).hexdigest(), "mode": mode}
    tree_hash = tree.hexdigest()
    if dirty:
        identity += "-" + tree_hash[:12]
        version += "-dirty-" + tree_hash[:12]
    info = {
        "schema_version": 1,
        "package_type": "superagent-source-bundle",
        "version": version,
        "identity": identity,
        "source_commit": commit,
        "source_date_epoch": epoch,
        "publishable": not dirty,
        "tree_sha256": tree_hash,
        "files": manifest,
    }
    if overlay_meta is not None:
        info["overlay"] = overlay_meta
    files["release.json"] = (
        (json.dumps(info, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        0o644,
    )
    out_dir = (output_dir or REPO / "dist").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"superagent-{identity}.tar.gz" if not overlay_name else f"superagent-{identity}-overlay-{overlay_name}.tar.gz"
    archive = out_dir / archive_name
    if archive.exists() or archive.is_symlink():
        raise FileExistsError("release archive already exists; use a new output directory")
    # Stage under a unique name and publish exclusively. Neither partial
    # compression nor concurrent builders can replace an approved artifact.
    fd, temporary_name = tempfile.mkstemp(prefix=".superagent-", suffix=".tmp", dir=out_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                    for name, (content, mode) in sorted(files.items()):
                        member = tarfile.TarInfo(name)
                        member.size, member.mode, member.mtime = len(content), mode, epoch
                        member.uid = member.gid = 0
                        member.uname = member.gname = ""
                        bundle.addfile(member, io.BytesIO(content))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="Output directory for tarball")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Build a non-publishable candidate from tracked working files")
    parser.add_argument("--profile-overlay", type=str,
                        help="Package custom profile assets (soul, skills, memories, mcp) into an overlay archive")
    args = parser.parse_args()
    try:
        package = build_package(args.out_dir, allow_dirty=args.allow_dirty,
                                profile_overlay=args.profile_overlay)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Do not echo arbitrary Git/OS error bodies or source contents.
        print(json.dumps({"status": "failed", "error": type(error).__name__,
                          "reason": str(error) if isinstance(error, ValueError)
                          else "release_build_failed"}), file=sys.stderr)
        return 1
    with tarfile.open(package) as bundle:
        release = json.load(bundle.extractfile("release.json"))
    result = {
        "status": "packaged",
        "tarball": str(package),
        "filename": package.name,
        "size_bytes": package.stat().st_size,
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "publishable": release["publishable"],
    }
    if "overlay" in release:
        result["overlay"] = release["overlay"].get("overlay_name")
        result["overlay_components"] = release["overlay"].get("components")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

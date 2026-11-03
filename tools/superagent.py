#!/usr/bin/env python3
"""Super Agent one-command installer, updater, doctor and uninstaller.

The entire Super Agent installation lives under one removable root:

    ~/.hermes/superagent/<profile>/
      current -> releases/<version>/
      releases/<version>/       # code, skills, toolsets, runtime, patches
      state/                    # queues, ledgers, memory spool
      config/                   # managed profile config snapshot
      backups/                  # snapshots from every mutation
      install-manifest.json     # ownership + release history

Hermes keeps only manifest-declared plugin links and config keys. `uninstall`
removes those links/keys and the root; an incomplete upgrade can always be
rolled back by switching `current` to the prior release.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentcore.profile_overlay import apply_profile_overlay

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path.home() / ".hermes" / "superagent"
DEFAULT_HERMES = Path.home() / ".hermes" / "hermes-agent"
PLUGIN_SOURCES = tuple(sorted(p for p in (REPO / "plugins").glob("*/")))
# Installed upstream plugins are pinned dependencies, not vendored source. They
# stay outside the repo but are copied into the managed release so a release is
# self-contained and rollback never depends on a profile-local directory.
_EXTERNAL_PLUGIN_NAMES = frozenset({"advanced-delegation", "langfuse", "mcp-serve"})
CARD_PATCH_TOOLS = (
    ("apply_feishu_card_replies.py", "card-replies", "legacy"),
    ("patch_card_finalize.py", "card-finalize", "legacy"),
    ("patch_card_markdown_native.py", "card-native-markdown", "legacy"),
    ("patch_feishu_update_retry.py", "card-update-retry", "legacy"),
    ("patch_feishu_delivery_uuid.py", "delivery-uuid", "legacy"),
)
KEEP_HOST_PATCH_TOOLS = (
    ("patch_chinese_continue_intent.py", "chinese-continue-intent"),
    ("patch_background_verification.py", "background-verification"),
    ("patch_memory_prefetch_observation.py", "memory-prefetch-observation"),
    ("patch_memory_conservation.py", "memory-conservation"),
    ("patch_durable_dispatch_visibility.py", "durable-dispatch-visibility"),
    ("patch_frontdesk_boundary.py", "frontdesk-boundary"),
)
CANDIDATE_TAG = "v0.22.5"
CANDIDATE_COMMIT = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
# Source bundles may be tested or built in place. None of these top-level
# generated directories are installer payload inputs; source paths remain exact.
_SOURCE_BUILD_DIRS = frozenset({".venv", ".pytest_cache", "build", "agentcore.egg-info"})


def _run(
    cmd: Sequence[str], *, cwd: Path | None = None, check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, text=True,
                          capture_output=True, env=env or os.environ.copy())
    if check and proc.returncode:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-1000:]}"
        )
    return proc


def _git_identity(root: Path = REPO) -> tuple[str, str]:
    descriptor = root / "release.json"
    if descriptor.is_file() and not (root / ".git").exists():
        release = json.loads(descriptor.read_text(encoding="utf-8"))
        if (release.get("package_type") != "superagent-source-bundle"
                or release.get("schema_version") != 1
                or not isinstance(release.get("version"), str)
                or not isinstance(release.get("identity"), str)
                or not release["version"] or not release["identity"]):
            raise ValueError("release bundle integrity check failed")
        files = release.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("release bundle integrity manifest missing")
        digest = hashlib.sha256()
        for name, entry in sorted(files.items(), key=lambda item: PurePosixPath(item[0])):
            relative = PurePosixPath(name)
            if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                    or relative.as_posix() != name or not relative.parts
                    or name == "release.json" or relative.parts[0] in _SOURCE_BUILD_DIRS
                    or not isinstance(entry, dict) or entry.get("mode") not in (0o644, 0o755)):
                raise ValueError("release bundle integrity manifest invalid")
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("release bundle integrity file check failed")
            content = path.read_bytes()
            if (hashlib.sha256(content).hexdigest() != entry.get("sha256")
                    or (path.stat().st_mode & 0o777) != entry["mode"]):
                raise ValueError("release bundle integrity file check failed")
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(content)
        if (digest.hexdigest() != release.get("tree_sha256")
                or _release_files(root, ignored_roots=_SOURCE_BUILD_DIRS) != files):
            raise ValueError("release bundle integrity check failed")
        return release["version"], release["identity"]
    short = _run(["git", "rev-parse", "--short", "HEAD"], cwd=root).stdout.strip() or "unknown"
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=root).stdout.strip())
    describe = _run(["git", "describe", "--tags", "--always", "--dirty"], cwd=root).stdout.strip()
    version = describe or short
    return version, short + ("-dirty" if dirty else "")


def _profile_home(args: argparse.Namespace) -> Path:
    if args.profile_home:
        return args.profile_home.expanduser().resolve()
    return (Path.home() / ".hermes" / "profiles" / args.profile).resolve()


def _managed_root(args: argparse.Namespace) -> Path:
    return (args.root.expanduser() / args.profile).resolve()


def _release_dir(root: Path, version: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in version)
    return root / "releases" / safe


def _plugin_name(source: Path) -> str | None:
    manifest = source / "plugin.yaml"
    if not manifest.is_file():
        return None
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
            return name or None
    return None


def _managed_plugin_sources(args: argparse.Namespace | None = None) -> list[Path]:
    sources: dict[str, Path] = {}
    for source in (*PLUGIN_SOURCES, *_external_plugin_sources(args)):
        name = _plugin_name(source)
        if name and source.is_dir() and not source.name.startswith("."):
            # Repo plugins are the baseline; an explicitly snapshotted upstream
            # plugin may fill one of the three external names only.
            if source in PLUGIN_SOURCES:
                sources[name] = source
            elif name in _EXTERNAL_PLUGIN_NAMES:
                sources[name] = source
    return [sources[name] for name in sorted(sources)]


def _external_plugin_root(args: argparse.Namespace | None = None) -> Path:
    base = _managed_root(args) if args is not None else DEFAULT_ROOT / "caixuf"
    return base / "external-plugins"


def _external_plugin_sources(args: argparse.Namespace | None = None) -> list[Path]:
    sources = []
    snapshot = _external_plugin_root(args)
    for name in sorted(_EXTERNAL_PLUGIN_NAMES):
        candidate = snapshot / name
        if candidate.is_dir():
            sources.append(candidate)
    return sources


def _snapshot_external_plugins(args: argparse.Namespace) -> list[Path]:
    """Copy pinned upstream plugins into the managed root before release writes."""
    snapshot = _external_plugin_root(args)
    snapshot.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in sorted(_EXTERNAL_PLUGIN_NAMES):
        target = snapshot / name
        candidates = [
            _profile_home(args) / "plugins" / name,
            Path.home() / ".hermes" / "profiles" / "caixufcanary" / "plugins" / name,
        ]
        source = next((p for p in candidates if p.is_dir() and not p.is_symlink()), None)
        if source is None:
            if target.is_dir():
                copied.append(target)
            continue
        if target.exists():
            shutil.rmtree(target)
        _copy_tree(source, target)
        copied.append(target)
    return copied


def _plugin_names(args: argparse.Namespace | None = None) -> list[str]:
    return [name for source in _managed_plugin_sources(args) if (name := _plugin_name(source))]


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _release_tree_hash(release: Path) -> str:
    """Hash release contents without the self-referential release.json."""
    digest = hashlib.sha256()
    for path in sorted(release.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(release).as_posix()
        if rel == "release.json":
            continue
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _release_files(
    release: Path, *, ignored_roots: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Describe payload bytes and modes; never accept links as release files."""
    files = {}
    for path in sorted(release.rglob("*")):
        relative = path.relative_to(release)
        if (relative.parts[0] in ignored_roots
                or "__pycache__" in relative.parts or path.suffix == ".pyc"):
            continue
        if path.is_symlink():
            raise ValueError("release contains a symlink")
        if path.is_dir() or relative.as_posix() == "release.json":
            continue
        if not path.is_file():
            raise ValueError("release contains an unsupported file type")
        files[relative.as_posix()] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": path.stat().st_mode & 0o777,
        }
    return files


def _copy_tree(src: Path, dst: Path) -> None:
    for source in sorted(src.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        target = dst / source.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _write_text_atomic(path: Path, text: str) -> None:
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".superagent-", dir=path.parent)
    try:
        os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _snapshot(profile_home: Path, hermes_root: Path, backup_root: Path) -> Path:
    target = backup_root / dt.datetime.now().strftime("backup-%Y%m%d-%H%M%S-%f")
    target.mkdir(parents=True, mode=0o700)
    for source, name in ((profile_home / "config.yaml", "config.yaml"),
                         (profile_home / ".env", "profile.env")):
        if source.is_file():
            shutil.copy2(source, target / name)
    status = _run(["git", "status", "--porcelain"], cwd=hermes_root).stdout
    (target / "hermes-status.txt").write_text(status, encoding="utf-8")
    tracked = _run(["git", "diff", "--binary"], cwd=hermes_root).stdout
    (target / "hermes-local.patch").write_text(tracked, encoding="utf-8")
    return target


def _patch_state(hermes_root: Path, spec: tuple[str, str, str]) -> dict[str, Any]:
    script_name, patch_id, *_ = spec
    script = REPO / "patches" / script_name
    if not script.is_file():
        return {"id": patch_id, "state": "missing-tool", "in_place": False}
    env = os.environ.copy()
    env["HERMES_AGENT_DIR"] = str(hermes_root)
    if script_name == "apply_feishu_card_replies.py":
        command = [sys.executable, str(script), "--check"]
    elif script_name == "patch_feishu_delivery_uuid.py":
        command = [sys.executable, str(script), "--hermes-root", str(hermes_root)]
    elif script_name in {"patch_memory_prefetch_observation.py", "patch_memory_conservation.py"}:
        command = [sys.executable, str(script), "--hermes-root", str(hermes_root)]
    else:
        command = [sys.executable, str(script), "--check", "--agent-dir", str(hermes_root)]
    proc = _run(command, env=env)
    return {"id": patch_id, "state": "in-place" if proc.returncode == 0 else "absent",
            "in_place": proc.returncode == 0}


def _legacy_patch_state(hermes_root: Path) -> list[dict[str, Any]]:
    return [_patch_state(hermes_root, spec) for spec in CARD_PATCH_TOOLS]


def _keep_patch_state(hermes_root: Path) -> list[dict[str, Any]]:
    return [_patch_state(hermes_root, spec) for spec in KEEP_HOST_PATCH_TOOLS]


def _apply_keep_patches(hermes_root: Path) -> list[dict[str, Any]]:
    results = []
    for script_name, patch_id in KEEP_HOST_PATCH_TOOLS:
        script = REPO / "patches" / script_name
        if not script.is_file():
            raise RuntimeError(f"required preserved patch missing: {patch_id}")
        if script_name in {"patch_memory_prefetch_observation.py", "patch_memory_conservation.py"}:
            probe = [sys.executable, str(script), "--hermes-root", str(hermes_root)]
            apply = [*probe, "--apply"]
        else:
            probe = [sys.executable, str(script), "--check", "--agent-dir", str(hermes_root)]
            apply = [sys.executable, str(script), "--apply", "--agent-dir", str(hermes_root)]
        checked = _run(probe)
        if checked.returncode == 0:
            results.append({"id": patch_id, "state": "present"})
            continue
        applied = _run(apply)
        results.append({"id": patch_id, "state": "applied" if applied.returncode == 0 else "failed"})
        if applied.returncode != 0:
            raise RuntimeError(f"cannot apply preserved patch {patch_id}")
    return results


def _hermes_python() -> Path | None:
    candidates = [
        Path.home() / ".hermes" / "tools" / "python-3.14.9+20261021-linux-x64" / "bin" / "python3.14",
        Path("/home/caixuf/.hermes/tools/python-3.14.9+20261021-linux-x64/bin/python3.14"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    resolved = shutil.which("python3")
    return Path(resolved) if resolved else None


def _candidate_python(work: Path) -> Path:
    """Return the store interpreter bound to a candidate checkout."""
    state_file = work / ".hermes-candidate-python"
    if state_file.is_file():
        candidate = Path(state_file.read_text(encoding="utf-8").strip())
        if candidate.is_file():
            return candidate
    python_bin = _hermes_python()
    if python_bin is None:
        raise RuntimeError("hermes_python_missing")
    return python_bin


def _candidate_env(work: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Hermes validates saved profile state from a real home. Use the user's
    # base home for PM/tool state; dependency state remains source-root-keyed.
    env["HERMES_HOME"] = str(Path.home() / ".hermes")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def _candidate_bootstrap_command(work: Path) -> list[str]:
    python_bin = _candidate_python(work)
    bootstrap = (
        "import os, sys, runpy; "
        "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
        "os.environ.pop('VIRTUAL_ENV', None); "
        f"sys.path.insert(0, {str(work)!r}); sys._hermes_pin_default_home = True; "
        "import hermes_bootstrap; "
        "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
    )
    return [str(python_bin), "-I", "-c", bootstrap]


def _run_candidate(work: Path, args: Sequence[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    command = [*_candidate_bootstrap_command(work), *args]
    try:
        return _run(command, cwd=work, env=_candidate_env(work))
    except subprocess.TimeoutExpired:
        raise RuntimeError("candidate_command_timeout") from None


def _candidate_version(work: Path) -> subprocess.CompletedProcess[str]:
    return _run_candidate(work, ["--version"], timeout=120)


def _sync_candidate_dependencies(work: Path) -> subprocess.CompletedProcess[str]:
    return _run_candidate(work, ["pm", "install", "--extra", "all"], timeout=3600)


def _smoke_candidate(work: Path) -> dict[str, Any]:
    version = _candidate_version(work)
    if version.returncode != 0:
        return {"ok": False, "reason": "candidate_version_failed",
                "output": (version.stdout + version.stderr)[-4000:]}
    if CANDIDATE_TAG.removeprefix("v") not in (version.stdout + version.stderr):
        return {"ok": False, "reason": "candidate_version_mismatch",
                "output": (version.stdout + version.stderr)[-4000:]}
    status = _run_candidate(work, ["pm", "status"], timeout=300)
    if status.returncode != 0:
        return {"ok": False, "reason": "candidate_pm_status_failed",
                "output": (status.stdout + status.stderr)[-4000:]}
    try:
        receipt = json.loads(status.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "candidate_pm_status_invalid",
                "output": status.stdout[-4000:]}
    if receipt.get("outcome") != "ok":
        return {"ok": False, "reason": "candidate_pm_sync_not_ok", "receipt": receipt}
    return {"ok": True, "version": (version.stdout + version.stderr).strip(),
            "pm_receipt": receipt}


def build_clean_hermes_candidate(args: argparse.Namespace, plan: dict[str, Any]) -> dict[str, Any]:
    """Build a pristine-tag candidate with a committed PM dependency environment."""
    root = _managed_root(args)
    work = root / "hermes" / (CANDIDATE_TAG + "-" + CANDIDATE_COMMIT[:12])
    current = root / "hermes-current"
    if work.exists():
        if current.is_symlink() and current.resolve() == work.resolve():
            return {"status": "blocked", "reason": "candidate_path_in_use",
                    "path": str(work)}
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    checkout = _run(["git", "clone", "--quiet", "--depth=1", "--branch", CANDIDATE_TAG,
                     "https://github.com/NousResearch/hermes-agent.git", str(work)])
    if checkout.returncode != 0:
        return {"status": "blocked", "reason": "candidate_clone_failed"}
    head = _run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
    if head != CANDIDATE_COMMIT:
        return {"status": "blocked", "reason": "candidate_revision_mismatch", "head": head}
    try:
        patches = _apply_keep_patches(work)
    except Exception as exc:
        return {"status": "blocked", "reason": str(exc)}
    # PM keys dependency state by the checkout's absolute path. A copied
    # candidate would therefore lose its PM generation, so publish launchers
    # and verify the candidate only after it has reached its final release path.
    card_patches = _legacy_patch_state(work)
    if any(item["in_place"] for item in card_patches):
        return {"status": "blocked", "reason": "candidate_contains_card_patch"}

    # First prepare PM itself with the store interpreter, then commit the
    # candidate's own dependency generation. Neither step may borrow the live
    # checkout's venv: the candidate has a different source root and install key.
    python_bin = _hermes_python()
    if python_bin is None:
        return {"status": "blocked", "reason": "hermes_python_missing"}
    (work / ".hermes-candidate-python").write_text(str(python_bin) + "\n", encoding="utf-8")
    synced = _sync_candidate_dependencies(work)
    (work / "candidate-pm-install.log").write_text(
        synced.stdout + synced.stderr, encoding="utf-8")
    if synced.returncode != 0:
        return {"status": "blocked", "reason": "candidate_dependencies_failed",
                "path": str(work), "head": head, "preserved_patches": patches,
                "card_patches": card_patches}
    try:
        _publish_candidate_launcher(work)
    except RuntimeError as exc:
        return {"status": "blocked", "reason": str(exc), "path": str(work),
                "head": head, "preserved_patches": patches,
                "card_patches": card_patches}
    smoke = _smoke_candidate(work)
    (work / "candidate-smoke.log").write_text(
        json.dumps(smoke, ensure_ascii=False, indent=2), encoding="utf-8")
    if not smoke["ok"]:
        return {"status": "blocked", "reason": smoke["reason"],
                "path": str(work), "head": head, "preserved_patches": patches,
                "card_patches": card_patches, "smoke": smoke}
    (work / "adapter-is-clean").write_text(head + "\n", encoding="utf-8")
    return {
        "status": "ready", "path": str(work), "head": head,
        "preserved_patches": patches, "card_patches": card_patches,
        "dependency_environment": "committed", "smoke": smoke,
        "tree_sha256": _sha256_tree(work),
    }

def _publish_candidate_launcher(work: Path) -> None:
    """Rebind candidate launchers to its final staged path and verify them."""
    python_bin = _candidate_python(work)
    launcher = work / ".hermes" / "bin" / "hermes"
    bootstrap = (
        "import os, re, sys; "
        "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
        "os.environ.pop('VIRTUAL_ENV', None); "
        f"sys.path.insert(0, {str(work)!r}); "
        "from hermes_cli._launchers import ensure_install_launchers; "
        f"ensure_install_launchers(__import__('pathlib').Path({str(work)!r}), "
        f"__import__('pathlib').Path({str(work / '.hermes' / 'bin')!r}))"
    )
    proc = _run([str(python_bin), "-I", "-c", bootstrap], cwd=work,
                env=_candidate_env(work))
    if proc.returncode != 0 or not launcher.is_file():
        raise RuntimeError("candidate_launcher_publish_failed")
    text = launcher.read_text(encoding="utf-8")
    if str(work) not in text:
        raise RuntimeError("candidate_launcher_not_rebound")
    run = _run([str(launcher), "--version"], cwd=work, env=_candidate_env(work))
    if run.returncode != 0 or CANDIDATE_TAG.removeprefix("v") not in (run.stdout + run.stderr):
        raise RuntimeError("candidate_launcher_smoke_failed")


def install_hermes_candidate(args: argparse.Namespace, plan: dict[str, Any]) -> int:
    """Activate the verified candidate that was built at its final path.

    PM dependency state is keyed by the absolute checkout path. Rebuilding at
    the final path (instead of copying a temporary tree) preserves that state.
    """
    root = _managed_root(args)
    candidate = root / "hermes" / (CANDIDATE_TAG + "-" + CANDIDATE_COMMIT[:12])
    marker = candidate / "adapter-is-clean"
    if not candidate.is_dir() or not marker.is_file():
        print(json.dumps({"status": "blocked", "reason": "verified_candidate_required",
                          "candidate": str(candidate)}))
        return 1
    if marker.read_text(encoding="utf-8").strip() != CANDIDATE_COMMIT:
        print(json.dumps({"status": "blocked", "reason": "candidate_marker_mismatch"}))
        return 1
    try:
        smoke = _smoke_candidate(candidate)
    except RuntimeError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    if not smoke["ok"]:
        print(json.dumps({"status": "blocked", "reason": smoke["reason"],
                          "smoke": smoke}, ensure_ascii=False))
        return 1
    current = root / "hermes-current"
    temp = root / ".hermes-current.next"
    if temp.is_symlink() or temp.is_file():
        temp.unlink()
    temp.symlink_to(candidate, target_is_directory=True)
    os.replace(temp, current)
    print(json.dumps({"status": "staged", "candidate": str(candidate),
                      "current": str(current), "smoke": smoke},
                     ensure_ascii=False, indent=2))
    return 0

def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    version, identity = _git_identity()
    profile_home = _profile_home(args)
    hermes_root = args.hermes_root.expanduser().resolve()
    root = _managed_root(args)
    release = _release_dir(root, version)
    blockers = []
    if not (profile_home / "config.yaml").is_file():
        blockers.append("profile_config_missing")
    if not (hermes_root / "plugins" / "platforms" / "feishu" / "adapter.py").is_file():
        blockers.append("hermes_source_missing")
    external_plugins = _external_plugin_sources(args)
    if not _plugin_names(args):
        blockers.append("no_managed_plugins")
    legacy = _legacy_patch_state(hermes_root)
    candidate_marker = root / "hermes" / (CANDIDATE_TAG + "-" + CANDIDATE_COMMIT[:12]) / "adapter-is-clean"
    if any(item["in_place"] for item in legacy) and not candidate_marker.is_file():
        # Installing over a live modified checkout would leave two card
        # producers. Require the independently built clean candidate first.
        blockers.append("legacy_card_patches_require_clean_candidate")
    return {
        "schema_version": 1,
        "action": "superagent-managed-install",
        "version": version,
        "identity": identity,
        "managed_root": str(root),
        "release": str(release),
        "profile_home": str(profile_home),
        "hermes_root": str(hermes_root),
        "plugins": _plugin_names(args),
        "memory_providers": ["localvec"] if (REPO / "plugins/localvec/__init__.py").is_file() else [],
        "external_plugins": [str(path) for path in external_plugins],
        "legacy_card_patches": legacy,
        "preserved_host_patches": _keep_patch_state(hermes_root),
        "blockers": blockers,
        "layout": {
            "current": str(root / "current"),
            "state": str(root / "state"),
            "config": str(root / "config"),
            "backups": str(root / "backups"),
            "manifest": str(root / "install-manifest.json"),
        },
        "managed_paths": {
            "engine_root": str(root / "current"),
            "queue_root": str(root / "state" / "analysis-queue"),
            "memory_spool": str(root / "state" / "memory-spool"),
            "markdown_outbox": str(root / "state" / "markdown-outbox"),
            "liveness_db": str(root / "state" / "liveness.db"),
            "colleague_registry": str(root / "state" / "colleague-team" / "registry.sqlite"),
        },
    }


def _release_valid(release: Path) -> bool:
    if not (
        not release.is_symlink()
        and release.is_dir()
        and (release / "agentcore" / "liveness.py").is_file()
        and (release / "plugins").is_dir()
        and (release / "skills").is_dir()
        and (release / "release.json").is_file()
        and not (release / "release.json").is_symlink()
    ):
        return False
    try:
        descriptor = json.loads((release / "release.json").read_text(encoding="utf-8"))
        files = _release_files(release)
        return (
            isinstance(descriptor, dict)
            and descriptor.get("schema_version") == 1
            and isinstance(descriptor.get("version"), str)
            and bool(descriptor["version"])
            and descriptor.get("tree_sha256") == _release_tree_hash(release)
            # Older managed releases recorded only the content hash. Preserve
            # rollback to those, but validate file modes on all new releases.
            and ("files" not in descriptor or descriptor["files"] == files)
        )
    except (OSError, ValueError):
        return False


def _switch_current(root: Path, release: Path) -> None:
    temp = root / ".current.next"
    if temp.is_symlink() or temp.is_file():
        temp.unlink()
    temp.symlink_to(release, target_is_directory=True)
    os.replace(temp, root / "current")


def _relink_plugins(profile_home: Path, release: Path, names: list[str], external_plugins: list[str] | None = None,
                    memory_providers: list[str] | None = None) -> None:
    destination = profile_home / "plugins"
    destination.mkdir(parents=True, exist_ok=True)
    source_roots = [*PLUGIN_SOURCES, *(Path(p) for p in (external_plugins or []))]
    repo_names = {p.name for p in PLUGIN_SOURCES}
    source_roots = [p for p in source_roots if p in PLUGIN_SOURCES or p.name not in repo_names]
    resolved = []
    for source in source_roots:
        name = _plugin_name(source)
        if name is None and source.name in (memory_providers or []):
            name = source.name
        if name not in [*names, *(memory_providers or [])]:
            continue
        if not (release / "plugins" / source.name / "__init__.py").is_file():
            raise ValueError("release plugin or provider missing")
        resolved.append((source, name))
    # Validate every target before replacing any link. A sealed but incomplete
    # release must not leave half the profile pointing at a different version.
    for source, name in resolved:
        link = destination / name
        temp = link.with_name(link.name + ".next")
        if temp.is_symlink() or temp.is_file():
            temp.unlink()
        elif temp.is_dir():
            shutil.rmtree(temp)
        temp.symlink_to(release / "plugins" / source.name, target_is_directory=True)
        if link.is_dir() and not link.is_symlink():
            # Existing hand-installed plugin trees are state, not disposable
            # cache. Move them under the managed backup root before replacing
            # the path with the release-owned link.
            managed_root = release.parents[1]
            backup_parent = managed_root / "backups" / "replaced-plugin-dirs"
            backup_parent.mkdir(parents=True, exist_ok=True)
            backup_target = backup_parent / f"{name}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
            if backup_target.exists():
                shutil.rmtree(backup_target)
            os.replace(link, backup_target)
        elif link.exists() or link.is_symlink():
            link.unlink()
        os.replace(temp, link)


def _enable_plugins(profile_home: Path, names: list[str]) -> None:
    config = profile_home / "config.yaml"
    if not config.is_file() or not names:
        return
    _run([sys.executable, str(REPO / "install" / "enable_plugins.py"),
          "--config", str(config), *sum((["--name", name] for name in names), [])], check=True)


def _disable_plugins(profile_home: Path, names: list[str]) -> None:
    config = profile_home / "config.yaml"
    if not config.is_file() or not names:
        return
    text = config.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    managed = set(names)
    out: list[str] = []
    in_enabled = False
    item_indent: int | None = None
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if in_enabled and stripped and not stripped.startswith("#"):
            if stripped.startswith("- "):
                if item_indent is None:
                    item_indent = indent
                name = stripped[2:].strip().strip("\"'")
                if indent == item_indent and name in managed:
                    continue
            elif item_indent is not None and indent <= item_indent - 2:
                in_enabled = False
        out.append(line)
        if indent == 2 and stripped == "enabled:":
            in_enabled = True
            item_indent = None
    updated = "".join(out)
    if updated != text:
        _write_text_atomic(config, updated)


def _purge_legacy_plugins(profile_home: Path) -> None:
    """Purge obsolete plugins and leftover state directories."""
    _disable_plugins(profile_home, ["task-progress"])
    stale_data = profile_home / "plugin-data" / "agent-plugin-task-progress-b1e89459"
    if stale_data.is_dir():
        shutil.rmtree(stale_data, ignore_errors=True)


def _apply_runtime_drop_ins(profile: str) -> None:
    """Synchronize service runtime drop-in configurations declared in manifests."""
    manifest_yaml = REPO / "identity" / "manifests" / f"{profile}.manifest.yaml"
    if not manifest_yaml.is_file():
        return
    try:
        import yaml
        data = yaml.safe_load(manifest_yaml.read_text(encoding="utf-8")) or {}
        runtime = data.get("runtime") or {}
        drop_ins = runtime.get("drop_ins") or {}
        gateway_service = data.get("gateway_service") or f"hermes-gateway-{profile}.service"
        if not drop_ins:
            return
        drop_in_dir = Path.home() / ".config" / "systemd" / "user" / f"{gateway_service}.d"
        drop_in_dir.mkdir(parents=True, exist_ok=True)
        changed = False
        for conf_name, env_vars in drop_ins.items():
            conf_path = drop_in_dir / conf_name
            lines = ["[Service]"]
            for k, v in env_vars.items():
                lines.append(f'Environment="{k}={v}"')
            content = "\n".join(lines) + "\n"
            if not conf_path.is_file() or conf_path.read_text(encoding="utf-8") != content:
                _write_text_atomic(conf_path, content)
                changed = True
        if changed:
            _run(["systemctl", "--user", "daemon-reload"])
    except Exception:
        pass


def migrate_config(args: argparse.Namespace, plan: dict[str, Any], *, quiet: bool = False) -> int:
    config = Path(plan["profile_home"]) / "config.yaml"
    original = config.read_text(encoding="utf-8")
    managed = plan["managed_paths"]
    mapping = {
        "/home/caixuf/code/super-agent": managed["engine_root"],
        "/home/caixuf/.local/state/agentcore/analysis-queue": managed["queue_root"],
        "/home/caixuf/.local/state/agentcore/memory-spool": managed["memory_spool"],
        "/home/caixuf/.local/state/agentcore/markdown-outbox": managed["markdown_outbox"],
        "/home/caixuf/.local/state/agentcore/liveness.db": managed["liveness_db"],
        "/home/caixuf/.local/state/agentcore/colleague-team/registry.sqlite": managed["colleague_registry"],
    }
    updated = original
    for old, new in mapping.items():
        updated = updated.replace(old, new)
    if updated != original:
        _write_text_atomic(config, updated)
    if not quiet:
        print(json.dumps({"status": "migrated" if updated != original else "unchanged",
                          "config": str(config)}, indent=2))
    return 0


def _stage_release(plan: dict[str, Any]) -> Path:
    release = Path(plan["release"])
    release.parent.mkdir(parents=True, exist_ok=True)
    # Build away from the live version. Failure must leave both `current` and
    # every previously published release intact.
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=release.parent) as work:
        staged = Path(work) / "release"
        staged.mkdir()
        for name in ("agentcore", "skills", "toolsets", "tools", "runtime", "patches", "overlay"):
            if (REPO / name).is_dir():
                _copy_tree(REPO / name, staged / name)
        managed_sources = [*PLUGIN_SOURCES, *(Path(p) for p in plan.get("external_plugins", []))]
        repo_names = {p.name for p in PLUGIN_SOURCES}
        for source in managed_sources:
            if source not in PLUGIN_SOURCES and source.name in repo_names:
                continue  # Repo-owned adapters must not be replaced by stale snapshots.
            _copy_tree(source, staged / "plugins" / source.name)
        descriptor = {
            "schema_version": 1,
            "version": plan["version"],
            "identity": plan["identity"],
            "tree_sha256": _release_tree_hash(staged),
            "files": _release_files(staged),
        }
        _write_json_atomic(staged / "release.json", descriptor)
        if not _release_valid(staged):
            raise RuntimeError("staged release is incomplete")
        if release.exists() or release.is_symlink():
            try:
                existing = json.loads((release / "release.json").read_text(encoding="utf-8"))
                matches = (
                    not release.is_symlink()
                    and _release_valid(release)
                    and existing == descriptor
                    and _release_tree_hash(release) == descriptor["tree_sha256"]
                    and _release_files(release) == descriptor["files"]
                )
            except (OSError, ValueError):
                matches = False
            if not matches:
                raise RuntimeError("existing release differs; refusing in-place replacement")
            return release
        # A complete release is nonempty, so rename cannot overwrite a release
        # concurrently published by another installer. Never remove it to retry.
        staged.rename(release)
    return release


def install(args: argparse.Namespace, plan: dict[str, Any]) -> int:
    root = Path(plan["managed_root"])
    current = root / "current"
    previous = current.resolve() if current.is_symlink() else None
    manifest_path = root / "install-manifest.json"
    previous_history: list[str] = []
    if manifest_path.is_file():
        try:
            previous_history = list(json.loads(manifest_path.read_text(encoding="utf-8")).get("release_history") or [])
        except (OSError, ValueError):
            previous_history = []
    _snapshot_external_plugins(args)
    plan = build_plan(args)
    if plan["blockers"]:
        print(json.dumps({"status": "blocked", "blockers": plan["blockers"]}, indent=2))
        return 1
    profile_home = Path(plan["profile_home"])
    backup = _snapshot(profile_home, Path(plan["hermes_root"]), root / "backups")
    release = _stage_release(plan)
    plugins = plan["plugins"]
    _switch_current(root, release)
    _relink_plugins(profile_home, release, plugins, plan.get("external_plugins", []), plan.get("memory_providers", []))
    migrate_config(args, plan, quiet=True)
    _enable_plugins(profile_home, plugins)
    _purge_legacy_plugins(profile_home)
    _apply_runtime_drop_ins(getattr(args, "profile", ""))
    overlay_summary = None
    if (release / "overlay").is_dir():
        overlay_summary = apply_profile_overlay(
            profile_home,
            release / "overlay",
            skip_overlay=getattr(args, "skip_overlay", False),
        )
    release_history = list(previous_history)
    if previous is not None and previous.resolve() != release.resolve():
        release_history = [*release_history, str(previous.resolve())][-10:]
    manifest = {
        **plan,
        "status": "installed",
        "tree_sha256": _release_tree_hash(release),
        "release_history": release_history,
        "backup": str(backup),
        "plugin_links": {name: str(profile_home / "plugins" / name)
                         for name in [*plugins, *plan.get("memory_providers", [])]},
        "config_plugin_entries": plugins,
        "legacy_card_patches_at_install": plan.get("legacy_card_patches", []),
        "overlay_applied": overlay_summary,
    }
    _write_json_atomic(root / "install-manifest.json", manifest)
    print(json.dumps({"status": "installed", **manifest}, ensure_ascii=False, indent=2))
    return 0


def _manifest(args: argparse.Namespace) -> tuple[Path, dict[str, Any]] | None:
    root = _managed_root(args)
    path = root / "install-manifest.json"
    if not path.is_file():
        print(json.dumps({"status": "not-installed", "root": str(root)}))
        return None
    return path, json.loads(path.read_text(encoding="utf-8"))


def activate_release(args: argparse.Namespace) -> int:
    found = _manifest(args)
    if found is None:
        return 1
    path, manifest = found
    root = _managed_root(args)
    release = args.release.expanduser().resolve()
    if release.parent != root / "releases" or not _release_valid(release):
        print(json.dumps({"status": "refused", "reason": "invalid_release"}))
        return 1
    current = root / "current"
    previous = current.resolve() if current.is_symlink() else None
    if previous == release:
        print(json.dumps({"status": "active", "release": str(release)}))
        return 0
    if previous is not None:
        manifest["release_history"] = [*manifest.get("release_history", []), str(previous)][-10:]
    _relink_plugins(Path(manifest["profile_home"]), release, manifest.get("plugins", []), manifest.get("external_plugins", []),
                    manifest.get("memory_providers", []))
    _switch_current(root, release)
    manifest["release"] = str(release)
    manifest["tree_sha256"] = _release_tree_hash(release)
    _write_json_atomic(path, manifest)
    print(json.dumps({"status": "activated", "release": str(release),
                      "previous": str(previous) if previous else None}, indent=2))
    return 0


def rollback(args: argparse.Namespace) -> int:
    found = _manifest(args)
    if found is None:
        return 1
    path, manifest = found
    history = list(manifest.get("release_history") or [])
    if not history:
        print(json.dumps({"status": "refused", "reason": "no_previous_release"}))
        return 1
    release = Path(history.pop()).resolve()
    if release.parent != _managed_root(args) / "releases" or not _release_valid(release):
        print(json.dumps({"status": "refused", "reason": "previous_release_incomplete"}))
        return 1
    current = _managed_root(args) / "current"
    previous = current.resolve() if current.is_symlink() else None
    _relink_plugins(Path(manifest["profile_home"]), release, manifest.get("plugins", []), manifest.get("external_plugins", []),
                    manifest.get("memory_providers", []))
    _switch_current(_managed_root(args), release)
    manifest["release"] = str(release)
    manifest["release_history"] = history
    manifest["tree_sha256"] = _release_tree_hash(release)
    _write_json_atomic(path, manifest)
    print(json.dumps({"status": "rolled-back", "release": str(release),
                      "previous": str(previous) if previous else None}, indent=2))
    return 0


def doctor(args: argparse.Namespace) -> int:
    found = _manifest(args)
    if found is None:
        return 1
    path, manifest = found
    root = _managed_root(args)
    current = root / "current"
    release_ok = current.is_symlink() and _release_valid(current.resolve())
    profile_home = Path(manifest["profile_home"])
    links_ok = all(
        (link := profile_home / "plugins" / name).is_symlink()
        and link.exists()
        and current.resolve() in link.resolve().parents
        for name in [*manifest.get("plugins", []), *manifest.get("memory_providers", [])]
    )
    hash_ok = release_ok and manifest.get("tree_sha256") == _release_tree_hash(current.resolve())
    ok = release_ok and links_ok and hash_ok
    print(json.dumps({"status": "ready" if ok else "broken",
                      "release_valid": release_ok, "plugin_links_valid": links_ok,
                      "release_hash_valid": hash_ok,
                      "manifest": manifest}, indent=2))
    return 0 if ok else 1


def uninstall(args: argparse.Namespace) -> int:
    found = _manifest(args)
    if found is None:
        return 1
    _path, manifest = found
    root = Path(manifest["managed_root"])
    profile_home = Path(manifest["profile_home"])
    names = list(manifest.get("config_plugin_entries") or [])
    _disable_plugins(profile_home, names)
    for name in [*names, *manifest.get("memory_providers", [])]:
        link = profile_home / "plugins" / name
        if link.is_symlink():
            try:
                link.resolve(strict=False).relative_to(root)
            except ValueError:
                continue
            link.unlink()
    current = root / "current"
    if current.is_symlink():
        current.unlink()
    if not args.keep_data:
        shutil.rmtree(root)
    print(json.dumps({"status": "uninstalled" if not args.keep_data else "unlinked",
                      "root": str(root), "kept_data": args.keep_data}, indent=2))
    return 0


def _gateway_dependency_smoke(args: argparse.Namespace, hermes: Path) -> dict[str, Any]:
    """Prove the candidate can load the profile's gateway plugins/dependencies."""
    profile_home = _profile_home(args)
    env = _candidate_env(hermes)
    env["HERMES_HOME"] = str(profile_home)
    launcher = hermes / ".hermes" / "bin" / "hermes"
    if not launcher.is_file():
        return {"ok": False, "reason": "candidate_launcher_missing"}
    # Do not import the candidate's modules into this installer process. Ask
    # its own launcher for the exact runtime command, then execute that command
    # under the requested profile home.
    runtime = _run([str(launcher), "--print-runtime-command"], cwd=hermes, env=env)
    if runtime.returncode != 0:
        return {"ok": False, "reason": "candidate_runtime_command_failed",
                "output": (runtime.stdout + runtime.stderr)[-5000:]}
    try:
        command = json.loads(runtime.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "candidate_runtime_command_invalid",
                "output": runtime.stdout[-5000:]}
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        return {"ok": False, "reason": "candidate_runtime_command_invalid"}
    command.extend(["gateway", "status"])
    proc = _run(command, cwd=hermes, env=env)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return {"ok": False, "reason": "candidate_gateway_status_failed",
                "output": output[-5000:]}
    return {"ok": True, "status_output": output[-5000:]}


def switch_gateway_source(args: argparse.Namespace) -> int:
    """Point the profile gateway unit at the managed Hermes candidate.

    The old unit and config are backed up. The command refuses unless the staged
    candidate is the verified v0.22.5 tree with no card patches.
    """
    root = _managed_root(args)
    current = root / "hermes-current"
    if not current.is_symlink():
        print(json.dumps({"status": "blocked", "reason": "managed_hermes_not_staged"}))
        return 1
    hermes = current.resolve()
    adapter = hermes / "plugins/platforms/feishu/adapter.py"
    if not adapter.is_file():
        print(json.dumps({"status": "blocked", "reason": "candidate_adapter_missing"}))
        return 1
    if any(item["in_place"] for item in _legacy_patch_state(hermes)):
        print(json.dumps({"status": "blocked", "reason": "candidate_has_card_patch"}))
        return 1
    try:
        smoke = _smoke_candidate(hermes)
    except RuntimeError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    if not smoke["ok"]:
        print(json.dumps({"status": "blocked", "reason": smoke["reason"],
                          "smoke": smoke}, ensure_ascii=False))
        return 1
    dependency_smoke = _gateway_dependency_smoke(args, hermes)
    if not dependency_smoke["ok"]:
        print(json.dumps({"status": "blocked", "reason": dependency_smoke["reason"],
                          "smoke": dependency_smoke}, ensure_ascii=False))
        return 1
    systemd_unit = Path.home() / ".config/systemd/user" / f"hermes-gateway-{args.profile}.service"
    if not systemd_unit.is_file():
        print(json.dumps({"status": "blocked", "reason": "gateway_unit_missing", "unit": str(systemd_unit)}))
        return 1
    backup = systemd_unit.with_name(systemd_unit.name + ".superagent-before")
    if not backup.exists():
        shutil.copy2(systemd_unit, backup)
    text = systemd_unit.read_text(encoding="utf-8")
    # Keep the unit's environment and lifecycle directives; replace only the
    # launcher path with the candidate's PM-bound launcher, never the source
    # script or the stale build-directory launcher.
    import re
    launcher = hermes / ".hermes" / "bin" / "hermes"
    if not launcher.is_file():
        print(json.dumps({"status": "blocked", "reason": "candidate_launcher_missing"}))
        return 1
    updated, count = re.subn(
        r'^ExecStart=.*$',
        f'ExecStart="{launcher}" "--profile" "{args.profile}" "gateway" "run"',
        text, flags=re.MULTILINE)
    if count != 1:
        print(json.dumps({"status": "blocked", "reason": "execstart_shape_invalid"}))
        return 1
    _write_text_atomic(systemd_unit, updated)
    print(json.dumps({"status": "switched", "unit": str(systemd_unit),
                      "exec_start": str(launcher), "smoke": smoke,
                      "backup": str(backup)}, ensure_ascii=False, indent=2))
    return 0


def update(args: argparse.Namespace, plan: dict[str, Any]) -> int:
    root = _managed_root(args)
    current = root / "current"
    previous = current.resolve() if current.is_symlink() else None
    profile_home = Path(plan["profile_home"])
    services = {
        "gateway": f"hermes-gateway-{args.profile}.service",
        "worker": f"superagent-worker-{args.profile}.service",
        "memory": f"superagent-memory-{args.profile}.service",
    }
    was_active = {
        label: _run(["systemctl", "--user", "is-active", "--quiet", name]).returncode == 0
        for label, name in services.items()
    }
    try:
        for label, name in services.items():
            if was_active[label]:
                _run(["systemctl", "--user", "stop", name], check=True)
        status = install(args, plan)
        if status != 0:
            raise RuntimeError("install failed")
        found = _manifest(args)
        if found is None:
            raise RuntimeError("manifest missing after install")
        _path, manifest = found
        if not _release_valid(Path(manifest["release"])):
            raise RuntimeError("release validation failed")
        _run(["systemctl", "--user", "daemon-reload"], check=True)
        for label, name in services.items():
            if was_active[label]:
                _run(["systemctl", "--user", "start", name], check=True)
        return 0
    except Exception as exc:
        if previous is not None:
            activate_release(argparse.Namespace(**{**vars(args), "release": previous}))
        _run(["systemctl", "--user", "daemon-reload"])
        for label, name in services.items():
            if was_active[label]:
                _run(["systemctl", "--user", "start", name])
        print(json.dumps({"status": "rolled-back", "error": str(exc),
                          "release": str(previous) if previous else None}, indent=2))
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--profile-home", type=Path)
    parser.add_argument("--hermes-root", type=Path, default=DEFAULT_HERMES)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--skip-overlay", action="store_true",
                        help="Skip applying profile overlay even if present in release archive")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "install", "update", "doctor", "uninstall",
                 "activate-release", "rollback", "migrate-config", "candidate-hermes", "install-hermes-candidate", "switch-gateway-source"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--keep-data", action="store_true")
        if name == "activate-release":
            cmd.add_argument("--release", type=Path, required=True)
    raw = list(argv if argv is not None else sys.argv[1:])
    known = {"plan", "install", "update", "doctor", "uninstall",
             "activate-release", "rollback", "migrate-config", "candidate-hermes", "install-hermes-candidate", "switch-gateway-source"}
    if (command_index := next((i for i, value in enumerate(raw) if value in known), None)) is not None:
        before, command, after = raw[:command_index], raw[command_index], raw[command_index + 1:]
        global_opts, command_opts = [], []
        takes_value = {"--profile", "--profile-home", "--hermes-root", "--root"}
        index = 0
        while index < len(after):
            token = after[index]
            if token in takes_value and index + 1 < len(after):
                global_opts.extend([token, after[index + 1]])
                index += 2
            else:
                command_opts.append(token)
                index += 1
        raw = [*before, *global_opts, command, *command_opts]
    args = parser.parse_args(raw)
    if args.command == "doctor":
        return doctor(args)
    if args.command == "uninstall":
        return uninstall(args)
    if args.command == "activate-release":
        return activate_release(args)
    if args.command == "rollback":
        return rollback(args)
    if args.command == "candidate-hermes":
        plan = build_plan(args)
        candidate = build_clean_hermes_candidate(args, plan)
        print(json.dumps(candidate, ensure_ascii=False, indent=2))
        return 0 if candidate["status"] == "ready" else 1
    if args.command == "install-hermes-candidate":
        plan = build_plan(args)
        return install_hermes_candidate(args, plan)
    if args.command == "switch-gateway-source":
        return switch_gateway_source(args)
    plan = build_plan(args)
    if args.command == "plan":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if not plan["blockers"] else 1
    if plan["blockers"]:
        print(json.dumps({"status": "blocked", "blockers": plan["blockers"]}, indent=2))
        return 1
    if args.command == "migrate-config":
        return migrate_config(args, plan)
    if args.command == "update":
        return update(args, plan)
    return install(args, plan)


if __name__ == "__main__":
    raise SystemExit(main())

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
    env: dict[str, str] | None = None, timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, text=True,
                          capture_output=True, env=env or os.environ.copy(), timeout=timeout)
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
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            try:
                mode = path.stat().st_mode & 0o777
            except FileNotFoundError:
                mode = 0o600
            os.fchmod(stream.fileno(), mode)
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
    if script_name in {"apply_feishu_card_replies.py", "apply_feishu_card_lifecycle.py"}:
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
        return _run(command, cwd=work, env=_candidate_env(work), timeout=timeout)
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
    try:
        checkout = _run(["git", "clone", "--quiet", "--depth=1", "--branch", CANDIDATE_TAG,
                         "https://github.com/NousResearch/hermes-agent.git", str(work)], timeout=120)
    except subprocess.TimeoutExpired:
        return {"status": "blocked", "reason": "candidate_clone_timeout"}
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

def _check_runtime_invariants(manifest_path: Path, profile_home: Path, profile: str, *,
                              config_text: str | None = None) -> list[dict[str, str]]:
    """Compare declared profile invariants without exposing configuration values."""
    import yaml
    data = yaml.safe_load(manifest_path.read_bytes())
    config = yaml.safe_load((profile_home / "config.yaml").read_bytes() if config_text is None else config_text)
    if not isinstance(data, dict) or not isinstance(config, dict) or data.get("profile", profile) != profile:
        raise ValueError("manifest or profile configuration is invalid")
    runtime = data.get("runtime", {})
    invariants = runtime.get("config_invariants", {}) if isinstance(runtime, dict) else None
    if not isinstance(invariants, dict):
        raise ValueError("config_invariants must be a mapping")
    report = []
    for path, expected in invariants.items():
        if not isinstance(path, str) or not path or any(not part for part in path.split(".")):
            raise ValueError("invalid configuration invariant path")
        missing = object()
        value = config
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                value = missing
                break
            value = value[part]
        matched = value is not missing and type(value) is type(expected) and value == expected
        report.append({"path": path, "status": "matched" if matched else "mismatch"})
    return report


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    version, identity = _git_identity()
    profile_home = _profile_home(args)
    hermes_root = args.hermes_root.expanduser().resolve()
    root = _managed_root(args)
    release = _release_dir(root, version)
    blockers = []
    runtime_manifest = getattr(args, "runtime_manifest", None)
    if runtime_manifest is not None and not runtime_manifest.is_file():
        blockers.append("runtime_manifest_missing")
    if not (profile_home / "config.yaml").is_file():
        blockers.append("profile_config_missing")
    if not (hermes_root / "plugins" / "platforms" / "feishu" / "adapter.py").is_file():
        blockers.append("hermes_source_missing")
    invariant_report = []
    if runtime_manifest is not None and runtime_manifest.is_file() and (profile_home / "config.yaml").is_file():
        try:
            invariant_report = _check_runtime_invariants(runtime_manifest, profile_home, args.profile)
            if any(row["status"] != "matched" for row in invariant_report):
                blockers.append("runtime_invariants_mismatch")
        except Exception:
            blockers.append("runtime_invariants_unreadable")
    external_plugins = _external_plugin_sources(args)
    if not _plugin_names(args):
        blockers.append("no_managed_plugins")
    legacy = _legacy_patch_state(hermes_root)
    # The released CardKit patch includes retry and delivery UUID support.
    # A separate staged candidate is no evidence about the requested host:
    # identify the actual target adapter before permitting activation.
    card_current = next((item["in_place"] for item in legacy if item["id"] == "card-replies"), False)
    obsolete_renderer = next((item["in_place"] for item in legacy if item["id"] == "card-native-markdown"), False)
    adapter = hermes_root / "plugins/platforms/feishu/adapter.py"
    try:
        card_marker = "_build_reply_card" in adapter.read_text(encoding="utf-8")
    except OSError:
        card_marker = False  # hermes_source_missing already blocks this plan
    if obsolete_renderer or (not card_current and (card_marker or any(item["in_place"] for item in legacy))):
        blockers.append("legacy_card_patches_require_clean_candidate")
    if card_current and not _patch_state(hermes_root, ("apply_feishu_card_lifecycle.py", "card-lifecycle"))["in_place"]:
        blockers.append("card_lifecycle_not_verified")
    if card_current and not _patch_state(hermes_root, ("apply_card_delivery_guard.py", "card-delivery-guard"))["in_place"]:
        blockers.append("card_delivery_guard_not_verified")
    import yaml
    try:
        profile_config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8")) or {}
        feishu = (profile_config.get("platforms") or {}).get("feishu") or {}
        if isinstance(feishu, dict) and feishu.get("enabled") is True and feishu.get("card_replies") is True and not card_current:
            blockers.append("feishu_card_implementation_unverified")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        blockers.append("feishu_card_config_unreadable")
    return {
        "schema_version": 1,
        "action": "superagent-managed-install",
        "version": version,
        "identity": identity,
        "managed_root": str(root),
        "release": str(release),
        "profile_home": str(profile_home),
        "hermes_root": str(hermes_root),
        "runtime_manifest": str(runtime_manifest) if runtime_manifest is not None else None,
        "config_invariants": invariant_report,
        "plugins": _plugin_names(args),
        "memory_providers": ["localvec"] if (REPO / "plugins/localvec/__init__.py").is_file() else [],
        "external_plugins": [str(path) for path in external_plugins],
        "legacy_card_patches": legacy,
        "card_implementation": "release-cardkit" if card_current and not obsolete_renderer else "unverified",
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
                    memory_providers: list[str] | None = None, rollback_backup: Path | None = None) -> None:
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
            backup_parent = rollback_backup or managed_root / "backups" / "replaced-plugin-dirs"
            backup_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            backup_target = (backup_parent / name if rollback_backup is not None else
                             backup_parent / f"{name}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}")
            if backup_target.exists() or backup_target.is_symlink():
                raise FileExistsError("plugin rollback target already exists")
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
    in_plugins = False
    item_indent: int | None = None
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0 and stripped and not stripped.startswith("#"):
            in_plugins = stripped == "plugins:"
            in_enabled = False
            item_indent = None
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
        if in_plugins and indent == 2 and stripped == "enabled:":
            in_enabled = True
            item_indent = None
    updated = "".join(out)
    if updated != text:
        _write_text_atomic(config, updated)


def _disable_legacy_card_producer(profile_home: Path) -> None:
    """Keep the current native card implementation as the sole producer."""
    import yaml
    names = {"hermes-feishu-card", "hfc-runtime-dependency"}
    _disable_plugins(profile_home, list(names))
    config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    plugins = config.get("plugins", {}) if isinstance(config, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if not isinstance(enabled, list) or names.intersection(enabled):
        raise ValueError("legacy card producer could not be disabled safely")


def _purge_legacy_plugins(profile_home: Path) -> None:
    """Disable the retired producer while retaining its state for rollback."""
    _disable_plugins(profile_home, ["task-progress"])
    import yaml
    config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    plugins = config.get("plugins", {}) if isinstance(config, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if not isinstance(enabled, list) or "task-progress" in enabled:
        raise ValueError("retired progress producer could not be disabled safely")


def _apply_runtime_drop_ins(profile: str, *, manifest_path: Path | None = None) -> dict[str, Any]:
    """Synchronize declared service settings; never hide a failed deployment."""
    manifest_yaml = manifest_path or REPO / "identity" / "manifests" / f"{profile}.manifest.yaml"
    if not manifest_yaml.is_file():
        if manifest_path is not None:
            raise FileNotFoundError("explicit runtime manifest is missing")
        return {"status": "not_configured"}
    import re
    import yaml
    manifest_content = manifest_yaml.read_bytes()
    data = yaml.safe_load(manifest_content)
    if not isinstance(data, dict):
        raise ValueError("runtime manifest must be a mapping")
    if data.get("profile", profile) != profile:
        raise ValueError("runtime manifest belongs to a different profile")
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("runtime manifest runtime must be a mapping")
    drop_ins = runtime.get("drop_ins", {})
    if not isinstance(drop_ins, dict):
        raise ValueError("runtime drop_ins must be a mapping")
    if not drop_ins:
        return {"status": "not_configured", "manifest_sha256": hashlib.sha256(manifest_content).hexdigest()}
    gateway_service = data.get("gateway_service") or f"hermes-gateway-{profile}.service"
    if not isinstance(gateway_service, str) or not re.fullmatch(r"[A-Za-z0-9_@.-]+\.service", gateway_service):
        raise ValueError("invalid gateway service name")
    # Validate every entry before changing any unit file.
    contents = {}
    for conf_name, env_vars in drop_ins.items():
        if not isinstance(conf_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.conf", conf_name):
            raise ValueError("invalid runtime drop-in filename")
        if not isinstance(env_vars, dict):
            raise ValueError("runtime drop-in environment must be a mapping")
        lines = ["[Service]"]
        for key, value in env_vars.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("invalid runtime environment name")
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError("invalid runtime environment value")
            setting = f"{key}={value}"
            if any(c in setting for c in "\r\n\0"):
                raise ValueError("invalid runtime environment value")
            escaped = setting.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'Environment="{escaped}"')
        contents[conf_name] = "\n".join(lines) + "\n"
    drop_in_dir = Path.home() / ".config" / "systemd" / "user" / f"{gateway_service}.d"
    drop_in_dir.mkdir(parents=True, exist_ok=True)
    for conf_name, content in contents.items():
        conf_path = drop_in_dir / conf_name
        if not conf_path.is_file() or conf_path.read_text(encoding="utf-8") != content:
            _write_text_atomic(conf_path, content)
    # Always reload, including retries after a prior reload failed. File equality
    # alone cannot establish that systemd has loaded the desired settings.
    _run(["systemctl", "--user", "daemon-reload"], check=True)
    return {"status": "applied", "manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
            "service": gateway_service, "files": sorted(contents)}


def _migrate_owned_path_scalars(original: str, mapping: dict[str, str]) -> str:
    """Rewrite direct path values only; preserve YAML comments and credentials."""
    import re
    import yaml
    from yaml.nodes import MappingNode, ScalarNode, SequenceNode

    root = yaml.compose(original, Loader=yaml.SafeLoader)
    if not isinstance(root, MappingNode):
        raise ValueError("profile configuration must be a YAML mapping")
    protected = re.compile(r"(?:secret|password|token|credential|authorization|api[_-]?key|private[_-]?key|access[_-]?key)", re.I)
    occurrences = []
    protected_nodes = set()

    def visit(node, keys=(), ancestors=frozenset()):
        if isinstance(node, (MappingNode, SequenceNode)):
            if id(node) in ancestors:
                raise ValueError("recursive YAML aliases require manual reconciliation")
            ancestors = ancestors | {id(node)}
        if isinstance(node, MappingNode):
            for key, value in node.value:
                visit(value, (*keys, key.value if isinstance(key, ScalarNode) else ""), ancestors)
        elif isinstance(node, SequenceNode):
            for child in node.value:
                visit(child, keys, ancestors)
        elif isinstance(node, ScalarNode) and node.tag == "tag:yaml.org,2002:str":
            if any(protected.search(str(key)) for key in keys):
                protected_nodes.add(id(node))
            else:
                occurrences.append(node)

    visit(root)
    edits = {}
    for node in occurrences:
        if id(node) in protected_nodes or node.style in ("|", ">"):
            continue
        for old, new in mapping.items():
            if node.value == old or node.value.startswith(old + "/"):
                start, end = node.start_mark.index, node.end_mark.index
                raw = original[start:end]
                if raw.count(old) != 1:
                    raise ValueError("owned path cannot be safely migrated")
                edits[(start, end)] = raw.replace(old, new, 1)
                break
    updated = original
    for (start, end), value in sorted(edits.items(), reverse=True):
        updated = updated[:start] + value + updated[end:]
    return updated



def _migrate_plugin_engine_roots(original: str, legacy_roots: Sequence[str], managed_root: str) -> str:
    """Rewrite explicitly nominated plugin engine roots, leaving business paths untouched."""
    import yaml
    from yaml.nodes import MappingNode, ScalarNode

    roots = set()
    for raw in legacy_roots:
        path = Path(raw)
        if not path.is_absolute() or str(path) in ("/", ".") or ".." in path.parts:
            raise ValueError("legacy engine root must be an absolute non-root path")
        roots.add(str(path))
    root = yaml.compose(original, Loader=yaml.SafeLoader)
    if not isinstance(root, MappingNode):
        raise ValueError("profile configuration must be a YAML mapping")
    edits = {}
    # Aliases can make one scalar serve both an engine setting and a business
    # or credential field. Refuse shared nodes rather than mutating both.
    occurrences = {}
    from yaml.nodes import SequenceNode
    def count_nodes(node):
        occurrences[id(node)] = occurrences.get(id(node), 0) + 1
        if occurrences[id(node)] > 1:
            return
        if isinstance(node, MappingNode):
            for key, value in node.value:
                count_nodes(key)
                count_nodes(value)
        elif isinstance(node, SequenceNode):
            for child in node.value:
                count_nodes(child)
    count_nodes(root)

    def visit(node, keys=(), shared=False, ancestors=frozenset()):
        if not isinstance(node, MappingNode):
            return
        if id(node) in ancestors:
            raise ValueError("recursive YAML aliases require manual reconciliation")
        ancestors = ancestors | {id(node)}
        shared = shared or occurrences[id(node)] > 1
        for key, value in node.value:
            if not isinstance(key, ScalarNode):
                continue
            path = (*keys, key.value)
            if (len(path) == 5 and path[:2] == ("plugins", "entries")
                    and path[3:] == ("settings", "engine_root")
                    and isinstance(value, ScalarNode)
                    and value.tag == "tag:yaml.org,2002:str"
                    and value.style not in ("|", ">") and value.value in roots):
                if shared or occurrences[id(value)] != 1:
                    raise ValueError("aliased engine path requires explicit manual reconciliation")
                raw = original[value.start_mark.index:value.end_mark.index]
                if raw.count(value.value) != 1:
                    raise ValueError("owned engine path cannot be safely migrated")
                edits[(value.start_mark.index, value.end_mark.index)] = raw.replace(value.value, managed_root, 1)
            else:
                visit(value, path, shared, ancestors)
    visit(root)
    for (start, end), value in sorted(edits.items(), reverse=True):
        original = original[:start] + value + original[end:]
    return original


def _migrated_config_text(original: str, args: argparse.Namespace, managed: dict[str, str]) -> str:
    """Compute the exact migration without touching the profile."""
    mapping = {
        "/home/caixuf/.local/state/agentcore/analysis-queue": managed["queue_root"],
        "/home/caixuf/.local/state/agentcore/memory-spool": managed["memory_spool"],
        "/home/caixuf/.local/state/agentcore/markdown-outbox": managed["markdown_outbox"],
        "/home/caixuf/.local/state/agentcore/liveness.db": managed["liveness_db"],
        "/home/caixuf/.local/state/agentcore/colleague-team/registry.sqlite": managed["colleague_registry"],
    }
    updated = _migrate_owned_path_scalars(original, mapping)
    # A checkout can also be a knowledge source or an authorized workspace.
    # Move only explicit plugin engine roots; replacing every checkout-prefixed
    # path would silently redirect unrelated data into an incomplete release.
    legacy_engines = ("/home/caixuf/code/super-agent", *getattr(args, "legacy_engine_root", ()))
    return _migrate_plugin_engine_roots(updated, legacy_engines, managed["engine_root"])


def migrate_config(args: argparse.Namespace, plan: dict[str, Any], *, quiet: bool = False) -> int:
    config = Path(plan["profile_home"]) / "config.yaml"
    original = config.read_text(encoding="utf-8")
    updated = _migrated_config_text(original, args, plan["managed_paths"])
    if updated != original:
        _write_text_atomic(config, updated)

    # Profile choices (model, platform and plugin settings) belong to the
    # operator. Baseline enforcement is an explicit separate operation; an
    # upgrade must not silently rewrite them or reserialize unrelated YAML.
    if not quiet:
        print(json.dumps({"status": "migrated" if updated != original else "unchanged",
                          "config": str(config)}, indent=2))
    return 0



def _planned_worker_pythonpath(profile: str, managed_current: Path, legacy_roots: Sequence[str]) -> dict[str, Any]:
    """Prepare an explicit worker import-path migration without modifying its unit."""
    import shlex
    service = f"superagent-worker-{profile}.service"
    roots = {str(Path(value)) for value in legacy_roots}
    if not roots or any(not Path(value).is_absolute() or value == "/" or ".." in Path(value).parts for value in roots):
        raise ValueError("explicit absolute legacy engine roots required")
    show = _run(["systemctl", "--user", "show", service, "--property=LoadState",
                 "--property=ExecStart", "--property=Environment"])
    if show.returncode != 0:
        raise RuntimeError("worker_configuration_unavailable")
    fields = dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
    if fields.get("LoadState") != "loaded" or "agentcore.pool" not in fields.get("ExecStart", ""):
        raise ValueError("worker_unit_not_recognized")
    env = dict(item.split("=", 1) for item in shlex.split(fields.get("Environment", "")) if "=" in item)
    value = env.get("PYTHONPATH")
    if not value:
        raise ValueError("worker_pythonpath_not_explicit")
    paths = value.split(os.pathsep)
    desired = str(managed_current)
    if not any(path in roots or path == desired for path in paths):
        raise ValueError("worker_legacy_root_not_present")
    paths = [desired if path in roots else path for path in paths]
    # Stable order and all non-engine components are preserved.
    if paths.count(desired) > 1:
        first = paths.index(desired)
        paths = [path for index, path in enumerate(paths) if path != desired or index == first]
    if any("\n" in path or "\r" in path or "\x00" in path for path in paths):
        raise ValueError("invalid_worker_pythonpath")
    return {"service": service, "pythonpath": os.pathsep.join(paths)}



def _apply_worker_pythonpath(profile: str, managed_current: Path, legacy_roots: Sequence[str]) -> dict[str, Any]:
    """Install a managed override after validating the current worker's import path."""
    prepared = _planned_worker_pythonpath(profile, managed_current, legacy_roots)
    value = prepared["pythonpath"].replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    target = Path.home() / ".config/systemd/user" / (prepared["service"] + ".d") / "99-superagent-engine.conf"
    # A consolidated unit already using current needs no competing override.
    if not target.exists() and not target.is_symlink():
        observed = _worker_runtime_observation(profile, managed_current)
        if observed["configured_pythonpath_matches"] is True:
            return {"service": prepared["service"], "override": None, "configured": True}
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("worker_override_not_regular")
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(target, '[Service]\nEnvironment="PYTHONPATH=' + value + '"\n')
    _run(["systemctl", "--user", "daemon-reload"], check=True)
    observed = _worker_runtime_observation(profile, managed_current)
    if observed["configured_pythonpath_matches"] is not True:
        raise RuntimeError("worker_override_not_effective")
    return {"service": prepared["service"], "override": str(target), "configured": True}


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
    # First installs and same-version repairs do not have update's rollback
    # wrapper. Refuse a predicted migration mismatch before any mutation.
    runtime_manifest = getattr(args, "runtime_manifest", None)
    if runtime_manifest is not None:
        try:
            home = Path(plan["profile_home"])
            original = (home / "config.yaml").read_text(encoding="utf-8")
            predicted = _migrated_config_text(original, args, plan["managed_paths"])
            rows = _check_runtime_invariants(Path(runtime_manifest), home, args.profile, config_text=predicted)
            if any(row["status"] != "matched" for row in rows):
                print(json.dumps({"status": "blocked", "reason": "migration_would_change_runtime_invariants"}))
                return 1
        except Exception:
            print(json.dumps({"status": "blocked", "reason": "migration_preview_unavailable"}))
            return 1
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
    _relink_plugins(profile_home, release, plugins, plan.get("external_plugins", []), plan.get("memory_providers", []),
                    rollback_backup=getattr(args, "_plugin_rollback_dir", None))
    migrate_config(args, plan, quiet=True)
    _enable_plugins(profile_home, plugins)
    _purge_legacy_plugins(profile_home)
    if plan.get("card_implementation") == "release-cardkit":
        _disable_legacy_card_producer(profile_home)
    runtime_configuration = _apply_runtime_drop_ins(
        getattr(args, "profile", ""), manifest_path=getattr(args, "runtime_manifest", None))
    worker_configuration = None
    if getattr(args, "command", None) == "update" and getattr(args, "legacy_engine_root", None):
        worker_configuration = _apply_worker_pythonpath(args.profile, root / "current", args.legacy_engine_root)
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
        "plugin_directory_backup": str(args._plugin_rollback_dir) if getattr(args, "_plugin_rollback_dir", None) else None,
        "plugin_links": {name: str(profile_home / "plugins" / name)
                         for name in [*plugins, *plan.get("memory_providers", [])]},
        "config_plugin_entries": plugins,
        "legacy_card_patches_at_install": plan.get("legacy_card_patches", []),
        "overlay_applied": overlay_summary,
        "runtime_configuration": runtime_configuration,
        "worker_configuration": worker_configuration,
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



def _gateway_runtime_observation(profile: str, expected_hermes_root: str | None) -> dict[str, Any]:
    """Report observable launch state without claiming functional readiness."""
    service = f"hermes-gateway-{profile}.service"
    result = {"service": service, "service_state": "unknown", "main_pid": None,
              "configured_launcher_matches": None, "process_command_matches": None,
              "functional_probe": "not_run"}
    try:
        show = _run(["systemctl", "--user", "show", service,
                     "--property=ActiveState", "--property=MainPID", "--property=ExecStart"])
        if show.returncode != 0:
            return result
        fields = dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
        result["service_state"] = fields.get("ActiveState") or "unknown"
        pid = int(fields.get("MainPID", "0"))
        result["main_pid"] = pid if pid > 0 else None
        if not isinstance(expected_hermes_root, str) or not Path(expected_hermes_root).is_absolute():
            return result
        launcher = str(Path(expected_hermes_root) / ".hermes/bin/hermes")
        result["configured_launcher_matches"] = launcher in fields.get("ExecStart", "")
        if pid > 0:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                pass
            else:
                # A command-line match is a launch hint, never proof of loaded
                # modules, plugin activation, or correct end-to-end behavior.
                result["process_command_matches"] = expected_hermes_root.encode() in cmdline
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return result




def _first_engine_path_matches(pythonpath: str, managed_current: Path) -> bool | None:
    """Use Python's import machinery to detect any earlier engine shadowing."""
    from importlib.machinery import PathFinder
    try:
        expected = (managed_current.resolve(strict=True) / "agentcore" / "__init__.py").resolve(strict=True)
        for component in pythonpath.split(os.pathsep):
            # Relative or empty paths depend on the process cwd and cannot be
            # proved from PYTHONPATH alone.
            if not component or not Path(component).is_absolute():
                return None
            spec = PathFinder.find_spec("agentcore", [component])
            if spec is None:
                continue
            # Namespace packages can be combined with later paths, and custom
            # importers or archive entries may not have a real file origin.
            if not spec.origin or spec.origin in {"namespace", "built-in", "frozen"}:
                return None
            try:
                return Path(spec.origin).resolve(strict=True) == expected
            except OSError:
                # A ZIP module still shadows the managed package even though
                # its pseudo-path cannot be resolved as a filesystem file.
                return False
        return None
    except (OSError, ValueError, ImportError):
        return None


def _worker_runtime_observation(profile: str, managed_current: Path) -> dict[str, Any]:
    """Observe worker import paths without exposing other environment values."""
    import shlex
    service = f"superagent-worker-{profile}.service"
    result = {"service": service, "service_state": "unknown", "main_pid": None,
              "configured_pythonpath_matches": None, "process_pythonpath_matches": None}
    try:
        show = _run(["systemctl", "--user", "show", service, "--property=ActiveState",
                     "--property=MainPID", "--property=Environment", "--property=ExecStart"])
        if show.returncode != 0:
            return result
        fields = dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
        result["service_state"] = fields.get("ActiveState") or "unknown"
        pid = int(fields.get("MainPID", "0"))
        result["main_pid"] = pid if pid > 0 else None
        env = dict(item.split("=", 1) for item in shlex.split(fields.get("Environment", "")) if "=" in item)
        expected = str(managed_current)
        if "PYTHONPATH" in env:
            result["configured_pythonpath_matches"] = _first_engine_path_matches(env["PYTHONPATH"], managed_current)
        if pid > 0:
            try:
                data = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                item = next((entry for entry in data if entry.startswith(b"PYTHONPATH=")), None)
                if item is not None:
                    path_value = os.fsdecode(item.split(b"=", 1)[1])
                    # Python -m searches its working directory before PYTHONPATH.
                    # An unknown command shape or inaccessible cwd is not proof.
                    if "agentcore.pool" in fields.get("ExecStart", ""):
                        cwd = Path(f"/proc/{pid}/cwd").resolve(strict=True)
                        # Python searches cwd before PYTHONPATH for -m, and
                        # a module file shadows a package just as a directory does.
                        from importlib.machinery import PathFinder
                        if PathFinder.find_spec("agentcore", [str(cwd)]) is not None:
                            result["process_pythonpath_matches"] = _first_engine_path_matches(str(cwd), managed_current)
                        else:
                            result["process_pythonpath_matches"] = _first_engine_path_matches(path_value, managed_current)
            except OSError:
                pass
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return result



def _legacy_colleague_settlement_observation(profile_home: Path) -> dict[str, Any]:
    """Count known weak historical settlement receipts without opening task contents."""
    import sqlite3
    import yaml
    report = {"state": "unknown", "legacy_settled": None, "legacy_pending": None}
    try:
        config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8")) or {}
        settings = (((config.get("plugins") or {}).get("entries") or {}).get("colleague-team") or {}).get("settings") or {}
        dispatch = settings.get("gateway_dispatch") or {}
        backend = dispatch.get("tool_backend") or dispatch.get("safe_backend") or {}
        root = backend.get("state_dir")
        if not isinstance(root, str) or not Path(root).is_absolute():
            report["state"] = "not_configured"
            return report
        database = Path(root) / "colleague_gateway_tasks.sqlite"
        if not database.is_file() or database.is_symlink():
            return report
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            rows = dict(db.execute("SELECT settlement_state, COUNT(*) FROM tasks "
                                   "WHERE quiescence_evidence_id LIKE 'quiescence-%' GROUP BY settlement_state"))
        report.update(state="observed", legacy_settled=rows.get("settled", 0), legacy_pending=rows.get("pending", 0))
        return report
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error, yaml.YAMLError):
        return report


def _cron_observation(profile_home: Path) -> dict[str, Any]:
    """Report cron failures without exposing job instructions or output."""
    import sqlite3
    from contextlib import closing

    unknown = {"state": "unknown", "enabled_jobs": None, "latest_status": None,
               "latest_delivery": None, "open_incidents": None}
    jobs_path = profile_home / "cron" / "jobs.json"
    db_path = profile_home / "cron" / "executions.db"
    try:
        if any(p.is_symlink() or not p.is_file() for p in (jobs_path, db_path)):
            return unknown
        if jobs_path.stat().st_size > 2 * 1024 * 1024:
            return unknown
        jobs_doc = json.loads(jobs_path.read_text(encoding="utf-8"))
        jobs = jobs_doc if isinstance(jobs_doc, list) else jobs_doc.get("jobs")
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            return unknown
        with closing(sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            latest = db.execute("SELECT status, delivery_outcome FROM executions ORDER BY started_at DESC LIMIT 1").fetchone()
            incidents = db.execute("SELECT COUNT(*) FROM cron_incidents WHERE state NOT IN ('closed', 'resolved')").fetchone()[0]
        # Never surface arbitrary database fields such as exception text or prompts.
        statuses = {"queued", "running", "completed", "failed", "cancelled", "unknown"}
        deliveries = {"sent", "delivered", "failed", "suppressed", "unknown"}
        return {"state": "observed", "enabled_jobs": sum(job.get("enabled") is True for job in jobs),
                "latest_status": latest[0] if latest and latest[0] in statuses else ("unknown" if latest else None),
                "latest_delivery": latest[1] if latest and latest[1] in deliveries else ("unknown" if latest else None),
                "open_incidents": incidents}
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return unknown


def _gateway_queue_observation(profile_home: Path) -> dict[str, Any]:
    """Count blocked admissions without reading message content or changing state."""
    import sqlite3
    from contextlib import closing

    report = {"state": "unknown", "queued": None, "started": None,
              "unknown": None, "queued_blocked_by_unknown": None}
    database = profile_home / "state.db"
    try:
        if database.is_symlink() or not database.is_file():
            return report
        with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            counts = dict(db.execute("SELECT status, COUNT(*) FROM session_admissions "
                                     "WHERE status IN ('queued','started','unknown') GROUP BY status"))
            blocked = db.execute("SELECT COUNT(*) FROM session_admissions q "
                                 "WHERE q.status='queued' AND EXISTS (SELECT 1 FROM session_admissions u "
                                 "WHERE u.target_session_id=q.target_session_id AND u.status='unknown')").fetchone()[0]
        return {"state": "observed", "queued": counts.get("queued", 0),
                "started": counts.get("started", 0), "unknown": counts.get("unknown", 0),
                "queued_blocked_by_unknown": blocked}
    except (OSError, ValueError, sqlite3.Error):
        return report


def _recall_diagnostics_observation(profile_home: Path, managed_current: Path | None = None) -> dict[str, Any]:
    """Observe configured recall reports; missing or old reports stay unknown."""
    import importlib.util
    import re
    import yaml

    try:
        config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise ValueError("invalid configuration")
        entries = config.get("plugins", {}).get("entries", {})
        settings = entries.get("colleague-team", {}).get("settings", {})
        dispatch = settings.get("gateway_dispatch", {})
        members = dispatch.get("tool_backend", {}).get("members", []) if dispatch.get("enabled") is True else []
        if not isinstance(members, list) or len(members) > 64:
            raise ValueError("invalid member list")
        homes = {profile_home.name: profile_home}
        for member in members:
            name, raw = member.get("profile"), member.get("home")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                raise ValueError("invalid member profile")
            path = Path(raw) if isinstance(raw, str) else Path("")
            if not path.is_absolute() or path.name != name or path.resolve().parent != profile_home.resolve().parent:
                raise ValueError("invalid member home")
            if name in homes:
                raise ValueError("duplicate member profile")
            homes[name] = path
        spec = importlib.util.spec_from_file_location("_superagent_recall_diagnostics", REPO / "tools/inspect_recall_diagnostics.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        observations = {}
        bindings = {}
        for name, home in homes.items():
            try:
                data = config if home == profile_home else yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
                command = data.get("memory", {}).get("localvec", {}).get("recall_cmd", [])
                binding = {"plugin": "unknown", "engine": "unknown"}
                if managed_current is not None:
                    release = managed_current.resolve(strict=True)
                    plugin = home / "plugins" / "localvec"
                    if plugin.is_symlink() and plugin.exists():
                        binding["plugin"] = "matched" if plugin.resolve().is_relative_to(release) else "mismatched"
                    elif plugin.exists():
                        binding["plugin"] = "unmanaged"
                    else:
                        binding["plugin"] = "missing"
                    if isinstance(command, list):
                        roots = [x.split("=", 1)[1] for x in command if isinstance(x, str) and x.startswith("AGENTCORE_ROOT=")]
                        direct = [x for x in command if isinstance(x, str) and x.endswith("/tools/recall_entity.py")]
                        candidates = roots + direct
                        if len(candidates) == 1 and Path(candidates[0]).is_absolute():
                            resolved = Path(candidates[0]).resolve()
                            binding["engine"] = "matched" if resolved == release or resolved.is_relative_to(release) else "mismatched"
                        elif candidates:
                            binding["engine"] = "ambiguous"
                bindings[name] = binding
                if not isinstance(command, list) or command.count("--diagnostic-file") != 1:
                    observations[name] = {"status": "unknown", "reason": "diagnostic_unconfigured"}
                    continue
                index = command.index("--diagnostic-file")
                path = Path(command[index + 1])
                if not path.is_absolute():
                    raise ValueError("nonabsolute diagnostic")
                observations[name] = module.inspect(path, now=time.time(), max_age=3600)
            except (OSError, ValueError, TypeError, AttributeError, IndexError, yaml.YAMLError):
                observations[name] = {"status": "unknown", "reason": "configuration_unavailable"}
                bindings.setdefault(name, {"plugin": "unknown", "engine": "unknown"})
        return {"sources": observations, "scope": "last_recall_observation_only",
                "disk_bindings": bindings, "binding_scope": "configured_files_only"}
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return {"status": "unknown", "reason": "configuration_unavailable"}


def _feishu_card_observation(profile_home: Path, hermes_root) -> dict[str, Any]:
    """Compare requested card behavior with installed host code, never claim delivery."""
    import yaml
    try:
        config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8")) or {}
        platform = (config.get("platforms") or {}).get("feishu") or {}
        if not isinstance(platform, dict):
            raise ValueError("invalid Feishu configuration")
        if platform.get("enabled") is not True or platform.get("card_replies") is not True:
            return {"status": "not_requested", "scope": "saved_profile_configuration_only"}
        if not isinstance(hermes_root, str) or not Path(hermes_root).is_absolute():
            return {"status": "unknown", "reason": "host_path_unavailable"}
        root = Path(hermes_root)
        specs = (("apply_feishu_card_replies.py", "card-replies"),
                 ("apply_feishu_card_lifecycle.py", "card-lifecycle"),
                 ("apply_card_delivery_guard.py", "card-delivery-guard"))
        checks = {name: _patch_state(root, (script, name))["in_place"] for script, name in specs}
        return {"status": "configured" if all(checks.values()) else "mismatch", "checks": checks,
                "scope": "saved_configuration_and_host_files_only"}
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return {"status": "unknown", "reason": "configuration_unavailable"}


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
    card_observation = _feishu_card_observation(profile_home, manifest.get("hermes_root"))
    ok = release_ok and links_ok and hash_ok and card_observation["status"] not in ("mismatch", "unknown")
    print(json.dumps({"status": "ready" if ok else "broken",
                      "feishu_card_observation": card_observation,
                      "release_valid": release_ok, "plugin_links_valid": links_ok,
                      "release_hash_valid": hash_ok,
                      "status_scope": "managed_release_files_and_requested_card_host_files_only",
                      "runtime_observation": _gateway_runtime_observation(
                          args.profile, manifest.get("hermes_root")),
                      "worker_observation": _worker_runtime_observation(args.profile, current),
                      "colleague_settlement_observation": _legacy_colleague_settlement_observation(profile_home),
                      "gateway_queue_observation": _gateway_queue_observation(profile_home),
                      "cron_observation": _cron_observation(profile_home),
                      "recall_diagnostics_observation": _recall_diagnostics_observation(profile_home, current),
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
    # A later systemd drop-in can override the unit's ExecStart. Refuse the
    # switch before mutation when systemd cannot describe its loaded drop-ins
    # or any of them declares ExecStart; an operator must first reconcile it.
    import re
    import shlex
    query = _run(["systemctl", "--user", "show", systemd_unit.name,
                  "--property=DropInPaths", "--value"])
    if query.returncode != 0:
        print(json.dumps({"status": "blocked", "reason": "gateway_dropins_unavailable"}))
        return 1
    try:
        dropins = set(shlex.split(query.stdout.strip()))
        # Include newly written user drop-ins that systemd has not reloaded yet.
        user_dropins = Path.home() / ".config" / "systemd" / "user" / (systemd_unit.name + ".d")
        if user_dropins.is_dir():
            dropins.update(str(path) for path in user_dropins.glob("*.conf"))
        conflicts = []
        for item in sorted(dropins):
            path = Path(item)
            if not path.is_absolute() or not path.is_file():
                raise ValueError("unreadable_dropin")
            content = path.read_text(encoding="utf-8")
            if any(re.match(r"^\s*ExecStart\s*=", line) for line in content.splitlines()
                   if not line.lstrip().startswith(("#", ";"))):
                conflicts.append(str(path))
    except (OSError, ValueError):
        print(json.dumps({"status": "blocked", "reason": "gateway_dropins_unreadable"}))
        return 1
    if conflicts:
        print(json.dumps({"status": "blocked", "reason": "gateway_execstart_dropin_conflict",
                          "dropins": conflicts}))
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


def _update_config_snapshot(args: argparse.Namespace, profile_home: Path, root: Path) -> tuple[dict[Path, tuple[bytes, int] | None], str]:
    """Capture files the update can rewrite before stopping any service."""
    import re
    import yaml
    paths = [profile_home / "config.yaml", root / "install-manifest.json"]
    if getattr(args, "legacy_engine_root", None):
        paths.append(Path.home() / ".config/systemd/user" / f"superagent-worker-{args.profile}.service.d" / "99-superagent-engine.conf")
    service = f"hermes-gateway-{args.profile}.service"
    manifest_path = getattr(args, "runtime_manifest", None) or REPO / "identity" / "manifests" / f"{args.profile}.manifest.yaml"
    if manifest_path.is_file():
        try:
            data = yaml.safe_load(manifest_path.read_bytes())
        except yaml.YAMLError as exc:
            raise ValueError("invalid runtime manifest YAML") from exc
        if not isinstance(data, dict) or data.get("profile", args.profile) != args.profile:
            raise ValueError("invalid runtime manifest profile")
        runtime = data.get("runtime", {})
        drop_ins = runtime.get("drop_ins", {}) if isinstance(runtime, dict) else None
        service = data.get("gateway_service") or f"hermes-gateway-{args.profile}.service"
        if not isinstance(drop_ins, dict) or not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_@.-]+\.service", service):
            raise ValueError("invalid runtime manifest drop-ins")
        for name in drop_ins:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.conf", name):
                raise ValueError("invalid runtime drop-in filename")
            paths.append(Path.home() / ".config/systemd/user" / f"{service}.d" / name)
    saved = {}
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("rollback target must be a regular file")
        saved[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.is_file() else None
    return saved, service


def _restore_update_config(saved: dict[Path, tuple[bytes, int] | None]) -> None:
    for path, prior in saved.items():
        if prior is None:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                raise ValueError("rollback target changed type")
            continue
        content, mode = prior
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".superagent-rollback-", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def _plugin_link_snapshot(profile_home: Path) -> dict[str, tuple[str, str | None]]:
    directory = profile_home / "plugins"
    if not directory.is_dir():
        return {}
    result = {}
    for path in directory.iterdir():
        if path.is_symlink():
            result[path.name] = ("link", os.readlink(path))
        elif path.is_dir():
            result[path.name] = ("directory", None)
        else:
            result[path.name] = ("other", None)
    return result


def _restore_managed_plugin_links(profile_home: Path, root: Path, saved: dict[str, tuple[str, str | None]],
                                  rollback_backup: Path | None = None) -> None:
    """Restore links only when their current target belongs to this managed release root."""
    directory = profile_home / "plugins"
    current = _plugin_link_snapshot(profile_home)
    for name in sorted(set(saved) | set(current)):
        old = saved.get(name)
        now = current.get(name)
        if old == now:
            continue
        path = directory / name
        if now is not None:
            if now[0] != "link":
                continue
            try:
                path.resolve(strict=False).relative_to((root / "releases").resolve())
            except ValueError:
                continue
        if old is None:
            if path.is_symlink():
                path.unlink()
        elif old[0] == "link":
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / (".superagent-restore-" + name)
            if temporary.exists() or temporary.is_symlink():
                raise ValueError("rollback temporary plugin path already exists")
            try:
                temporary.symlink_to(old[1], target_is_directory=True)
                os.replace(temporary, path)
            finally:
                if temporary.is_symlink():
                    temporary.unlink()
        elif old[0] == "directory" and rollback_backup is not None:
            backup = rollback_backup / name
            if backup.is_symlink() or not backup.is_dir():
                continue
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                continue
            os.replace(backup, path)


def _assert_service_quiescent(name: str, *, cgroup_root: Path = Path("/sys/fs/cgroup")) -> None:
    """Require proof that systemd's service control group contains no processes."""
    result = _run(["systemctl", "--user", "show", name, "--property=ControlGroup", "--value"])
    if result.returncode != 0:
        raise RuntimeError("service control group unavailable")
    group = result.stdout.strip()
    if not group:
        return
    relative = Path(group)
    if not relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("invalid service control group")
    events = cgroup_root.joinpath(*relative.parts[1:]) / "cgroup.events"
    try:
        fields = dict(line.split(None, 1) for line in events.read_text().splitlines())
    except (OSError, ValueError):
        raise RuntimeError("service control group state unavailable") from None
    if fields.get("populated") != "0":
        raise RuntimeError("service control group is not empty")


def _declared_runtime_services(args: argparse.Namespace) -> list[str]:
    """Read explicitly owned services from the host's runtime manifest."""
    import re
    import yaml
    manifest = getattr(args, "runtime_manifest", None)
    if manifest is None:
        return []
    data = yaml.safe_load(Path(manifest).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or data.get("profile", args.profile) != args.profile:
        raise ValueError("invalid_runtime_service_manifest")
    runtime = data.get("runtime") or {}
    if not isinstance(runtime, dict):
        raise ValueError("invalid_runtime_services")
    names = runtime.get("managed_services", [])
    if (not isinstance(names, list) or len(names) > 64
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_@.-]+\.service", name)
                   for name in names) or len(set(names)) != len(names)):
        raise ValueError("invalid_runtime_services")
    return names


def _declared_runtime_scripts(args: argparse.Namespace, services: list[str]) -> dict[str, str]:
    """Bind declared script-form services to exact host-owned script paths."""
    import yaml
    manifest = getattr(args, "runtime_manifest", None)
    if manifest is None:
        return {}
    data = yaml.safe_load(Path(manifest).read_text(encoding="utf-8")) or {}
    runtime = data.get("runtime") or {}
    scripts = runtime.get("managed_script_services", {})
    if not isinstance(scripts, dict):
        raise ValueError("invalid_runtime_scripts")
    for name, script in scripts.items():
        if (name not in services or not isinstance(script, str) or not Path(script).is_absolute()
                or ".." in Path(script).parts or not script.endswith(".py")):
            raise ValueError("invalid_runtime_scripts")
    return scripts


def _managed_service_import_observation(service: str, managed_current: Path, *, script: str | None = None) -> bool | None:
    """Observe Python's current import search path for a declared agentcore service."""
    import importlib.machinery
    try:
        result = _run(["systemctl", "--user", "show", service, "--property=ActiveState", "--property=MainPID"])
        if result.returncode:
            return None
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        pid = int(fields.get("MainPID", "0"))
        if fields.get("ActiveState") != "active" or pid <= 0:
            return None
        proc = Path("/proc") / str(pid)
        args = [os.fsdecode(part) for part in proc.joinpath("cmdline").read_bytes().split(b"\0") if part]
        cwd = proc.joinpath("cwd").resolve(strict=True)
        if any(part in {"-I", "-E"} for part in args[1:]):
            return None  # Python ignores PYTHONPATH in isolated/environment-ignoring modes.
        if script is None:
            if not any(args[i] == "-m" and args[i + 1].startswith("agentcore.") for i in range(len(args) - 1)):
                return None
        else:
            expected_script = Path(script).resolve(strict=True)
            candidates = [Path(part) if Path(part).is_absolute() else cwd / part
                          for part in args[1:] if part.endswith(".py")]
            if not any(candidate.resolve(strict=True) == expected_script for candidate in candidates):
                return None
        env = proc.joinpath("environ").read_bytes().split(b"\0")
        value = next((os.fsdecode(x.split(b"=", 1)[1]) for x in env if x.startswith(b"PYTHONPATH=")), None)
        if value is None:
            return None
        if importlib.machinery.PathFinder.find_spec("agentcore", [str(cwd)]) is not None:
            return _first_engine_path_matches(str(cwd), managed_current)
        return _first_engine_path_matches(value, managed_current)
    except (OSError, ValueError, ImportError, subprocess.SubprocessError):
        return None


def update(args: argparse.Namespace, plan: dict[str, Any]) -> int:
    root = _managed_root(args)
    current = root / "current"
    previous = current.resolve() if current.is_symlink() else None
    profile_home = Path(plan["profile_home"])
    try:
        saved_config, gateway_service = _update_config_snapshot(args, profile_home, root)
        saved_links = _plugin_link_snapshot(profile_home)
        extra_services = _declared_runtime_services(args)
        extra_scripts = _declared_runtime_scripts(args, extra_services)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": "rollback_snapshot_failed", "error": type(exc).__name__}))
        return 1
    services = {
        "gateway": gateway_service,
        "worker": f"superagent-worker-{args.profile}.service",
        "memory": f"superagent-memory-{args.profile}.service",
    }
    if set(extra_services) & set(services.values()):
        print(json.dumps({"status": "blocked", "reason": "duplicate_runtime_service"}))
        return 1
    services.update({f"managed-{index}": name for index, name in enumerate(extra_services)})
    # Declared services are listed in stop order; reverse them on start so
    # dependencies such as alert consumers come up before their monitors.
    start_order = list(services.items())[:3] + list(reversed(list(services.items())[3:]))
    was_active = {
        label: _run(["systemctl", "--user", "is-active", "--quiet", name]).returncode == 0
        for label, name in services.items()
    }
    # A failed or inactive unit can still own uninterruptible processes.
    # Refuse to touch code before proving their cgroups are empty.
    try:
        for label, name in services.items():
            if not was_active[label]:
                _assert_service_quiescent(name)
    except RuntimeError as exc:
        print(json.dumps({"status": "blocked", "reason": "service_stop_unproven",
                          "service": name, "error": str(exc)}))
        return 1
    stopped = []
    try:
        for label, name in services.items():
            if was_active[label]:
                _run(["systemctl", "--user", "stop", name], check=True)
                _assert_service_quiescent(name)
                stopped.append(name)
    except Exception as exc:
        restart_errors = []
        for stopped_name in reversed(stopped):
            if _run(["systemctl", "--user", "start", stopped_name]).returncode:
                restart_errors.append(stopped_name)
        print(json.dumps({"status": "blocked", "reason": "service_stop_unproven",
                          "service": name, "error": type(exc).__name__,
                          "restart_errors": restart_errors}))
        return 1
    started_during_update = []
    try:
        backup_parent = root / "backups"
        backup_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        args._plugin_rollback_dir = Path(tempfile.mkdtemp(prefix="update-plugins-", dir=backup_parent))
        status = install(args, plan)
        if status != 0:
            raise RuntimeError("install failed")
        found = _manifest(args)
        if found is None:
            raise RuntimeError("manifest missing after install")
        _path, manifest = found
        if not _release_valid(Path(manifest["release"])):
            raise RuntimeError("release validation failed")
        # Migration and plugin setup run after the preflight plan. Check the
        # declared values again before restarting any service; update's normal
        # exception path restores the previous config, links and release.
        runtime_manifest = getattr(args, "runtime_manifest", None)
        if runtime_manifest is not None:
            invariants = _check_runtime_invariants(Path(runtime_manifest), profile_home, args.profile)
            if any(item["status"] != "matched" for item in invariants):
                raise RuntimeError("runtime_invariants_changed_after_install")
        _run(["systemctl", "--user", "daemon-reload"], check=True)
        for label, name in start_order:
            if was_active[label]:
                # A failed start may still have launched a process; record it
                # before calling systemd so recovery must stop it as well.
                started_during_update.append((label, name))
                _run(["systemctl", "--user", "start", name], check=True)
        if was_active["worker"] and getattr(args, "legacy_engine_root", None):
            observed = _worker_runtime_observation(args.profile, root / "current")
            if observed.get("process_pythonpath_matches") is not True:
                raise RuntimeError("worker_process_import_path_unproven")
        for index, name in enumerate(extra_services):
            if not was_active[f"managed-{index}"]:
                continue
            deadline = time.monotonic() + 10
            options = {"script": extra_scripts[name]} if name in extra_scripts else {}
            while _managed_service_import_observation(name, root / "current", **options) is not True:
                if time.monotonic() >= deadline:
                    raise RuntimeError("managed_service_import_path_unproven:" + name)
                time.sleep(0.2)
        return 0
    except Exception as exc:
        rollback_errors = []
        for label, name in reversed(started_during_update):
            try:
                _run(["systemctl", "--user", "stop", name], check=True)
                _assert_service_quiescent(name)
            except Exception:
                rollback_errors.append(f"stop-{label}")
        if rollback_errors:
            # Never swap code or configuration underneath an unproven stop.
            print(json.dumps({"status": "rollback-incomplete", "rollback_errors": rollback_errors,
                              "error": str(exc), "release": str(current.resolve()) if current.is_symlink() else None}, indent=2))
            return 1
        # Overlay assets can span skills, identity and memories. Their rollback
        # needs its own receipt; do not report full recovery without one.
        if (REPO / "overlay").is_dir() and not getattr(args, "skip_overlay", False):
            rollback_errors.append("overlay-unverified")
        if previous is not None:
            try:
                if activate_release(argparse.Namespace(**{**vars(args), "release": previous})) != 0:
                    rollback_errors.append("release")
            except Exception:
                rollback_errors.append("release")
        try:
            _restore_update_config(saved_config)
        except (OSError, ValueError):
            rollback_errors.append("configuration")
        try:
            _restore_managed_plugin_links(profile_home, root, saved_links, getattr(args, "_plugin_rollback_dir", None))
            if _plugin_link_snapshot(profile_home) != saved_links:
                rollback_errors.append("plugin-links")
            if previous is None and current.is_symlink():
                expected = plan.get("release")
                if expected and current.resolve(strict=True) == Path(expected).resolve(strict=True):
                    current.unlink()
                else:
                    rollback_errors.append("release")
        except OSError:
            rollback_errors.append("plugin-links")
        if _run(["systemctl", "--user", "daemon-reload"]).returncode:
            rollback_errors.append("daemon-reload")
        for label, name in start_order:
            if was_active[label] and _run(["systemctl", "--user", "start", name]).returncode:
                rollback_errors.append(f"restart-{label}")
        print(json.dumps({"status": "rollback-incomplete" if rollback_errors else "rolled-back",
                          "rollback_errors": rollback_errors, "error": str(exc),
                          "release": str(previous) if previous else None}, indent=2))
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--profile-home", type=Path)
    parser.add_argument("--hermes-root", type=Path, default=DEFAULT_HERMES)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--legacy-engine-root", action="append", default=[],
                        help="Explicit prior plugin engine root; repeat for each trusted path")
    parser.add_argument("--runtime-manifest", type=Path,
                        help="Explicit host runtime manifest; missing files fail deployment")
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
        takes_value = {"--profile", "--profile-home", "--hermes-root", "--root", "--runtime-manifest", "--legacy-engine-root"}
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

import ast
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "patches/apply_managed_worker_scope.py"

BASE = """import asyncio, os, subprocess, sys
async def spawn(authority, ref, row):
    try:
        from gateway.session_worker_spawn import acquire_process
        from gateway.session_managed_worker_log import worker_log_path
        log_path = worker_log_path(authority.profile_id)
        process = await acquire_process(subprocess.Popen, [sys.executable, '-m', 'agent.managed_worker'],
            cwd=cwd, stdin=subprocess.PIPE, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if log_path is None else subprocess.PIPE, **WORKER_POPEN)
    except asyncio.CancelledError:
        # acquire_process owns late-child cleanup; no bootstrap or reservation was sent.
        spawning.pop(ref.session_id, None)
        _interrupted_before_bootstrap(authority, row, _latched_stop_cause(authority, ref.session_id, generation))
"""


def call(root, action):
    return subprocess.run([sys.executable, str(SCRIPT), action], env={**os.environ, "HERMES_AGENT_DIR": str(root)},
                          capture_output=True, text=True)


def test_scope_patch_is_opt_in_and_idempotent(tmp_path):
    path = tmp_path / "gateway/session_managed_worker.py"
    path.parent.mkdir()
    path.write_text(BASE)
    assert call(tmp_path, "--preflight").returncode == 0
    assert call(tmp_path, "--apply").returncode == 0
    updated = path.read_text()
    ast.parse(updated)
    assert "os.environ.get('SUPERAGENT_MANAGED_WORKER_SCOPE', '0')" in updated
    assert "--scope" in updated
    assert call(tmp_path, "--apply").returncode == 0
    assert path.read_text() == updated


def test_scope_patch_refuses_unknown_host_without_modification(tmp_path):
    path = tmp_path / "gateway/session_managed_worker.py"
    path.parent.mkdir()
    original = BASE.replace("agent.managed_worker", "different.worker")
    path.write_text(original)
    assert call(tmp_path, "--apply").returncode != 0
    assert path.read_text() == original


def test_scope_launch_is_explicit_and_invalid_mode_never_launches(tmp_path, monkeypatch):
    import asyncio
    import types
    import pytest
    path = tmp_path / "gateway/session_managed_worker.py"
    path.parent.mkdir()
    path.write_text(BASE)
    assert call(tmp_path, "--apply").returncode == 0
    calls = []
    async def acquire(_factory, command, **kwargs):
        calls.append(command)
        return object()
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    spawn = types.ModuleType("gateway.session_worker_spawn")
    spawn.acquire_process = acquire
    worker_log = types.ModuleType("gateway.session_managed_worker_log")
    worker_log.worker_log_path = lambda profile: None
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_worker_spawn", spawn)
    monkeypatch.setitem(sys.modules, "gateway.session_managed_worker_log", worker_log)
    namespace = {"cwd": str(tmp_path), "env": {}, "generation": 3, "WORKER_POPEN": {}}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    authority = types.SimpleNamespace(profile_id="/profile/cases")
    ref = types.SimpleNamespace(session_id="test-session")
    row = {"admission_id": "admission-1"}
    monkeypatch.delenv("SUPERAGENT_MANAGED_WORKER_SCOPE", raising=False)
    asyncio.run(namespace["spawn"](authority, ref, row))
    assert calls.pop() == [sys.executable, "-m", "agent.managed_worker"]
    monkeypatch.setenv("SUPERAGENT_MANAGED_WORKER_SCOPE", "1")
    asyncio.run(namespace["spawn"](authority, ref, row))
    command = calls.pop()
    assert command[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
    assert command[-4:] == ["--", sys.executable, "-m", "agent.managed_worker"]
    monkeypatch.setenv("SUPERAGENT_MANAGED_WORKER_SCOPE", "invalid")
    with pytest.raises(RuntimeError, match="invalid_managed_worker_scope_setting"):
        asyncio.run(namespace["spawn"](authority, ref, row))
    assert calls == []

import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import types

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "patches/apply_managed_worker_scope_binding.py"
BASE = """import asyncio, os
async def run(authority, row, process, worker, hello, policy, text, ref):
        from gateway.session_runtime_workers import tracked_write
        scope = await tracked_write(authority, partial(reserve_admission_worker, authority,
                    admission_id=row['admission_id'], process=process, principal_id=row['principal_id'], hello=hello))
        worker.worker = (scope['pid'], scope['birth'])
        # The child reads nothing else until the exact reservation has committed.
        frame = await asyncio.to_thread(_bootstrap, authority, ref, row, policy, scope, text)
"""


@pytest.fixture
def module(tmp_path, monkeypatch):
    source = tmp_path / "gateway/session_managed_worker.py"
    source.parent.mkdir()
    source.write_text(BASE)
    env = {**os.environ, "HERMES_AGENT_DIR": str(tmp_path)}
    applied = subprocess.run([sys.executable, str(SCRIPT), "--apply"], env=env, capture_output=True, text=True)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    fake = types.ModuleType("hermes_state_runtime")
    fake.RuntimeStoreError = type("RuntimeStoreError", (Exception,), {})
    fake._epoch = lambda conn, epoch: None
    fake._admission = lambda conn, admission: {"status": "started", "owner_epoch": 7, "generation": 3}
    monkeypatch.setitem(sys.modules, "hermes_state_runtime", fake)
    spec = importlib.util.spec_from_file_location("scope_binding_test", tmp_path / "gateway/superagent_scope_binding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scope_binding_requires_live_matching_worker_and_is_idempotent(module, monkeypatch):
    import hashlib
    profile = "/home/example/.hermes/profiles/cases"
    admission = "admission-1"
    identity = hashlib.sha256((profile + "\0" + admission + "\0" + "3").encode()).hexdigest()[:32]
    group = "/user.slice/superagent-turn-" + identity + ".scope"
    result = types.SimpleNamespace(returncode=0, stdout="ActiveState=active\nDelegate=no\nControlGroup=" + group + "\n")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: result)
    original_read = Path.read_text
    def read(path, *a, **kw):
        name = str(path)
        if name == "/proc/123/cgroup": return "0::" + group + "\n"
        if name.endswith("/cgroup.events"): return "populated 1\nfrozen 0\n"
        if name == "/proc/sys/kernel/random/boot_id": return "01234567-89ab-cdef-0123-456789abcdef\n"
        return original_read(path, *a, **kw)
    monkeypatch.setattr(Path, "read_text", read)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("create table worker_executions(execution_id text,session_id text,generation integer,owner_epoch integer,status text)")
    conn.execute("insert into worker_executions values('exec-1','session-1',3,7,'registered')")
    db = types.SimpleNamespace(_execute_write=lambda fn: fn(conn))
    authority = types.SimpleNamespace(profile_id=profile, epoch=7, db=db)
    row = {"admission_id": admission, "generation": 3}
    scope = {"profile_id": profile, "generation": 3, "pid": 123, "birth": 44.0,
             "session_id": "session-1", "execution_id": "exec-1"}
    module.bind_scope(authority, row, scope)
    module.bind_scope(authority, row, scope)
    assert conn.execute("select count(*) from superagent_scope_bindings").fetchone()[0] == 1
    result.stdout = "ActiveState=active\nDelegate=yes\nControlGroup=" + group + "\n"
    with pytest.raises(Exception, match="scope_binding_unavailable"):
        module.bind_scope(authority, row, scope)

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
HOST = Path(os.environ.get("HERMES_SOURCE", Path.home() / ".hermes" / "hermes-agent"))
PATCH = REPO / "patches" / "patch_background_verification.py"
HOST_PYTHON = HOST / "venv" / "bin" / "python"


def _run(*args: object, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCH), *map(str, args)],
        cwd=cwd or REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _stage_original(tmp_path: Path) -> Path:
    if not (HOST / "agent" / "verification_evidence.py").is_file():
        pytest.skip("Hermes source unavailable for background verification integration")
    if not (HOST / "tools" / "process_registry.py").is_file():
        pytest.skip("Hermes process registry unavailable")
    if not HOST_PYTHON.is_file():
        pytest.skip("Hermes host interpreter unavailable")

    stage = tmp_path / "host"
    stage.mkdir()
    excluded = {
        ".git", "agent", "tools", "node_modules", "venv", ".venv", "ui-tui", "web", "website",
        "apps", "tests", ".pytest_cache", "__pycache__",
    }
    for item in HOST.iterdir():
        if item.name in excluded:
            continue
        os.symlink(item, stage / item.name, target_is_directory=item.is_dir())
    for package, copied_name in (
        ("agent", "verification_evidence.py"),
        ("tools", "process_registry.py"),
    ):
        destination = stage / package
        destination.mkdir()
        for item in (HOST / package).iterdir():
            if item.name == copied_name:
                continue
            os.symlink(item, destination / item.name, target_is_directory=item.is_dir())
        shutil.copy2(HOST / package / copied_name, destination / copied_name)
    # The developer host may already have this release installed. Reconstruct
    # only exact known pristine fragments in the isolated copies, so absent/
    # partial-patch tests do not depend on installation state. No host write.
    spec = importlib.util.spec_from_file_location("background_fixture_patch", PATCH)
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)
    for relative, transform, replacements in (
        (patcher.EVIDENCE_REL, patcher.transform_evidence, (
            (patcher.EVIDENCE_NEW, patcher.EVIDENCE_OLD),
            (patcher.TRANSACTION_NEW, patcher.TRANSACTION_OLD),
            (patcher.INSERT_NEW, patcher.INSERT_OLD),
        )),
        (patcher.REGISTRY_REL, patcher.transform_registry, (
            (patcher.REG_HELPER, ""),
            (patcher.HANDLE_NEW, patcher.HANDLE_OLD),
        )),
    ):
        target = stage / relative
        installed = target.read_text(encoding="utf-8")
        transformed = transform(installed)  # rejects unknown/partial drift
        if transformed == installed:
            pristine = installed
            for new, old in replacements:
                assert pristine.count(new) == 1
                pristine = pristine.replace(new, old, 1)
            assert transform(pristine) == installed
            target.write_text(pristine, encoding="utf-8")
    return stage


def _stage_patched(tmp_path: Path) -> Path:
    stage = _stage_original(tmp_path)
    applied = _run("--apply", "--agent-dir", stage)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    return stage


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_host(stage: Path, tmp_path: Path, code: str, *args: object) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "hermes-home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(HERMES_HOME=str(home), HERMES_VERIFY_ON_STOP="1", PYTHONPATH=str(stage))
    return subprocess.run(
        [str(HOST_PYTHON), "-c", code, *map(str, args)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )


_BOOTSTRAP = r'''
import json, sqlite3, sys, time
from pathlib import Path
stage, root_text = sys.argv[1], sys.argv[2]
sys.path.insert(0, stage)
root = Path(root_text)
root.mkdir(parents=True, exist_ok=True)
(root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
import agent.coding_context as coding
coding.project_facts_for = lambda cwd=None: ({"root": str(root), "verifyCommands": ["pytest"]}
    if cwd is not None and Path(cwd).resolve().is_relative_to(root.resolve()) else None)
import agent.verification_evidence as ve
import tools.process_registry as pr
import model_tools

def process(sid, *, exit_code=0, reason="exited", started_at=None, output="1 passed", total=None,
            parent="caller-session", owner="caller-task", exited=True,
            command="python -m pytest tests -q"):
    started_at = time.time() - 30 if started_at is None else started_at
    total = len(output) if total is None else total
    s = pr.ProcessSession(
        id=sid, command=command, task_id=owner, owner_task_id=owner,
        session_key="route", parent_session_id=parent, cwd=str(root), started_at=started_at,
        exited=exited, exit_code=exit_code, completion_reason=reason,
        output_buffer=output, total_output_chars=total,
    )
    pr.process_registry._finished[s.id] = s
    return s

def dispatch(sid, *, caller_session="caller-session", caller_task="caller-task"):
    raw = model_tools.handle_function_call(
        "process_manage", {"action": "poll", "session_id": sid},
        task_id=caller_task, session_id=caller_session,
        skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )
    return json.loads(raw)

def count_events(home):
    path = Path(home) / "verification_evidence.db"
    if not path.is_file():
        return 0
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return conn.execute("select count(*) from verification_events").fetchone()[0]
'''


def _run_scenario(stage: Path, tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "hermes-home"
    root = tmp_path / "project"
    code = _BOOTSTRAP + "\n" + body
    result = subprocess.run(
        [str(HOST_PYTHON), "-c", code, str(stage), str(root), str(home)],
        cwd=tmp_path,
        env={**os.environ, "HERMES_HOME": str(home), "HERMES_VERIFY_ON_STOP": "1", "PYTHONPATH": str(stage)},
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_completed_success_is_passed_once_across_repeated_observations(tmp_path):
    stage = _stage_patched(tmp_path)
    result = _run_scenario(stage, tmp_path, r'''
proc = process("proc_success0001")
first = dispatch(proc.id)
second = dispatch(proc.id)
assert first["status"] == second["status"] == "exited"
assert count_events(sys.argv[3]) == 1
status = ve.verification_status(session_id="caller-session", cwd=root)
assert status["status"] == "passed", status
assert status["evidence"]["command"] == proc.command
assert status["evidence"]["canonical_command"] == "pytest"
print("success=passed repeat=one")
''')
    assert "success=passed repeat=one" in result.stdout


def test_completed_failure_is_recorded_as_failed(tmp_path):
    stage = _stage_patched(tmp_path)
    result = _run_scenario(stage, tmp_path, r'''
proc = process("proc_failure0001", exit_code=1, output="1 failed",
               command="python -m pytest tests -q")
response = dispatch(proc.id)
assert response["exit_code"] == 1
assert count_events(sys.argv[3]) == 1
status = ve.verification_status(session_id="caller-session", cwd=root)
assert status["status"] == "failed", status
print("failure=failed")
''')
    assert "failure=failed" in result.stdout


@pytest.mark.parametrize("command", ["echo pytest", "printf pytest", "cat pytest"])
def test_existing_classifier_rejects_incidental_pytest_text(tmp_path, command):
    stage = _stage_patched(tmp_path)
    result = _run_scenario(stage, tmp_path, f'''
process("proc_incidental01", command={command!r})
r = dispatch("proc_incidental01")
assert r["status"] == "exited" and r["exit_code"] == 0
assert count_events(sys.argv[3]) == 0
print("incidental_rejected")
''')
    assert "incidental_rejected" in result.stdout


@pytest.mark.parametrize(
    "case",
    [
        "no_exit_code",
        "spawn_receipt",
        "running",
        "unknown_reason",
        "truncated_output",
        "result_record_mismatch",
    ],
)
def test_untrusted_or_incomplete_observations_never_pass(tmp_path, case):
    stage = _stage_patched(tmp_path)
    bodies = {
        "no_exit_code": r'''
process("proc_noexit00001", exit_code=None)
r = dispatch("proc_noexit00001")
assert r["status"] == "exited" and r["exit_code"] is None
assert count_events(sys.argv[3]) == 0
''',
        "spawn_receipt": r'''
process("proc_spawn000001", exited=False, exit_code=None, reason="exited")
pr._record_completed_verification_evidence(
    action="poll", session_id="proc_spawn000001",
    result={"status": "running", "exit_code": 0},
    caller_session_id="caller-session", caller_task_id="caller-task")
assert count_events(sys.argv[3]) == 0
''',
        "running": r'''
process("proc_running0001", exited=False, exit_code=None, reason="exited")
pr._record_completed_verification_evidence(
    action="poll", session_id="proc_running0001",
    result={"status": "running", "exit_code": 0},
    caller_session_id="caller-session", caller_task_id="caller-task")
assert count_events(sys.argv[3]) == 0
''',
        "unknown_reason": r'''
process("proc_unknown0001", exit_code=0, reason="lost")
r = dispatch("proc_unknown0001")
assert r["status"] == "exited"
assert count_events(sys.argv[3]) == 0
''',
        "truncated_output": r'''
process("proc_truncated01", output="tail only", total=10000)
r = dispatch("proc_truncated01")
assert r["status"] == "exited" and r["exit_code"] == 0
assert count_events(sys.argv[3]) == 0
''',
        "result_record_mismatch": r'''
process("proc_mismatch001", exit_code=0)
pr._record_completed_verification_evidence(
    action="poll", session_id="proc_mismatch001",
    result={"status": "exited", "exit_code": 1, "completion_reason": "exited"},
    caller_session_id="caller-session", caller_task_id="caller-task")
assert count_events(sys.argv[3]) == 0
''',
    }
    result = _run_scenario(stage, tmp_path, bodies[case] + f'print("rejected={case}")\n')
    assert f"rejected={case}" in result.stdout


@pytest.mark.parametrize("mismatch", ["owner", "session", "missing_caller"])
def test_callers_without_exact_process_ownership_are_rejected(tmp_path, mismatch):
    stage = _stage_patched(tmp_path)
    if mismatch == "owner":
        body = r'''
process("proc_ownerbad0001")
r = dispatch("proc_ownerbad0001", caller_task="other-task")
assert r["status"] == "exited"
assert count_events(sys.argv[3]) == 0
'''
    elif mismatch == "session":
        body = r'''
process("proc_sessbad00001")
r = dispatch("proc_sessbad00001", caller_session="other-session")
assert r["status"] == "exited"
assert count_events(sys.argv[3]) == 0
'''
    else:
        body = r'''
process("proc_nocaller0001")
r = dispatch("proc_nocaller0001", caller_session="", caller_task="")
assert r["status"] == "exited"
assert count_events(sys.argv[3]) == 0
'''
    result = _run_scenario(stage, tmp_path, body + f'print("ownership_rejected={mismatch}")\n')
    assert f"ownership_rejected={mismatch}" in result.stdout


def test_other_session_edit_after_start_makes_completed_test_stale(tmp_path):
    stage = _stage_patched(tmp_path)
    result = _run_scenario(stage, tmp_path, r'''
proc = process("proc_stale000001", started_at=time.time() - 60)
ve.mark_workspace_edited(session_id="other-session", cwd=root, paths=[str(root / "changed.py")])
r = dispatch(proc.id)
assert r["status"] == "exited" and r["exit_code"] == 0
assert count_events(sys.argv[3]) == 0
status = ve.verification_status(session_id="caller-session", cwd=root)
assert status["status"] == "unverified", status
print("cross_session_edit=stale")
''')
    assert "cross_session_edit=stale" in result.stdout


def test_newer_event_prevents_old_test_after_competing_edit_and_pass(tmp_path):
    stage = _stage_patched(tmp_path)
    result = _run_scenario(stage, tmp_path, r'''
proc = process("proc_highwater001", started_at=time.time() - 60,
               command="python -m pytest tests -q")
ve.mark_workspace_edited(session_id="other-session", cwd=root, paths=[str(root / "changed.py")])
assert ve.record_terminal_result(
    command="pytest", cwd=root, session_id="other-session", exit_code=0, output="1 passed")
assert ve.verification_status(session_id="other-session", cwd=root)["status"] == "passed"
r = dispatch(proc.id)
assert r["status"] == "exited" and r["exit_code"] == 0
assert count_events(sys.argv[3]) == 1
status = ve.verification_status(session_id="other-session", cwd=root)
assert status["status"] == "passed"
assert status["evidence"]["session_id"] == "other-session"
print("old_completion_rejected_after_newer_event")
''')
    assert "old_completion_rejected_after_newer_event" in result.stdout


def test_evidence_failure_never_breaks_process_manage_result(tmp_path):
    stage = _stage_patched(tmp_path)
    result = _run_scenario(stage, tmp_path, r'''
process("proc_evidenceerr1")
def explode(*args, **kwargs):
    raise RuntimeError("evidence observer failure")
ve.record_terminal_result = explode
r = dispatch("proc_evidenceerr1")
assert r["status"] == "exited" and r["exit_code"] == 0
print("process_result_survived_evidence_error")
''')
    assert "process_result_survived_evidence_error" in result.stdout


def test_real_spawn_local_to_dispatch_satisfies_verification_stop(tmp_path):
    stage = _stage_patched(tmp_path)
    result = _run_scenario(stage, tmp_path, r'''
import os
from gateway.session_context import set_session_vars, clear_session_vars
from agent.verification_stop import build_verify_on_stop_nudge
tests = root / "tests"
tests.mkdir(exist_ok=True)
(tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
changed = str(tests / "test_ok.py")
ve.mark_workspace_edited(session_id="caller-session", cwd=root, paths=[changed])
tokens = set_session_vars(session_id="caller-session", session_key="route")
try:
    proc = pr.process_registry.spawn_local(
        "pytest tests -q", cwd=str(root), task_id="caller-task",
        owner_task_id="caller-task", session_key="route",
    )
    raw = model_tools.handle_function_call(
        "process_manage", {"action": "wait", "session_id": proc.id, "timeout": 30},
        task_id="caller-task", session_id="caller-session",
        skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )
    response = json.loads(raw)
    assert response["status"] == "exited", response
    assert response["exit_code"] == 0, response
    status = ve.verification_status(session_id="caller-session", cwd=root)
    assert status["status"] == "passed", status
    assert build_verify_on_stop_nudge(
        session_id="caller-session", changed_paths=[changed]) is None
    print("real_spawn_satisfies_stop")
finally:
    clear_session_vars(tokens)
''')
    assert "real_spawn_satisfies_stop" in result.stdout


def test_patch_is_idempotent_and_check_only(tmp_path):
    stage = _stage_original(tmp_path)
    evidence = stage / "agent" / "verification_evidence.py"
    registry = stage / "tools" / "process_registry.py"
    before = (_digest(evidence), _digest(registry))
    absent = _run("--check", "--agent-dir", stage)
    assert absent.returncode == 1
    assert (_digest(evidence), _digest(registry)) == before

    first = _run("--apply", "--agent-dir", stage)
    assert first.returncode == 0, first.stdout + first.stderr
    installed = (_digest(evidence), _digest(registry))
    assert installed != before
    second = _run("--apply", "--agent-dir", stage)
    checked = _run("--check", "--agent-dir", stage)
    assert second.returncode == checked.returncode == 0
    assert (_digest(evidence), _digest(registry)) == installed


def test_anchor_drift_is_fail_closed_and_writes_nothing(tmp_path):
    stage = _stage_original(tmp_path)
    evidence = stage / "agent" / "verification_evidence.py"
    registry = stage / "tools" / "process_registry.py"
    text = evidence.read_text(encoding="utf-8")
    evidence.write_text(text.replace("def record_terminal_result(", "def drifted_record_terminal_result(", 1), encoding="utf-8")
    before = (_digest(evidence), _digest(registry))
    result = _run("--apply", "--agent-dir", stage)
    assert result.returncode == 2, result.stdout + result.stderr
    assert (_digest(evidence), _digest(registry)) == before


def test_partial_patch_is_rejected_even_when_one_new_fragment_exists(tmp_path):
    stage = _stage_original(tmp_path)
    spec = importlib.util.spec_from_file_location("background_patch", PATCH)
    patch = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(patch)
    evidence = stage / "agent" / "verification_evidence.py"
    registry = stage / "tools" / "process_registry.py"
    text = evidence.read_text(encoding="utf-8")
    assert text.count(patch.EVIDENCE_OLD) == 1
    evidence.write_text(text.replace(patch.EVIDENCE_OLD, patch.EVIDENCE_NEW, 1), encoding="utf-8")
    before = (_digest(evidence), _digest(registry))
    checked = _run("--check", "--agent-dir", stage)
    applied = _run("--apply", "--agent-dir", stage)
    assert checked.returncode == applied.returncode == 2
    assert (_digest(evidence), _digest(registry)) == before

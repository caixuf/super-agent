"""Unit tests for agentcore.dev_runner and colleague_dev."""
from __future__ import annotations

import json
from pathlib import Path
import pytest
import sys

from agentcore.dev_runner import (
    _goal,
    _policy,
    _role,
    _workdir,
    run_dev_job,
    worktree_lock,
)
from agentcore.colleague_dev import get_dev_roles, get_role_contract_markdown


def test_colleague_dev_roles_and_contract():
    roles = get_dev_roles()
    assert "architect" in roles
    assert "cases" in roles
    assert "reviewer" in roles
    assert "test" in roles
    assert "triage" in roles

    cases_contract = get_role_contract_markdown("cases")
    assert "核心研发与用例开发专家" in cases_contract
    assert "逆向状态转移条件" in cases_contract
    assert "短时序高敏链路前禁止插入无谓硬延时" in cases_contract

    with pytest.raises(ValueError, match="unknown_role"):
        get_role_contract_markdown("non_existent_role")


def test_dev_runner_workdir_and_policy(tmp_path: Path):
    allowed = tmp_path / "worktrees"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"allowed_roots": [str(allowed)]}))
    policy = _policy(str(policy_file))

    # Within root
    worktree_dir = allowed / "backend"
    worktree_dir.mkdir()
    resolved = _workdir(str(worktree_dir), policy)
    assert resolved == worktree_dir.resolve()

    # Outside root -> raises ValueError
    with pytest.raises(ValueError, match="workdir_outside_roots"):
        _workdir(str(outside), policy)

    # Missing workdir -> raises ValueError
    with pytest.raises(ValueError, match="workdir_missing"):
        _workdir(str(allowed / "not_there"), policy)


def test_dev_runner_worktree_lock(tmp_path: Path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    lock_root = tmp_path / "locks"

    with worktree_lock(workdir, lock_root=lock_root):
        # A second lock on the same workdir within the same or another context must be rejected
        with pytest.raises(ValueError, match="worktree_busy"):
            with worktree_lock(workdir, lock_root=lock_root):
                pass


def test_run_dev_job_success(tmp_path: Path):
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    agent_dir = tmp_path / "agent_dir"
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    lock_root = tmp_path / "locks"

    policy = {"allowed_roots": [str(workdir)]}
    role_contract = "# Test Dev Role\nWrite code and tests."

    job = {
        "job": "dev-job-101",
        "attempt": "att-1",
        "payload": {
            "role": "cases",
            "goal": "编写雨刮新需求测试用例并在 worktree 下落盘",
        },
    }

    def mock_runner(prompt: str, kwargs: dict):
        assert "编写雨刮新需求" in prompt
        # Simulate creating an actual test case in workdir
        case_file = kwargs["workdir"] / "test_wiper.dsl"
        case_file.write_text("feature: wiper_test\nprocess: main\n")
        return {
            "ok": True,
            "final_text": "Successfully generated test_wiper.dsl and passed DSL validation.",
            "tool_calls": [{"tool": "write", "args": {"file": "test_wiper.dsl"}}],
        }

    res = run_dev_job(
        job=job,
        policy=policy,
        role=role_contract,
        workdir=workdir,
        agent_dir=agent_dir,
        output_dir=out_dir,
        lock_root=lock_root,
        runner_callable=mock_runner,
    )

    assert res["ok"] is True
    assert res["result"]["role"] == "cases"
    assert res["result"]["job"] == "dev-job-101"
    assert "wiper_test" in (workdir / "test_wiper.dsl").read_text()

    # Check generated report files
    report_json = json.loads((out_dir / "report.json").read_text())
    assert report_json["ok"] is True
    assert report_json["role"] == "cases"
    assert report_json["job"] == "dev-job-101"

    manifest_json = json.loads((out_dir / "manifest.json").read_text())
    assert manifest_json["role"] == "cases"

    assert (out_dir / "report.md").is_file()
    assert len(res["result"]["artifacts"]) == 3


def test_run_dev_job_failure(tmp_path: Path):
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    agent_dir = tmp_path / "agent_dir"
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    lock_root = tmp_path / "locks"

    policy = {"allowed_roots": [str(workdir)]}
    role_contract = "# Test Dev Role"

    job = {
        "job": "dev-job-102",
        "attempt": "att-1",
        "payload": {
            "role": "cases",
            "goal": "任务因为依赖缺失失败",
        },
    }

    def mock_failing_runner(prompt: str, kwargs: dict):
        return {
            "ok": False,
            "error_message": "conan build failed with exit code 1",
        }

    res = run_dev_job(
        job=job,
        policy=policy,
        role=role_contract,
        workdir=workdir,
        agent_dir=agent_dir,
        output_dir=out_dir,
        lock_root=lock_root,
        runner_callable=mock_failing_runner,
    )

    assert res["ok"] is False
    assert "conan build failed" in res["error"]
    assert (out_dir / "report.json").is_file()


def _real_orchestrator_available() -> bool:
    """The real orchestrator needs the business bridge checkout; skip clean rooms.

    This runs at collection time (``skipif`` evaluates immediately), and the bridge
    probe appends its checkout to the *global* ``sys.path``. Left behind, that path
    lets a business ``tools`` package shadow the repo's ``tools`` namespace for every
    test collected afterwards. Probe inside a snapshot and restore both path and
    modules so availability detection stays side-effect free.
    """
    import sys
    from pathlib import Path

    path_before = list(sys.path)
    modules_before = set(sys.modules)
    try:
        from agentcore.colleague_dev import ColleagueDevOrchestrator
        orchestrator = ColleagueDevOrchestrator()
        if not getattr(orchestrator, "workspace_root", None):
            return False
        orchestrator._ensure_bridge()
        return True
    except Exception:
        return False
    finally:
        added = [Path(p).resolve() for p in sys.path if p and p not in path_before]
        sys.path[:] = path_before
        for name, module in list(sys.modules.items()):
            if name in modules_before:
                continue
            origin = getattr(module, "__file__", None)
            if not origin:
                continue
            try:
                origin_path = Path(origin).resolve()
            except OSError:
                continue
            if any(origin_path.is_relative_to(root) for root in added):
                del sys.modules[name]


@pytest.mark.skipif(not _real_orchestrator_available(),
                    reason="real colleague bridge checkout unavailable")
def test_colleague_dev_orchestrator(tmp_path: Path):
    from agentcore.colleague_dev import ColleagueDevOrchestrator
    orchestrator = ColleagueDevOrchestrator()

    # 1. Test role execution
    res = orchestrator.execute_role_task(
        role="architect",
        task_payload={"story": "当雨刮开关置于高档位时，雨刮高速循环"},
        workdir=tmp_path,
    )
    assert res["ok"] is True
    assert res["role"] == "architect"
    assert (tmp_path / "contract_requirements.yaml").is_file()

    # 2. Test autonomous dev pipeline
    pipeline_res = orchestrator.execute_autonomous_case_dev(
        requirement_text="定速巡航激活与刹车退出用例",
        workdir=tmp_path,
        domain="domain_a",
    )
    assert pipeline_res["ok"] is True
    assert pipeline_res["case_name"] is not None
    assert (tmp_path / "manifest.json").is_file()


def test_colleague_dev_self_healing_retry_loop(tmp_path: Path):
    from agentcore.colleague_dev import ColleagueDevOrchestrator

    class MockSelfHealingBridge:
        def __init__(self):
            self.revisions_recorded = []

        def execute_autonomous_dev_pipeline(
            self, requirement_text, source_input="", workdir=None, max_revisions=3, target_domain="default"
        ):
            work_path = Path(workdir)
            work_path.mkdir(parents=True, exist_ok=True)
            for rev in range(1, max_revisions + 1):
                score = 50.0 if rev == 1 else 88.0
                approved = score >= 80.0
                self.revisions_recorded.append({"rev": rev, "score": score, "approved": approved})
                if approved:
                    break

            manifest = {
                "task_id": "task-heal-1",
                "verification": "dev_traceability_v1",
                "approved": True,
                "score": 88.0,
                "revisions": len(self.revisions_recorded),
                "artifacts": ["contract_requirements.yaml", "red_team_review.yaml"],
            }
            (work_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (work_path / "report.json").write_text(json.dumps({"ok": True, "score": 88.0}), encoding="utf-8")
            return {
                "ok": True,
                "task_id": "task-heal-1",
                "case_name": f"test_{target_domain}.dsl",
                "score": 88.0,
                "approved": True,
                "revisions": len(self.revisions_recorded),
            }

    mock_bridge = MockSelfHealingBridge()
    orchestrator = ColleagueDevOrchestrator(bridge=mock_bridge)
    out_dir = tmp_path / "healing_work"
    result = orchestrator.execute_autonomous_case_dev(
        requirement_text="雨刮高档位与关闭退出",
        workdir=out_dir,
        max_revisions=3,
        domain="domain_b",
    )

    assert result["ok"] is True
    assert result["approved"] is True
    assert result["revisions"] == 2
    assert result["score"] == 88.0
    assert (out_dir / "manifest.json").is_file()


@pytest.mark.skipif(not _real_orchestrator_available(),
                    reason="real colleague bridge checkout unavailable")
def test_colleague_dev_execute_in_worktree(tmp_path: Path):
    import subprocess
    from agentcore.colleague_dev import ColleagueDevOrchestrator

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestRunner"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)

    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    orchestrator = ColleagueDevOrchestrator()
    task_id = "test-wt-1"
    res = orchestrator.execute_in_worktree(
        repo_path=repo_dir,
        task_id=task_id,
        requirement_text="泊车激活与中断退出",
        domain="domain_c",
        max_revisions=3,
    )

    assert res["ok"] is True
    assert res["approved"] is True

    branch_check = subprocess.run(
        ["git", "log", "-1", f"dev-colleague/{task_id}", "--pretty=%B"],
        cwd=str(repo_dir), capture_output=True, text=True, check=True
    )
    assert f"autonomous colleague pipeline {task_id}" in branch_check.stdout


def _init_worktree_test_repo(tmp_path: Path) -> Path:
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestRunner"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def test_execute_in_worktree_refuses_existing_branch_without_reset(tmp_path: Path):
    import subprocess
    from agentcore.colleague_dev import ColleagueDevOrchestrator

    repo = _init_worktree_test_repo(tmp_path)
    branch = "dev-colleague/existing"
    subprocess.run(["git", "branch", branch], cwd=repo, check=True)
    before = subprocess.run(["git", "rev-parse", branch], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    with pytest.raises(ValueError, match="worktree_branch_exists"):
        ColleagueDevOrchestrator().execute_in_worktree(repo, "existing", "task", branch=branch)
    after = subprocess.run(["git", "rev-parse", branch], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert after == before
    assert not (tmp_path / "worktree-existing").exists()


def test_execute_in_worktree_preserves_failed_worktree(tmp_path: Path):
    import subprocess
    from agentcore.colleague_dev import ColleagueDevOrchestrator

    repo = _init_worktree_test_repo(tmp_path)
    orchestrator = ColleagueDevOrchestrator()

    def fail_after_writing(**kwargs):
        workdir = Path(kwargs["workdir"])
        (workdir / "recovery.txt").write_text("keep me", encoding="utf-8")
        raise RuntimeError("simulated execution failure")

    orchestrator.execute_autonomous_case_dev = fail_after_writing
    with pytest.raises(RuntimeError, match="preserved at"):
        orchestrator.execute_in_worktree(repo, "failure", "task")

    preserved = tmp_path / "worktree-failure"
    assert (preserved / "recovery.txt").read_text(encoding="utf-8") == "keep me"
    assert subprocess.run(["git", "rev-parse", "--verify", "dev-colleague/failure"],
                         cwd=repo, capture_output=True).returncode == 0


def test_execute_in_worktree_preserves_existing_path(tmp_path: Path):
    from agentcore.colleague_dev import ColleagueDevOrchestrator

    repo = _init_worktree_test_repo(tmp_path)
    existing = tmp_path / "worktree-collision"
    existing.mkdir()
    marker = existing / "user.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="worktree_path_exists"):
        ColleagueDevOrchestrator().execute_in_worktree(repo, "collision", "task")
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("task_id", ["../escape", "nested/path", "x/../../escape", "/absolute", "a\\b", "."])
def test_execute_in_worktree_rejects_unsafe_task_id(tmp_path: Path, task_id: str):
    from agentcore.colleague_dev import ColleagueDevOrchestrator

    repo = _init_worktree_test_repo(tmp_path)
    with pytest.raises(ValueError, match="invalid_task_id"):
        ColleagueDevOrchestrator().execute_in_worktree(repo, task_id, "task")
    assert not (tmp_path.parent / "worktree-escape").exists()


def test_run_dev_job_with_verification_checks(tmp_path: Path):
    workdir = tmp_path / "worktree_vc"
    workdir.mkdir()
    agent_dir = tmp_path / "agent_dir_vc"
    out_dir = tmp_path / "output_vc"
    out_dir.mkdir()

    policy = {"allowed_roots": [str(workdir)]}
    role_contract = "# Dev Role"

    job_pass = {
        "job": "dev-job-vc-1",
        "attempt": "att-1",
        "payload": {
            "role": "cases",
            "goal": "Write code and verify",
            "verification_checks": [
                {"check_id": "test_pass", "command": "python3 -c 'exit(0)'"},
            ],
        },
    }

    res_pass = run_dev_job(
        job=job_pass,
        policy={**policy, "verification_checks": job_pass["payload"]["verification_checks"]},
        role=role_contract,
        workdir=workdir,
        agent_dir=agent_dir,
        output_dir=out_dir,
        runner_callable=lambda prompt, kwargs: {"ok": True, "final_text": "done"},
    )
    assert res_pass["ok"] is True
    assert "verification_receipt" in res_pass
    assert res_pass["verification_receipt"]["all_passed"] is True

    # When verification check fails, whole job fails and failure_decision is attached
    job_fail = {
        "job": "dev-job-vc-2",
        "attempt": "att-1",
        "payload": {
            "role": "cases",
            "goal": "Write code and fail verification",
            "verification_checks": [
                {"check_id": "test_fail", "command": "python3 -c 'exit(1)'", "critical": True},
            ],
        },
    }

    res_fail = run_dev_job(
        job=job_fail,
        policy={**policy, "verification_checks": job_fail["payload"]["verification_checks"]},
        role=role_contract,
        workdir=workdir,
        agent_dir=agent_dir,
        output_dir=out_dir,
        runner_callable=lambda prompt, kwargs: {"ok": True, "final_text": "done"},
    )
    assert res_fail["ok"] is False
    assert "verification_checks_failed" in res_fail["error"]
    assert "failure_decision" in res_fail
    assert res_fail["failure_decision"]["action"] in {"stop", "escalate", "replan"}


@pytest.mark.parametrize("changes", [
    {"command": ["python3", "-c", "exit(0)"]},
    {"env": {"PYTHONPATH": "/untrusted"}},
    {"expected_exit_code": 1},
    {"critical": False},
    {"timeout_s": 1},
    {"scope": "environment_scoped"},
    {"safety_tier": "mutating"},
])
def test_dev_job_cannot_change_admin_verification_contract(tmp_path, changes):
    trusted = {"check_id": "required", "command": [sys.executable, "-c", "exit(0)"]}
    calls = []
    with pytest.raises(PermissionError, match="verification_checks_not_authorized"):
        run_dev_job(
            {"payload": {"goal": "Do work", "verification_checks": [{**trusted, **changes}]}},
            {"allowed_roots": [str(tmp_path)], "verification_checks": [trusted]},
            "role", tmp_path, tmp_path / "agent", output_dir=tmp_path,
            runner_callable=lambda *args: calls.append(args) or {"ok": True},
        )
    assert not calls and not (tmp_path / "agent").exists()


def test_dev_job_unconfigured_verification_cannot_execute_on_host(tmp_path):
    marker = tmp_path / "host-command-ran"
    calls = []
    command = [sys.executable, "-c", "from pathlib import Path; Path('host-command-ran').touch()"]
    with pytest.raises(PermissionError, match="verification_checks_not_authorized"):
        run_dev_job(
            {"payload": {"goal": "Do work", "verification_checks": [
                {"check_id": "untrusted", "command": command}]}},
            {"allowed_roots": [str(tmp_path)]}, "role", tmp_path, tmp_path / "agent",
            output_dir=tmp_path, runner_callable=lambda *args: calls.append(args) or {"ok": True},
        )
    assert not marker.exists() and not calls


@pytest.mark.parametrize("declaration", ["absent", "empty"])
def test_admin_verification_is_mandatory_and_retains_worktree_lock(tmp_path, declaration):
    import agentcore.verification_gate as gate
    from unittest.mock import patch

    check = {"check_id": "required", "command": [sys.executable, "-c", "exit(1)"]}
    payload = {"goal": "Do work"}
    if declaration == "empty":
        payload["verification_checks"] = []
    lock_root = tmp_path / "locks"
    observed = []
    actual = gate.evaluate_verification_checks

    def evaluate(checks, workdir, **kwargs):
        with pytest.raises(ValueError, match="worktree_busy"):
            with worktree_lock(tmp_path, lock_root):
                pytest.fail("verification must retain the execution worktree lock")
        observed.append(True)
        return actual(checks, workdir, **kwargs)

    with patch.object(gate, "evaluate_verification_checks", evaluate):
        result = run_dev_job(
            {"payload": payload}, {"allowed_roots": [str(tmp_path)], "verification_checks": [check]},
            "role", tmp_path, tmp_path / "agent", output_dir=tmp_path, lock_root=lock_root,
            runner_callable=lambda *args: {"ok": True},
        )
    assert observed and result["ok"] is False
    assert result["verification_receipt"]["check_count"] == 1


def test_dev_job_snapshots_admin_verification_before_model_runs(tmp_path):
    check = {"check_id": "required", "command": [sys.executable, "-c", "exit(1)"]}
    policy = {"allowed_roots": [str(tmp_path)], "verification_checks": [check]}
    job = {"payload": {"goal": "Do work", "verification_checks": [dict(check)]}}

    def model(*args):
        policy["verification_checks"].clear()
        job["payload"]["verification_checks"] = []
        return {"ok": True}

    result = run_dev_job(job, policy, "role", tmp_path, tmp_path / "agent",
                         output_dir=tmp_path, runner_callable=model)
    assert result["ok"] is False
    assert result["verification_receipt"]["check_count"] == 1


@pytest.mark.parametrize("configured", [
    None, {}, "not-a-list", [None],
    [{"check_id": "bad", "command": ""}],
    [{"check_id": "bad", "command": "true", "unknown": True}],
    [{"check_id": "same", "command": "true"}] * 2,
    [{"check_id": "check-" + str(i), "command": "true"} for i in range(9)],
])
def test_invalid_admin_verification_fails_before_model_execution(tmp_path, configured):
    calls = []
    with pytest.raises(ValueError, match="invalid_verification_policy"):
        run_dev_job(
            {"payload": {"goal": "Do work"}},
            {"allowed_roots": [str(tmp_path)], "verification_checks": configured},
            "role", tmp_path, tmp_path / "agent", output_dir=tmp_path,
            runner_callable=lambda *args: calls.append(args) or {"ok": True},
        )
    assert not calls


@pytest.mark.parametrize("requested", [None, {}, "not-a-list", [None],
    [{"check_id": "check", "command": "true", "unknown": True}]])
def test_invalid_task_verification_fails_before_model_execution(tmp_path, requested):
    calls = []
    with pytest.raises(PermissionError, match="verification_checks_not_authorized"):
        run_dev_job(
            {"payload": {"goal": "Do work", "verification_checks": requested}},
            {"allowed_roots": [str(tmp_path)]}, "role", tmp_path, tmp_path / "agent",
            output_dir=tmp_path, runner_callable=lambda *args: calls.append(args) or {"ok": True},
        )
    assert not calls


def test_dev_runner_cli_rejects_task_command_before_loading_business_bridge(tmp_path):
    import subprocess

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"allowed_roots": [str(tmp_path)]}))
    role = tmp_path / "role.md"
    role.write_text("Development role")
    job = {"payload": {"goal": "Do work", "verification_checks": [
        {"check_id": "host", "command": [sys.executable, "-c", "open('ran','w').close()"]}]}}
    result = subprocess.run(
        [sys.executable, "-m", "agentcore.dev_runner", "--policy", str(policy),
         "--role-contract", str(role), "--workdir", str(tmp_path),
         "--agent-dir", str(tmp_path / "agent"), "--output-dir", str(tmp_path)],
        input=json.dumps(job), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt == {"ok": False, "error": "PermissionError: verification_checks_not_authorized"}
    assert not (tmp_path / "ran").exists() and not (tmp_path / "agent").exists()


def test_sandbox_verification_never_falls_back_to_host(tmp_path):
    calls = []
    policy = {"allowed_roots": [str(tmp_path)], "verification_checks": [
        {"check_id": "required", "command": [sys.executable, "-c", "exit(0)"]}]}
    with pytest.raises(RuntimeError, match="sandbox_verification_executor_required"):
        run_dev_job(
            {"payload": {"goal": "Do work"}}, policy, "role", tmp_path, tmp_path / "agent",
            sandbox=True, runner_callable=lambda *args: calls.append(args) or {"ok": True},
        )
    assert not calls and not (tmp_path / "agent").exists()


def test_host_verification_executor_is_used_without_running_task_command(tmp_path):
    observed = []
    policy = {"allowed_roots": [str(tmp_path)], "verification_checks": [
        {"check_id": "required", "command": ["not-an-installed-program", "test"],
         "env": {"TEST_MODE": "isolated"}}]}

    def execute(command, timeout, workdir, *, env):
        observed.append((command, workdir, env))
        return {"exit_code": 0}

    result = run_dev_job(
        {"payload": {"goal": "Do work"}}, policy, "role", tmp_path, tmp_path / "agent",
        sandbox=True, output_dir=tmp_path, runner_callable=lambda *args: {"ok": True},
        verification_executor=execute,
    )
    assert result["ok"] is True
    assert observed == [(("not-an-installed-program", "test"), tmp_path, {"TEST_MODE": "isolated"})]


def test_real_host_injected_sandbox_verifies_artifacts_without_host_access(tmp_path, monkeypatch):
    import os
    import shutil
    import socket
    import subprocess

    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap unavailable; not evidence that sandbox execution works")
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    secret = tmp_path / "host-only"
    secret.write_text("must not be visible")
    monkeypatch.setenv("SUPERAGENT_TEST_HOST_SECRET", "must-not-be-inherited")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    command = ["/usr/bin/python3", "-m", "unittest", "-q"]
    policy = {"allowed_roots": [str(workdir)], "verification_checks": [
        {"check_id": "isolated-tests", "command": command}]}
    outcomes = []

    def model(prompt, options):
        (workdir / "answer.py").write_text("def answer():\n    return 42\n")
        (workdir / "test_answer.py").write_text(
            "import os, pathlib, socket, unittest\n"
            "from answer import answer\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_answer(self): self.assertEqual(answer(), 42)\n"
            "    def test_private_host(self):\n"
            f"        self.assertFalse(pathlib.Path({str(secret)!r}).exists())\n"
            "        self.assertNotIn('SUPERAGENT_TEST_HOST_SECRET', os.environ)\n"
            "        with self.assertRaises(OSError):\n"
            f"            socket.create_connection(('127.0.0.1', {port}), timeout=0.5)\n"
        )
        return {"ok": True, "final_text": "unverified model claim"}

    def execute(argv, timeout_s, directory, *, env):
        assert directory == workdir and not env
        args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
        for root in ("/usr", "/lib", "/lib64", "/bin"):
            if Path(root).exists():
                args += ["--ro-bind", root, root]
        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                 "--bind", str(directory), "/work", "--chdir", "/work",
                 "--setenv", "HOME", "/tmp", "--setenv", "PATH", "/usr/bin:/bin", "--", *argv]
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s,
                                env={"PATH": "/usr/bin:/bin"})
        outcomes.append(result)
        return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    try:
        result = run_dev_job(
            {"payload": {"goal": "Write answer and tests"}}, policy, "role", workdir,
            tmp_path / "agent", output_dir=tmp_path, sandbox=True,
            runner_callable=model, verification_executor=execute,
        )
    finally:
        listener.close()
    assert len(outcomes) == 1
    assert outcomes[0].returncode == 0, outcomes[0].stderr
    assert "Ran 2 tests" in outcomes[0].stderr
    assert result["ok"] is True and result["verification_receipt"]["all_passed"] is True
    assert secret.read_text() == "must not be visible"


@pytest.fixture
def dev_cli(tmp_path):
    """Real CLI process with an administrator-owned deterministic test adapter."""
    import os
    import subprocess

    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "allowed_roots": [str(work)],
        "verification_checks": [{"check_id": "unit",
                                 "command": [sys.executable, "-m", "unittest", "-q"]}],
    }))
    role = tmp_path / "role.md"
    role.write_text("Write code and tests.")
    (tmp_path / "trusted_dev_adapter.py").write_text(
        "print('adapter import diagnostics')\n"
        "from agentcore.verification_gate import default_process_executor\n"
        "def run(prompt, options):\n"
        "    print('adapter execution diagnostics')\n"
        "    assert options['sandbox'] is True\n"
        "    work = options['workdir']\n"
        "    (work / 'answer.py').write_text('def answer():\\n    return 42\\n')\n"
        "    (work / 'test_answer.py').write_text("
        "'import unittest\\nfrom answer import answer\\n"
        "class Tests(unittest.TestCase):\\n"
        "    def test_answer(self): self.assertEqual(answer(), 42)\\n')\n"
        "    return {'ok': True, 'final_text': 'Generated answer and test'}\n"
        "def verify(argv, timeout_s, workdir, *, env):\n"
        "    print('verification diagnostics')\n"
        "    return default_process_executor(argv, timeout_s, workdir, env=env)\n"
        "def fail(*args, **kwargs):\n"
        "    from pathlib import Path\n"
        f"    with Path({str(tmp_path / 'failure-calls')!r}).open('a') as log: log.write('called\\n')\n"
        "    raise RuntimeError('private-adapter-detail')\n"
        "async def async_adapter(*args, **kwargs): return {'ok': True}\n"
        "def wrong_signature(): return {'ok': True}\n"
        "def invalid_result(*args, **kwargs): return None\n"
        "def truthy_result(*args, **kwargs): return {'ok': 'false', 'exit_code': False}\n"
        "class AsyncCallable:\n"
        "    async def __call__(self, *args, **kwargs): return {'ok': True}\n"
        "async_callable = AsyncCallable()\n"
        "def wait_for_stop(workdir):\n"
        "    import subprocess, time\n"
        "    child = subprocess.Popen(['/bin/sh', '-c', "
        "'exec setsid /bin/sleep 60'], env={}, close_fds=True)\n"
        "    (workdir / 'active-child.pid').write_text(str(child.pid))\n"
        "    time.sleep(60)\n"
        "def waiting_runner(prompt, options):\n"
        "    wait_for_stop(options['workdir'])\n"
        "    return {'ok': True}\n"
        "def waiting_verifier(argv, timeout_s, workdir, *, env):\n"
        "    wait_for_stop(workdir)\n"
        "    return {'exit_code': 0}\n"
        "def bad_answer(prompt, options):\n"
        "    result = run(prompt, options)\n"
        "    (options['workdir'] / 'answer.py').write_text('def answer():\\n    return 0\\n')\n"
        "    return result\n"
        "not_callable = 42\n"
    )
    (tmp_path / "broken_dev_adapter.py").write_text(
        "raise RuntimeError('private-import-detail')\n")
    (tmp_path / "payload_adapter.py").write_text(
        f"from pathlib import Path\nPath({str(tmp_path / 'payload-imported')!r}).touch()\n")
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home),
               PYTHONPATH=os.pathsep.join([str(tmp_path), str(repo)]),
               PYTHONDONTWRITEBYTECODE="1")
    for key in tuple(env):
        if key.startswith(("FEISHU_", "LONGTASK_")):
            env.pop(key, None)
    command = [
        sys.executable, "-m", "agentcore.dev_runner", "--policy", str(policy),
        "--role-contract", str(role), "--workdir", str(work),
        "--agent-dir", str(tmp_path / "agent"), "--output-dir", str(tmp_path / "out"),
        "--sandbox",
    ]
    job = {"job": "cli-dev", "attempt": "attempt-cli", "payload": {
        "goal": "Generate answer and tests",
        "runner_adapter": "payload_adapter:run",
        "verification_adapter": "payload_adapter:verify",
    }}

    def run(*flags, payload=None):
        return subprocess.run(command + list(flags), cwd=home, env=env,
                              input=json.dumps(job if payload is None else payload),
                              capture_output=True, text=True, timeout=15)

    return run, command, env, job, work


def test_cli_explicit_adapters_generate_verify_and_report_without_business_bridge(dev_cli):
    run, _, _, _, work = dev_cli
    proc = run("--runner-adapter", "trusted_dev_adapter:run",
               "--verification-adapter", "trusted_dev_adapter:verify")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["verification_receipt"]["all_passed"] is True
    assert receipt["verification_receipt"]["results"][0]["exit_code"] == 0
    assert (work / "answer.py").read_text() == "def answer():\n    return 42\n"
    assert not (work.parent / "payload-imported").exists()
    assert "adapter import diagnostics" in proc.stderr
    assert "verification diagnostics" in proc.stderr
    for artifact in receipt["result"]["artifacts"]:
        assert Path(artifact).is_file()
    assert json.loads((work.parent / "out/report.json").read_text())["ok"] is True


@pytest.mark.parametrize("reference", [
    "missing_adapter:run", "broken_dev_adapter:run", "trusted_dev_adapter:missing",
    "trusted_dev_adapter:not_callable", "trusted_dev_adapter:run:extra", "",
    "trusted_dev_adapter:wrong_signature", "trusted_dev_adapter:async_adapter",
    "trusted_dev_adapter:async_callable",
])
@pytest.mark.parametrize("flag", ["--runner-adapter", "--verification-adapter"])
def test_cli_explicit_adapter_load_failure_never_falls_back(dev_cli, flag, reference):
    run, _, _, _, work = dev_cli
    flags = {"--runner-adapter": "trusted_dev_adapter:run",
             "--verification-adapter": "trusted_dev_adapter:verify"}
    flags[flag] = reference
    proc = run(*(item for pair in flags.items() for item in pair))
    assert proc.returncode == 1
    assert json.loads(proc.stdout) == {
        "ok": False, "error": f"RuntimeError: {flag[2:].replace('-', '_')}_unavailable"}
    assert not (work / "answer.py").exists()
    assert not (work.parent / "agent").exists()
    assert "private-import-detail" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("flag", ["--runner-adapter", "--verification-adapter"])
def test_cli_adapter_execution_error_is_redacted_and_not_retried(dev_cli, flag):
    run, _, _, _, work = dev_cli
    flags = {"--runner-adapter": "trusted_dev_adapter:run",
             "--verification-adapter": "trusted_dev_adapter:verify"}
    flags[flag] = "trusted_dev_adapter:fail"
    proc = run(*(item for pair in flags.items() for item in pair))
    assert proc.returncode == 1
    assert json.loads(proc.stdout) == {
        "ok": False, "error": f"RuntimeError: {flag[2:].replace('-', '_')}_failed"}
    assert "private-adapter-detail" not in proc.stdout + proc.stderr
    assert not (work.parent / "out/report.json").exists()
    assert (work.parent / "failure-calls").read_text().splitlines() == ["called"]


@pytest.mark.parametrize("option", ["--runner-adaptr", "--runner-adap"])
def test_cli_unknown_safety_option_is_not_silently_ignored(dev_cli, option):
    run, _, _, _, work = dev_cli
    proc = run(option, "trusted_dev_adapter:run")
    assert proc.returncode == 2
    assert "unrecognized arguments" in proc.stderr
    assert not (work.parent / "agent").exists()


@pytest.mark.parametrize("symbol", ["invalid_result", "truthy_result"])
@pytest.mark.parametrize("flag", ["--runner-adapter", "--verification-adapter"])
def test_cli_malformed_adapter_result_cannot_be_success(dev_cli, symbol, flag):
    run, _, _, _, work = dev_cli
    flags = {"--runner-adapter": "trusted_dev_adapter:run",
             "--verification-adapter": "trusted_dev_adapter:verify"}
    flags[flag] = f"trusted_dev_adapter:{symbol}"
    proc = run(*(item for pair in flags.items() for item in pair))
    assert proc.returncode == 1
    assert json.loads(proc.stdout) == {
        "ok": False, "error": f"RuntimeError: {flag[2:].replace('-', '_')}_failed"}
    assert not (work.parent / "out/report.json").exists()


def test_cli_missing_sandbox_verifier_rejects_before_runner(dev_cli):
    run, _, _, _, work = dev_cli
    proc = run("--runner-adapter", "trusted_dev_adapter:run")
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["error"] == "RuntimeError: sandbox_verification_executor_required"
    assert not (work / "answer.py").exists() and not (work.parent / "agent").exists()


def test_cli_real_isolated_adapters_generate_and_verify_without_host_access(dev_cli):
    import shutil
    import socket

    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap unavailable; no evidence of CLI sandbox containment")
    run, _, env, _, work = dev_cli
    secret = work.parent / "host-only-secret"
    secret.write_text("host-only")
    env["SUPERAGENT_TEST_HOST_SECRET"] = "not-for-child"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    source = (
        "import os, pathlib, socket, unittest\n"
        "from answer import answer\n"
        "class Tests(unittest.TestCase):\n"
        "    def test_answer(self): self.assertEqual(answer(), 42)\n"
        "    def test_boundary(self):\n"
        f"        self.assertFalse(pathlib.Path({str(secret)!r}).exists())\n"
        "        self.assertNotIn('SUPERAGENT_TEST_HOST_SECRET', os.environ)\n"
        "        with self.assertRaises(OSError):\n"
        f"            socket.create_connection(('127.0.0.1', {port}), timeout=.5)\n"
    )
    generate = (
        "import os, pathlib, socket\n"
        f"assert not pathlib.Path({str(secret)!r}).exists()\n"
        "assert 'SUPERAGENT_TEST_HOST_SECRET' not in os.environ\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=.5)\n"
        "except OSError: pass\n"
        "else: raise AssertionError('host network reachable')\n"
        "pathlib.Path('answer.py').write_text('def answer():\\n    return 42\\n')\n"
        f"pathlib.Path('test_answer.py').write_text({source!r})\n"
    )
    (work.parent / "isolated_dev_adapter.py").write_text(
        "from pathlib import Path\nimport subprocess\n"
        "def execute(argv, timeout_s, workdir, *, env):\n"
        f"    args = [{bwrap!r}, '--unshare-all', '--die-with-parent', '--new-session', '--clearenv']\n"
        "    for root in ('/usr', '/lib', '/lib64', '/bin'):\n"
        "        if Path(root).exists(): args += ['--ro-bind', root, root]\n"
        "    args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',\n"
        "             '--bind', str(workdir), '/work', '--chdir', '/work',\n"
        "             '--setenv', 'HOME', '/tmp', '--setenv', 'PATH', '/usr/bin:/bin', '--', *argv]\n"
        "    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s,\n"
        "                          env={'PATH': '/usr/bin:/bin'})\n"
        "    return {'exit_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}\n"
        "def run(prompt, options):\n"
        "    assert options['sandbox'] is True\n"
        f"    result = execute(['/usr/bin/python3', '-c', {generate!r}],\n"
        "                     options['timeout'], options['workdir'], env={})\n"
        "    return {'ok': result['exit_code'] == 0, 'error': result['stderr']}\n"
        "def verify(argv, timeout_s, workdir, *, env):\n"
        "    assert tuple(argv[1:]) == ('-m', 'unittest', '-q') and not env\n"
        "    result = execute(['/usr/bin/python3', *argv[1:]], timeout_s, workdir, env=env)\n"
        "    assert 'Ran 2 tests' in result['stderr'], result['stderr']\n"
        "    return result\n"
    )
    try:
        proc = run("--runner-adapter", "isolated_dev_adapter:run",
                   "--verification-adapter", "isolated_dev_adapter:verify")
    finally:
        listener.close()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True and receipt["verification_receipt"]["all_passed"] is True
    assert secret.read_text() == "host-only" and (work / "answer.py").is_file()


@pytest.mark.parametrize("runner,expected", [("run", "completed"), ("bad_answer", "failed")])
def test_pool_dev_cli_reports_actual_generated_code_verdict(dev_cli, monkeypatch, runner, expected):
    from agentcore.pool import Pool

    _, command, env, job, work = dev_cli
    for key in ("HOME", "HERMES_HOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"):
        monkeypatch.setenv(key, env[key])
    pool = Pool(work.parent / "queue")
    task = pool.submit("dev-cli-task", job["payload"])
    assert pool.run_once(command + ["--runner-adapter", f"trusted_dev_adapter:{runner}",
                                   "--verification-adapter", "trusted_dev_adapter:verify"],
                         timeout=10, lease=.2)
    status = pool.status(task)
    assert status["status"] == expected, status
    assert status["execution_state"] == "supervised"
    assert len(pool.receipts()) == 1
    result = json.loads(status["result"])
    assert result["attempt_quiescent"] is True
    report = json.loads((work.parent / "out/report.json").read_text())
    assert report["job"] == task and report["attempt"] == status["attempt"]
    assert report["ok"] is (expected == "completed")
    check = report["verification_receipt"]["results"][0]
    assert check["exit_code"] == (0 if expected == "completed" else 1)
    assert check["passed"] is (expected == "completed")
    if expected == "completed":
        assert result["verification_receipt"] == report["verification_receipt"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper lifecycle contract")
@pytest.mark.parametrize("phase", ["generation", "verification"])
@pytest.mark.parametrize("outcome", ["cancel", "timeout"])
def test_pool_dev_cli_stop_reaps_actual_adapter_descendants(dev_cli, monkeypatch, phase, outcome):
    import os
    import time
    from concurrent.futures import ThreadPoolExecutor
    from agentcore.pool import Pool

    _, command, env, job, work = dev_cli
    for key in ("HOME", "HERMES_HOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"):
        monkeypatch.setenv(key, env[key])
    pool = Pool(work.parent / "queue")
    task = pool.submit("dev-cli-stop", job["payload"])
    command += ["--runner-adapter", "trusted_dev_adapter:" + (
        "waiting_runner" if phase == "generation" else "run"),
        "--verification-adapter", "trusted_dev_adapter:" + (
        "waiting_verifier" if phase == "verification" else "verify")]
    child_file = work / "active-child.pid"
    child_pid = child_start = None
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(pool.run_once, command, 2 if outcome == "timeout" else 15, .2)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child_file.exists() and child_file.read_text().strip():
                    child_pid = int(child_file.read_text())
                    child_start = pool._process_start(child_pid)
                    try:
                        if Path(f"/proc/{child_pid}/cmdline").read_bytes().startswith(b"/bin/sleep\0"):
                            break
                    except FileNotFoundError:
                        pass
                time.sleep(.01)
            else:
                pytest.fail("test adapter descendant did not become ready")
            assert child_start
            assert pool.status(task)["status"] == "running"
            if outcome == "cancel":
                pool.cancel(task)
            assert future.result(timeout=10)
            status = pool.status(task)
            assert status["status"] == ("cancelled" if outcome == "cancel" else "failed"), status
            assert pool._process_start(child_pid) != child_start
            assert len(pool.receipts()) == 1
            if outcome == "timeout":
                assert json.loads(status["result"])["attempt_quiescent"] is True
            else:
                assert json.loads(status["result"]) == {"reason": "cancelled"}
            assert not (work.parent / "out/report.json").exists()
            evidence = pool.root / "attempts" / status["attempt"] / "stop.json"
            proof = json.loads(evidence.read_text())
            assert proof["job"] == task and proof["attempt"] == status["attempt"]
            assert proof["quiescent"] is True and proof["proof_scope"] == "subreaper_tree"
            with pool.db() as db:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (task,)).fetchone()
                assert pool._attempt_stopped(row)
        finally:
            pool.cancel(task)
            if child_pid and child_start and pool._process_start(child_pid) == child_start:
                os.kill(child_pid, 9)
            future.result(timeout=12)


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
    assert "用例开发大师" in cases_contract
    assert "逆向状态机条件宏" in cases_contract
    assert "300ms 以内短脉冲前禁止插入 sleep" in cases_contract

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
    """The real orchestrator needs the business bridge checkout; skip clean rooms."""
    try:
        from agentcore.colleague_dev import ColleagueDevOrchestrator
        orchestrator = ColleagueDevOrchestrator()
        if not getattr(orchestrator, "workspace_root", None):
            return False
        orchestrator._ensure_bridge()
        return True
    except Exception:
        return False


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

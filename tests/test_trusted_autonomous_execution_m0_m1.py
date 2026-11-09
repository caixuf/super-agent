"""Tests verifying M0 and M1 (First Batch Delivery) of TRUSTED_AUTONOMOUS_EXECUTION_PLAN.

Covers:
1. M0: Prohibiting synthetic success on real paths, separating execution/verification/artifacts,
   enforcing required verification checks when mandated by policy, and recording skills as candidates.
2. M1 & Section 4: Attempt-bound execution backend with Linux subreaper lifecycle,
   controlled file modification in isolated test repository with test profile,
   stopping of attempt and detached descendants, and tamper rejection.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from agentcore import colleague_containment as containment
from agentcore.colleague_dev import ColleagueDevOrchestrator
from agentcore.dev_runner import run_dev_job
from agentcore.pool import Pool


# ==============================================================================
# M0: Baseline & Eliminating False Success
# ==============================================================================

def test_m0_bridge_failure_does_not_silently_generate_synthetic_success(tmp_path: Path, monkeypatch):
    """When the real bridge is unavailable, execution must fail closed, not generate synthetic pass."""
    orchestrator = ColleagueDevOrchestrator()
    def fail_bridge():
        raise RuntimeError("simulated_bridge_missing")
    monkeypatch.setattr(orchestrator, "_ensure_bridge", fail_bridge)

    res = orchestrator.execute_autonomous_case_dev(
        requirement_text="ACC Cruise activation",
        workdir=tmp_path / "work",
        demo_mode=False,
    )
    assert res["ok"] is False
    assert res["approved"] is False
    assert res["is_approved"] is False
    assert res["score"] == 0.0
    assert res["synthetic"] is False
    assert "colleague_bridge_unavailable" in res["error"]
    assert "synthetic fallback is disabled" in res["error"]


def test_m0_demo_mode_explicitly_labeled_as_synthetic(tmp_path: Path, monkeypatch):
    """When demo_mode is explicitly enabled, outputs must be prominently marked as synthetic."""
    orchestrator = ColleagueDevOrchestrator()
    def fail_bridge():
        raise RuntimeError("simulated_bridge_missing")
    monkeypatch.setattr(orchestrator, "_ensure_bridge", fail_bridge)

    workdir = tmp_path / "demo_work"
    res = orchestrator.execute_autonomous_case_dev(
        requirement_text="ACC Cruise demo",
        workdir=workdir,
        demo_mode=True,
    )
    assert res["ok"] is True
    assert res["synthetic"] is True
    assert "Synthetic demo execution only" in res["claim_boundary"]

    manifest = json.loads((workdir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("synthetic") is True
    assert "Synthetic demo execution only" in manifest.get("claim_boundary", "")

    report = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
    assert report.get("synthetic") is True
    assert "Synthetic demo execution only" in report.get("claim_boundary", "")


def test_m0_execute_in_worktree_does_not_commit_on_failure(tmp_path: Path, monkeypatch):
    """Failed autonomous dev must not create a commit in the git worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)

    orchestrator = ColleagueDevOrchestrator()
    def fail_bridge():
        raise RuntimeError("simulated_bridge_missing")
    monkeypatch.setattr(orchestrator, "_ensure_bridge", fail_bridge)

    res = orchestrator.execute_in_worktree(
        repo_path=repo,
        task_id="task-fail-1",
        requirement_text="Fix brake failure",
        demo_mode=False,
    )
    assert res["ok"] is False
    assert res.get("worktree_preserved") is True

    # Branch must not have a synthetic commit
    log = subprocess.run(["git", "log", "-1", "dev-colleague/task-fail-1", "--pretty=%s"],
                         cwd=repo, capture_output=True, text=True)
    assert log.returncode != 0 or "autonomous colleague pipeline" not in log.stdout


def test_m0_dev_runner_policy_require_verification_fails_closed_when_empty(tmp_path: Path):
    """When policy mandates verification, tasks missing verification checks must be rejected."""
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    policy = {
        "allowed_roots": [str(workdir)],
        "require_verification": True,
    }
    role_contract = "# Dev Role"
    job = {
        "job": "job-vc-req",
        "attempt": "att-1",
        "payload": {
            "role": "cases",
            "goal": "Write code without checks",
        },
    }
    with pytest.raises(ValueError, match="required_verification_checks_missing"):
        run_dev_job(
            job=job,
            policy=policy,
            role=role_contract,
            workdir=workdir,
            agent_dir=tmp_path / "agent",
            output_dir=tmp_path / "out",
            runner_callable=lambda p, k: {"ok": True},
        )


def test_m0_dev_runner_payload_cannot_bypass_policy_require_verification(tmp_path: Path):
    """Payload attempting to unset require_verification cannot bypass policy mandate."""
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    policy = {
        "allowed_roots": [str(workdir)],
        "require_verification": True,
    }
    job = {
        "job": "job-vc-bypass",
        "attempt": "att-1",
        "payload": {
            "role": "cases",
            "goal": "Attempting bypass",
            "require_verification": False,
        },
    }
    with pytest.raises(ValueError, match="required_verification_checks_missing"):
        run_dev_job(
            job=job,
            policy=policy,
            role="# Dev",
            workdir=workdir,
            agent_dir=tmp_path / "agent",
            output_dir=tmp_path / "out",
            runner_callable=lambda p, k: {"ok": True},
        )


def test_m0_dev_runner_report_separates_execution_verification_and_artifacts(tmp_path: Path):
    """Receipt and report must separate execution_completed, verification_passed, and artifacts_valid."""
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    policy = {
        "allowed_roots": [str(workdir)],
        "verification_checks": [
            {"check_id": "test_pass", "command": "python3 -c 'exit(0)'"},
        ],
    }
    job = {
        "job": "job-m0-report",
        "attempt": "att-1",
        "payload": {
            "role": "cases",
            "goal": "Verify report fields",
            "verification_checks": [
                {"check_id": "test_pass", "command": "python3 -c 'exit(0)'"},
            ],
        },
    }
    res = run_dev_job(
        job=job,
        policy=policy,
        role="# Role",
        workdir=workdir,
        agent_dir=tmp_path / "agent",
        output_dir=out_dir,
        runner_callable=lambda p, k: {"ok": True, "final_text": "Completed task"},
    )
    assert res["ok"] is True
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["execution_completed"] is True
    assert report["verification_passed"] is True
    assert report["artifacts_valid"] is True


def test_m0_dev_runner_skills_recorded_as_unapproved_candidate(tmp_path: Path, monkeypatch):
    """Skills recorded by dev_runner must remain unapproved candidates, never auto-promoted."""
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    policy = {"allowed_roots": [str(workdir)]}
    job = {
        "job": "job-candidate-skill",
        "attempt": "att-1",
        "payload": {"role": "cases", "goal": "Solve problem cleanly"},
    }

    recorded_cases = []
    class FakeForge:
        def record_case(self, case):
            recorded_cases.append(case)

    monkeypatch.setattr("agentcore.skill_forge.SkillForge", FakeForge)

    res = run_dev_job(
        job=job,
        policy=policy,
        role="# Role",
        workdir=workdir,
        agent_dir=tmp_path / "agent",
        output_dir=out_dir,
        runner_callable=lambda p, k: {"ok": True, "final_text": "Clean solution"},
    )
    assert res["ok"] is True
    assert len(recorded_cases) == 1
    assert recorded_cases[0].approved is False


# ==============================================================================
# M1 & Section 4: Attempt-Bound Execution Backend in Isolated Environment
# ==============================================================================

def test_section4_attempt_bound_supervisor_positive_file_edit_and_verification(tmp_path: Path):
    """First batch positive: attempt-bound execution backend modifies a test file in an isolated test repo."""
    # 1. Set up an isolated test repository and profile (no production queue or memory)
    test_repo = tmp_path / "isolated_test_repo"
    test_repo.mkdir()
    subprocess.run(["git", "init"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "IsolatedTester"], cwd=test_repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=test_repo, check=True)

    # Failing code file to be modified by the attempt
    code_file = test_repo / "calc.py"
    code_file.write_text("def add(a, b):\n    return a - b  # Bug!\n", encoding="utf-8")
    test_file = test_repo / "test_calc.py"
    test_file.write_text("import calc\nassert calc.add(2, 3) == 5, 'add bug'\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=test_repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial with bug"], cwd=test_repo, check=True, capture_output=True)

    # 2. Set up isolated test pool (not ~/.hermes or production pool)
    pool_root = tmp_path / "isolated_pool"
    pool = Pool(pool_root)

    # 3. Submit task to fix calc.py and run verification
    job_id = pool.submit(
        "fix_calc_bug",
        {
            "repo": str(test_repo),
            "fix": "def add(a, b):\n    return a + b\n",
        },
        origin={"profile": "test_isolated_profile", "session": "test_session_001"},
    )

    # Python runner script that applies fix and passes verification
    runner_script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.loads(sys.stdin.read())['payload']\n"
        "repo = Path(payload['repo'])\n"
        "(repo / 'calc.py').write_text(payload['fix'], encoding='utf-8')\n"
        "import subprocess\n"
        "subprocess.run([sys.executable, str(repo / 'test_calc.py')], check=True)\n"
        "print(json.dumps({'ok': True, 'summary': 'fixed calc.py and passed test_calc.py'}))\n"
    )

    # Run once under attempt_supervisor
    token = containment.new_token()
    claimed = pool.claim(lease=30)
    assert claimed is not None
    row, fd = claimed
    job_uuid = row["id"]
    attempt_uuid = row["attempt"]

    success = pool.run_claimed(
        claimed,
        [sys.executable, "-c", runner_script],
        timeout=15,
        lease=30,
    )
    assert success is True
    st = pool.status(job_id)
    assert st["status"] == "completed"

    # Verify file was modified and test now passes
    assert "return a + b" in code_file.read_text(encoding="utf-8")
    assert subprocess.run([sys.executable, str(test_file)], capture_output=True).returncode == 0

    # Verify attempt_supervisor proof in attempts/<attempt>/stop.json
    attempt_dir = pool_root / "attempts" / attempt_uuid
    stop_file = attempt_dir / "stop.json"
    assert stop_file.is_file()
    proof = json.loads(stop_file.read_text(encoding="utf-8"))
    assert proof["job"] == job_uuid
    assert proof["attempt"] == attempt_uuid
    assert proof["proof_scope"] == "subreaper_tree"
    assert proof["quiescent"] is True
    assert proof["exit_code"] == 0

    # Verify subreaper containment verification
    verify_res = containment.verify_subreaper_containment(
        proof,
        expected_token=proof["token"],
        expected_job=job_uuid,
        expected_attempt=attempt_uuid,
    )
    assert verify_res["ok"] is True
    assert verify_res["quiescent"] is True
    assert verify_res["evidence_id"] is not None


def test_section4_attempt_bound_supervisor_stops_detached_descendants(tmp_path: Path):
    """When an attempt is stopped, attempt_supervisor must reap the attempt and all detached descendants."""
    pool_root = tmp_path / "pool_stop_test"
    pool = Pool(pool_root)

    # Task script that spawns a detached background child and sleeps
    runner_script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )

    job_id = pool.submit("long_running_task", {})
    claimed = pool.claim(lease=10)
    assert claimed is not None
    row, fd = claimed
    attempt_uuid = row["attempt"]

    # Run in a separate thread and cancel early
    import threading
    t = threading.Thread(
        target=lambda: pool.run_claimed(claimed, [sys.executable, "-c", runner_script], timeout=2, lease=10),
        daemon=True,
    )
    t.start()

    # Wait for attempt directory to be created and child process started
    attempt_dir = pool_root / "attempts" / attempt_uuid
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not (attempt_dir / "stdout").exists():
        time.sleep(0.05)

    # Let the supervisor timeout/stop trigger
    t.join(timeout=10)

    # Read stop proof
    stop_file = attempt_dir / "stop.json"
    assert stop_file.is_file()
    proof = json.loads(stop_file.read_text(encoding="utf-8"))
    assert proof["proof_scope"] == "subreaper_tree"
    assert proof["quiescent"] is True
    assert not proof.get("unquiesced_pids")

    # Read the spawned child's pid from stdout
    stdout_text = (attempt_dir / "stdout").read_text(encoding="utf-8").strip()
    if stdout_text and stdout_text.split()[0].isdigit():
        child_pid = int(stdout_text.split()[0])
        # Ensure the detached descendant was killed by the supervisor
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


def test_section4_tampered_or_invalid_proof_rejected():
    """Verify that verify_subreaper_containment rejects tampered or mismatched proofs."""
    valid_token = containment.new_token()
    base_proof = {
        "job": "job-sec4",
        "attempt": "att-sec4",
        "token": valid_token,
        "nonce": "nonce-12345",
        "quiescent": True,
        "proof_scope": "subreaper_tree",
        "supervisor_pid": 12345,
        "exit_code": 0,
    }

    # 1. Valid proof passes
    res = containment.verify_subreaper_containment(base_proof, expected_token=valid_token)
    assert res["ok"] is True
    assert res["evidence_id"] is not None

    # 2. Token mismatch
    mismatch_tok = containment.verify_subreaper_containment(base_proof, expected_token=containment.new_token())
    assert mismatch_tok["ok"] is False
    assert mismatch_tok["reason"] == "proof_token_mismatch"

    # 3. Job mismatch
    mismatch_job = containment.verify_subreaper_containment(base_proof, expected_job="other_job")
    assert mismatch_job["ok"] is False
    assert mismatch_job["reason"] == "proof_job_mismatch"

    # 4. Attempt mismatch
    mismatch_att = containment.verify_subreaper_containment(base_proof, expected_attempt="other_attempt")
    assert mismatch_att["ok"] is False
    assert mismatch_att["reason"] == "proof_attempt_mismatch"

    # 5. Quiescent is False
    non_quiescent = dict(base_proof, quiescent=False)
    res_nq = containment.verify_subreaper_containment(non_quiescent)
    assert res_nq["ok"] is False
    assert res_nq["reason"] == "attempt_not_quiescent"

    # 6. Unquiesced pids remain
    survivors = dict(base_proof, unquiesced_pids=[9999])
    res_surv = containment.verify_subreaper_containment(survivors)
    assert res_surv["ok"] is False
    assert res_surv["reason"] == "unquiesced_descendants_remain"

    # 7. Invalid proof scope
    wrong_scope = dict(base_proof, proof_scope="token_scan")
    res_scope = containment.verify_subreaper_containment(wrong_scope)
    assert res_scope["ok"] is False
    assert res_scope["reason"] == "invalid_proof_scope"

    # 8. Plain boolean alone rejected by sweep_and_verify
    res_bool = containment.sweep_and_verify(valid_token, runner_contained=True)
    assert res_bool.get("quiescent") is False
    assert containment.evidence_id(res_bool) is None


def test_m0_execute_in_worktree_does_not_commit_on_synthetic_demo_mode(tmp_path: Path, monkeypatch):
    """Synthetic execution in demo_mode must not commit to the git branch and must preserve worktree."""
    repo = tmp_path / "repo_demo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)

    orchestrator = ColleagueDevOrchestrator()
    def fail_bridge():
        raise RuntimeError("simulated_bridge_missing")
    monkeypatch.setattr(orchestrator, "_ensure_bridge", fail_bridge)

    res = orchestrator.execute_in_worktree(
        repo_path=repo,
        task_id="task-demo-commit-1",
        requirement_text="Fix brake failure demo",
        demo_mode=True,
    )
    assert res["ok"] is True
    assert res.get("synthetic") is True
    assert res.get("worktree_preserved") is True

    # Branch must not have an autonomous colleague commit
    log = subprocess.run(["git", "log", "-1", "dev-colleague/task-demo-commit-1", "--pretty=%s"],
                         cwd=repo, capture_output=True, text=True)
    assert log.returncode != 0 or "autonomous colleague pipeline" not in log.stdout


def test_m0_demo_mode_env_var_falsy_values_disable_demo_mode(tmp_path: Path, monkeypatch):
    """Setting AGENTCORE_DEMO_MODE=0 or false must NOT enable demo mode."""
    orchestrator = ColleagueDevOrchestrator()
    def fail_bridge():
        raise RuntimeError("simulated_bridge_missing")
    monkeypatch.setattr(orchestrator, "_ensure_bridge", fail_bridge)

    for val in ("0", "false", "False", "no"):
        monkeypatch.setenv("AGENTCORE_DEMO_MODE", val)
        res = orchestrator.execute_autonomous_case_dev(
            requirement_text="ACC Cruise activation",
            workdir=tmp_path / f"work_{val}",
            demo_mode=False,
        )
        assert res["ok"] is False
        assert "colleague_bridge_unavailable" in res["error"]


def test_m0_dev_runner_report_execution_failed_marks_execution_completed_false(tmp_path: Path):
    """When runner fails, execution_completed must be False in receipt and report."""
    workdir = tmp_path / "worktree_fail"
    workdir.mkdir()
    out_dir = tmp_path / "out_fail"
    out_dir.mkdir()
    policy = {"allowed_roots": [str(workdir)]}
    job = {
        "job": "job-fail-report",
        "attempt": "att-1",
        "payload": {"role": "cases", "goal": "Failing run"},
    }
    res = run_dev_job(
        job=job,
        policy=policy,
        role="# Role",
        workdir=workdir,
        agent_dir=tmp_path / "agent",
        output_dir=out_dir,
        runner_callable=lambda p, k: {"ok": False, "error": "fatal_execution_error"},
    )
    assert res["ok"] is False
    assert res["execution_completed"] is False
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["execution_completed"] is False
    assert report["ok"] is False


def test_section4_verify_subreaper_containment_rejects_missing_identity_fields():
    """Proofs missing job, attempt, nonce, supervisor_pid, or exit_code must be rejected."""
    valid_token = containment.new_token()
    base_proof = {
        "job": "job-sec4",
        "attempt": "att-sec4",
        "token": valid_token,
        "nonce": "nonce-12345",
        "quiescent": True,
        "proof_scope": "subreaper_tree",
        "supervisor_pid": 12345,
        "exit_code": 0,
    }

    # Missing / empty job
    res_no_job = containment.verify_subreaper_containment(dict(base_proof, job=""))
    assert res_no_job["ok"] is False
    assert res_no_job["reason"] == "missing_or_invalid_proof_job"

    # Missing / empty attempt
    res_no_att = containment.verify_subreaper_containment(dict(base_proof, attempt=None))
    assert res_no_att["ok"] is False
    assert res_no_att["reason"] == "missing_or_invalid_proof_attempt"

    # Missing / empty nonce
    res_no_nonce = containment.verify_subreaper_containment(dict(base_proof, nonce=""))
    assert res_no_nonce["ok"] is False
    assert res_no_nonce["reason"] == "missing_or_invalid_proof_nonce"

    # Invalid supervisor_pid
    res_bad_pid = containment.verify_subreaper_containment(dict(base_proof, supervisor_pid="not_int"))
    assert res_bad_pid["ok"] is False
    assert res_bad_pid["reason"] == "invalid_supervisor_pid"

    res_zero_pid = containment.verify_subreaper_containment(dict(base_proof, supervisor_pid=0))
    assert res_zero_pid["ok"] is False
    assert res_zero_pid["reason"] == "invalid_supervisor_pid"

    # Invalid exit_code
    res_bad_code = containment.verify_subreaper_containment(dict(base_proof, exit_code=None))
    assert res_bad_code["ok"] is False
    assert res_bad_code["reason"] == "invalid_proof_exit_code"

    # Evidence ID returns None if fields are missing
    bad_evidence = dict(base_proof, supervisor_pid=0)
    assert containment.evidence_id(bad_evidence) is None


def test_colleague_worker_refuses_fake_supervisor_proof_file(tmp_path: Path, monkeypatch):
    """An arbitrary or unverified file in AGENTCORE_SUPERVISOR_PROOF_PATH must be refused."""
    fake_proof_file = tmp_path / "fake_proof.json"
    fake_proof_file.write_text('{"token": "not-valid", "quiescent": false}', encoding="utf-8")
    monkeypatch.setenv("AGENTCORE_SUPERVISOR_PROOF_PATH", str(fake_proof_file))

    class DummyRunner:
        attempt_containment = "runner_and_descendants"
    runner = DummyRunner()

    from agentcore.colleague_worker import _trusted_containment_available
    assert _trusted_containment_available(runner) is False

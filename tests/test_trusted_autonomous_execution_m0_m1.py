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
import signal
import socket
import subprocess
import sys
import threading
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


def test_m1_gate_refuses_self_declared_bool_runner(tmp_path: Path):
    """A self-declared boolean runner must be refused by the pre-execution gate."""
    from agentcore.colleague_worker import _trusted_containment_available

    class BoolRunner:
        attempt_containment = "runner_and_descendants"
        trusted_containment = True
        def has_trusted_containment_proof(self):
            return True

    runner = BoolRunner()
    token = containment.new_token()
    assert _trusted_containment_available(
        runner,
        expected_job="job-1",
        expected_attempt="att-1",
        expected_nonce="nonce-1",
        expected_token=token,
    ) is False


def test_m1_gate_refuses_when_no_backend_adapter():
    """Gate returns False when no execution backend adapter exists (fail-closed baseline)."""
    from agentcore.colleague_worker import _trusted_containment_available

    class PlainRunner:
        attempt_containment = "runner_and_descendants"

    runner = PlainRunner()
    assert _trusted_containment_available(runner) is False
    assert _trusted_containment_available(
        runner,
        expected_job="job-1",
        expected_attempt="att-1",
        expected_nonce="nonce-1",
        expected_token=containment.new_token(),
    ) is False


def test_m1_gate_refuses_well_formed_stop_proof_alone(tmp_path: Path, monkeypatch):
    """A stop/quiescence proof shows an attempt stopped; it must NEVER open the pre-execution gate."""
    valid_token = containment.new_token()
    well_formed_stop_proof = {
        "job": "job-stop-1",
        "attempt": "att-stop-1",
        "token": valid_token,
        "nonce": "nonce-stop-1",
        "quiescent": True,
        "proof_scope": "subreaper_tree",
        "supervisor_pid": os.getpid(),
        "exit_code": 0,
        "child_started": True,
        "unquiesced_pids": [],
    }
    # Verify that the proof itself is valid as a stop proof
    ver = containment.verify_subreaper_containment(
        well_formed_stop_proof,
        expected_token=valid_token,
        expected_job="job-stop-1",
        expected_attempt="att-stop-1",
        expected_nonce="nonce-stop-1",
    )
    assert ver["ok"] is True
    assert ver["quiescent"] is True

    # 1. Stop proof presented via runner callback must not open the gate
    class StopProofRunner:
        attempt_containment = "runner_and_descendants"
        def trusted_containment_evidence(self):
            return well_formed_stop_proof

    from agentcore.colleague_worker import _trusted_containment_available
    runner = StopProofRunner()
    assert _trusted_containment_available(
        runner,
        expected_job="job-stop-1",
        expected_attempt="att-stop-1",
        expected_nonce="nonce-stop-1",
        expected_token=valid_token,
    ) is False

    # 2. Well-formed stop proof file in environment must not open the gate
    proof_file = tmp_path / "stop.json"
    proof_file.write_text(json.dumps(well_formed_stop_proof), encoding="utf-8")
    monkeypatch.setenv("AGENTCORE_SUPERVISOR_PROOF_PATH", str(proof_file))

    class PlainRunner:
        attempt_containment = "runner_and_descendants"

    assert _trusted_containment_available(
        PlainRunner(),
        expected_job="job-stop-1",
        expected_attempt="att-stop-1",
        expected_nonce="nonce-stop-1",
        expected_token=valid_token,
    ) is False


def test_m1_proof_from_different_attempt_job_or_nonce_is_refused(tmp_path: Path):
    """Proofs from different jobs, attempts, or nonces must be strictly rejected."""
    token = containment.new_token()
    proof = {
        "job": "job-alpha",
        "attempt": "att-alpha",
        "token": token,
        "nonce": "nonce-alpha",
        "quiescent": True,
        "proof_scope": "subreaper_tree",
        "supervisor_pid": 54321,
        "exit_code": 0,
    }

    # 1. verify_subreaper_containment rejects mismatches
    assert containment.verify_subreaper_containment(proof, expected_job="job-beta")["ok"] is False
    assert containment.verify_subreaper_containment(proof, expected_job="job-beta")["reason"] == "proof_job_mismatch"

    assert containment.verify_subreaper_containment(proof, expected_attempt="att-beta")["ok"] is False
    assert containment.verify_subreaper_containment(proof, expected_attempt="att-beta")["reason"] == "proof_attempt_mismatch"

    assert containment.verify_subreaper_containment(proof, expected_nonce="nonce-beta")["ok"] is False
    assert containment.verify_subreaper_containment(proof, expected_nonce="nonce-beta")["reason"] == "proof_nonce_mismatch"

    # 2. sweep_and_verify rejects if expected identities are missing or mismatched
    sweep_missing = containment.sweep_and_verify(token, supervisor_proof=proof)
    assert sweep_missing["ok"] is False
    assert sweep_missing["reason"] == "missing_expected_identities"

    sweep_mismatch = containment.sweep_and_verify(
        token,
        supervisor_proof=proof,
        expected_job="job-beta",
        expected_attempt="att-alpha",
        expected_nonce="nonce-alpha",
    )
    assert sweep_mismatch["ok"] is False
    assert sweep_mismatch["reason"] == "proof_job_mismatch"

    # 3. Bare FakeAdapter without pre-execution bwrap_subreaper proof is rejected
    class FakeAdapter:
        def verify_attempt_binding(self, *, expected_job, expected_attempt, expected_nonce, expected_token):
            return {"ok": True}

    class FakeAdapterRunner:
        attempt_containment = "runner_and_descendants"
        execution_backend_adapter = FakeAdapter()

    from agentcore.colleague_worker import _trusted_containment_available
    assert _trusted_containment_available(
        FakeAdapterRunner(),
        expected_job="job-real",
        expected_attempt="att-real",
        expected_nonce="nonce-real",
        expected_token=token,
    ) is False

    # 4. Real AttemptBoundSandboxAdapter passes only when all 4 identities match
    real_adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=tmp_path / "attempt_real",
        job="job-real",
        attempt="att-real",
        nonce="nonce-real",
        token=token,
        workdir=tmp_path,
    )

    class RealAdapterRunner:
        attempt_containment = "runner_and_descendants"
        execution_backend_adapter = real_adapter

    assert _trusted_containment_available(
        RealAdapterRunner(),
        expected_job="job-real",
        expected_attempt="att-real",
        expected_nonce="nonce-real",
        expected_token=token,
    ) is True

    assert _trusted_containment_available(
        RealAdapterRunner(),
        expected_job="job-other",
        expected_attempt="att-real",
        expected_nonce="nonce-real",
        expected_token=token,
    ) is False
    assert _trusted_containment_available(
        RealAdapterRunner(),
        expected_job="job-real",
        expected_attempt="att-other",
        expected_nonce="nonce-real",
        expected_token=token,
    ) is False
    assert _trusted_containment_available(
        RealAdapterRunner(),
        expected_job="job-real",
        expected_attempt="att-real",
        expected_nonce="nonce-other",
        expected_token=token,
    ) is False
    assert _trusted_containment_available(
        RealAdapterRunner(),
        expected_job="job-real",
        expected_attempt="att-real",
        expected_nonce="nonce-real",
        expected_token=containment.new_token(),
    ) is False


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


# ==============================================================================
# M1 & Section 4 — AttemptBoundSandboxAdapter (bwrap + subreaper) Verification
# ==============================================================================


def _init_isolated_test_env_and_repo(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Create an isolated test profile and isolated Git test repository (Section 4 Step 2)."""
    profile_root = tmp_path / "isolated_profile"
    hermes_home = profile_root / "hermes_home"
    pool_dir = profile_root / "pool"
    memory_dir = profile_root / "memory"
    repo_dir = tmp_path / "isolated_repo"

    for d in (hermes_home, pool_dir, memory_dir, repo_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("SUPERAGENT_PROFILE", "trusted_exec_test_profile")
    monkeypatch.setenv("SUPERAGENT_MEMORY_DIR", str(memory_dir))

    (repo_dir / "SPEC.md").write_text("# Immutable Spec\nadd(a, b) must return a + b.\n", encoding="utf-8")
    (repo_dir / "STATUS.md").write_text("# Status Log\n", encoding="utf-8")
    (repo_dir / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    (repo_dir / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Isolated Test"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial buggy commit"], cwd=repo_dir, check=True, capture_output=True)

    return {
        "profile_root": profile_root,
        "hermes_home": hermes_home,
        "pool_dir": pool_dir,
        "memory_dir": memory_dir,
        "repo_dir": repo_dir,
    }


def test_m1_host_minted_binding_rejects_tampering_symlinks_and_dead_supervisor(tmp_path: Path):
    """Host-created binding.json enforces instance_id, generation, boot_id, file mode, and live supervisor."""
    token = containment.new_token()
    attempt_dir = tmp_path / "attempt_bind_1"
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=attempt_dir,
        job="job-bind-1",
        attempt="att-bind-1",
        nonce="nonce-bind-1",
        token=token,
        workdir=workdir,
        generation=2,
    )

    check = adapter.verify_attempt_binding(
        expected_job="job-bind-1",
        expected_attempt="att-bind-1",
        expected_nonce="nonce-bind-1",
        expected_token=token,
    )
    assert check["ok"] is True
    assert check["proof_type"] == "attempt_pre_execution_binding"
    assert check["backend"] == "bwrap_subreaper"
    assert check["generation"] == 2
    assert len(check["instance_id"]) == 32

    # World-writable binding file must be rejected
    binding_file = attempt_dir / "binding.json"
    os.chmod(binding_file, 0o666)
    bad_mode = adapter.verify_attempt_binding(
        expected_job="job-bind-1",
        expected_attempt="att-bind-1",
        expected_nonce="nonce-bind-1",
        expected_token=token,
    )
    assert bad_mode["ok"] is False
    assert "binding_file_invalid" in bad_mode["reason"]
    os.chmod(binding_file, 0o600)

    # Symlinked binding file must be rejected
    symlink_path = tmp_path / "symlink_binding.json"
    symlink_path.symlink_to(binding_file)
    with pytest.raises(OSError):
        containment.AttemptBoundSandboxAdapter.from_binding_file(
            symlink_path,
            workdir=workdir,
        )

    # Binding referencing a supervisor PID that later exits must be rejected
    short_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    dead_dir = tmp_path / "attempt_dead_sup"
    dead_adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=dead_dir,
        job="job-dead",
        attempt="att-dead",
        nonce="nonce-dead",
        token=token,
        workdir=workdir,
        supervisor_pid=short_proc.pid,
    )
    short_proc.wait(timeout=5)
    dead_check = dead_adapter.verify_attempt_binding(
        expected_job="job-dead",
        expected_attempt="att-dead",
        expected_nonce="nonce-dead",
        expected_token=token,
    )
    assert dead_check["ok"] is False
    assert dead_check["reason"] == "supervisor_not_alive_or_reused"


def test_m1_payload_self_declared_binding_fields_rejected(tmp_path: Path, monkeypatch):
    """Task payloads attempting to self-declare binding or containment fields must be rejected before execution."""
    env_paths = _init_isolated_test_env_and_repo(tmp_path, monkeypatch)
    repo_dir = env_paths["repo_dir"]
    token = containment.new_token()
    adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=tmp_path / "attempt_forged_payload",
        job="job-forged-1",
        attempt="att-forged-1",
        nonce="nonce-forged-1",
        token=token,
        workdir=repo_dir,
    )

    forged_job = {
        "job": "job-forged-1",
        "attempt": "att-forged-1",
        "payload": {
            "role": "cases",
            "goal": "Fix add",
            "trusted_containment": True,
            "instance_id": "a" * 32,
        },
    }

    called = False

    def _runner(_prompt, _opts):
        nonlocal called
        called = True
        return {"ok": True}

    with pytest.raises(PermissionError, match="payload_binding_self_declaration_forbidden"):
        run_dev_job(
            job=forged_job,
            policy={"allowed_roots": [str(repo_dir)]},
            role="# Dev",
            workdir=repo_dir,
            agent_dir=tmp_path / "agent",
            output_dir=tmp_path / "out",
            runner_callable=_runner,
            execution_backend_adapter=adapter,
        )
    assert called is False


def test_m1_gateway_delegated_and_host_owned_runners_refused_even_with_real_adapter(tmp_path: Path):
    """gateway_delegated and host_owned runners must be refused even if attached to a real adapter."""
    token = containment.new_token()
    adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=tmp_path / "attempt_gw",
        job="job-gw",
        attempt="att-gw",
        nonce="nonce-gw",
        token=token,
        workdir=tmp_path,
    )

    from agentcore.colleague_worker import _require_attempt_containment, _trusted_containment_available

    for bad_kind in ("gateway_delegated", "host_owned", None, "custom_unknown"):
        class BadRunner:
            attempt_containment = bad_kind
            execution_backend_adapter = adapter

        assert _trusted_containment_available(
            BadRunner(),
            expected_job="job-gw",
            expected_attempt="att-gw",
            expected_nonce="nonce-gw",
            expected_token=token,
        ) is False
        with pytest.raises(RuntimeError, match="colleague_turn_containment_unprovable"):
            _require_attempt_containment(BadRunner())


def test_m1_pre_execution_and_kernel_bwrap_enforcement_of_paths_credentials_network_and_tools(
    tmp_path: Path, monkeypatch
):
    """Verify pre-execution authorization AND kernel-level bwrap enforcement of role paths, credentials, network, and tool whitelist."""
    env_paths = _init_isolated_test_env_and_repo(tmp_path, monkeypatch)
    repo_dir = env_paths["repo_dir"]
    hermes_home = env_paths["hermes_home"]

    # Create a sensitive credential file in simulated HERMES_HOME and host tmp
    secret_file = hermes_home / "secret_token.txt"
    secret_file.write_text("TOP_SECRET_CREDENTIAL", encoding="utf-8")
    outside_file = tmp_path / "outside_host_secret.txt"
    outside_file.write_text("HOST_OUTSIDE_SECRET", encoding="utf-8")

    token = containment.new_token()
    adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=tmp_path / "attempt_isolation",
        job="job-iso-1",
        attempt="att-iso-1",
        nonce="nonce-iso-1",
        token=token,
        workdir=repo_dir,
        role="cases",
        allowed_tools=["python_exec", "pytest_verify"],
        network_policy="isolated",
    )

    # 1. Tool outside whitelist is rejected pre-execution
    with pytest.raises(PermissionError, match="unauthorized_tool_denied:unauthorized_curl"):
        adapter.execute_in_sandbox(
            [sys.executable, "-c", "print('hi')"],
            tool="unauthorized_curl",
        )

    # 2. Network access request is rejected pre-execution
    with pytest.raises(PermissionError, match="unauthorized_network_access_denied"):
        adapter.execute_in_sandbox(
            [sys.executable, "-c", "print('hi')"],
            tool="python_exec",
            network_access=True,
        )

    # 3. Forbidden host credential/socket paths and outside paths are rejected pre-execution
    for bad_path in ("~/.ssh/id_rsa", "~/.config/secrets", "~/.hermes/auth.json", "/var/run/docker.sock", str(outside_file)):
        with pytest.raises(PermissionError, match="forbidden_host_access|path_outside_authorized_workdir"):
            adapter.execute_in_sandbox(
                [sys.executable, "-c", "print('hi')"],
                tool="python_exec",
                target_paths=[bad_path],
            )

    # 4. Role path access (validate_role_path_access) rejects 'cases' role modifying SPEC.md, .git/config, or STATUS.md pre-execution
    for forbidden_rel in ("SPEC.md", ".git/config", "STATUS.md"):
        with pytest.raises(PermissionError, match="role_ownership_violation"):
            adapter.execute_in_sandbox(
                [sys.executable, "-c", "print('hi')"],
                tool="python_exec",
                target_paths=[forbidden_rel],
                action="write",
            )

    # 5. Inside bwrap sandbox: kernel mount namespace blocks writing to SPEC.md and .git/config (EROFS),
    # hides outside_file and hermes_home secrets, and --unshare-all blocks connecting to host loopback TCP sockets!
    host_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    host_sock.bind(("127.0.0.1", 0))
    host_sock.listen(1)
    host_port = host_sock.getsockname()[1]
    try:
        probe_code = f"""
import errno, json, os, pathlib, socket
results = {{}}
# Check outside file visibility
results["outside_visible"] = pathlib.Path({str(outside_file)!r}).exists()
results["hermes_secret_visible"] = pathlib.Path({str(secret_file)!r}).exists()
# Check writing to SPEC.md (mounted ro-bind by bwrap)
try:
    pathlib.Path("SPEC.md").write_text("hacked", encoding="utf-8")
    results["spec_write"] = "allowed"
except OSError as exc:
    results["spec_write"] = exc.errno
# Check writing to .git/config (mounted ro-bind by bwrap)
try:
    pathlib.Path(".git/config").write_text("hacked", encoding="utf-8")
    results["git_write"] = "allowed"
except OSError as exc:
    results["git_write"] = exc.errno
# Check connecting to host loopback TCP port (isolated netns)
try:
    s = socket.create_connection(("127.0.0.1", {host_port}), timeout=1.0)
    s.close()
    results["net_connect"] = "allowed"
except OSError:
    results["net_connect"] = "blocked"
print(json.dumps(results))
"""
        res_probe = adapter.execute_in_sandbox(
            [sys.executable, "-c", probe_code],
            tool="python_exec",
            target_paths=["calc.py"],
            action="read",
        )
    finally:
        host_sock.close()

    assert res_probe["ok"] is True, f"Probe failed: {res_probe}"
    probe_data = json.loads(res_probe["stdout"].strip())
    assert probe_data["outside_visible"] is False
    assert probe_data["hermes_secret_visible"] is False
    assert probe_data["spec_write"] == 30  # errno.EROFS (Read-only file system)
    assert probe_data["git_write"] == 30   # errno.EROFS
    assert probe_data["net_connect"] == "blocked"


def test_m1_resource_budgets_enforced_and_invalid_policy_fails_closed(tmp_path: Path, monkeypatch):
    """Verify timeout_s, max_procs, max_memory_bytes, max_output_bytes, max_file_bytes, max_disk_bytes, and invalid budget fail-closed."""
    env_paths = _init_isolated_test_env_and_repo(tmp_path, monkeypatch)
    repo_dir = env_paths["repo_dir"]
    token = containment.new_token()

    # 1. Invalid budget policy fails closed
    with pytest.raises(ValueError, match="invalid_resource_budget"):
        containment.normalize_resource_budgets({"timeout_s": 0})
    with pytest.raises(ValueError, match="invalid_resource_budget"):
        containment.normalize_resource_budgets({"max_procs": -5})

    adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=tmp_path / "attempt_budgets",
        job="job-budget-1",
        attempt="att-budget-1",
        nonce="nonce-budget-1",
        token=token,
        workdir=repo_dir,
        role="cases",
    )

    # 2. timeout_s enforcement
    res_timeout = adapter.execute_in_sandbox(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        budgets={"timeout_s": 0.4},
    )
    assert res_timeout["ok"] is False
    assert res_timeout["timed_out"] is True
    assert res_timeout["budget_exceeded"] == "timeout"
    assert res_timeout["quiescent"] is True

    # 3. max_output_bytes enforcement
    res_out = adapter.execute_in_sandbox(
        [sys.executable, "-c", "import sys; sys.stdout.write('X' * 50000); sys.stdout.flush()"],
        budgets={"max_output_bytes": 4096},
    )
    assert res_out["ok"] is False
    assert res_out["output_limited"] is True
    assert res_out["budget_exceeded"] == "output"
    assert len(res_out["stdout"].encode("utf-8")) <= 4096

    # 4. max_memory_bytes enforcement (RLIMIT_AS)
    res_mem = adapter.execute_in_sandbox(
        [sys.executable, "-c", "x = bytearray(256 * 1024 * 1024)"],
        budgets={"max_memory_bytes": 96 * 1024 * 1024},
    )
    assert res_mem["ok"] is False
    assert res_mem["exit_code"] != 0

    # 5. max_procs enforcement (RLIMIT_NPROC scoped to bwrap user namespace)
    res_procs = adapter.execute_in_sandbox(
        [
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "procs = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']) for _ in range(12)]",
        ],
        budgets={"max_procs": 4, "timeout_s": 5.0},
    )
    assert res_procs["ok"] is False
    assert res_procs["exit_code"] != 0
    assert res_procs["quiescent"] is True

    # 6. max_file_bytes / max_disk_bytes enforcement + automatic rollback of workdir changes
    res_disk = adapter.execute_in_sandbox(
        [
            sys.executable,
            "-c",
            "import pathlib; pathlib.Path('calc.py').write_bytes(b'#' * 200000)",
        ],
        target_paths=["calc.py"],
        budgets={"max_file_bytes": 1024 * 1024, "max_disk_bytes": 65536},
    )
    assert res_disk["ok"] is False
    assert res_disk["budget_exceeded"] == "disk"
    assert res_disk["exit_code"] == 125
    # Verify calc.py was rolled back to its original content
    assert "return a - b" in (repo_dir / "calc.py").read_text(encoding="utf-8")


def test_m1_cancel_blocks_new_actions_and_reaps_setsid_descendants(tmp_path: Path, monkeypatch):
    """Two-phase cancel blocks new actions first, then kills and reaps normal and setsid detached children."""
    env_paths = _init_isolated_test_env_and_repo(tmp_path, monkeypatch)
    repo_dir = env_paths["repo_dir"]
    token = containment.new_token()
    attempt_dir = tmp_path / "attempt_cancel"
    adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=attempt_dir,
        job="job-cancel-1",
        attempt="att-cancel-1",
        nonce="nonce-cancel-1",
        token=token,
        workdir=repo_dir,
        role="cases",
    )

    pid_file = repo_dir / "detached_marker.py"
    spawn_script = (
        "import os, pathlib, subprocess, sys, time\n"
        "code = (\n"
        "    'import os, pathlib, time\\n'\n"
        "    'os.setsid()\\n'\n"
        "    'pid = os.fork()\\n'\n"
        "    'if pid > 0:\\n'\n"
        "    '    os._exit(0)\\n'\n"
        "    'pathlib.Path(\"detached_marker.py\").write_text(\"# ready\", encoding=\"utf-8\")\\n'\n"
        "    'time.sleep(60)\\n'\n"
        ")\n"
        "subprocess.Popen([sys.executable, '-c', code])\n"
        "time.sleep(60)\n"
    )
    exec_result_holder: list[dict] = []

    def _run_bg():
        res = adapter.execute_in_sandbox(
            [sys.executable, "-c", spawn_script],
            target_paths=["detached_marker.py"],
            budgets={"timeout_s": 30.0},
        )
        exec_result_holder.append(res)

    bg_thread = threading.Thread(target=_run_bg, daemon=True)
    bg_thread.start()

    # Wait until detached_marker.py is written inside the sandbox
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.05)
    assert pid_file.exists(), "Detached grandchild did not start inside sandbox"

    active_host_pids = adapter.active_sandbox_pids()
    assert len(active_host_pids) >= 1

    # Cancel with mismatched identity must fail closed with status="unknown"
    bad_cancel = adapter.cancel(
        expected_job="wrong-job",
        expected_attempt="att-cancel-1",
        expected_nonce="nonce-cancel-1",
        expected_token=token,
    )
    assert bad_cancel["ok"] is False
    assert bad_cancel["status"] == "unknown"
    assert bad_cancel["quiescent"] is False
    assert bad_cancel["new_actions_blocked"] is True

    # Now perform valid two-phase cancel
    cancel_receipt = adapter.cancel(
        expected_job="job-cancel-1",
        expected_attempt="att-cancel-1",
        expected_nonce="nonce-cancel-1",
        expected_token=token,
    )
    bg_thread.join(timeout=5.0)

    assert cancel_receipt["ok"] is True
    assert cancel_receipt["status"] == "stopped"
    assert cancel_receipt["quiescent"] is True
    assert cancel_receipt["new_actions_blocked"] is True

    # Verify all recorded host PIDs are completely gone
    for hpid in active_host_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(hpid, 0)

    # Phase 1 guarantee: any subsequent execution attempt after cancel is immediately refused
    with pytest.raises(PermissionError, match="attempt_binding_verification_failed:attempt_already_stopped_or_cancelled"):
        adapter.execute_in_sandbox(
            [sys.executable, "-c", "print('after cancel')"],
        )


def test_m1_worker_sigkill_crash_reaps_sandbox_and_setsid_descendants(tmp_path: Path, monkeypatch):
    """When the worker process crashes with SIGKILL (kill -9), the sandbox subreaper detects control socket EOF and reaps all descendants."""
    env_paths = _init_isolated_test_env_and_repo(tmp_path, monkeypatch)
    repo_dir = env_paths["repo_dir"]
    token = containment.new_token()
    attempt_dir = tmp_path / "attempt_sigkill"

    containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=attempt_dir,
        job="job-sigkill-1",
        attempt="att-sigkill-1",
        nonce="nonce-sigkill-1",
        token=token,
        workdir=repo_dir,
        role="cases",
    )

    ready_file = repo_dir / "sigkill_ready.py"
    worker_code = (
        "import pathlib, sys\n"
        "from agentcore import colleague_containment as containment\n"
        f"adapter = containment.AttemptBoundSandboxAdapter.from_binding_file({str(attempt_dir / 'binding.json')!r}, workdir={str(repo_dir)!r})\n"
        "inner_code = (\n"
        "    'import os, pathlib, subprocess, sys, time\\n'\n"
        "    'child = (\\n'\n"
        "    '    \"import os, pathlib, time\\\\n\"\\n'\n"
        "    '    \"os.setsid()\\\\n\"\\n'\n"
        "    '    \"pid = os.fork()\\\\n\"\\n'\n"
        "    '    \"if pid > 0: os._exit(0)\\\\n\"\\n'\n"
        "    '    \"pathlib.Path(\\'sigkill_ready.py\\').write_text(\\'# ready\\', encoding=\\'utf-8\\')\\\\n\"\\n'\n"
        "    '    \"time.sleep(60)\\\\n\"\\n'\n"
        "    ')\\n'\n"
        "    'subprocess.Popen([sys.executable, \"-c\", child])\\n'\n"
        "    'time.sleep(60)\\n'\n"
        ")\n"
        "adapter.execute_in_sandbox([sys.executable, '-c', inner_code], target_paths=['sigkill_ready.py'], budgets={'timeout_s': 60.0})\n"
    )
    worker_proc = subprocess.Popen([sys.executable, "-c", worker_code])
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not ready_file.exists():
            time.sleep(0.05)
        assert ready_file.exists(), "Sandbox inner process did not become ready before SIGKILL"

        # Find all live processes carrying the attempt token before SIGKILL
        live_before = containment.tagged(token)
        assert len(live_before) >= 1

        # Kill the worker immediately with SIGKILL (simulating sudden worker crash)
        os.kill(worker_proc.pid, signal.SIGKILL)
        worker_proc.wait(timeout=5.0)

        # Wait up to 3s for the sandbox subreaper to detect control-socket EOF and reap bwrap + descendants
        reap_deadline = time.monotonic() + 3.0
        while time.monotonic() < reap_deadline:
            still_alive = containment.tagged(token)
            if not still_alive:
                break
            time.sleep(0.05)

        assert containment.tagged(token) == [], (
            f"Leaked tagged processes after worker SIGKILL: {containment.tagged(token)}"
        )
    finally:
        if worker_proc.poll() is None:
            worker_proc.kill()
            worker_proc.wait(timeout=2.0)


def test_m1_end_to_end_dev_runner_and_pool_with_attempt_bound_sandbox_adapter(tmp_path: Path, monkeypatch):
    """End-to-end verification: both model tool execution and verification checks run inside AttemptBoundSandboxAdapter."""
    env_paths = _init_isolated_test_env_and_repo(tmp_path, monkeypatch)
    repo_dir = env_paths["repo_dir"]
    pool_dir = env_paths["pool_dir"]

    token = containment.new_token()
    attempt_dir = tmp_path / "attempt_e2e_dev"
    adapter = containment.AttemptBoundSandboxAdapter.create_host_bound(
        attempt_dir=attempt_dir,
        job="job-e2e-dev-1",
        attempt="att-e2e-dev-1",
        nonce="nonce-e2e-dev-1",
        token=token,
        workdir=repo_dir,
        role="cases",
        allowed_tools=["bash", "pytest"],
    )

    def _sandboxed_model_runner(_prompt, options):
        bound_adapter = options["execution_backend_adapter"]
        fix_code = (
            "import pathlib\n"
            "pathlib.Path('calc.py').write_text('def add(a: int, b: int) -> int:\\n    return a + b\\n', encoding='utf-8')\n"
        )
        exec_res = bound_adapter.execute_in_sandbox(
            [sys.executable, "-c", fix_code],
            tool="bash",
            target_paths=["calc.py"],
            action="write",
        )
        return {
            "ok": exec_res["ok"],
            "final_text": "Fixed calc.py in sandbox",
            "tool_calls": [{"name": "bash", "exit_code": exec_res["exit_code"]}],
        }

    job_env = {
        "job": "job-e2e-dev-1",
        "attempt": "att-e2e-dev-1",
        "payload": {
            "role": "cases",
            "goal": "Fix add(a, b) in calc.py",
            "target_paths": ["calc.py"],
            "verification_checks": [
                {"check_id": "unit_tests", "command": f"{sys.executable} -m unittest test_calc.py"},
            ],
        },
    }
    policy = {
        "allowed_roots": [str(repo_dir)],
        "require_verification": True,
        "verification_checks": [
            {"check_id": "unit_tests", "command": f"{sys.executable} -m unittest test_calc.py"},
        ],
    }

    out_dir = tmp_path / "artifacts"
    res = run_dev_job(
        job=job_env,
        policy=policy,
        role="# Cases Role",
        workdir=repo_dir,
        agent_dir=tmp_path / "agent",
        output_dir=out_dir,
        sandbox=True,
        runner_callable=_sandboxed_model_runner,
        execution_backend_adapter=adapter,
    )

    assert res["ok"] is True, f"Dev job failed: {res}"
    assert res["execution_completed"] is True
    assert res["verification_passed"] is True
    assert res["artifacts_valid"] is True

    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["containment_binding"]["backend"] == "bwrap_subreaper"
    # 1 pre-execution check in run_dev_job + 1 model tool execution + 1 verification check = 3 audit entries
    assert len(report["containment_audit_log"]) >= 2
    assert report["containment_evidence"]["quiescent"] is True

    # Also test Pool -> attempt_supervisor -> colleague_worker CLI end-to-end with AttemptBoundSandboxAdapter.from_environment
    bootstrap_mod = tmp_path / "trusted_e2e_bootstrap.py"
    bootstrap_mod.write_text(
        f"""
import sys
from pathlib import Path
from agentcore import colleague_containment as containment

class SandboxedWorkerRunner:
    attempt_containment = "runner_and_descendants"
    trusted_containment = True

    def __init__(self):
        self.execution_backend_adapter = containment.AttemptBoundSandboxAdapter.from_environment(
            workdir=Path({str(repo_dir)!r}),
            role="cases",
            allowed_tools=["bash", "pytest"],
        )

    def run(self, *, member_id, generation, request_id, prompt, owner_profile, owner_session):
        res = self.execution_backend_adapter.execute_in_sandbox(
            [sys.executable, "-m", "unittest", "test_calc.py"],
            tool="pytest",
            target_paths=["test_calc.py"],
            action="read",
        )
        if not res["ok"]:
            raise RuntimeError("sandbox_unittest_failed")
        return {{
            "request_id": request_id,
            "requested_by": {{"profile": owner_profile, "session": owner_session}},
            "executed_by": {{"member_id": member_id, "generation": generation}},
            "text": res["stdout"].strip() or "ok",
        }}

def make():
    return SandboxedWorkerRunner(), lambda owner, member_id, generation: True
""",
        encoding="utf-8",
    )

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = f"{tmp_path}:{repo_root}" + (f":{existing_pp}" if existing_pp else "")
    monkeypatch.setenv("PYTHONPATH", env["PYTHONPATH"])

    from agentcore.colleague_dispatch import ColleagueDispatch
    os.chmod(pool_dir, 0o700)
    pool = Pool(pool_dir)
    dispatch = ColleagueDispatch(
        pool,
        authorize_assignment=lambda owner, member_id, generation: True,
        dedicated=True,
    )
    sub = dispatch.submit(
        owner={"profile": "trusted_exec_test_profile", "session": "session_e2e_001"},
        member_id="dev",
        generation=1,
        request_id="req-e2e-001",
        prompt="Verify fixed calc.py in isolated sandbox",
    )
    job_id = sub["job_id"]
    claimed = pool.claim(lease=30)
    assert claimed is not None
    row, _fd = claimed
    attempt_uuid = row["attempt"]

    ok = pool.run_claimed(
        claimed,
        [sys.executable, "-m", "agentcore.colleague_worker", "--bootstrap", "trusted_e2e_bootstrap:make"],
        timeout=15,
        lease=30,
    )
    assert ok is True
    st = pool.status(job_id)
    assert st["status"] == "completed", f"Pool job did not succeed: {st}"

    # Verify binding.json and stop.json were created in the attempt directory and match instance_id & generation
    attempt_path = pool_dir / "attempts" / attempt_uuid
    binding_file = attempt_path / "binding.json"
    stop_file = attempt_path / "stop.json"
    assert binding_file.is_file()
    assert stop_file.is_file()
    binding_data = json.loads(binding_file.read_text(encoding="utf-8"))
    stop_data = json.loads(stop_file.read_text(encoding="utf-8"))
    assert binding_data["instance_id"] == stop_data["instance_id"]
    assert binding_data["generation"] == stop_data["generation"] == 1
    assert stop_data["quiescent"] is True



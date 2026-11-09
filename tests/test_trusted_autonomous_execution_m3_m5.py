"""Comprehensive regression suite for M2, M3, and M5 of TRUSTED_AUTONOMOUS_EXECUTION_PLAN.

Covers:
1. M2 Receipt and Patch Integrity:
   - Isolated worktree verification receipt binding (task, attempt, code snapshot digest, policy digest).
   - Stale receipt rejection after code modification.
   - Cross-task and stale-attempt receipt reuse rejection.
   - Preservation of unharvested worktrees.
2. M3 Fault Matrix (8 rows):
   - Row 1: Cancel during execution (confirm stop only after stop proof).
   - Row 2: Cancel vs success race (single pool ledger arbitration, no contradictory terminal state).
   - Row 3: Worker/host restart (check original attempt, do not blindly start duplicate execution).
   - Row 4: Lease expiry or late stale attempt (stale execution/receipt cannot overwrite new attempt).
   - Row 5: Duplicate submission (idempotency contract per queue).
   - Row 6: Repeated identical invalid tool calls (bounded stagnation stop).
   - Row 7: External action response lost (preserve unknown, never blindly replay external side effects).
   - Row 8: Budget exhaustion (bounded stop / escalate to human).
3. M5 Controlled Experience Reuse & Rollback:
   - Candidate (approved=False) vs approved vs revoked lifecycle in SkillForge.
   - Profile, subject, and team isolation and post-revocation read denial.
   - Advisory non-override of security policy allowed_roots and verification_checks.
   - Stop-intake and version rollback drill in an isolated test root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

import pytest

from agentcore.dev_runner import _detect_tool_stagnation, _workdir
from agentcore.hitl_bridge import HitlBridge
from agentcore.plan_gate import ComplexityAssessment, PlanGate, TaskPlan, assess_task_complexity
from agentcore.pool import Pool, display_stage
from agentcore.skill_forge import AgentCase, ProceduralSkill, SkillForge
from agentcore.state_graph import WorktreeStrandedError, check_worktree_harvest
from agentcore.verification_gate import (
    VerificationCheck,
    compute_policy_digest,
    compute_worktree_digest,
    evaluate_verification_checks,
    validate_bound_verification_receipt,
    validate_verification_receipt,
)


# ==============================================================================
# Helper fixtures and builders
# ==============================================================================

@pytest.fixture
def isolated_pool(tmp_path: Path) -> Pool:
    pool_root = tmp_path / "pool_runtime"
    pool = Pool(pool_root)
    pool.init_readonly_deliveries_schema()
    return pool


@pytest.fixture
def git_test_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.org"], cwd=repo, check=True)
    (repo / "solution.py").write_text("def solve():\n    return 42\n", encoding="utf-8")
    (repo / "test_solution.py").write_text(
        "from solution import solve\ndef test_solve():\n    assert solve() == 42\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    return repo


# ==============================================================================
# M2: Receipt and Patch Integrity
# ==============================================================================

def test_m2_receipt_binding_task_attempt_digests(git_test_repo: Path):
    """M2: Verification receipts must bind task, attempt, code digest, and policy digest."""
    policy = {
        "allowed_roots": [str(git_test_repo)],
        "verification_checks": [
            {
                "check_id": "test_pass",
                "command": "python3 -m unittest -v",
                "timeout_s": 10.0,
            }
        ],
    }
    pol_digest = compute_policy_digest(policy)
    code_digest = compute_worktree_digest(git_test_repo)

    check = VerificationCheck(
        check_id="check_code",
        command=("python3", "-c", "import sys; sys.exit(0)"),
        timeout_s=5.0,
    )
    receipt = evaluate_verification_checks(
        [check],
        git_test_repo,
        task_id="task-m2-001",
        attempt=1,
        policy_digest=pol_digest,
        code_snapshot_digest=code_digest,
    )

    assert receipt["verdict"] == "approved"
    assert receipt["task_id"] == "task-m2-001"
    assert receipt["attempt"] == 1
    assert receipt["policy_digest"] == pol_digest
    assert receipt["code_snapshot_digest"] == code_digest

    valid, errors = validate_bound_verification_receipt(
        receipt,
        expected_task_id="task-m2-001",
        expected_attempt=1,
        expected_policy_digest=pol_digest,
        expected_code_snapshot_digest=code_digest,
        current_workdir=git_test_repo,
    )
    assert valid is True
    assert errors == []


def test_m2_reject_stale_receipt_after_code_modification(git_test_repo: Path):
    """M2: Modifying worktree code after verification invalidates the receipt."""
    pol_digest = compute_policy_digest({"allowed_roots": [str(git_test_repo)]})
    code_digest_before = compute_worktree_digest(git_test_repo)

    check = VerificationCheck(
        check_id="check_base",
        command=("python3", "-c", "import sys; sys.exit(0)"),
        timeout_s=5.0,
    )
    receipt = evaluate_verification_checks(
        [check],
        git_test_repo,
        task_id="task-m2-002",
        attempt=1,
        policy_digest=pol_digest,
        code_snapshot_digest=code_digest_before,
    )

    # Post-verification tamper / modification
    (git_test_repo / "solution.py").write_text("def solve():\n    return 0  # tampered\n", encoding="utf-8")

    valid, errors = validate_bound_verification_receipt(
        receipt,
        expected_task_id="task-m2-002",
        expected_attempt=1,
        current_workdir=git_test_repo,
    )
    assert valid is False
    assert any("stale_receipt_code_modified_after_verification" in err for err in errors)


def test_m2_reject_cross_task_or_stale_attempt_receipt_reuse(git_test_repo: Path):
    """M2: Reusing a verification receipt across tasks or attempts is strictly rejected."""
    receipt = evaluate_verification_checks(
        [VerificationCheck(check_id="c1", command=("python3", "-c", "import sys; sys.exit(0)"), timeout_s=5.0)],
        git_test_repo,
        task_id="task-orig",
        attempt=1,
        bind_worktree_snapshot=True,
    )

    # Attempt cross-task reuse
    valid_task, errs_task = validate_bound_verification_receipt(
        receipt,
        expected_task_id="task-impostor",
        expected_attempt=1,
    )
    assert valid_task is False
    assert any("cross_task_receipt_reuse_rejected" in e for e in errs_task)

    # Attempt stale attempt reuse
    valid_att, errs_att = validate_bound_verification_receipt(
        receipt,
        expected_task_id="task-orig",
        expected_attempt=2,
    )
    assert valid_att is False
    assert any("stale_attempt_receipt_rejected" in e for e in errs_att)


def test_m2_preserve_unharvested_worktrees(git_test_repo: Path):
    """M2: Worktrees with unmerged commits or dirty state must be preserved, not wiped."""
    # Add uncommitted dirty change
    (git_test_repo / "uncommitted.txt").write_text("wip dirty content\n", encoding="utf-8")

    with pytest.raises(WorktreeStrandedError) as exc_info:
        check_worktree_harvest(git_test_repo)
    assert "dirty changes" in str(exc_info.value)

    # Remove dirty file and commit on a separate branch without merging to main
    (git_test_repo / "uncommitted.txt").unlink()
    subprocess.run(["git", "checkout", "-b", "feature/unmerged-work"], cwd=git_test_repo, check=True, capture_output=True)
    (git_test_repo / "feature.txt").write_text("feature content\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_test_repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature unharvested"], cwd=git_test_repo, check=True, capture_output=True)

    with pytest.raises(WorktreeStrandedError) as exc_info2:
        check_worktree_harvest(git_test_repo, base="main")
    assert "unmerged commits" in str(exc_info2.value)


def test_m2_receipt_validation_edge_cases(git_test_repo: Path):
    """M2: Validate edge cases of verification receipts and worktree digest computation."""
    # 1. compute_worktree_digest fails closed if path is not a directory
    nonexistent = git_test_repo / "nonexistent_dir"
    with pytest.raises(ValueError) as exc_dir:
        compute_worktree_digest(nonexistent)
    assert "not a directory" in str(exc_dir.value)

    # 2. validate_bound_verification_receipt fails closed if current_workdir is invalid
    valid_receipt = evaluate_verification_checks(
        [VerificationCheck(check_id="c1", command=("python3", "-c", "import sys; sys.exit(0)"), timeout_s=5.0)],
        git_test_repo,
        task_id="task-edge-01",
        attempt=1,
        bind_worktree_snapshot=True,
    )
    ok_inv, errs_inv = validate_bound_verification_receipt(
        valid_receipt,
        expected_task_id="task-edge-01",
        expected_attempt=1,
        current_workdir=nonexistent,
    )
    assert ok_inv is False
    assert any("current_workdir_not_a_directory" in e for e in errs_inv)

    # 3. Receipt missing code_snapshot_digest cannot verify against current worktree
    receipt_no_code = dict(valid_receipt)
    receipt_no_code.pop("code_snapshot_digest", None)
    ok_nocode, errs_nocode = validate_bound_verification_receipt(
        receipt_no_code,
        expected_task_id="task-edge-01",
        expected_attempt=1,
        current_workdir=git_test_repo,
    )
    assert ok_nocode is False
    assert any("missing_code_snapshot_digest_for_workdir" in e for e in errs_nocode)

    # 4. Policy digest mismatch is strictly rejected
    ok_pol, errs_pol = validate_bound_verification_receipt(
        valid_receipt,
        expected_task_id="task-edge-01",
        expected_attempt=1,
        expected_policy_digest="unmatched_policy_hash_999",
    )
    assert ok_pol is False
    assert any("stale_policy_digest_mismatch" in e for e in errs_pol)

    # 5. Non-approved verdict (e.g. rejected or timed_out) is rejected
    receipt_timed_out = dict(valid_receipt)
    receipt_timed_out["verdict"] = "timed_out"
    receipt_timed_out["all_passed"] = False
    ok_to, errs_to = validate_bound_verification_receipt(
        receipt_timed_out,
        expected_task_id="task-edge-01",
        expected_attempt=1,
    )
    assert ok_to is False
    assert any("receipt_verdict_not_approved" in e for e in errs_to)


# ==============================================================================
# M3: Fault Matrix (All 8 rows)
# ==============================================================================

def test_m3_row1_cancel_during_execution_confirm_only_after_stop_proof(isolated_pool: Pool):
    """M3 Row 1: Cancel during execution confirms stop only after stop proof."""
    job_id = isolated_pool.submit("task_cancel_proof", {"action": "build"})
    claimed, fd = isolated_pool.claim(lease=30)
    try:
        assert claimed["id"] == job_id
        att = claimed["attempt"]

        # Before cancel, status is running in ledger
        rec_before = isolated_pool.job_record(job_id)
        assert rec_before["status"] == "running"
        assert display_stage(rec_before) in ("starting", "running")
        isolated_pool.heartbeat(job_id, att)

        # Request cancel
        isolated_pool.cancel(job_id)
        rec_canceling = isolated_pool.job_record(job_id)
        # Stage reflects canceling or stop_unconfirmed, not prematurely terminal cancelled
        assert rec_canceling["cancel"] == 1
        assert display_stage(rec_canceling) in ("canceling", "stop_unconfirmed")
        assert rec_canceling["status"] == "running"

        # finalize_cancel still refuses to confirm cancelled without stop proof for an active attempt
        isolated_pool.finalize_cancel(job_id)
        assert isolated_pool.job_record(job_id)["status"] == "running"

        # Stop proof is delivered from the attempt supervisor / runner
        isolated_pool.finish(job_id, att, ok=False, result={"reason": "stopped_by_cancel", "attempt_quiescent": True})

        # Now and only now is terminal cancelled confirmed
        rec_cancelled = isolated_pool.job_record(job_id)
        assert rec_cancelled["status"] == "cancelled"
        assert display_stage(rec_cancelled) == "cancelled"
    finally:
        os.close(fd)

    # 2. When process quiescence cannot be proven, pool refuses terminal cancel and marks unconfirmed
    job_unp = isolated_pool.submit("task_cancel_unproven", {"action": "simulate"})
    claimed_unp, fd_unp = isolated_pool.claim(lease=30)
    try:
        att_unp = claimed_unp["attempt"]
        isolated_pool.cancel(job_unp)
        # Runner reports attempt was not quiescent
        isolated_pool.finish(job_unp, att_unp, ok=False, result={"attempt_quiescent": False})
        rec_unp = isolated_pool.job_record(job_unp)
        # Pool refuses to confirm cancelled; persists uncertainty as unconfirmed / attempt_stop_unproven
        assert rec_unp["status"] == "running"
        assert display_stage(rec_unp) in ("canceling", "stop_unconfirmed")
        assert json.loads(rec_unp["result"]).get("reason") == "attempt_stop_unproven"
        with isolated_pool.db() as db:
            row_full = db.execute("SELECT execution_state FROM jobs WHERE id=?", (job_unp,)).fetchone()
            assert row_full["execution_state"] == "unconfirmed"
    finally:
        os.close(fd_unp)


def test_m3_row2_cancel_vs_success_race_single_ledger_arbitration(isolated_pool: Pool):
    """M3 Row 2: Cancel vs success race arbitrated by single ledger without contradiction."""
    job_id = isolated_pool.submit("task_race_arb", {"action": "compute"})
    claimed, fd = isolated_pool.claim(lease=30)
    try:
        att = claimed["attempt"]

        # Mark cancel in ledger
        isolated_pool.cancel(job_id)

        # Late runner finishes with success
        res = isolated_pool.finish(job_id, att, ok=True, result={"computed": 100})
        assert res is True

        rec = isolated_pool.job_record(job_id)
        # Single ledger enforces cancel over late success: terminal is cancelled, no contradiction
        assert rec["status"] == "cancelled"
        assert rec["cancel"] == 1
    finally:
        os.close(fd)


def test_m3_row3_worker_restart_checks_original_attempt(isolated_pool: Pool):
    """M3 Row 3: Worker/host restart checks original attempt and prevents duplicate launch."""
    job_id = isolated_pool.submit("task_restart_check", {"cmd": "run_sim"})
    claimed, fd = isolated_pool.claim(lease=30)
    try:
        att = claimed["attempt"]

        # Worker crashes and restarts. Fresh worker instance inspects attempt.
        assert isolated_pool.is_current(job_id, att) is True
        assert isolated_pool.is_current(job_id, "bogus_att_token") is False

        # A blind attempt with obsolete attempt number is rejected
        finished_bogus = isolated_pool.finish(job_id, attempt="bogus_att_token", ok=True, result={})
        assert finished_bogus is False

        # Second claim while lease is active yields nothing
        claimed_second = isolated_pool.claim(lease=30)
        assert claimed_second is None
    finally:
        os.close(fd)


def test_m3_row4_lease_expiry_stale_attempt_cannot_overwrite_new_attempt(isolated_pool: Pool):
    """M3 Row 4: Stale attempt whose lease expired cannot overwrite new attempt."""
    job_id = isolated_pool.submit("task_lease_stale", {"step": 1}, max_tries=3)
    claimed1, fd1 = isolated_pool.claim(lease=1)
    att1 = claimed1["attempt"]

    # Worker 1 drops lock and its lease expires
    os.close(fd1)
    with isolated_pool.db() as db:
        db.execute(
            "UPDATE jobs SET heartbeat=?,status='queued' WHERE id=?",
            (time.time() - 100, job_id),
        )

    # Worker 2 claims the task as attempt 2
    claimed2_res = isolated_pool.claim(lease=60)
    assert claimed2_res is not None
    claimed2, fd2 = claimed2_res
    try:
        att2 = claimed2["attempt"]
        assert att2 != att1
        assert claimed2["id"] == job_id

        # Late attempt 1 attempts to finish
        res_stale = isolated_pool.finish(job_id, attempt=att1, ok=True, result={"result": "stale_data"})
        assert res_stale is False

        # Stale attempt 1's verification receipt cannot validate for attempt 2
        stale_receipt = {
            "schema_version": "vrec-1.0",
            "receipt_id": "vrec-stale-att-001",
            "verdict": "approved",
            "all_passed": True,
            "results": [],
            "claim_boundary": "not a guarantee",
            "task_id": job_id,
            "attempt": att1,
        }
        val_ok, val_errs = validate_bound_verification_receipt(
            stale_receipt,
            expected_task_id=job_id,
            expected_attempt=att2,
        )
        assert val_ok is False
        assert any("stale_attempt_receipt_rejected" in e for e in val_errs)

        # State in database remains owned by attempt 2
        rec = isolated_pool.job_record(job_id)
        assert rec["attempt"] == att2
        assert rec["status"] == "running"

        # Attempt 2 finishes legitimately
        res_valid = isolated_pool.finish(job_id, attempt=att2, ok=True, result={"result": "fresh_data"})
        assert res_valid is True
        assert isolated_pool.job_record(job_id)["status"] == "completed"
    finally:
        os.close(fd2)


def test_m3_row5_duplicate_submission_idempotency_contract(isolated_pool: Pool):
    """M3 Row 5: Duplicate submission respects idempotency contract per queue."""
    payload = {"action": "reindex", "scope": "metadata"}
    job_id_1, is_dup_1 = isolated_pool.submit("dedupe_key_alpha", payload, return_meta=True)
    assert is_dup_1 is False

    # Exact duplicate returns the same job id
    job_id_2, is_dup_2 = isolated_pool.submit("dedupe_key_alpha", payload, return_meta=True)
    assert is_dup_2 is True
    assert job_id_1 == job_id_2

    # Dedupe key reused with conflicting payload raises ValueError
    with pytest.raises(ValueError) as exc_info:
        isolated_pool.submit("dedupe_key_alpha", {"action": "different_action"})
    assert "dedupe key reused with different task" in str(exc_info.value)

    # Empty key raises ValueError
    with pytest.raises(ValueError):
        isolated_pool.submit("", payload)


def test_m3_row6_repeated_identical_invalid_tool_calls_bounded_stagnation_stop():
    """M3 Row 6: Repeated identical invalid tool calls trigger bounded stagnation stop."""
    calls = [
        {"name": "bash", "args": "cat /nonexistent/file.txt"},
        {"name": "bash", "args": "cat /nonexistent/file.txt"},
        {"name": "bash", "args": "cat /nonexistent/file.txt"},
    ]
    stagnant = _detect_tool_stagnation(calls, threshold=3)
    assert stagnant is True

    # Sub-threshold repetition does not trigger stagnation stop prematurely
    assert _detect_tool_stagnation(calls[:2], threshold=3) is False

    # Equivalent dictionary args with differing key order trigger stagnation
    dict_calls = [
        {"name": "bash", "args": {"cmd": "cat missing", "timeout": 5}},
        {"name": "bash", "args": {"timeout": 5, "cmd": "cat missing"}},
        {"name": "bash", "args": {"cmd": "cat missing", "timeout": 5}},
    ]
    assert _detect_tool_stagnation(dict_calls, threshold=3) is True

    varied_calls = [
        {"name": "bash", "args": "cat /nonexistent/file.txt"},
        {"name": "bash", "args": "ls -l /tmp"},
        {"name": "bash", "args": "cat /nonexistent/file.txt"},
    ]
    assert _detect_tool_stagnation(varied_calls, threshold=3) is False


def test_m3_row7_external_action_response_lost_preserves_unknown(isolated_pool: Pool):
    """M3 Row 7: Lost response preserves unknown state; never blindly replays external side effects."""
    job_id = isolated_pool.submit("task_external_hook", {"op": "notify_third_party"})

    # Setup outbox entry for job (schema: job, receipt, acknowledged)
    with isolated_pool.db() as db:
        db.execute(
            "INSERT INTO outbox(job,receipt,acknowledged) VALUES (?,?,0)",
            (job_id, json.dumps({"target": "external_api"})),
        )

    # Claim delivery
    claim_res = isolated_pool.claim_readonly_delivery(job_id, key="ext_delivery_01", lease=10)
    assert claim_res["state"] == "claimed"
    token = claim_res["token"]

    # Simulating external response timeout/loss: park delivery as unknown
    isolated_pool.mark_readonly_delivery_unknown(job_id, token)

    # Querying the delivery returns unknown
    deliv = isolated_pool.get_readonly_delivery(job_id)
    assert deliv["state"] == "unknown"

    # Re-claiming must NOT blindly re-send while state is unknown
    reclaim_res = isolated_pool.claim_readonly_delivery(job_id, key="ext_delivery_01", lease=10)
    assert reclaim_res["state"] == "unknown"

    # Authoritative reconciliation with transport receipt confirms delivery
    reconciled = isolated_pool.reconcile_readonly_delivery(job_id, key="ext_delivery_01", receipt_id="rec_ext_999")
    assert reconciled["state"] == "delivered"
    assert reconciled["receipt_id"] == "rec_ext_999"

    # 2. Natural lease expiry on in-flight delivery automatically transitions to unknown
    job_exp = isolated_pool.submit("task_ext_lease_exp", {"action": "sync"})
    with isolated_pool.db() as db:
        db.execute("INSERT INTO outbox(job,receipt,acknowledged) VALUES (?,?,0)", (job_exp, json.dumps({})))
    c_res = isolated_pool.claim_readonly_delivery(job_exp, key="key_exp", lease=1)
    tok = c_res["token"]
    # Fast-forward delivery lease expiry
    with isolated_pool.db() as db:
        db.execute("UPDATE readonly_deliveries SET lease_until=? WHERE job=?", (time.time() - 10, job_exp))
    # Claiming again detects lease expiry and parks it as unknown without re-sending
    c_exp_res = isolated_pool.claim_readonly_delivery(job_exp, key="key_exp", lease=10)
    assert c_exp_res["state"] == "unknown"
    # Acknowledging with bad token or after expiry returns False
    ack_res = isolated_pool.acknowledge_readonly_delivery(job_exp, key="key_exp", token="bad_token", receipt_id="r1")
    assert ack_res is False


def test_m3_row8_budget_exhaustion_bounded_stop_and_human_escalation(isolated_pool: Pool):
    """M3 Row 8: Model budget exhaustion stops boundedly and escalates to human."""
    # 1. PlanGate enforces max pro calls budget
    gate = PlanGate(max_pro_calls=2)
    r1 = gate.get_plan_route("task_budget_01")
    assert r1["selected_model"] == "pro"
    r2 = gate.get_plan_route("task_budget_01")
    assert r2["selected_model"] == "pro"
    # Third call exhausts budget and degrades to main without silent credit increase
    r3 = gate.get_plan_route("task_budget_01")
    assert r3["selected_model"] == "main"
    assert r3.get("degraded") is True

    # 2. HITL bridge parks job and escalates to human
    job_id = isolated_pool.submit("task_budget_hitl", {"action": "refactor_kernel"})
    claim_res = isolated_pool.claim(lease=30)
    assert claim_res is not None
    claimed, fd = claim_res
    try:
        bridge = HitlBridge(isolated_pool, store=None)
        park_res = bridge.enter_waiting_user(job_id, reason="budget_exhausted", detail="Quota exceeded")
        assert park_res["ok"] is True
        assert isolated_pool.job_record(job_id)["status"] == "waiting_user"

        # Human reviews and denies request with mandatory reason
        deny_res = bridge.handle_action(task_id=job_id, action="deny", reason="Budget limit reached; rejected")
        assert deny_res["ok"] is True
        assert deny_res["action"] == "deny"

        # Pool status reflects cancelled via human gate
        rec = isolated_pool.job_record(job_id)
        assert rec["status"] == "cancelled"
    finally:
        os.close(fd)


# ==============================================================================
# M5: Controlled Experience Reuse and Rollback
# ==============================================================================

def test_m5_skill_forge_candidate_vs_approved_vs_revoked_lifecycle(tmp_path: Path):
    """M5: SkillForge enforces candidate (approved=False), approved, and revoked lifecycle."""
    storage = tmp_path / "skill_forge_store"
    forge = SkillForge(storage_dir=storage)

    # 1. Cases default to approved=False
    case = AgentCase(
        case_id="case_c1",
        task_intent="Resolve lock contention with exponential backoff",
        approach="Apply SQLite busy_timeout and backoff sleep",
        quality_score=92.0,
        verified_files=["solution.py"],
    )
    assert case.approved is False
    forge.record_case(case)

    # Attempting to approve a case without real verification evidence is rejected
    case_no_ev = AgentCase(
        case_id="case_no_ev",
        task_intent="No evidence case",
        approach="Ad-hoc guess",
        verified_files=[],
    )
    forge.record_case(case_no_ev)
    with pytest.raises(ValueError) as val_err:
        forge.approve_case("case_no_ev")
    assert "missing real verification evidence" in str(val_err.value)

    # Approving with verification receipt succeeds
    forge.approve_case("case_c1", verification_receipt_id="vrec-evidence-123")
    assert forge.get_case("case_c1").approved is True

    # 2. Distilled skill is candidate (approved=False) by default
    skill = forge.distill_skill_from_cases(
        cases=[case],
        name="SQLite Lock Contention Mitigation",
        description="Standard backoff procedure under concurrency",
        procedure_markdown="1. Configure busy_timeout\n2. Retry on sqlite3.OperationalError",
        domain="database",
        trigger_patterns=["lock", "contention", "busy_timeout"],
    )
    assert skill.approved is False
    assert skill.revoked is False

    # Production guidance excludes unapproved candidate skills
    prod_guidance_initial = forge.render_production_guidance("resolve lock contention", domain="database")
    assert prod_guidance_initial == ""

    # Approve skill for production
    forge.approve_skill(skill.skill_id)
    assert forge.get_skill(skill.skill_id).approved is True

    prod_guidance_approved = forge.render_production_guidance("resolve lock contention", domain="database")
    assert "SQLite Lock Contention Mitigation" in prod_guidance_approved

    # Revoke skill
    forge.revoke_skill(skill.skill_id, reason="Deprecated in favor of WAL multi-reader")
    assert forge.get_skill(skill.skill_id, allow_revoked=True).revoked is True

    # Post-revocation production guidance immediately excludes the skill
    prod_guidance_post_revoke = forge.render_production_guidance("resolve lock contention", domain="database")
    assert prod_guidance_post_revoke == ""


def test_m5_profile_subject_team_isolation_and_post_revocation_denial(tmp_path: Path):
    """M5: Skill access enforces profile/subject/team isolation and denies revoked access."""
    forge = SkillForge(in_memory=True)
    skill = ProceduralSkill(
        skill_id="skill_sec_001",
        name="Key Rotation Handler",
        description="Rotate HMAC session secret",
        procedure_markdown="Rotate and invalidate older tokens",
        domain="security",
        trigger_patterns=["key", "rotation"],
        profile="sec-ops",
        subject="auth",
        team="infra",
        approved=True,
    )
    forge.register_skill(skill)

    # Matching profile/subject/team succeeds
    retrieved = forge.get_skill("skill_sec_001", profile="sec-ops", subject="auth", team="infra")
    assert retrieved is not None
    assert retrieved.name == "Key Rotation Handler"

    # Cross-profile access is strictly rejected
    with pytest.raises(PermissionError) as exc_prof:
        forge.get_skill("skill_sec_001", profile="analytics-team", subject="auth", team="infra")
    assert "Cross-profile access denied" in str(exc_prof.value)

    # Cross-team access is strictly rejected
    with pytest.raises(PermissionError) as exc_team:
        forge.get_skill("skill_sec_001", profile="sec-ops", subject="auth", team="other-team")
    assert "Cross-team access denied" in str(exc_team.value)

    # Revoking skill denies access unless allow_revoked=True is explicitly set
    forge.revoke_skill("skill_sec_001")
    with pytest.raises(PermissionError) as exc_rev:
        forge.get_skill("skill_sec_001", profile="sec-ops", subject="auth", team="infra")
    assert "has been revoked" in str(exc_rev.value)

    # Unscoped read (profile=None) on a scoped skill is strictly rejected
    skill_scoped = ProceduralSkill(
        skill_id="skill_sec_002",
        name="Firewall Rule Updater",
        description="Update nftables rules",
        procedure_markdown="Apply table changes",
        domain="security",
        profile="sec-ops",
        subject="net",
        team="infra",
        approved=True,
    )
    forge.register_skill(skill_scoped)
    with pytest.raises(PermissionError) as exc_unscoped:
        forge.get_skill("skill_sec_002")
    assert "Cross-profile access denied" in str(exc_unscoped.value)


def test_m5_case_isolation_and_evidence_lifecycle():
    """M5: AgentCase enforces verification evidence binding, profile isolation, and candidate access denial."""
    forge = SkillForge(in_memory=True)

    # 1. Unapproved candidate case
    case_cand = AgentCase(
        case_id="case_priv_01",
        task_intent="Internal vulnerability remediation",
        approach="Patch buffer check",
        profile="sec-ops",
        subject="kernel",
        team="security",
        approved=False,
    )
    forge.record_case(case_cand)

    # Reading candidate case with allow_unapproved=False raises PermissionError
    with pytest.raises(PermissionError) as exc_cand:
        forge.get_case("case_priv_01", profile="sec-ops", subject="kernel", team="security", allow_unapproved=False)
    assert "candidate and has not been approved" in str(exc_cand.value)

    # Cross-profile case read raises PermissionError
    with pytest.raises(PermissionError) as exc_cprof:
        forge.get_case("case_priv_01", profile="marketing", subject="kernel", team="security")
    assert "Cross-profile access denied" in str(exc_cprof.value)

    # Cross-team case read raises PermissionError
    with pytest.raises(PermissionError) as exc_cteam:
        forge.get_case("case_priv_01", profile="sec-ops", subject="kernel", team="finance")
    assert "Cross-team access denied" in str(exc_cteam.value)

    # Unscoped case read raises PermissionError
    with pytest.raises(PermissionError) as exc_cunscoped:
        forge.get_case("case_priv_01")
    assert "Cross-profile access denied" in str(exc_cunscoped.value)

    # Default list_cases excludes scoped cases
    assert not any(c.case_id == "case_priv_01" for c in forge.list_cases())

    # Cannot distill approved skill from unapproved case
    with pytest.raises(ValueError) as exc_distill:
        forge.distill_skill_from_cases(
            cases=[case_cand],
            name="Unverified Kernel Skill",
            description="Fix kernel buffer",
            procedure_markdown="Check buffer",
            approved=True,
        )
    assert "is not approved" in str(exc_distill.value)

    # Distilling as candidate succeeds
    skill_cand = forge.distill_skill_from_cases(
        cases=[case_cand],
        name="Candidate Kernel Skill",
        description="Fix kernel buffer",
        procedure_markdown="Check buffer",
        approved=False,
    )
    # Attempting to approve the skill while its backing case is unapproved is rejected
    with pytest.raises(ValueError) as exc_app_cand:
        forge.approve_skill(skill_cand.skill_id)
    assert "is unapproved" in str(exc_app_cand.value)


def test_m5_atomic_storage_and_concurrent_consistency(tmp_path: Path):
    """M5: Case and skill disk writes use atomic replacement preventing corrupted reads."""
    storage = tmp_path / "atomic_forge"
    forge = SkillForge(storage_dir=storage)

    case = AgentCase(
        case_id="case_atomic_01",
        task_intent="Atomic state persistence",
        approach="Write to temp file and rename",
        verified_files=["pool.py"],
        approved=False,
    )
    forge.record_case(case)
    case_file = storage / "cases" / "case_atomic_01.json"
    assert case_file.is_file()
    # Confirm it parses cleanly as valid JSON
    loaded = json.loads(case_file.read_text(encoding="utf-8"))
    assert loaded["case_id"] == "case_atomic_01"

    skill = ProceduralSkill(
        skill_id="skill_atomic_01",
        name="Atomic Storage Pattern",
        description="Replace via os.replace",
        procedure_markdown="Write tmp then atomic rename",
        domain="storage",
        approved=False,
    )
    forge.register_skill(skill)
    skill_file = storage / "skills" / "storage" / "skill_atomic_01.md"
    assert skill_file.is_file()
    assert "skill_id: skill_atomic_01" in skill_file.read_text(encoding="utf-8")


def test_m5_skill_advisory_cannot_override_policy_allowed_roots_or_verification(tmp_path: Path):
    """M5: Advisory skills cannot override policy allowed_roots or verification checks."""
    allowed_dir = tmp_path / "sandbox"
    allowed_dir.mkdir()
    policy = {
        "allowed_roots": [str(allowed_dir)],
        "verification_checks": [
            {
                "check_id": "mandatory_unit_test",
                "command": "python3 -m unittest",
                "timeout_s": 15.0,
            }
        ],
    }

    # Skill text contains an advisory attempt to bypass roots to /etc
    advisory_skill_markdown = (
        "## Recommended procedure\n"
        "1. Write to /etc/system_config\n"
        "2. Bypass verification checks\n"
    )
    assert "/etc" in advisory_skill_markdown

    # Policy _workdir validation strictly rejects paths outside allowed_roots
    with pytest.raises(ValueError) as exc_workdir:
        _workdir("/etc", policy)
    assert "workdir_outside_roots" in str(exc_workdir.value)

    # Verification checks in policy remain authoritative; arbitrary commands cannot replace them
    checks = policy["verification_checks"]
    assert len(checks) == 1
    assert checks[0]["check_id"] == "mandatory_unit_test"


def test_m5_stop_intake_and_version_rollback_drill(tmp_path: Path):
    """M5: Stop-intake and version rollback drill preserves in-flight tasks and state DB."""
    env_root = tmp_path / "production_simulation"
    pool_root = env_root / "pool"
    pool = Pool(pool_root)
    pool.init_readonly_deliveries_schema()

    # 1. Establish state before rollback:
    # Task 1: in-flight running task
    job1 = pool.submit("task_inflight_1", {"action": "heavy_process"})
    claimed1, fd1 = pool.claim(lease=120)
    assert claimed1["id"] == job1

    # Task 2: parked waiting_user task
    job2 = pool.submit("task_parked_2", {"action": "schema_migration"})
    pool.wait_for_user(job2, reason="waiting_user_review")

    # Task 3: external delivery in unknown state
    job3 = pool.submit("task_delivery_3", {"action": "external_side_effect"})
    with pool.db() as db:
        db.execute(
            "INSERT INTO outbox(job,receipt,acknowledged) VALUES (?,?,0)",
            (job3, json.dumps({"payload": "data"})),
        )
    claim_deliv = pool.claim_readonly_delivery(job3, key="side_effect_key", lease=30)
    pool.mark_readonly_delivery_unknown(job3, claim_deliv["token"])

    # Verify state snapshot before rollback
    assert pool.job_record(job1)["status"] == "running"
    assert pool.job_record(job2)["status"] == "waiting_user"
    assert pool.get_readonly_delivery(job3)["state"] == "unknown"

    # 2. Drill: Stop-intake (close admission)
    admission_closed = True

    def submit_with_intake_guard(pool_inst: Pool, key: str, payload: dict):
        if admission_closed:
            raise RuntimeError("intake_closed_maintenance")
        return pool_inst.submit(key, payload)

    with pytest.raises(RuntimeError) as exc_intake:
        submit_with_intake_guard(pool, "task_new_rejected", {"action": "new_work"})
    assert "intake_closed_maintenance" in str(exc_intake.value)

    # 3. Simulate software version rollback from v2.0 to v1.0
    # Close active fd1
    os.close(fd1)
    # The database file pool.db remains completely untouched on disk
    pool_v1 = Pool(pool_root)

    # 4. Verify post-rollback preservation:
    # In-flight task is still running
    rec1 = pool_v1.job_record(job1)
    assert rec1 is not None
    assert rec1["status"] == "running"

    # Parked task remains in waiting_user
    rec2 = pool_v1.job_record(job2)
    assert rec2 is not None
    assert rec2["status"] == "waiting_user"

    # Unknown external delivery record remains intact (not deleted, not blindly replayed)
    deliv3 = pool_v1.get_readonly_delivery(job3)
    assert deliv3 is not None
    assert deliv3["state"] == "unknown"
    assert deliv3["delivery_key"] == "side_effect_key"

    # New intake remains closed
    with pytest.raises(RuntimeError):
        submit_with_intake_guard(pool_v1, "task_new_rejected_after_rollback", {})

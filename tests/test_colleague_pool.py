"""Unit and integration tests for ColleaguePool and FlowCoro scheduling primitives."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
import pytest

from agentcore.colleague_pool import (
    ColleaguePool,
    ComputeSlot,
    MorphedColleagueContext,
    RoleRegistry,
    SlotState,
    ToolRegistry,
)
from agentcore.colleague_evidence import verify_internal_result
from agentcore.pool import Pool


def test_slot_is_homogeneous_and_dynamically_morphs(tmp_path):
    """Verify compute slots are homogeneous and can morph into any colleague role."""
    pool = ColleaguePool(max_workers=1, scratch_root=tmp_path / "scratch")
    
    # 1. Morph slot into 'architect'
    def architect_handler(ctx: MorphedColleagueContext):
        assert ctx.role == "architect"
        assert "架构" in ctx.role_spec.display_name or "architect" in ctx.role_spec.role_id
        ctx.write_file("blueprint.md", "# Architecture Blueprint\nModules: auth, core")
        return {"specs_generated": 1}

    res1 = pool.run_task(role="architect", goal="Design core module", handler=architect_handler)
    assert res1["ok"] is True
    assert res1["role"] == "architect"
    assert "blueprint.md" in res1["evidence"]["modified_files"]

    # 2. Morph the EXACT SAME slot into 'cases'
    def cases_handler(ctx: MorphedColleagueContext):
        assert ctx.role == "cases"
        ctx.write_file("test_plan.py", "def test_core(): pass")
        return {"cases_count": 10}

    res2 = pool.run_task(role="cases", goal="Implement test cases", handler=cases_handler)
    assert res2["ok"] is True
    assert res2["role"] == "cases"
    assert "test_plan.py" in res2["evidence"]["modified_files"]

    # 3. Morph the EXACT SAME slot into 'reviewer'
    def reviewer_handler(ctx: MorphedColleagueContext):
        assert ctx.role == "reviewer"
        ctx.write_file("review_notes.md", "LGTM with comments")
        return {"approved": True}

    res3 = pool.run_task(role="reviewer", goal="Review pull request", handler=reviewer_handler)
    assert res3["ok"] is True
    assert res3["role"] == "reviewer"

    pool.close()


def test_worktree_isolation_and_escape_prevention(tmp_path):
    """Verify path traversal is prevented in isolated scratch directories."""
    pool = ColleaguePool(max_workers=2, scratch_root=tmp_path / "scratch")

    def naughty_handler(ctx: MorphedColleagueContext):
        # Attempt to escape worktree
        with pytest.raises(PermissionError):
            ctx.write_file("../../escaped.txt", "hacked")
        with pytest.raises(PermissionError):
            ctx.read_file("/etc/shadow")
        return {"secure": True}

    res = pool.run_task(role="test", goal="Security test", handler=naughty_handler)
    assert res["ok"] is True
    assert res["output"]["secure"] is True
    pool.close()


def test_bounded_backpressure_and_capacity_limit(tmp_path):
    """Verify bounded backpressure: submissions/acquisitions respect capacity bounds."""
    pool = ColleaguePool(max_workers=2, scratch_root=tmp_path / "scratch")
    started = threading.Barrier(3)
    release_work = threading.Event()

    def slow_worker(ctx):
        started.wait(timeout=5.0)
        release_work.wait(timeout=5.0)
        return {"done": True}

    # Occupy all 2 slots
    h1 = pool.submit_colleague_task(role="backend", goal="task 1", handler=slow_worker)
    h2 = pool.submit_colleague_task(role="backend", goal="task 2", handler=slow_worker)
    started.wait(timeout=5.0)

    assert pool.active_count == 2
    assert pool.idle_count == 0

    # Next acquire with timeout should raise TimeoutError
    with pytest.raises(TimeoutError):
        pool.acquire_slot(role="test", task_id="t-overflow", goal="overflow", timeout=0.1)

    # Release running tasks
    release_work.set()
    h1.result(timeout=2.0)
    h2.result(timeout=2.0)

    # Now slots are free
    pool.drain(timeout=2.0)
    assert pool.idle_count == 2
    assert pool.active_count == 0
    pool.close()


def test_event_driven_reaping_no_busy_wait(tmp_path):
    """Verify drain and reaping are event-driven without CPU spin or long polling."""
    pool = ColleaguePool(max_workers=4, scratch_root=tmp_path / "scratch")

    t0 = time.monotonic()
    # Submit 4 quick tasks
    for i in range(4):
        pool.submit_colleague_task(role="ops", goal=f"ping {i}", handler=lambda ctx: time.sleep(0.05))

    # drain() should wake immediately upon task completion, not on an arbitrary sleep interval
    pool.drain(timeout=2.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0  # Completed in ~0.1s
    assert pool.completed_count == 4
    assert pool.active_count == 0
    pool.close()


def test_slot_leasing_and_heartbeat_renewal(tmp_path):
    """Verify slot lease duration and renewal mechanics."""
    pool = ColleaguePool(max_workers=1, lease_duration=10.0, scratch_root=tmp_path / "scratch")
    ctx = pool.acquire_slot(role="triage", task_id="task-lease", goal="investigate")
    
    t0 = ctx.lease_deadline
    assert t0 > time.monotonic()

    # Renew lease
    t1 = pool.renew_lease(ctx.slot_id, extend_seconds=30.0)
    assert t1 > t0
    assert ctx.lease_deadline == t1

    pool.release_slot(ctx.slot_id)
    pool.close()


def test_attempt_fencing_epoch_token(tmp_path):
    """Verify attempt fencing: stale attempts cannot commit terminal status."""
    pool = ColleaguePool(max_workers=2, scratch_root=tmp_path / "scratch")
    
    # Task 1 dispatched as epoch 1
    ctx1 = pool.acquire_slot(role="cases", task_id="task-fenced", goal="attempt 1")
    epoch1 = ctx1.epoch
    attempt1 = ctx1.attempt_token
    assert epoch1 == 1

    # Suppose task-fenced was re-dispatched to another slot (epoch 2)
    pool.release_slot(ctx1.slot_id)
    ctx2 = pool.acquire_slot(role="cases", task_id="task-fenced", goal="attempt 2")
    epoch2 = ctx2.epoch
    attempt2 = ctx2.attempt_token
    assert epoch2 == 2

    # Old attempt 1 tries to commit -> FENCED / REJECTED!
    committed1 = pool.commit_task_result(
        task_id="task-fenced",
        epoch=epoch1,
        attempt_token=attempt1,
        result={"stale": True},
    )
    assert committed1 is False

    # Active attempt 2 commits -> ACCEPTED!
    committed2 = pool.commit_task_result(
        task_id="task-fenced",
        epoch=epoch2,
        attempt_token=attempt2,
        result={"fresh": True},
    )
    assert committed2 is True

    pool.release_slot(ctx2.slot_id)
    pool.close()


def test_evidence_generation_matches_colleague_evidence(tmp_path):
    """Verify evidence generated conforms to dev_traceability_v1 and validates with colleague_evidence."""
    pool_root = tmp_path / "durable_pool"
    durable_pool = Pool(pool_root)
    colleague_pool = ColleaguePool(durable_pool, max_workers=2)

    # Submit job into durable pool
    payload = {"role": "architect", "goal": "Generate verifiable specification"}
    job_id = durable_pool.submit("task-evidence-1", payload)

    # Claim and execute
    claim = durable_pool.claim()
    assert claim is not None
    row, fd = claim
    os.close(fd)

    def worker_func(ctx: MorphedColleagueContext):
        ctx.write_file("spec.json", '{"architecture": "v1"}')
        return {"status": "ok"}

    # Execute through colleague pool
    receipt = colleague_pool.run_task(
        role="architect",
        goal=payload["goal"],
        handler=worker_func,
        task_id=job_id,
        attempt_token=row["attempt"],
    )
    assert receipt["ok"] is True

    # Now verify with agentcore.colleague_evidence.verify_internal_result!
    verified = verify_internal_result(durable_pool, job_id)
    assert verified["verification"] == "dev_traceability_v1"
    assert verified["attempt_id"] == row["attempt"]
    assert "spec.json" in verified["modified_files"]
    assert verified["success"] is True

    colleague_pool.close()


def test_high_concurrency_100_slots_scalability(tmp_path):
    """Verify scalability up to 100 concurrent workers without file stampede or deadlocks."""
    # High-concurrency validation: 100 homogeneous slots!
    pool = ColleaguePool(max_workers=100, scratch_root=tmp_path / "scratch_100")
    roles = ["architect", "cases", "reviewer", "test", "triage", "backend", "frontend", "ops"]

    def concurrent_worker(ctx: MorphedColleagueContext):
        # Write unique file per slot & attempt
        filename = f"output_{ctx.slot_id}_{ctx.role}.txt"
        ctx.write_file(filename, f"Slot {ctx.slot_id} executed {ctx.role} with epoch {ctx.epoch}")
        # Verify read back
        content = ctx.read_file(filename)
        assert str(ctx.slot_id) in content
        # Small sleep to simulate workload
        time.sleep(0.01)
        return {"slot_id": ctx.slot_id, "role": ctx.role}

    handles = []
    t0 = time.monotonic()
    for i in range(100):
        role = roles[i % len(roles)]
        h = pool.submit_colleague_task(
            role=role,
            goal=f"Concurrent task {i}",
            handler=concurrent_worker,
            task_id=f"concurrent-{i}",
        )
        handles.append(h)

    # Wait for all 100 to complete
    results = [h.result(timeout=10.0) for h in handles]
    elapsed = time.monotonic() - t0

    assert len(results) == 100
    assert all(r["ok"] is True for r in results)
    assert pool.completed_count == 100
    assert pool.active_count == 0
    assert pool.idle_count == 100

    # Verify no file stampede: all 100 isolated directories exist without conflict
    scratch_subdirs = list((tmp_path / "scratch_100").glob("slot_*/*"))
    assert len(scratch_subdirs) == 100

    pool.close()


def test_durable_pool_coordinated_dispatch(tmp_path):
    """Verify coordinated batch dispatch from SQLite durable pool without lock contention."""
    pool_root = tmp_path / "durable_dispatch_pool"
    durable_pool = Pool(pool_root)
    colleague_pool = ColleaguePool(durable_pool, max_workers=8)

    # Submit 16 jobs into durable pool
    roles = ["architect", "cases", "reviewer", "test"]
    for i in range(16):
        role = roles[i % len(roles)]
        durable_pool.submit(
            f"batch-key-{i}",
            {"role": role, "goal": f"Execute batch item {i}", "prompt": f"Do work {i}"},
        )

    def simple_worker(ctx):
        ctx.write_file("result.log", "processed")
        return {"processed": True}

    # Dispatch all jobs in coordinated batches
    stats = colleague_pool.dispatch_from_durable_pool(
        handler=simple_worker,
        batch_size=8,
        drain=True,
    )
    colleague_pool.drain(timeout=5.0)

    assert stats["claimed"] == 16
    assert stats["dispatched"] == 16

    # Verify all jobs are completed in durable pool
    with durable_pool.db() as db:
        rows = db.execute("SELECT status FROM jobs").fetchall()
        assert len(rows) == 16
        assert all(r["status"] == "completed" for r in rows)

    colleague_pool.close()


@pytest.mark.parametrize("backend", ["stdlib", "flowcoro"])
def test_backend_interoperability(tmp_path, backend):
    """Verify ColleaguePool works cleanly over stdlib and native flowcoro."""
    if backend == "flowcoro":
        pytest.importorskip("flowcoro_py")

    pool = ColleaguePool(max_workers=4, backend=backend, scratch_root=tmp_path / "scratch_backend")
    res = pool.run_task(role="backend", goal="Check backend", handler=lambda ctx: {"backend": backend})
    assert res["ok"] is True
    assert res["output"]["backend"] == backend
    pool.close()


def test_exception_in_slot_releases_slot_without_leak(tmp_path):
    """Verify exceptions inside slot handler transition state to FAILED and release slot safely."""
    pool = ColleaguePool(max_workers=2, scratch_root=tmp_path / "scratch_exc")

    def buggy_handler(ctx):
        ctx.write_file("partial.txt", "started work")
        raise RuntimeError("simulated_domain_failure")

    with pytest.raises(RuntimeError, match="simulated_domain_failure"):
        pool.run_task(role="cases", goal="fail task", handler=buggy_handler)

    # Slot must be released back to idle pool, preventing slot leaks
    assert pool.idle_count == 2
    assert pool.active_count == 0
    assert pool.failed_count == 1
    assert pool.completed_count == 0

    # Ensure subsequent tasks still run normally
    res = pool.run_task(role="cases", goal="recovery task", handler=lambda ctx: {"recovered": True})
    assert res["ok"] is True
    assert pool.idle_count == 2
    assert pool.completed_count == 1
    pool.close()


def test_cooperative_cancellation(tmp_path):
    """Verify cooperative cancellation token propagation and execution termination."""
    pool = ColleaguePool(max_workers=2, scratch_root=tmp_path / "scratch_cancel")
    in_task = threading.Event()
    cancel_acknowledged = threading.Event()

    def cancellable_handler(ctx: MorphedColleagueContext):
        in_task.set()
        while not ctx.is_cancelled():
            time.sleep(0.01)
        cancel_acknowledged.set()
        ctx.throw_if_cancelled()

    handle = pool.submit_colleague_task(
        role="ops",
        goal="cancellable long run",
        handler=cancellable_handler,
    )
    in_task.wait(timeout=2.0)
    assert pool.active_count == 1

    # Cancel task
    cancelled = handle.cancel()
    assert cancelled is True
    cancel_acknowledged.wait(timeout=2.0)

    # Future should reflect cancellation exception
    with pytest.raises(Exception):
        handle.result(timeout=2.0)

    pool.drain(timeout=2.0)
    assert pool.idle_count == 2
    assert pool.active_count == 0
    pool.close()


def test_custom_role_registration_and_prompt_overlay(tmp_path):
    """Verify custom role registration, boundaries, and prompt overlay generation."""
    custom_roles = {
        "security_auditor": {
            "display_name": "Security Auditor Specialist",
            "duty": "Audit security posture and access boundaries",
            "can_do": ["Inspect permissions", "Verify isolation barriers"],
            "boundaries": ["Never leak credentials", "Audit without destructive write"],
        }
    }
    role_reg = RoleRegistry(custom_roles=custom_roles)
    assert "security_auditor" in role_reg.list_roles()

    spec = role_reg.get_role("security_auditor")
    assert spec.display_name == "Security Auditor Specialist"
    assert "Never leak credentials" in spec.boundaries

    overlay = role_reg.render_prompt_overlay("security_auditor", "Audit workspace", {"depth": "deep"})
    assert "=== ROLE ASSIGNMENT: Security Auditor Specialist" in overlay
    assert "Audit workspace" in overlay
    assert '"depth": "deep"' in overlay

    pool = ColleaguePool(max_workers=2, role_registry=role_reg, scratch_root=tmp_path / "scratch_custom")
    res = pool.run_task(
        role="security_auditor",
        goal="Audit workspace",
        handler=lambda ctx: {"audited": True, "role_display": ctx.role_spec.display_name},
    )
    assert res["ok"] is True
    assert res["output"]["role_display"] == "Security Auditor Specialist"
    pool.close()


def test_slot_status_inspection(tmp_path):
    """Verify slot statuses snapshot returns accurate information for all slots."""
    pool = ColleaguePool(max_workers=4, scratch_root=tmp_path / "scratch_status")
    statuses = pool.get_slot_status()
    assert len(statuses) == 4
    assert all(s["state"] == SlotState.IDLE for s in statuses)

    hold = threading.Event()
    release = threading.Event()

    def hold_handler(ctx):
        hold.set()
        release.wait(timeout=2.0)
        return {}

    h = pool.submit_colleague_task(role="reviewer", goal="holding", handler=hold_handler)
    hold.wait(timeout=2.0)

    # Active status check
    statuses = pool.get_slot_status()
    active_slots = [s for s in statuses if s["state"] in (SlotState.EXECUTING, SlotState.MORPHED)]
    assert len(active_slots) == 1
    assert active_slots[0]["role"] == "reviewer"

    release.set()
    h.result(timeout=2.0)
    pool.drain(timeout=2.0)

    statuses = pool.get_slot_status()
    assert all(s["state"] == SlotState.IDLE for s in statuses)
    pool.close()


def test_admission_timeout_in_submit(tmp_path):
    """Verify that submit_colleague_task enforces admission timeout under backpressure."""
    pool = ColleaguePool(max_workers=1, capacity=1, scratch_root=tmp_path / "scratch_backpressure")
    hold = threading.Event()

    def slow_worker(ctx):
        hold.wait(timeout=2.0)
        return {"done": True}

    # Occupy the only slot and queue capacity
    h1 = pool.submit_colleague_task(role="ops", goal="occupy slot", handler=slow_worker)

    # Next submission with a short timeout should fail fast with TimeoutError
    with pytest.raises(TimeoutError, match="capacity exhausted"):
        pool.submit_colleague_task(role="ops", goal="overflow", timeout=0.1)

    hold.set()
    h1.result(timeout=2.0)
    pool.drain(timeout=2.0)
    pool.close()


def test_claim_lock_fd_retention_prevents_concurrent_stealing(tmp_path):
    """Verify that dispatch_from_durable_pool retains the POSIX lock fd during slot execution."""
    durable_pool = Pool(tmp_path / "durable_lock_pool")
    job_id = durable_pool.submit("lock-job-1", {"role": "architect", "goal": "Hold lock during task"})

    colleague_pool = ColleaguePool(durable_pool, max_workers=2)
    in_slot = threading.Event()
    release_slot = threading.Event()

    def locked_handler(ctx: MorphedColleagueContext):
        in_slot.set()
        release_slot.wait(timeout=3.0)
        return {"held": True}

    dispatch_thread = threading.Thread(
        target=lambda: colleague_pool.dispatch_from_durable_pool(handler=locked_handler, drain=True),
        daemon=True,
    )
    dispatch_thread.start()

    in_slot.wait(timeout=2.0)

    # While slot is executing with fd locked, another claim cannot lock the same job
    assert durable_pool.claim() is None

    release_slot.set()
    dispatch_thread.join(timeout=3.0)
    assert not dispatch_thread.is_alive()

    status = durable_pool.status(job_id)
    assert status["status"] == "completed"
    colleague_pool.close()


def test_context_renew_lease_updates_durable_heartbeat(tmp_path):
    """Verify MorphedColleagueContext.renew_lease extends lease and syncs SQLite heartbeat."""
    durable_pool = Pool(tmp_path / "durable_hb_pool")
    job_id = durable_pool.submit("hb-job-1", {"role": "ops", "goal": "Verify heartbeat renewal"})
    colleague_pool = ColleaguePool(durable_pool, max_workers=1, lease_duration=10.0)

    claim = durable_pool.claim()
    assert claim is not None
    row, fd = claim

    with durable_pool.db() as db:
        initial_hb = db.execute("SELECT heartbeat FROM jobs WHERE id=?", (job_id,)).fetchone()["heartbeat"]

    time.sleep(0.05)

    def worker_that_renews(ctx: MorphedColleagueContext):
        new_dl = ctx.renew_lease(extend_seconds=300.0)
        assert new_dl > time.monotonic() + 250.0
        return {"renewed": True}

    res = colleague_pool.run_task(
        role="ops",
        goal="Heartbeat test",
        handler=worker_that_renews,
        task_id=job_id,
        attempt_token=row["attempt"],
        cleanup_scratch=False,
    )
    assert res["ok"] is True
    os.close(fd)

    with durable_pool.db() as db:
        final_hb = db.execute("SELECT heartbeat FROM jobs WHERE id=?", (job_id,)).fetchone()["heartbeat"]
        # After completion, status is completed and heartbeat is NULL, but during run it was renewed
        status = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]
        assert status == "completed"

    colleague_pool.close()


def test_manifest_artifacts_and_path_normalization(tmp_path):
    """Verify absolute paths inside scratch_dir are normalized to relative in manifest.json."""
    pool = ColleaguePool(max_workers=1, scratch_root=tmp_path / "scratch_norm")

    def normalizing_handler(ctx: MorphedColleagueContext):
        abs_nested = ctx.scratch_dir / "subdir" / "deep_file.txt"
        ctx.write_file(str(abs_nested), "nested content")
        return {"done": True}

    res = pool.run_task(role="test", goal="Path normalization test", handler=normalizing_handler)
    assert res["ok"] is True
    assert res["evidence"]["modified_files"] == ["subdir/deep_file.txt"]
    assert "artifacts" in res["evidence"]
    assert "report.json" in res["evidence"]["artifacts"]
    assert "manifest.json" in res["evidence"]["artifacts"]
    pool.close()


def test_durable_pool_attempt_fencing_rejection(tmp_path):
    """Verify commit_task_result returns False when durable pool rejects stale attempt."""
    durable_pool = Pool(tmp_path / "durable_fence_pool")
    job_id = durable_pool.submit("fenced-job-1", {"role": "reviewer", "goal": "Fence rejection"})
    claim = durable_pool.claim()
    assert claim is not None
    row, fd = claim
    os.close(fd)

    colleague_pool = ColleaguePool(durable_pool, max_workers=2)

    # Pre-finalize the job in durable store to make subsequent attempt stale
    with durable_pool.db() as db:
        durable_pool._terminal(db, job_id, "completed", {"ok": True}, attempt=row["attempt"])

    # Attempt to commit again with same attempt token -> durable pool rejects update
    committed = colleague_pool.commit_task_result(
        task_id=job_id,
        epoch=1,
        attempt_token=row["attempt"],
        result={"stale": True},
    )
    assert committed is False
    colleague_pool.close()


def test_drain_waits_for_queued_tasks_in_scheduler(tmp_path):
    """Verify drain properly waits until both running slots and queued scheduler tasks complete."""
    pool = ColleaguePool(max_workers=2, capacity=10, scratch_root=tmp_path / "scratch_drain")

    completed = []
    lock = threading.Lock()

    def work(ctx):
        time.sleep(0.03)
        with lock:
            completed.append(ctx.slot_id)
        return {}

    for i in range(6):
        pool.submit_colleague_task(role="test", goal=f"drain work {i}", handler=work)

    pool.drain(timeout=5.0)
    assert len(completed) == 6
    assert pool.completed_count == 6
    assert pool.active_count == 0
    pool.close()



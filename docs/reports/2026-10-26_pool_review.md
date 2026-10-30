# Pool reliability review — 2026-10-26

## Scope and ownership

Reviewed `agentcore/pool.py`, `agentcore/exec_runner.py`, `runtime/agentcore-worker@.service`, and the existing pool tests. Production services, messaging, credentials, and production databases were not touched. Every process probe used a temporary database and reviewer-created child processes; cleanup signalled only their recorded process groups.

Reviewer changes are limited to:

- `tests/test_pool_recovery.py` (new process-level regression tests).
- `docs/reports/2026-10-26_pool_review.md` (this report).

The main implementer made the core fixes during review. This report does not claim authorship of those fixes or user acceptance.

## Verified fixes and evidence

| Finding | Fix inspected | Evidence |
| --- | --- | --- |
| A fast successful runner could exceed the combined output budget on stderr after the monitoring loop ended | Recheck stdout + stderr after process exit, before accepting receipt | `test_exited_runner_stderr_cannot_bypass_output_budget` |
| Reusing a dedupe key with different replay/retry policy silently accepted the old task | Dedupe compares replay policy and max attempts | Parametrized `test_dedupe_rejects_execution_policy_changes` |
| Attempt-directory creation failed outside cleanup, leaking the held lock in a continuing process | Directory creation now inside run_claimed try/finally | `test_attempt_directory_failure_releases_job_lock` acquires the same OS lock after failure |
| A killed CLI worker left an inherited-lock runner alive beyond cancellation/deadline | Persist PID/starttime/deadline; a claimant reaps only the matching Linux process group | `test_crashed_worker_orphan_is_stopped_before_recovery` covers deadline and cancellation; old attempt finish is rejected |
| Risk of signalling a reused PID | Match `/proc` starttime and require process group leader identity | `test_orphan_reaper_does_not_signal_mismatched_pid_identity` keeps the deliberately mismatched child alive |
| The original cancellation test cancelled only a queued task | Added running-task cancellation test | `test_running_cancel_stops_runner_and_commits_one_receipt` verifies process death, terminal state, one receipt |
| A generator passed as dependencies was consumed by validation, leaving zero dependencies | Materialize dependencies into a tuple once | Before fix, a temporary DB printed `generator_dependency_rows 0`; added `test_generator_dependencies_survive_validation_and_block_claim` |
| A polling output budget could allow a burst to write a large single file | A fresh interpreter applies RLIMIT_FSIZE before exec; no threaded preexec_fn | `test_runner_file_limit_bounds_an_individual_artifact` verifies a 1 MiB attempted write stays <=4096 bytes |
| Applying the output limit directly to artifacts could reject legitimate files | Separate max_output and max_file | `test_artifact_budget_is_distinct_from_stdout_budget` accepts a 2 MiB artifact with a 1 KiB stdout budget |

The reaper inspection was repeated after the implementer moved deadline/cancellation checks before the heartbeat-freshness shortcut. This permits prompt termination of a known overdue runner even if its last heartbeat is still within the normal lease. Reclaiming a replacement attempt still follows the lease/lock rules.

## Test runs

1. `python3 -m pytest tests/test_pool.py tests/test_pool_recovery.py -q`
   - 14 passed in 122.80s (the recovery file had 8 expanded cases at this point).
2. After the executable bootstrap and reaper change: `python3 -m pytest tests/test_pool_recovery.py -q`
   - 8 passed in 45.34s.
3. Added file-limit cases: `python3 -m pytest tests/test_pool_recovery.py -k 'file_limit or artifact_budget' -q`
   - 2 passed, 8 deselected in 12.27s.
4. Generator regression added after the above runs; its focused result is recorded in the final addendum below.

The host was concurrently running compute-intensive jobs. No other process was interrupted to speed up tests. Full integrated verification belongs to the main implementer's final gate; this review did not duplicate that full run.

## Remaining boundary demonstrated on the latest bootstrap

### A detached descendant can outlive a completed task

An administrator-supplied probe runner started a sleeping descendant with `subprocess.Popen(..., start_new_session=True)`, recorded that descendant PID in its attempt directory, then returned `{ok: true}`. After `Pool.run_once()` completed, the probe observed:

```json
{"job_status": "completed", "escaped_descendant_alive": true, "group_is_descendant": true}
```

The reviewer then killed that specific reviewer-owned descendant group. No production process was involved.

This is an execution contract limitation, not fixed by an inherited lock: Python subprocesses normally close unrelated descriptors, and a detached child is no longer in the runner's process group. `killpg(runner_pid)` cannot kill a different process group. The systemd unit's `KillMode=control-group` helps when the whole worker service stops; it does not ensure per-task descendants are gone before a task is marked complete.

Required wording for the first release: **trusted runner, must remain in the assigned process group; independent working directory is not a filesystem or process sandbox**. To support arbitrary tools that daemonize, run each attempt in its own cgroup/container, wait for that scope to be empty before releasing the claim, and bound CPU/memory/total disk there. A scheduler library does not provide this containment.

### Crash window before PID registration

Popen happens before the DB PID/starttime update. A crash in that interval can leave a runner holding the inherited lock with no durable process identity. The implementation explicitly documents conservative blocking in this case. The current tests wait for durable PID registration before killing the worker; they do not prove recovery from this narrow launch window.

For stronger guarantees, have the fresh bootstrap register identity while holding the claim, use a parent/child launch handshake, or make process scope identity durable before releasing execution. Do not recover this case by deleting the lock file or signalling a guessed PID.

### Budgets and isolation are deliberately partial

- RLIMIT_FSIZE bounds each file, not aggregate disk. Many small files can still exhaust a volume.
- The queue limits wall time, attempts, and output. It does not itself enforce model tokens or monetary spend.
- `cwd` changes do not restrict access to other files or sanitize the inherited environment.
- Outbox consumer leases avoid simultaneous normal delivery, but exactly-once external messaging still requires destination dedupe/reconciliation around “sent, crashed before ack”. The updated API documentation acknowledges this.

These are release constraints to document and enforce in the runner/deployment layer, not reasons to replace the durable DB with a scheduler's in-memory state.

## Acceptance boundary

This review establishes local process-level properties only. It does not establish a live model runner, production deployment, chat responsiveness, message delivery, or user acceptance. Keep those separate in the task board.

## Final addendum

The main implementer owns final integrated verification and commits. This reviewer did not commit or push.

Focused generator result: `python3 -m pytest tests/test_pool_recovery.py -k generator -q` → **1 passed, 10 deselected in 11.94s**. Final recovery file contains 11 expanded cases. All cases were exercised across the focused runs; a single final integrated run remains the main implementer's gate.

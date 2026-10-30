# Durable pool + FlowCoro runtime

## Ownership

The durable job store owns job identity, dependencies, leases, replay policy,
attempt fencing and the result outbox. `agentcore.scheduler.AgentScheduler`
owns bounded process-local execution. `agentcore.worker.Worker` connects them.
No in-memory Future is a recoverable task record.

```text
submit -> SQLite job -> claim + job lock -> bounded scheduler
                                            | stdlib or flowcoro_py
                                            v
                                  isolated working directory
                                            |
                                 trusted runner subprocess
                                            |
                                  validated JSON receipt
                                            v
                            SQLite terminal state + outbox
```

FlowCoro references checked for this implementation:
- https://github.com/caixuf/flowcoro/blob/main/docs/AGENT_SCHED.md
- https://github.com/caixuf/flowcoro/blob/main/docs/PYTHON_BINDING.md
- https://github.com/caixuf/flowcoro/blob/main/examples/python/flowcoro_agent.py

Reuse is through the actual optional `flowcoro_py.CoroutineThreadPool` and
`CancellationTokenSource`. Python Future completion bridges expose an event
wait without polling every Future. Native bindings are not a required package
dependency. Explicit `flowcoro` selection fails if unavailable; there is no
silent fallback. The default `stdlib` backend uses the same public contract.
This is an interoperability choice, not a claimed speedup over Python threads.

## Administrator configuration

Configure a trusted runner as an argv JSON array. The job payload never chooses
an executable. Use an absolute executable/script path because the cwd is a new
attempt directory. Modules invoked by `-m` must be installed in the selected
interpreter, not merely importable from a development checkout.

Example runner interface:

```python
import json, sys
job = json.load(sys.stdin)
# Invoke approved tools using job['payload']; validate their real artifacts.
# Diagnostics go to stderr. stdout must contain exactly one JSON object.
json.dump({'ok': True, 'result': {'evidence': 'verified artifact reference'}}, sys.stdout)
```

The example only documents the protocol; a production model adapter must decide
success from tool evidence. A model's success claim alone does not validate a
business task. Use an independent model profile/identity for each scope and
enforce tool permissions in that adapter. Do not automatically use a CLI mode
that bypasses approvals.

From the source root:

```bash
python3 -m agentcore.pool --root /absolute/private/state submit \
  --key platform-event-id --payload-file /absolute/task.json
python3 -m agentcore.pool --root /absolute/private/state worker \
  --runner-config /absolute/runner.json --workers 2 --backend stdlib --drain
python3 -m agentcore.pool --root /absolute/private/state worker \
  --runner-config /absolute/runner.json --workers 2 --backend flowcoro
```

`--drain` completes currently runnable work and exits when it has no in-process
attempts. Tasks owned by another worker or awaiting a surviving orphan may
remain; inspect `status`. `--once` executes one eligible attempt. Continuous
mode rechecks readiness at a bounded interval. SIGTERM/SIGINT stops admission
and drains active attempts; a service stop deadline may then kill its cgroup.

The service template uses two workers and stdlib by default. To select native
execution, set `AGENTCORE_BACKEND=flowcoro` in its optional worker.env and set
PYTHONPATH to a binding built for the service interpreter. No service has been
enabled or restarted by adding the template. Each instance uses the same
configured queue; use a separate state directory and runner config for distinct
permission/profile boundaries.

## Execution and cancellation

- Scheduler capacity bounds running plus queued functions; it does not limit
  model tokens or API quotas. Native Python functions are still subject to GIL.
- The Worker only admits as many durable claims as it has execution slots.
- Scheduler tool cancellation is cooperative. Ignored tokens do not kill a
  Python thread; a tool can still finish successfully after a cancellation request.
- Durable job cancellation uses `Pool.cancel` / CLI `cancel JOB`. The runner
  process group is terminated and the job finishes cancelled.
- Each attempt has a wall-clock deadline, combined stdout/stderr budget and a
  configurable per-file limit (default 64 MiB). A fresh interpreter applies
  RLIMIT_FSIZE before exec, avoiding preexec_fn in threaded code. The file limit
  also applies to generated artifacts; configure it for the workload. It is not
  a total storage, memory, CPU or network quota.
- Logs, input and artifacts remain private attempt files. Outbox delivery should
  transmit selected evidence, not dump raw logs or secrets into chat.

## Recovery guarantees and boundaries

Job locks are inherited by the immediate runner. A lease expiry alone cannot
start an overlapping runner that still holds that lock. Linux orphan reaping
checks boot ID, PID starttime and process-group identity before killing a
cancelled or deadline-expired runner. Old attempt tokens cannot finish a new
attempt. Default replay is disabled; mark a task replay-safe only if its actions
are idempotent, or independently check their external effects.

The tiny crash window between process launch and PID registration is handled
conservatively: a surviving lock can require operator recovery. A runner which
deliberately daemonizes, closes the inherited lock and escapes its process group
is outside this trusted-runner contract. For arbitrary code, use a per-attempt
cgroup/container with filesystem and tool restrictions. An attempt directory is
workspace separation, not a security sandbox. SQLite WAL/flock require a local
POSIX filesystem; this implementation is not a multi-host coordinator.

## Outbox delivery

Use `claim-receipt` to atomically lease one result. After confirmed transport
delivery, use `ack JOB --token TOKEN`. Stale/wrong tokens cannot acknowledge a
new lease. A crashed consumer can be replaced after lease expiry.

The external send and local ack are not one transaction: after a send succeeds
but before ack, redelivery is possible. The transport must deduplicate/reconcile
using job ID. Read-only `receipts` is an inspection API, not a multi-consumer
claim. Never claim exactly-once external messages from this mechanism.

## Verification

```bash
python3 -m pytest tests/test_pool.py tests/test_pool_recovery.py \
  tests/test_scheduler.py tests/test_worker.py -q
bash verify.sh
```

Native tests explicitly skip if the binding cannot import; skipped native tests
do not prove the native path. Build with FLOWCORO_BUILD_PYTHON=ON, set PYTHONPATH
to its python output directory and rerun. Include the real worker test, not only
the thread-pool wrapper tests.


### Recorded verification (2026-10-26)

Upstream FlowCoro commit aaac885e673e01b6f21037a47930ef744668d26b built
with Python 3.12.3, GCC 13.3 and pybind11 2.13.6. Native module reports 4.0.0;
upstream Python tests: 33 passed. Final repository verify: 137 passed, no skips,
shell checks and install dry-run passed. The count includes concurrent uncommitted
tests outside this change. Temporary test databases were on local tmpfs due to
heavy host disk load. This verifies recovery semantics, not power-loss durability
or production storage capacity. See the dated handover for the contention fix
and the earlier disk-backed test limitations.

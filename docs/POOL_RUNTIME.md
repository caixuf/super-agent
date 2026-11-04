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
- Durable job cancellation uses `Pool.cancel` / CLI `cancel JOB`. The request
  remains nonterminal until the trusted executor confirms stop. Local subprocess
  attempts use a Linux subreaper supervisor: detached/environment-clearing children
  are adopted and reaped before an attempt-bound stop receipt permits cancellation.
  `finalize_cancel` acknowledges a request, not stop; only never-claimed work can
  finish immediately. A cancellation timeout is not evidence of stopped execution.
- Each attempt has a wall-clock deadline, combined stdout/stderr budget and a
  configurable per-file limit (default 64 MiB). A fresh interpreter applies
  RLIMIT_FSIZE before exec, avoiding preexec_fn in threaded code. The file limit
  also applies to generated artifacts; configure it for the workload. It is not
  a total storage, memory, CPU or network quota.
- Logs, input and artifacts remain private attempt files. Outbox delivery should
  transmit selected evidence, not dump raw logs or secrets into chat.

## Recovery guarantees and boundaries

Job locks are inherited by the supervisor, not the task command. A lease expiry
alone cannot start an overlapping attempt. Before effects, every executor must
pass `begin_execution`; a timeout/reclaimed attempt cannot later launch its stale
runner. Local supervisor stop receipts bind the job, attempt, token, nonce and
supervisor PID/start identity. Worker EOF also requests tree cleanup. A missing
receipt, killed supervisor or synchronous owner crash remains unknown: no retry
or terminal outbox is invented. Old attempt tokens cannot finish a new attempt.
Default replay is disabled; even proven process exit does not make external
effects idempotent. Replay still requires its explicit policy and remaining budget.

Before the ledger registration commits, the supervisor has no launch permission;
afterwards it waits for a separate go message. The ready handshake precedes PID
publication so early cancel cannot kill it before its stop handler is installed.
This is a trusted local process-tree contract, not a hostile-code sandbox or proof
about remote/daemon work. For arbitrary code, use a trusted attempt-bound sandbox
with filesystem/tool restrictions; colleague write execution remains gated.
Legacy active rows without these records remain uncertain and require operator
reconciliation, not automatic replay. Drain/coordinate all queue participants
before upgrading; mixed old/new workers do not share the admission invariant.
See [stop contract](ATTEMPT_STOP_CONTRACT.md). SQLite WAL/flock require a local
POSIX filesystem; this is not a multi-host coordinator.

## Outbox delivery

Use `claim-receipt` to atomically lease one result. After confirmed transport
delivery, use `ack JOB --token TOKEN`. Stale/wrong tokens cannot acknowledge a
new lease. A crashed consumer can be replaced after lease expiry.

The external send and local ack are not one transaction: after a send succeeds
but before ack, redelivery is possible. The transport must deduplicate/reconcile
using job ID. Read-only `receipts` is an inspection API, not a multi-consumer
claim. Never claim exactly-once external messages from this mechanism.

## Development verification authority

`dev_runner` treats queued payloads as data, not permission to launch host
verification commands. Configure mandatory checks in the administrator-owned
policy passed through `--policy`, alongside `allowed_roots`:

```json
{
  "allowed_roots": ["/srv/isolated-worktrees"],
  "verification_checks": [
    {
      "check_id": "unit",
      "command": ["python3", "-m", "unittest", "-q"],
      "timeout_s": 30,
      "scope": "workspace_scoped",
      "safety_tier": "isolated_test",
      "critical": true
    }
  ]
}
```

All policy checks run after a successful model result, whether or not the task
declares checks. A nonempty payload `verification_checks` must match the entire
normalized policy contract, including argv, environment, timeout, expected exit
code, criticality, scope and safety tier. Unknown fields, duplicate configured
check IDs, excessive counts and invalid contracts fail before model execution.
Missing policy checks allow no task-supplied commands; omission of payload checks
does not disable configured checks. Existing callers that previously supplied
commands only in the payload must move their reviewed contracts into host policy.
The runner snapshots contracts before model execution and holds the same worktree
lock through verification. `safety_tier` describes intent; it does not sandbox code.

For `sandbox=True`, configured checks require the host-injected Python API
`verification_executor(argv, timeout_s, workdir, *, env)` or the explicit CLI
`--verification-adapter module:callable`. No executor means an explicit
pre-execution error, never a fallback to host commands.
The host must independently verify that its adapter constrains files, credentials,
network, budgets and descendants. Merely supplying a callback is not proof.
Legacy three-argument verification executors remain compatible only when no
check needs environment overrides. Signature compatibility is resolved before any
check; executor exceptions never trigger a second invocation without environment.

The CLI can also select `--runner-adapter module:callable` to avoid the legacy
business bridge. Both flags are trusted administrator launch configuration, not
queued payload fields; task fields with the same names do not select imports.
Keep adapter modules and their import search path outside writable worktrees and
under administrator ownership. Importing an adapter executes trusted Python code
in the runner process; the CLI does not sandbox that module or certify it safe.

```bash
python3 -m agentcore.dev_runner \
  --policy /srv/worker/policy.json --role-contract /srv/worker/role.md \
  --workdir /srv/isolated-worktrees/task --agent-dir /srv/worker/task-cache \
  --output-dir /srv/worker/task-receipts --sandbox \
  --runner-adapter reviewed_runtime:generate \
  --verification-adapter reviewed_runtime:verify
```

`reviewed_runtime` above is a deployment-specific module, not a bundled runtime.
The synchronous generation contract is `generate(prompt, options)`, where options
contains `workdir` and `agent_dir` as paths, plus `model`, `provider`, `tools`,
`timeout`, and the requested `sandbox` boolean. It returns a result mapping (or
an object with `to_dict()`) whose `ok` is an actual boolean. The verification
contract is `verify(argv, timeout_s, workdir, *, env)`, returning a mapping with
an integer or null `exit_code` and optional capture/status fields as used by
`verification_gate`. New CLI adapters must accept the `env` keyword; legacy
three-argument compatibility belongs to the existing Python API only.

Explicit references are validated for synchronous callable signatures before
generation. Invalid references/imports/signatures, raised adapter exceptions and
malformed result shapes fail without retrying or falling back to the bridge.
Unknown or abbreviated CLI flags are rejected. Python-level import/execution
stdout is redirected to stderr so stdout carries one JSON receipt; this is not
an OS-level output or secret-filtering boundary. Adapters still own their direct
file-descriptor output, public results, budgets and isolation behavior. Omitted
flags retain the previous bridge/local-verification behavior subject to the
sandbox verification requirement above.

Without sandbox mode and without an injected executor, administrator-approved
checks still use the local process executor. Tests can execute arbitrary code
from a worktree, so an allowlisted `pytest` or `unittest` command alone is not a
security boundary. The legacy `dev_runner` model bridge and its `--sandbox` root
mount also do not establish the trusted whole-attempt boundary required by
`colleague_worker`. That gate remains closed. No arbitrary-code production
readiness, actual model quality, or complete P2 delivery is claimed here.

`tests/test_dev_runner.py` includes a real host-injected bubblewrap verification
probe: generated artifacts pass two unit tests while a host-only file, inherited
secret environment value and host loopback listener are inaccessible. Its model
is a deterministic test callback, not a real model, and it tests this verification
seam only. Bubblewrap absence is explicitly skipped, not counted as containment
success. Process-tree cancellation and crash recovery remain separate gates.

The CLI regressions additionally run both deterministic generation and unit-test
verification in real bubblewrap subprocesses, then exercise the CLI as an actual
Pool runner: passing code completes, failing generated code fails, cancellation
and timeout during either phase reap an environment-clearing detached descendant
before one terminal receipt. The local supervisor validates the attempt-bound
stop proof. These are offline component/lifecycle checks, not actual model
quality, hostile-code containment, production service readiness or IM delivery.

The default local verification executor reads stdout and stderr concurrently,
retaining at most 1 MiB per stream before UTF-8 decoding. Exceeding either
budget stops the process group and sets `output_limited`; it cannot approve a
check even when the returned exit code matches `expected_exit_code`.
Invalid UTF-8 is replaced without discarding the observed exit status.
Spawn/collection errors are likewise not successful reproduction checks.
The command deadline also covers waiting after pipe EOF, and cleanup never
performs an unbounded second `communicate()`. Direct-child reap is limited to
one additional second. These limits constrain this local call's capture and
wait behavior; they do not constrain all memory/disk/process usage by a verifier.
Detached pipe holders may survive best-effort group cleanup: only a trusted
attempt boundary can certify that the whole task has stopped.

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

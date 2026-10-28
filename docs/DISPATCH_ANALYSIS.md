# Durable dispatch and read-only failure analysis

`plugins/durable_dispatch` registers three tools through the public Hermes API:
`durable_submit`, `durable_status`, and `durable_cancel`. Submit returns after
the task is committed; execution happens in the existing independent Worker.
The returned instruction asks the model to finish its turn. Turn release was observed through the live API on a synthetic task; the plugin
cannot force the model to stop on every request.

Dispatcher session identity and the registration profile determine ownership.
They are never accepted as model arguments or read from an ambient environment
fallback. A shared queue holds the authoritative task; status and cancellation
verify its owner. A new session cannot automatically access the old session's
tasks. Multi-user sharing requires an explicit authorization design.

The runner is:

```bash
python3 -m agentcore.analysis_runner --policy /absolute/admin-policy.json \
  --model-adapter approved_module:callable
```

Its stdin is the existing pool job envelope. Payload contains `goal` and
`sources` (absolute path and optional line range). Administrator policy supplies
allowed roots, denied globs, byte/file/line budgets and optional `model_options`.
The callable receives `messages` and those administrator options. Job payloads
cannot choose a model callable or override its options.

Evidence is opened relative to pinned directory descriptors using O_NOFOLLOW.
Only bounded regular UTF-8 files are accepted. Sensitive filenames and common
credential assignments are rejected; this is conservative filtering, not a
guarantee that arbitrary logs contain no private information. Limit approved
roots and select appropriate source ranges before sending them to a model.

The model has no tools, executable command, writable workspace or chat history.
It returns a structured conclusion and claims with line-numbered quotes. Each
quote must occur in the captured source line; empty evidence, invented sources
and mismatched quotes fail the task. The manifest records SHA-256 and exact
snapshots. Successful results carry `citation_traceability_only`: semantic and
causal judgments still require review. Model token/time limits come from the
approved adapter; the Worker adds a wall-clock timeout and output/file limits.

The CLI atomically writes `report.json`, `report.md` and `manifest.json` in the
attempt directory, and prints one machine-readable receipt. Admin options should
disable hidden token expansion in the chosen adapter and set an explicit request
timeout below the Worker wall deadline.

## Development colleague runner (dev_runner)

While `analysis_runner` operates in read-only citation verification mode, `dev_runner`
executes active engineering tasks with full development tooling inside isolated Git
worktrees.

The runner is invoked as:

```bash
python3 -m agentcore.dev_runner \
  --policy /absolute/admin-policy.json \
  --role-contract /path/to/role_contract.md \
  --workdir /path/to/worktree \
  --agent-dir /path/to/agent_dir \
  --output-dir /path/to/attempt_output
```

Core properties:

1. **Physical Worktree Isolation & POSIX Locks**:
   Each development colleague executes within an independent worktree directory.
   Concurrency across processes is guarded by `fcntl.flock(LOCK_EX | LOCK_NB)` on
   the target directory hash. Concurrent tasks targeting the same busy worktree are
   safely rejected (`worktree_busy`) rather than corrupting work in progress.

2. **Full Engineering Toolset**:
   Unlike read-only verification, the runner equips colleague roles with coding tools
   (`read, write, edit, bash`). When `--sandbox` is enabled, execution runs inside
   `bwrap` sandboxing with the host root read-only and only the worktree and agent
   directory writable.

3. **dev_traceability_v1 Evidence Protocol**:
   Every execution atomically generates `report.json`, `report.md`, and `manifest.json`
   in the output directory. Evidence records include `modified_files`, `role`, `job`,
   `attempt`, tool calls, and execution summaries verified by `agentcore.colleague_evidence`.

4. **Colleague Role Roster**:
   `agentcore.colleague_dev` provides standardized role contracts for 8 development
   roles (`architect`, `cases`, `reviewer`, `test`, `triage`, `backend`, `frontend`, `ops`),
   enabling multi-stage pipelines (`architect` -> `cases` -> `reviewer` with score-based
   self-healing loops -> `test` syntax/AST verification).

## Acceptance and deployment

Tests cover fake and actual Hermes registry dispatch, session isolation,
deduplication, submission without waiting, short sources, budget/path rejection,
subprocess analysis, forged citations, restart visibility and outbox ack. A real
configured model was separately exercised on synthetic evidence through an
actual FlowCoro worker; it generated three checked claims. This proves wiring,
not the accuracy of analysis on real incidents.

Deploy plugin settings, source policy and a separate Worker service together.
Do not replace a profile's entire configuration or enabled-plugin list. Activate
new tool schemas at a new session boundary. Install any optional native backend
for the exact service interpreter in a persistent location. Source policies and
runner module names belong to the host deployment, never the task request.

A host deployment now has the plugin and independent FlowCoro Worker enabled;
live API submission, query, terminal cancellation and crash recovery were checked
with synthetic fixtures. See reports/2026-10-26_core_activation.md. Human business
acceptance is pending. Automated outbound delivery remains a host integration;
status exposes a bounded summary and verified artifact references. For one-card delivery, reuse the existing CardStore/CardEmitter and
outbox protocols rather than creating another task state machine.

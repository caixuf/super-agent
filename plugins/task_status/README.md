# Task status — read-only liveness snapshots

Long tasks were either silent until completion or reported only `running/finished`.
This optional plugin exposes one tool, `task_status`, backed by the shared
`agentcore.liveness` ledger:

| action | result |
| :--- | :--- |
| `list` | active tasks owned by the calling session, with computed verdicts |
| `inspect` | one task: state, elapsed, heartbeat age, phase, progress, pid health, bounded tail |
| `tail` | the host background process's log when available, else the ledger tail |

The handler is read-only. It never starts, re-runs, polls in a loop, or kills a task.
When asked about a host background process, it performs **one** `process.poll()` to
persist the real exit code; a missing process is left to the ledger's pid evidence
(`lost`), never guessed as success.

## Settings

```yaml
plugins:
  entries:
    task-status:
      settings:
        enabled: true
        engine_root: /absolute/path/to/agentcore-checkout   # omit when agentcore is installed
        liveness_db: /absolute/private/liveness.db           # optional; defaults per-user
        stall_seconds: 120                                   # no-pid silence threshold
```

`enabled` is required: the plugin does nothing unless the administrator opts in.
The database is shared with `longtask_guard` (same path), which registers converted
background processes after dispatch and arms the host's mid-run heartbeat.

## Ownership

The tool uses the host dispatcher's `session_id`, never a model argument. A task
registered by another conversation returns `unknown_task` without its label or
output. The plugin does not provide a cross-session "all tasks" view; that needs an
administrator surface, not a model tool.

## What it does not do

- No platform delivery and no card rendering. Routine heartbeats must not start a
  model turn; use the daemon/transport layer for that.
- No scheduling authority: the durable queue and the host process registry remain
  the only sources of execution truth.
- No fabricated completion: `unknown`, `stalled`, and `lost` are reported as such.

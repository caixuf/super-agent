# Durable Dispatch plugin

This optional plugin adds `durable_submit`, `durable_status` and `durable_cancel`
to Hermes. It persists an analysis request and immediately returns a task ID.
Execution belongs to the separate worker service, not the chat gateway process.

Install this directory using the host's normal standalone plugin mechanism and
opt in. The host can import an installed engine package, or load the canonical
`pool.py` from an administrator-supplied checkout without changing `sys.path`:

```yaml
plugins:
  enabled:
    - durable-dispatch
  entries:
    durable-dispatch:
      settings:
        enabled: true
        engine_root: /absolute/path/to/agentcore-checkout
        queue_root: /absolute/private/analysis-queue
        allowed_roots:
          - /absolute/read-only/project
        denied_globs:
          - private-config.json
```

Add the `durable_dispatch` toolset to the intended profile/platform using the
host's normal tool configuration. Match the worker's queue root and analysis
policy. Configuration above is a template; installing the plugin does not start
a worker or send messages. Built-in sensitive path denials cannot be removed by
supplying an empty custom list.

`durable-dispatch` is the plugin manifest/enable name; `durable_dispatch` is the
registered toolset name. They are intentionally different. The public Hermes
`cmd_enable('durable-dispatch')` resolves the registered toolset and appends it
to each existing saved `platform_toolsets` list. It leaves platforms without a
saved selection at their normal defaults. Do not replace a platform list with
only this plugin, which would remove built-in tools. New tool schemas are
available at a new session boundary, not injected into an ongoing cached turn.

Settings should be written through the host's public `save_plugin_setting`
helper, preserving unrelated configuration. On the current host its signature
is `(plugin_id, segments_tuple, value)`, for example:

```python
save_plugin_setting('durable-dispatch', ('engine_root',), '/absolute/engine')
```

The plugin's module has no mandatory engine import at load time. Disabled
registration creates no queue. Enabled registration first tries the installed
engine; only a missing engine package permits the administrator checkout
fallback. An unrelated import/dependency error remains an error. Job arguments
cannot provide `engine_root`, change the executable, or install a second copy of
the queue implementation.

Submission arguments contain only `goal`, `sources` and `request_key`. Source
items contain an absolute `path` and optional `start_line` / `end_line`. A missing
end is deliberately left unspecified so the runner can bound it using the real
file length. At most eight sources are accepted. Sources are normalized and
checked against administrator roots before admission, then must be rechecked
and snapshotted by the runner when it opens them.

The host supplies `session_id` as a dispatcher keyword; it is not in the model
schema. A hash of that identity and the profile owns each job. Missing identity,
model-supplied ownership/executable fields, unknown task IDs and cross-session
status/cancel requests fail closed. No session identity is read from environment
variables. This intentionally does not provide cross-session task sharing.

Status returns structured JSON. Unicode text is bounded by its decoded content,
not by escape-sequence size, and an oversized result is never sliced into an
invalid JSON fragment. A truncated summary is marked; full reports remain in
the artifact paths. Cancellation distinguishes a newly accepted request from a
task that was already completed, failed or cancelled. `cancellation_requested`
reflects the stored flag, while `already_terminal` and `request_accepted` explain
the current call.

The task payload contains `goal`, `sources` and internal `_dispatch_owner`. That
hash is for access checks; analysis runners should ignore it. The stable request
key is namespaced by owner and hashed before deduplication. An intentional rerun
uses a new request key. The queue sets replay-safe for this read-only analysis
role; it must not be reused for effectful runners.

The submit result asks the chat agent to return the ID and finish its turn.
This is an instruction, not an enforced end-turn primitive. Confirm actual
gateway behavior in a live test. Result delivery remains an outbox transport
integration, and this plugin never sends a message itself.

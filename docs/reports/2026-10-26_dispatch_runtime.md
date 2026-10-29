# Durable dispatch host-runtime integration

Date: 2026-10-26. This slice changes only the durable-dispatch plugin, its tests
and documentation. It does not install the plugin, change production settings,
start a worker, restart a gateway or send an external message.

## Problem and behavior

The real host interpreter is an isolated Python 3.14 environment without an
installed `agentcore` package. The previous top-level `from agentcore.pool
import Pool` prevented plugin discovery before administrator settings could be
read. Plugin import is now independent of the engine. Enabled registration uses
an installed engine when available; otherwise `engine_root` loads the single
canonical `agentcore/pool.py` file. It never mutates interpreter search paths or
copies a second durable queue implementation. Missing configuration fails
explicitly. The standalone `Pool(root)` factory and DispatchService API remain
compatible for existing callers with an importable engine.

Identity remains dispatcher-supplied `session_id` plus profile. Model arguments
cannot select engine root, owner, runner or executable. Three tools remain in
the `durable_dispatch` toolset; the manifest/enable name is `durable-dispatch`.

The status tool now parses the result before applying content limits. A Chinese
summary is not lost just because JSON escape sequences are longer than its
human-readable text. Large results remain structured, limits and summary
truncation are explicit, and full reports remain separate artifacts. Small
historical result mappings remain compatible.

The cancel tool reads real task state. Completed, failed and cancelled tasks
return `already_terminal: true` and `request_accepted: false`, retaining the
stored cancellation flag rather than claiming a new cancellation succeeded.

## Actual host verification

`python3 -m pytest tests/test_durable_dispatch.py -q`:

**18 passed in 0.71s**, no skipped tests.

After documentation, the combined plugin and repository-neutrality gate passed:
**31 passed in 12.23s**, no skips; changed-file whitespace checks also passed.

The new subprocess check uses the actual isolated host Python 3.14 and real
Hermes PluginContext, PluginManager, public settings writer and ToolRegistry.
Its temporary profile verifies all of the following:

- `agentcore` is not installed before or after registration.
- Registration does not mutate `sys.path`.
- Exactly three tools register under `durable_dispatch`.
- Missing dispatcher identity is rejected.
- A trusted session can submit and query a persisted task.
- Another session cannot cancel it.

Focused regressions also cover administrator-only engine fallback, decoded
Unicode result preservation, large-result JSON shape and truncation, and the
three terminal cancellation states.

## Activation guidance

Write `plugins.entries.durable-dispatch.settings` through the public settings
writer with enabled, engine_root, queue_root and allowed_roots. Install the
plugin directory, then use the public `cmd_enable('durable-dispatch')`. The
current host resolves the registered toolset name and adds it to saved platform
toolset selections; it does not seed a list that would strip built-in tools.
Tool schemas wait for a new session boundary. The gateway need not be restarted
to discover the plugin, and active cached conversations should not be mutated.

Runtime activation, a live model turn and production worker acceptance belong
to the parent integration. This slice does not claim those were completed.

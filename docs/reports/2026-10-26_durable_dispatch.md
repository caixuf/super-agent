# Durable Dispatch implementation receipt — 2026-10-26

## Result

Implemented the optional standalone Hermes plugin and scoped dispatch service.
Only plugin files and its focused test were changed by this slice. No production
plugin installation, configuration change, gateway restart or external message
was performed. Host wiring and live-user acceptance remain pending.

Files:
- `plugins/durable_dispatch/__init__.py`
- `plugins/durable_dispatch/plugin.yaml`
- `plugins/durable_dispatch/README.md`
- `tests/test_durable_dispatch.py`

## Verified host interfaces

- `hermes_cli/plugins.py::PluginContext.get_config` reads plugin-relative settings.
- `PluginContext.profile_name` is an actual public property, not a fabricated API.
- `PluginContext.register_tool` accepts the schema, handler and toolset used here.
- `model_tools.py::_execute_tool` passes host-owned `task_id` and `session_id`
  separately from model arguments to `ToolRegistry.dispatch`.
- `tools/registry.py::ToolRegistry.dispatch` signature-filters those trusted
  keywords and requires a string result. Handlers return JSON strings.

The plugin does not consume ambient session environment variables because the
host's public `get_session_env` includes an environment fallback. Identity comes
from dispatcher kwargs, and absence is a denial. Profile identity is captured at
plugin registration.

## Contract

One central Pool remains the scheduling authority. Each payload reserves a
server-generated `_dispatch_owner` hash of profile and dispatcher session ID.
Deduplication keys are owner-namespaced. `status` and `cancel` read the stored
ownership before exposing state or mutating cancellation. Model schema and runtime
checks reject extra fields, including commands, owner or session spoofing.

Source admission requires absolute regular files under administrator roots,
bounded line ranges and eight sources maximum. Sensitive filenames and symlink
escapes are denied. The runner must revalidate and snapshot sources at read time;
metadata prevalidation here is not a filesystem sandbox.

Only an explicitly supplied end_line is persisted. Synthesizing a fixed end for
a short file was removed during integration review, because the analysis runner
validates explicit end bounds against the real line count.

## Validation

`TMPDIR=<isolated-local-tmpfs> python3 -m pytest tests/test_durable_dispatch.py tests/test_repo_neutrality.py -q`

Result: **24 passed in 0.44s**, no skips. Eleven plugin cases cover opt-in,
missing identity, environment/model spoofing, cross-session/profile denial,
source restrictions, dedupe conflicts and scoped results. The remaining thirteen
are repository neutrality gates.

The host integration case runs a fresh Python process with an isolated host
home, imports the real `ToolRegistry`, registers the actual plugin handlers,
and dispatches the three identity cases through the real registry. A fake
Context only supplies explicit settings and forwards register_tool into that
registry; the plugin does not depend on a fake dispatcher contract.

## Remaining boundaries

- The submit handler returns immediately and requests turn completion; it cannot
  itself force an LLM to stop polling. The host role/end-turn contract needs a
  real messaging acceptance test.
- Status/cancel scope is deliberately per conversation; switching conversation
  cannot recover access without a future explicitly authorized sharing design.
- It does not route outbox messages or create cards. Delivery must use existing
  durable transport primitives and reconcile send/ack crash windows.
- Source root policy is administrator-controlled; the model cannot choose a
  runner executable. The worker must run only the intended read-only analysis role.

Three-stage status: code implemented; focused local and real-registry validation
passed; live-user acceptance pending.

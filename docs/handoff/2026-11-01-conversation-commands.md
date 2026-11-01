# Handoff — task-progress command conflicts

**Date:** 2026-11-01 Asia/Shanghai
**Scope:** isolated command-surface fix; offline integration only, no deployment or control wiring

## Facts

- The host's `PluginContext.register_command` rejects a plugin command when
  `hermes_cli.commands.resolve_command(name)` resolves to a built-in. The host
  skips that registration rather than replacing the built-in.
- In the inspected host registry, `status` and `pause` resolve to built-ins;
  `cancel` did not resolve there.
- The task-progress handlers for pause and cancel did not call a controller.
  Their previous success-shaped replies therefore did not represent executed
  control actions.
- The inherited worktree contained unrelated uncommitted changes. This fix
  deliberately left them untouched.

## Changes

- Added an ordered stable alias chain for each verb:
  `status` / `taskstatus` / `task-progress-status`,
  `pause` / `taskpause` / `task-progress-pause`, and
  `cancel` / `taskcancel` / `task-progress-cancel`.
- Before registration, query the host resolver and choose the first name not
  owned by the host. If all candidates collide, register none rather than
  pretending a skipped command exists.
- Derive usage and help text from the command actually registered. If the
  resolver is unavailable or raises, do not treat the plain name as known-free:
  use the stable `taskstatus` / `taskpause` / `taskcancel` alias instead and do
  not break plugin loading.
- Keep pause and cancel explicitly unwired: replies now state that no pause or
  cancellation was executed and the task state was not changed.
- Require a current session in the status short path before querying the ledger,
  preventing a fallback to a shared `local` owner.

## Evidence

- Worker phase tested in the isolated `commands` worktree.
- Required command:
  `/usr/bin/python3 -m pytest tests/test_task_progress_plugin.py -q`
- Result: `50 passed in 63.80s`.
- Repository neutrality gate:
  `/usr/bin/python3 -m pytest tests/test_repo_neutrality.py -q`
- Result: `17 passed in 0.80s`.
- Coverage includes host-owned detection, missing and raising resolvers,
  plain/secondary alias collisions and fail-closed registration, consistent
  usage/help text, session isolation, and no success-shaped pause/cancel
  response.
- Collision checks cover names visible through the host resolver. Names owned by
  other plugins that the resolver cannot inspect remain outside this boundary;
  the task-prefixed namespace reduces that exposure.

## Unverified / intentionally out of scope

- No live-host registration or platform delivery was exercised.
- No scheduler, active profile, configuration, installation tree, message
  transport, deployment, or process restart was changed.
- Pause and cancel remain unimplemented command surfaces. Wiring them to an
  execution controller is a separate change.


## Independent integration follow-up

The integration reviewer added one counterexample: when primary-name resolution
throws but alias lookups still report collisions, occupied aliases must still
be checked and skipped. The follow-up predicate and regression are included.
A real host `PluginManager` load into a temporary profile also passed, checking
actual registered names, help/usage and honest unwired controls. This is
offline host registration, not loading the plugin into the running instance.
Final combined focused regression counts are in
`2026-11-01-conversation-reliability.md`.

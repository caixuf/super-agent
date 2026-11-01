# Handoff — background completion verification evidence

**Date:** 2026-11-01 Asia/Shanghai
**Scope:** task A only. Pre-stop / early-stop handling is explicitly out of scope.
**Deliverable type:** experimental, repo-local, replayable host patch. Nothing was deployed, loaded, committed, pushed, or written into the installed host tree.

## Exact breakpoint (source facts)

The live host chain is:

1. `terminal(background=true)` calls `spawn_background_process`; the tool result is a launch receipt (`"Background process started"`, `session_id`, `pid`, `exit_code: 0`). It is not a completed verification result.
2. `process_manage` later calls `ProcessRegistry.wait/poll`; a completed process returns `status: exited`, the registry's `exit_code` / `completion_reason`, and output.
3. Before this patch, `_handle_process` redacted that result and returned it. Only the foreground path (`terminal_tool_result.finalize_foreground_result`) called `record_terminal_result`.
4. Therefore the verification ledger never saw the background completion. A live example under the active profile had a completed pytest process with `completion_reason=exited` and `exit_code=0`, while its workspace state had `last_event_id=NULL` and remained stale. That is the `verification_required` loop.

The dispatcher was verified, not assumed: `agent/tool_executor.py::_resolve_sequential_dispatch` and `model_tools.handle_function_call` pass `task_id` and `session_id` through `registry.dispatch` to `_handle_process(args, **kw)`. The integration test exercises that exact path.

## Minimal patch

`patches/patch_background_verification.py` patches only two host files:

- `agent/verification_evidence.py`
  - `record_terminal_result(..., started_at=None)` forwards a background start time to the shared recorder.
  - `_insert_evidence(..., started_at=...)` performs the freshness gate in one immediate SQLite transaction and returns no event when stale.
  - Freshness checks both:
    - maximum `verification_state.last_edit_at` for the workspace root across all sessions; and
    - any newer `verification_events.created_at` for that root. This closes the case where a competing edit is followed by a new passing test that clears `last_edit_at`.
- `tools/process_registry.py`
  - `_handle_process` invokes one observer after the unchanged `wait/poll` result is produced and redacted.
  - The observer requires: action `wait/poll`; result `status=exited`; result and live record both `completion_reason=exited`; equal integer `exit_code`; live registry record; exact caller `session_id == parent_session_id`; exact caller `task_id == owner_task_id`; and no output truncation.
  - It records the host's command and cwd (not model arguments), marks a session observation once, and wraps all evidence work in try/except so it cannot break `process_manage`.
  - It rejects spawn receipts, running states, timeout/lost/unknown outcomes, missing exit codes, mismatched result records, other-session callers, and stale completions.

The command/evidence classifier was **not changed**. The unsafe suffix-matching experiment was fully removed. `echo pytest`, `printf pytest`, and `cat pytest` remain non-evidence, with explicit negative tests.

## Idempotent / fail-closed replay

Command:

```bash
/usr/bin/python3 patches/patch_background_verification.py --check --agent-dir <temp-host>
/usr/bin/python3 patches/patch_background_verification.py --apply --agent-dir <temp-host>
```

Behavior:

- `0`: complete patch present / successfully applied.
- `1`: pristine source present under `--check`.
- `2`: anchor drift, partial patch, old/new fragment mixing, or invalid generated Python.

The patcher verifies all complete NEW fragments exactly once. A partial patch is rejected even when a single marker or one new fragment exists. Both output files are compiled before either is replaced. A drifted anchor leaves both files byte-identical.

## Tests run

Primary command:

```text
/usr/bin/python3 -m pytest tests/test_background_verification_patch.py -q
21 passed in 17.77s
```

Neutrality gate:

```text
/usr/bin/python3 -m pytest tests/test_repo_neutrality.py -q
17 passed in 0.82s
```

The 21 tests cover:

- real `ProcessRegistry.spawn_local("pytest tests -q")` in a temporary project, followed by the real `model_tools.handle_function_call("process_manage", ...)` dispatch; confirms `session_id` / `task_id` propagation, exit 0, recorded evidence, and `build_verify_on_stop_nudge(...) is None`;
- completed success and failure;
- repeated wait/poll observation records once;
- no exit code, spawn receipt, running, unknown/lost reason, truncated output, and handler/record mismatch are rejected;
- owner, session, and missing-caller mismatches are rejected;
- an edit by another session after process start prevents a pass;
- the harder ordering — old test start, other-session edit, other-session passing event, old completion — remains rejected via the newer-event high-water check;
- evidence-layer exception does not fail `process_manage`;
- `echo pytest`, `printf pytest`, `cat pytest` do not create evidence;
- patch apply idempotence, check-only behavior, anchor drift with zero writes, and partial-patch rejection.

All tests operate on a temporary staged host composed mainly of symlinks, with only the two target files copied. `<hermes-host>` was not modified by patch apply/check. Host Python was used as the child process interpreter; the outer test suite was invoked by `/usr/bin/python3` as required.

## Known limits (fail closed)

- The existing classifier recognizes `pytest` and its documented spellings such as `python3 -m pytest`. It does **not** recognize an absolute interpreter path such as `/usr/bin/python3 -m pytest`, or a leading shell `cd ... &&`: the current foreground classifier rejects those too. No broader matcher was added. Such background completions remain unrecorded.
- A command whose rolling 200k character output buffer has dropped bytes is rejected, because complete evidence cannot be proven from the tail alone.
- Recovered receipt snapshots (`load_completed_results`) are not accepted as proof of observation; only a live registry record completing in the current runtime is eligible.
- Foreground recorder behavior is unchanged unless `started_at` is explicitly supplied. Independent integration also ran the host's existing evidence, disabled-ledger, descriptor-leak, stop, stop-cache and continuation-budget regressions against temporary patched packages: 57 passed in 9.20s. An initial harness run was 56 passed / 1 failed because the host test home-I/O guard rejected a dependency-store metadata probe under the temporary default home. Separating the temporary HOME, HERMES_HOME and HERMES_RUNTIME_DIR fixed the harness without product changes.
- This is experimental and not deployed. Activation requires a staged host reload, which was intentionally not performed.

## Boundary / write set

Modified files:

- `patches/patch_background_verification.py` (new)
- `tests/test_background_verification_patch.py` (new)
- `docs/handoff/2026-11-01-background-verification.md` (new)

No runtime module was needed. No profile/database/config file was modified. No deploy, restart, message send, network model call, commit, or push was performed.

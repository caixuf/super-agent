# Conversation reliability repair — 2026-11-01

## Scope and baseline

Baseline commit: `56fea05`, including the pre-existing uncommitted working tree.
All investigation and implementation used isolated copies. Existing source edits
were preserved. No credentials were copied into reports. No gateway restart,
platform send, model API request, push, or production deployment was performed.

## Confirmed: an announced next step can end the turn

A recorded response ended with “先查清是不是……引发的。” and
`finish_reason=stop`. There was no intervening tool call before the next human
message. This proves an early model stop in that sample; it does not prove a
streaming failure or imply that every apparent truncation has the same cause.

The host already has a bounded continuation guard:
`agent/turn_final_response.py` calls `trailing_continue_intent` from
`agent/agent_runtime_helpers.py`. The inspected classifier covers narrow English
action tails but misses the observed Chinese tail. Its guard is independent of
the API-specific acknowledgement opt-in, and already checks available tools,
`agent.stall_guards`, and a two-continuation bound.

### Change

`patches/patch_chinese_continue_intent.py` adds a narrow Chinese inspection/test
tail detector to the existing classifier. It does not create a new loop,
scheduler, background task, or model caller. Advice, quoted examples, approval
waits, deferred work and completed statements are excluded. The original
400-character response cap and host retry bound remain. This is a targeted
mitigation, not a general natural-language task-completion oracle.

Supported host shape is checked before writing. Partial patches, safety-guard
drift and mismatched anchors are refused. Reapplying the exact patch is a no-op.
Only apply to a reviewed host tree; a running process is not changed by writing
its source files.

```sh
python3 patches/patch_chinese_continue_intent.py --check --agent-dir <host-root>
python3 patches/patch_chinese_continue_intent.py --apply --agent-dir <staged-host-root>
HERMES_SOURCE=<host-root> python3 -m pytest tests/test_chinese_continue_intent_patch.py -q
```

`--check`: 0 means the supported patch is present, 1 means absent, 2 means the
host is missing/unsupported/drifted. `--apply` does not restart anything. Rollback
is restoring the backed-up target file; do not reset the entire host checkout,
which may contain independent patches.

### Verification

Tests first demonstrate that the unpatched classifier misses the sample. They
then execute the patched classifier and the real host `finish_text_response`
in temporary package copies, without a live profile or model. Real finalizer
checks cover the first two continuations, exhausted budget, disabled guard,
missing tools, and normal final answers. Unrelated stop gates and output
transport are replaced at their boundaries in that focused test; this is not
a real platform delivery test. The inspected host's 46 existing stall/intent
regressions also passed against a temporary patched package overlay.

## Independent work

- Command registration: see `2026-11-01-conversation-commands.md`.
- Background verification evidence: see `2026-11-01-background-verification.md`.

## Unresolved / not claimed

- Real user-facing card truncation and fallback duplication are separate from
  the confirmed early-stop sample and have not been fixed in this batch.
- Historical restart-drain timeouts require a separate runtime version check.
- No production behavior change is claimed until deployment, loaded-version
  verification and a user-initiated channel acceptance exercise are complete.

## Baseline attribution

Before integrating worker changes, the inherited baseline suite reported
`4 failed, 1906 passed, 30 skipped` in 248.36 seconds. Three failures came from
`AF_UNIX path too long` because the chosen temporary-directory prefix exceeded
the socket path budget. Re-running `tests/test_memory_recall_service.py` with
a short temporary root produced `21 passed in 2.58s`, without source changes.

The other failure was the existing machine-topology golden evaluation: the
entity store automatically imported live profile memory into the temporary
evaluation database, changing the top-three retrieval results. This is a
non-hermetic baseline problem, not a continuation-patch regression. Setting the
existing `SUPER_AGENT_MEMORY_FILE` override to a deliberately empty temporary
file selects the built-in seed fallback instead; `tests/evals` then reported
`5 passed in 0.18s`. Both modes must be reported separately. No production
memory file or entity-store implementation was modified to force a pass.

## Independent integration checks

The reviewed command repair was copied into the integration checkout. A follow-up
regression covers a resolver that fails only on plain names while still correctly
reporting occupied aliases; aliases must be checked independently. A separate
real `PluginManager` test loads the actual plugin into a temporary profile and
checks registered names, help/usage consistency and honest unwired-control
responses. No model or platform transport is used.

The previously flaky
`test_terminal_hook_does_not_race_or_block_slow_running_edit` passed five
consecutive isolated runs (2.23s, 4.20s, 4.18s, 4.18s, 4.18s). This is bounded
repeat evidence, not proof that every timing race has been eliminated; no timing
assertion was weakened by this repair.

## Deployment checklist — not executed

1. Review only the repair diff against the captured working-tree baseline.
2. Back up the host target files and profile plugin, recording checksums and
   permissions. Preserve unrelated local patches and current task state.
3. Stage both patchers on a host copy, run `--check`, `--apply`, then `--check`
   again and the focused host tests. Unsupported anchors block deployment.
4. Copy only the reviewed plugin and host changes during an authorized window;
   `install/install.sh` copies patch files but does not automatically apply these
   two new patchers. This batch deliberately does not change that default.
5. Use one authorized reload/restart method, check the loaded versions, and
   verify a user-initiated task in the original channel. Do not restart a gateway
   merely to demonstrate activity or apply changes to other instances at once.
6. On failure restore only backed-up touched files, reload by the same reviewed
   method, and retain evidence for investigation. A full checkout reset is not
   an acceptable rollback when unrelated modifications exist.


## Final isolated verification

The reviewed integration checkout (before the final background-evidence files
were added) completed the full repository suite with the existing memory-file
override pointing to an empty temporary file and a short temporary directory:
`1956 passed, 39 skipped in 240.74s`. Skips include optional dependencies,
explicit-host integrations and real platform credentials. The empty-memory
seed mode is separate from the live-memory baseline failure documented above.

The background-evidence worker then delivered 21 passing patched-host tests,
including a real local process through actual tool dispatch and the existing
verification stop gate. An independent existing-host verification regression
run against temporary patched packages passed 57 tests. No command classifier
was widened; absolute interpreter paths and shell wrappers remain unsupported
by that existing classifier. Recovered or truncated-output receipts, other
owners and stale completions remain rejected.

Static checks: all 12 shell files passed `bash -n`, installation `--dry-run`
returned zero, and `git diff --check` passed. No new patcher was applied to
the installed host. The final focused integrated test result is recorded below.


Final combined focused run after integrating all three repairs:
`HERMES_SOURCE=<host-root> TMPDIR=<short-temp> python3 -m pytest
tests/test_background_verification_patch.py
tests/test_chinese_continue_intent_patch.py
tests/test_conversation_commands_real_host.py
tests/test_task_progress_plugin.py tests/test_repo_neutrality.py -q`
reported **135 passed in 88.28s**. This includes the complete 51-test command
plugin file, 45 Chinese-continuation cases, 21 background-evidence cases,
one actual PluginManager test and 17 repository-neutrality checks.
The original source fingerprints were checked before delivery; only the ten
reviewed repair files were synchronized, preserving all unrelated edits.


Post-delivery read-back and checks in the original project working tree:
- The 10 delivered files matched the reviewed integration bytes. All 558
  pre-existing files outside the two changed tracked files retained their
  captured fingerprints.
- Delivered patch/real-host/neutrality tests: **84 passed in 22.89s** (the
  complete command plugin suite is included in the 135-test integration run).
- Final Chinese-patched temporary host: **46 passed in 1.36s** on existing
  stall/intent tests. The earlier temporary overlay had an older draft; exact
  patch checking correctly refused that draft before rebuilding from original
  host source and applying the final patch.
- Both installed-host `--check` commands returned 1 (absent), as intended.
  The running gateway remained active with the same process ID.
- `git diff --check` passed. No commit, push, deployment or restart occurred.


## Subsequent authorized deployment

The earlier no-deployment statements above describe the offline repair phase.
After explicit user authorization, commit `27ca8ba` was pushed and deployed.
See `2026-11-01-conversation-deployment.md` for activation, health checks,
remaining acceptance boundaries and the targeted rollback procedure.

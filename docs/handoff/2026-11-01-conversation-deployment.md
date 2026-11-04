# Conversation repair deployment — 2026-11-01

## Authorization and release

The user explicitly authorized push and deployment after offline acceptance.
Repair commit: `27ca8ba9b6bdab9f97af63188f02b03e9418239d` on `main`.
A normal push advanced remote `main` to that commit; no force push was used.
Before committing, remote `7490725` was fast-forwarded; its two card-compatibility
files did not overlap this repair. Its eight tests passed.

Only the ten reviewed repair files were committed. The original working-tree
changes remained uncommitted. Their added/deleted lines exactly match the
captured baseline; only blob metadata and hunk line numbers changed. The two
original untracked files retained their SHA-256 fingerprints.

## Deployment scope

At deployment the target gateway reported zero active agents. The three host
modules and existing profile plugin were backed up with permissions and hashes.
Only these runtime files changed:

- `<host-root>/agent/agent_runtime_helpers.py`
- `<host-root>/agent/verification_evidence.py`
- `<host-root>/tools/process_registry.py`
- `<profile>/plugins/task-progress/__init__.py`

The two patcher scripts were also copied into the profile's managed patch
directory. The deployed plugin retains its already-installed, unrelated
`unverified` display label; that pre-existing label was not included in the
repair commit. No broad installer, identity/config replacement or host update
was run. No credentials or business data were changed.

Backups and machine-local deployment hashes are retained at:
`<profile>/state-snapshots/20261101-111927-conversation-repair/`.
The private `manifest.json` records the exact six target paths, existence,
original modes and backup files; `deployment.json` records deployed hashes.

A single authorized systemd restart activated the changes. PID changed from
1891694 to 2288706; the service remained active/running and `NRestarts=0`.

## Verification

- Pure release snapshot (without pre-existing edits): 135 focused tests passed
  in 86.77s. The independent remote card update passed 8 tests.
- Both installed-host patcher `--check` commands return 0 (present).
- The six deployed file hashes match the deployment record.
- The local health endpoint returned HTTP 200 / `status=ok`.
- Fresh runtime records identify the new PID as the connected messaging adapter
  and API writer. An old different-PID platform failure remains in the status
  map; it is historical, not evidence of a new-process adapter failure.
- Existing-endpoint-only, read-only gateway RPC reported task-progress enabled,
  no load error, four hooks and five registered commands. Command discovery
  returned `longtasks`, `longtask`, `taskstatus`, `taskpause`, `cancel`; pause
  and cancel help correctly says they are unwired.
- Post-deployment smoke tests copied the installed patched modules into temporary
  projects: 4 passed / 17 deselected in 4.57s. They cover a real background
  process through dispatch and the stop gate, successful deduplication, a stale
  completion after a newer event, and observer failure not breaking results.
- `git diff --cached --check` and the working-tree diff check passed.

These checks do not invoke a model or send a user-channel message. A real
user-initiated conversation acceptance exercise is still distinct from this
health and isolated-process validation. Card truncation/fallback issues and
actually wiring pause/cancel remain outside this release.

## Targeted rollback

Do not reset either repository or reinstall all plugins. During an authorized
maintenance window, ensure the gateway is idle, then for each manifest entry:
restore the backup with its mode if `existed=true`; otherwise remove only the
newly deployed file after checking its hash still matches `deployment.json`.
Before restoring, verify each current file still has the recorded deployed
hash; if it has drifted, review that later change rather than overwriting it.
Restart the same service once, then repeat the health and command discovery
checks. Preserve the backup, runtime status and diagnostic evidence.

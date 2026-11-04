# Memory health diagnostic — 2026-11-01

## Delivered

- `tools/memory_health.py` — aggregate-only, read-only JSON CLI.
- `tests/test_memory_health.py` — focused unit/integration tests.
- `docs/handoff/2026-11-01-memory-health.md` — this handoff.

No commit, push, deployment, worker start, queue consumption, spool ingestion,
schema migration, or production configuration load was performed.

## Contract

Required arguments:

```sh
python3 tools/memory_health.py \
  --entity-db PATH \
  --memory-file PATH \
  --spool-dir PATH \
  --profile NAME \
  --user-id ID \
  --agent-id ID
```

The command:

- resolves the entity database and memory source before access;
- opens SQLite only through a `mode=ro` URI (plus `query_only`);
- uses `immutable=1` for non-WAL databases so a read cannot create `-wal` or
  `-shm`;
- requires an existing `-shm` before reading an active WAL database, rather
  than creating one;
- never instantiates `EntityStore` or `TurnSpool`;
- never creates a path, database, spool record, or lock;
- reads spool records directly as JSON and reports aggregate state counts for
  the explicitly supplied profile/user/agent identity;
- does not print memory text, spool content, identities, paths, SQL errors, or
  exception messages;
- does not import `agentcore` configuration or runtime modules.

A healthy diagnostic snapshot returns exit code 0. `unhealthy`, `unknown`, and
`degraded` snapshots return exit code 1. Invalid arguments return exit code 2
with JSON that does not echo argument values.

## Report semantics

`entity_index` reports entity count, rule count, index row count, imported-rule
count (`rule_id` prefix `rule-mem-`), invalid timestamp count, quick-check
status, and `max(updated_at)`.

`memory_source` reports filesystem mtime and size metadata only. The file body
is not read.

`sync` compares memory-source mtime against indexed `max(updated_at)` and flags
a possible stale index when source mtime is newer. This is explicitly marked
`memory_mtime_is_heuristic: true`; mtime is not proof that the memory file's
content was imported or that the same process wrote each side.

`spool` reports state counts only for the specified identity. It separately
counts invalid JSON, invalid records, identity mismatches, and unreadable
records. Oldest completed age uses record-file mtime and is explicitly marked
as heuristic.

Missing inputs are `unknown`; structural or integrity failures are `unhealthy`.
They are never represented as healthy zero-row datasets. Some recoverable
deficiencies, including an empty source file, stale-index risk, missing import
markers, malformed spool records, and index/count drift, are `degraded`.

`worker_liveness` is always `not_inferred`. Spool contents and state counts do
not prove that a worker process is running, stopped, or making progress.

## Tests

Focused suite:

```sh
PYTHONPATH=. pytest -q tests/test_memory_health.py
```

Coverage includes:

- missing inputs remain missing and zero paths/files are created;
- corrupt and structurally incomplete databases are not reported healthy;
- identity isolation and complete state counts;
- invalid JSON, malformed records, bad states, unreadable entries, and oldest
  completed age;
- source/index timestamps, imported-rule counting, and heuristic sync risk;
- content and exception sentinels never appear in output;
- live WAL database and WAL bytes remain unchanged;
- no missing `-shm` creation, and no sidecars for non-WAL databases;
- symlinked database/spool inputs resolve correctly;
- every SQLite connection uses `mode=ro` plus URI mode;
- worker liveness is never inferred; completed records flag pending ingestion without asserting worker failure.

## Known boundary

The CLI is a point-in-time artifact diagnostic. It does not establish worker
liveness, prove end-to-end ingestion, validate memory semantics, or replace a
supervised worker health check. It does not consume spool records.

## Verification

Final isolated verification root and result are reported in the delivery
message. The repository's pre-existing uncommitted changes were preserved.

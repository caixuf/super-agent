# Curated memory refresh

Curated Markdown now has an independent background watcher. Previously a file
could change while the fact index stayed at an older revision until a manual
sync. The watcher waits for a stable content hash, then calls the administrator's
trusted sync hook with that exact revision. Unchanged polls read only the bounded
source file; they do not open the fact database or invoke a model.

Run under a process supervisor, separately from turn ingestion:

```sh
python3 -m agentcore.memory.document_watcher \
  --bootstrap host.memory_profile:configure \
  --sync-hook host.curated_profile:sync \
  --source /absolute/profile/memories/MEMORY.md \
  --user personal_owner --agent personal_agent
```

The hook accepts `(source, expected_revision=...)` and must return `ok=True` and
the published `revision`. Use `curated_sync.sync_document` for immutable source
snapshots, staged vectors and transactional publication. Never configure hooks
from conversation text. Configured user and agent must match the command line.

Failures retry after 30 seconds by default; changed content starts a new settling
period. Logs contain state, hashes and exception type, never exception bodies or
source text. A changed file during sync is not marked current. A restart safely
reconciles the current source again. Use a stop timeout longer than the hook's
index flush timeout. Stopping this watcher leaves ingestion and recall running.

Candidate replay with an explicit content-addressed fact id now checks that id
by primary key and validates owner, bucket, subject, statement and source. This
avoids scanning a large owner's library once per unchanged section. An identity
collision fails without overwriting another fact. The general deduplication path
is retained for new facts and callers without an explicit candidate id.

Focused tests cover real publication and revision retirement, independent fact
preservation, no work on unchanged polls, failed publication, mutation during
sync, missing/oversized sources, bounded retry and identity collisions. A host
rehearsal used the actual daemon and a real 1024-dimensional vector backend in a
fresh isolated store: two revisions published, old fact deprecated, independent
fact retained, graceful exit zero. This does not prove large-library latency,
semantic quality of arbitrary source edits or correctness of restoring obsolete
facts. Those remain separate evaluation and governance requirements.

# Transactional updates for the memory read view

The read view needs to see new facts and revoked facts before its next answer.
A periodically copied database cannot guarantee that. The new opt-in
`memory.change_journal` provides the capture and checkpoint layer; it does not
yet build a read view, run a service or change the ordinary recall path.

An administrator installs the journal on an existing canonical memory database.
Nine SQLite triggers capture inserts, updates and deletes on `working_fact`,
`fact_tags` and `fact_embeddings` in the same source transaction. A primary-key
rewrite records both the old and new identity. Rollback cancels both the source
mutation and its change records, including a capacity failure halfway through a
bulk update. Ordinary direct SQL writers are covered without importing Python
callbacks. Importing the module installs nothing.

A reader registers a leased, fenced consumer before copying its baseline. It
loads the baseline inside `read_baseline`'s read snapshot and then acknowledges
that snapshot's head. Writes during the copy remain pinned in the log. Before
serving, it uses `read_changes` to load dirty facts, tags and embeddings in one
read snapshot, applies them atomically to its local view, and only then calls
`acknowledge`. A partial batch is not ready to serve. Acknowledgement is explicit:
the journal cannot know whether the caller actually finished applying its view.

Every checkpoint binds a database-file identity, generation, schema version,
sequence position and random event token. Missing events, rewritten consumed
history, source-file replacement, schema changes and lost consumer ownership
raise `JournalError`. Restoring the exact text of a removed trigger does not make
an existing reader valid again. Source columns and index definitions are also
fingerprinted, because a new unique constraint can change replacement behavior.

Defaults bound the log to 65,536 events and consumers to 32. A full log aborts a
source mutation with `memory_view_change_capacity`; it never skips capture.
Each event contains only a bounded fact id, entity, operation and token, not the
fact text. `prune` retains all events needed by unexpired consumers, removes
expired consumers, and advances a durable floor token transactionally. Optional
recent-event retention is capped at one quarter of the event budget so a small
budget does not permanently prevent progress. A lease cannot be revived after
expiry; a restarted reader must register a new owner and rebuild its baseline.

The view service must still integrate lease renewal during long builds,
incremental application, memory limits, readiness withdrawal, source migration,
fault reporting and rebuild. Journal capture remains disabled in production
until that consumer and its retention loop exist. Otherwise the bounded log
would eventually backpressure writes with no consumer to drain it. This layer
alone does not prove recall quality or the cold-cache latency requirement.

Validation: 20 focused tests and the complete working-tree suite (849 passed,
9 skipped) passed. A full 1.92 GB isolated-store rehearsal streamed and hashed
177,295 facts, 886,656 tags and 177,299 embedding rows while an external writer
committed a new fact using the real 1024-dimensional embedding backend. The new
fact was absent from the baseline snapshot and present in the next delta. A
single transaction updating text, scope and vector produced three changes;
retirement and physical deletion were also observed. A deliberately removed
tail event rejected reading; restoring it allowed recovery and bounded pruning.
The rehearsal did not create journal tables in the production database. It
hashed a baseline rather than constructing a serving view, so neither memory
footprint nor query latency of that future view has been proven.

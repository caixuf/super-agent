# In-memory memory recall with transactional updates

`memory.read_view.MemoryReadView` copies the canonical SQLite read snapshot into
an owned in-memory SQLite database, then consumes the opt-in change journal.
Recall uses the existing candidate search, filters, cosine ranking, confidence
weights and score floors. It does not replace semantic search with a smaller
corpus or introduce an approximate index.

Registering before the copy pins concurrent changes. The copy uses online backup
inside the baseline read transaction. The memory copy removes its capture
triggers and journal control tables so applying changes cannot create an
independent history. Before every query, dirty facts, tags and embeddings are
loaded from one source snapshot and applied in one memory transaction. Fact
rowids are retained and affected vector entries are updated. Acknowledgement
follows successful application; pending or stale embeddings postpone it.

Missing events, replaced sources, lost leases or failed application withdraw
readiness. An invalid view must be closed and rebuilt. Pending embeddings and
bounded catch-up can be retried without serving the old view. Closing a borrowed
source leaves that caller's connection open. Importing the module or calling
ordinary `recall_facts` does not build a view or install change capture.

The snapshot and growing SQLite page count have an explicit `max_bytes` budget.
Incremental batches are bounded by event count and payload bytes (64 MiB by
default); supported SQLite builds also cap row length during loading and restore
the borrowed connection's previous limit afterward. This is not a complete
process RSS limit. The separate recall service adds address-space limits, idle
renewal and recovery witnesses; deployment still needs supervision and alerts.
The constructor
renews through a separate connection during a long file-backed backup. Active
queries renew leases as needed and prune acknowledged changes; a dormant caller
must call `refresh` periodically before its lease expires.

Eight focused tests cover exact-engine equivalence, correction and retirement,
physical deletion, concurrent backup handoff, pending embeddings, missing
history, snapshot/delta budgets, default identity/scope filters and operation
without the optional vector extension. A full-size
1.92 GB isolated-store rehearsal used real 1024-dimensional vectors. With an
isolated source inode cache-eviction request before each query, five measured
calls took 2.898, 0.586, 0.564, 0.543 and 2.370 seconds. Those cover a ready view,
an external write, atomic text/tag/vector correction, retirement and deletion.
Missing history withdrew readiness, and rebuilding recovered. Initial building
took 8.383 seconds and rebuilding 16.468 seconds; these readiness costs are not
included in query latency. Peak RSS was 2,057,616 KiB. The cache request does not
guarantee a hardware-cold start.

The independent 21-case memory-view quality gate passed: positive hit rate 100%,
negative false-hit rate zero, positive precision 90.48%, no cross-user result,
maximum query latency 0.592 seconds. This is a small evaluation set and isolated
read-view evidence, not production acceptance. No production journal tables,
recall service or routing switch were enabled by this work.

# Recoverable memory maintenance

The maintenance plan takes an online SQLite snapshot, reopens it with the vector
extension and checks integrity. Explicitly selected source/alias directories are
copied with per-file hashes. A real rehearsal initially failed because copying
only the database omitted its curated-source dependencies; the complete bundle
now includes those resources. It can be copied into a fresh durable backup
directory and validated independently of the original rehearsal directory.

Only snapshot-proven orphan index records are eligible for cleanup. Under one
live write transaction, each record is checked again: a newly attached fact or
changed index record is kept. Facts are never rewritten by this operation.
Concurrent writes wait for the transaction and retain their data afterward.

On a full deployment-size database, 23 orphan embedding records and 128 orphan
vector records were removed in an isolated copy, restored exactly, and removed
again. Fact text and valid tags had identical hashes throughout. The same plan
then removed the 151 records in production. All three audited index tables had
zero orphan IDs and zero active-fact coverage gaps afterward.

Curated publication now records both source and alias revisions. An isolated
rehearsal restored the 28-section historical document, recalled its original
topic, and republished the exact current 18-section manifest and alias version.
A newly written independent fact survived both switches. The production prompt
file was unchanged. Snapshot recovery never replaces the live database file.

The final host verify script passed 779 tests, with 9 optional native-binding
skips; shell checks and installation dry-run passed. Full semantic preservation
of every compressed statement and automatic compression publication gates are
still separate, unfinished requirements.

Performance evidence is deliberately split: one production evaluation retained
all relevant results but failed its 8-second latency gate at 8.25 seconds.
Diagnostics found rotational-storage waits while reading candidate embeddings;
the same three hot-cache queries took about 0.81–0.85 seconds. A later warmed
19-case run passed with a 4.37-second maximum. The warmed result does not prove
the cold-latency problem solved.

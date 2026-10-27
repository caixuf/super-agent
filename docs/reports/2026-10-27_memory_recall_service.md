# Supervised memory recall

`python3 -m agentcore.memory.recall_service` runs one memory view behind a private
Unix socket. Requests bind the configured user, agent, database-path hash and
loaded service build. Only health and recall are exposed; request arguments,
deadlines and response sizes are bounded. No query text, fact body or provider
exception body is written by the service's state logger.

The caller must explicitly install the change journal before starting this
service. Nothing installs capture or switches callers merely by importing these
modules. Use administrator-owned bootstrap code and private socket/state
directories. The command requires user and agent to match the configured memory
principal. An unavailable selected service fails instead of silently running a
slow direct query.

```sh
python3 -m agentcore.memory.recall_service \
  --bootstrap host.memory_profile:configure \
  --socket /private/memory/recall.sock \
  --checkpoint /private/memory/checkpoint.json \
  --user personal_owner --agent personal_agent \
  --view-mib 3072 --delta-mib 64 --address-space-mib 4096 \
  --initialize
```

`--initialize` allows the first checkpoint only. It cannot overwrite an existing
corrupt witness or waive source validation. Later starts omit that flag. The
checkpoint is private, atomically replaced and fsynced outside the source
database. It records identity, generation, schema version and an acknowledged
history position/token. It is saved **before** the source cursor advances and
before old events can be pruned. Failure prevents serving the newly applied
view. Normal restart validates the witness before constructing a new baseline.
Older backups, changed sources and unavailable checkpoint history require
reviewed recovery; restarting does not authorize copying arbitrary old data.
The witness itself must not be rolled back with the database backup. This is
not a substitute for the outstanding semantic correction/revocation replay.
Normal restart may advance the checkpoint if the source committed more changes
after the last query. Byte-for-byte checkpoint equality is not the invariant:
identity/history must match and the cursor must never move backwards.

Idle maintenance renews the lease, applies changes and prunes acknowledged
events. It also renews while embeddings are pending. Pending work can recover;
invalid source/history or a failed view withdraws readiness. State transitions
are structured local events. A process supervisor, durable alert delivery and a
reviewed rebuild policy remain deployment responsibilities. The daemon does not
automatically reset recovery history or issue group messages.

Snapshot pages, delta payload and request/response sizes have separate budgets.
The CLI enforces a process address-space limit with RLIMIT_AS; this is not a
cgroup RSS/file-cache budget. Production units must apply their own memory and
restart limits. Safe stop releases the consumer and removes the socket. Exclusive
locks prevent two writers of the same socket/checkpoint.

Validation includes 25 focused service/view tests: checkpoint failure ordering,
older-source rejection, idle renewal, configuration drift, payload caps, protocol
identity/build/type checks and duplicate owners. A 1.92 GB isolated-store daemon
rehearsal used real 1024-dimensional vectors, a six-second lease and an eight-second
client-free idle period. Four source-cache-eviction IPC calls took 0.648, 0.664,
0.960 and 0.485 seconds. Normal checkpoint-validated restart passed. Peak RSS was
2,050,904 KiB, and the actual address-space soft limit was 4 GiB. Startup/restart
readiness took 16.628/16.827 seconds, separately from query latency.

The existing host CLI completed a real socket round trip in 0.154 seconds and
returned exit 2 in 0.143 seconds while the selected service was stopped. The
21-case quality suite also passed through IPC: positives all hit, negatives had
no false hit, precision 90.48%, no cross-user data, maximum 0.664 seconds. The
foreign-principal probe was explicitly denied. Cache eviction is best effort on
the isolated source inode, not a guarantee of hardware-cold state. No production
journal, service unit or caller routing was activated in this rehearsal.

A subsequent full IPC repeat measured 2.746/0.706/0.635/0.499 seconds, with the
21-case maximum at 0.645 seconds. Its CLI round trip was 0.145 seconds and stopped
service failure 0.139 seconds; diagnostics confirmed the socket transport. A
prior harness incorrectly required byte-identical checkpoints across restart;
that failure was retained and replaced with source/history/monotonic-cursor
checks. The full working-tree verification passed 874 tests with 9 skips.

Further host rehearsals exposed an error in the external cache-eviction helper:
it opened and closed the database file in a process that still owned SQLite
connections. On Unix, that close can release the process's SQLite file locks;
see [SQLite's locking warning](https://sqlite.org/howtocorrupt.html#_posix_advisory_locks_canceled_by_a_separate_thread_doing_close_).
Two isolated stores later failed freelist checks. Earlier host latency figures
remain historical observations, not proof of storage safety. Neither damaged
store was installed as canonical data.

The host now executes raw-file cache/page diagnostics in independent processes.
A real writer-lock control on SQLite 3.45.1 reproduced loss of exclusion after
the old same-process close; the new helpers preserved exclusion. A fresh complete
daemon rehearsal then passed both initial and final quick_check, including
external write, retirement, restart and fixture cleanup. Four IPC calls measured
0.769/0.649/0.636/0.517 seconds; the 21-case quality maximum was 0.612 seconds.
Sequential reads aid integrity scans only, separately from timed queries, each
of which still requests source-file cache eviction through the safe helper.

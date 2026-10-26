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

Validation includes 24 focused service/view tests: checkpoint failure ordering,
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

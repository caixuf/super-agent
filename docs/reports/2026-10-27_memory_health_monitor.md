# Durable recall health observations

The independent observer reads recall health without invoking a model. It binds
the expected principal and loaded service build and can consult a host resource
probe for kernel memory limits, OOM history and source-journal pressure.
Observations whitelist status and numeric metadata; memory text, raw provider
errors, credentials and arbitrary response fields do not enter events.

The existing alert outbox now records health faults and recoveries in the same
transaction as durable debounce state. A restart cannot duplicate an active
episode or lose its grace window. Healthy startup emits no notification; faults
and recoveries have distinct stable IDs and a correlation key. Clock regression
restarts grace observation instead of incorrectly maturing it. Pending events
are never trimmed. Only delivered history owned by this health API can expire;
row, payload and condition counts have explicit budgets.

The observer uses a dedicated bounded queue, with page and WAL guards, and never
constructs a sender. Delivery still requires an explicitly configured transport
and a real receipt through the existing outbox contract. Queue persistence or
capacity failures exit visibly for supervision rather than discarding events.

Offline tests exercise durable grace/restart behavior, flapping, capacity rollback,
delivered-history retention, clock regression, input budgets, response redaction,
binding drift, source pressure and invalid resource probes. These tests do not
prove production supervision, actual fault injection or messaging delivery.
The module is not activated merely by publication or import.

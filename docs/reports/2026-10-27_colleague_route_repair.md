# Addressable named-colleague dispatch

A live user request reached a named worker through a raw queue CLI. The worker
completed and produced three artifacts, but the job had no persisted origin and
no platform receipt. A worker being healthy did not imply its result was routed.

The public durable tools now accept an optional administrator-defined `role`.
The default `analysis` role preserves existing calls. Additional roles configure
only their queue root, approved source roots and denied globs through the
`colleagues` setting. Task arguments cannot choose queue paths, owners, runners
or model adapters. Each service still validates sources and stores the trusted
session origin; status and cancellation require the same role and owner.

When completion delivery is enabled, registration requires an addressable
submitting route by default. A missing route fails before job submission. Each
configured queue gets the existing persistent direct-delivery bridge; artifact
snapshots and uncertain-send fences remain scoped to its own attempt directory.
The routing instruction explicitly prohibits raw CLI submission for interactive
work. The CLI remains available for administrator-owned offline work that does
not promise a conversation reply.

Role queues must be distinct. Old unaddressed work is not assigned to a recent
chat. Any repair requires separate, explicit provenance from the actual request
and tool receipt; it is not evidence that the original dispatch path worked.
The live named-worker request still needs actual delivery receipts and original
card association before its platform acceptance can be claimed.

Four new offline tests cover per-role source policy, real persisted origins,
all configured backfill registrations, session isolation, missing-route refusal,
forged configuration and duplicate queue rejection. Existing routing/backfill
tests are retained. Production activation and live acceptance are recorded by
the host separately.

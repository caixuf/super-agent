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

## Live recovery result

The old job ddaf0ad5793a4a758ab5fb70940e83ed was attached to its exact
Feishu request only after the persisted request text, tool call receipt, queue
role and source line range matched. The repair ledger recorded the proof hash;
it did not edit the job payload or result and did not resend the analysis.
The existing completion worker then delivered the summary and all three files,
and the outbox recorded its receipt. This proves recovery of that one legacy
job, not that a fresh natural request follows the new role-aware tool path.

The gateway loaded eight role queues and Feishu reconnected after a transient
DNS failure without a second restart. T2 still needs a fresh natural request
and user confirmation that its completion appears on the intended original
card. The health monitor remains code-only; it has no production unit or
operations-group delivery yet.

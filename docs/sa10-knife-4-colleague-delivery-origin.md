# Super Agent 1.0 knife-4 — colleague delivery / origin session

Short handoff (2026-11-13, Asia/Shanghai). Knife-1 fail-closed receipt, knife-2 version **1.0.0**, and knife-3 named-runner visibility stay closed. Prefer-B1 remains PARK. The SQLite pool remains the job ledger; no Temporal / second scheduler / StateGraph-as-ledger.

## What landed

- Completions return only to the persisted `jobs.origin` chat (`route` + `channel` + `chat`, plus `thread` when the submitting turn had one). They never land in a dropped, rebound, current, or otherwise wrong chat, and never in the colleague `hermes_session_id`.
- A missing IM `chat` or `channel` is incomplete, not a wildcard, and is not filled from a later `plugin_messages.resolve`. `routable()` parks those receipts; `HostSender.send` / create / patch refuse with `not_sent`; `deliver_once` leaves them pending and inspectable.
- `record_colleague_completion` and gateway handoff require a complete IM `owner_origin` before creating a sendable outbox row. A route-only origin leaves `delivery_job_id` unset and retryable. A dropped / unresolvable route stays pending — no consolation send.
- `attach_origin` stays proven / terminal / one-shot and is not expanded to backfill a missing chat without host proof. C2 remains ≤1 terminal notify, ack only on transport acceptance.

## Pointers

- Transport / match: `runtime/hermes_completion_transport.py` (`_origin_matches`, `HostSender`).
- Drain / outbox: `agentcore/completion_backfill.py`, `agentcore/completion_delivery.py`, `agentcore/pool.py` (`record_colleague_completion`).
- Colleague handoff: `agentcore/colleague_gateway_dispatch.py`.
- Wiki one-liner: `docs/wiki/04-Feishu-Cards-and-Delivery.md`.

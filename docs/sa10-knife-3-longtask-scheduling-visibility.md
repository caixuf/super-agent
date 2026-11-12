# Super Agent 1.0 knife-3 — long-task / scheduling visibility

Short handoff (2026-11-13, Asia/Shanghai). Knife-1 fail-closed receipt and knife-2 version unify **1.0.0** stay closed. Prefer-B1 remains PARK. The SQLite pool remains the job ledger; no Temporal / second scheduler / StateGraph-as-ledger.

## What landed

- Feishu / bound-IM running and progressive task cards show `{display_name}在进行` (CardKit may split `{display_name} · 在进行`). Name comes from catalog / CardHandle / `_colleague_name`, then `display_label`, then `role_id`. No invented nicknames, no nameless generic-only running header.
- Queued / stalled / lost / terminal keep existing verdict words, but the header still carries the same runner name. After a Prefer-D4 stalled|lost transition the card leaves `「在进行」`.
- `DispatchService.status` / `list_owned_statuses` are short-path ledger reads and now surface `display_name`, `scheduled`, `verdict` / `phase`, `heartbeat_age_s`, `cancellation_requested`, and `status_phrase`. A no-`task_id` status lists recent owned jobs, including scheduled jobs whose registered route matches the caller (`_owned` route rule unchanged; `cross_session_status` default stays off).
- Ad-hoc and route-matched scheduled jobs on a bound delivery conversation remain cancellable via Prefer-D1/D2 + `pool.cancel` and reach a real terminal. C2 notify stays ≤1 on true terminal. D2 clocks are unchanged.
- Stall discoverability stays Prefer-D4 (`stalled_card_bridge`) + `pool.reconcile_watchdog` as the ledger watchdog. Silence is not treated as done. Scheduled Feishu/IM jobs are included.

## Pointers

- Card writers: `agentcore/task_card_bridge.py`, `agentcore/cardkit2_renderer.py`, `agentcore/stalled_card_bridge.py`, `agentcore/feishu_transport.py` (`colleague_card_payload`).
- Status / progress copy: `plugins/durable_dispatch`, `plugins/task_status`, `plugins/task_progress`, `agentcore/task_progress.py`.
- Wiki table: `docs/wiki/04-Feishu-Cards-and-Delivery.md`.
- Tests: `tests/test_sa10_longtask_scheduling_visibility.py` (LV-1..LV-8) plus existing knife-1 reject tests (do not rewrite).

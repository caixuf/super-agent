# Prefer-D3 — durable Tab follow-up inbox + fast-ack SLO (thin)

Date: 2026-10-31 Asia/Shanghai  
Owner: 焊枪  
Branch: `feat/prefer-d3-followup-durable`  
Base tip: `9cc59db` (Prefer-D2 #8 MERGED) — reconfirm at PR open.

## Knife (FINAL LOCK · schema-synced)

1. **Durable inbox** — `agentcore/follow_up_store.py` SQLite beside pool (`follow_up.db`).
   Required: id · task_id · text(≤2000) · kind · created_at(date-time) · state  
   kind: follow_up | urgent_follow_up | replan  
   state: queued | draining | applied | dropped | blocked_irreversible  
   Enqueue crash-safe BEFORE ok-ack; idempotent on id; ledger wins after restart.
2. **Fast-ack SLO** — ControlAck additive: ack_id · ack_emitted_at · ack_latency_ms · slo_class · slo_met  
   Host control_fast p95 classify→ack dict ≤ 2000 ms; Feishu attempt ≤ 5000 ms best-effort.  
   Miss → slo_met=false + log; still ack; no LLM.
3. **No second long exec** (D1) — enqueue/status/pause/cancel: no submit · llm=0. Drain → ONE subsequent turn.
4. **Stack** — barge_in_control + FollowUpInbox(+store) + mark_queued_follow_up durable peek.

## Land set

- `agentcore/follow_up_store.py` (new)
- `agentcore/barge_in_control.py` (wire durable enqueue + ControlAck SLO)
- `agentcore/card_emit.py` (durable peek count helper)
- `tests/test_prefer_d3_followup_durable.py`
- `docs/handoff/2026-10-31-prefer-d3-followup-durable.md`

## PARK (clean)

Temporal · Electron/Jev · Bun/Chroma · cellular/KAD · Prefer-B1 · C1–D2 rework · Prefer-D5 UI · Prefer-D4 stalled/lost (stashed separately) · land-set collision with 手术刀 D4.

## Acceptance

| ID | Result |
|---|---|
| D3-0 | Branch off D2 tip |
| D3-1 | enqueue→restart→peek queued |
| D3-2 | second_exec=false · durable_dispatch_submit=false · llm=0 |
| D3-3 | fake-clock ≤2000 → slo_met=true; >2000 → slo_met=false still ack |
| D3-4 | IRREVERSIBLE → blocked_irreversible (or stay queued) |
| D3-5 | durable before ok-ack (order assertion) |
| D3-6 | CardEmitter durable_queued_follow_up_count from peek |
| D3-7 | classify + D1 flags light regression |
| D3-S* | state / date-time / text≤2000 / dual SLO held |

## Gate

SPEED+: self-test then open PR — **no 找茬 FULL gate**. Boss (扳手) verifies.

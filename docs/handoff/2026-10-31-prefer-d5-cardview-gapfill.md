# Prefer-D5 — CardView / Feishu adapter gap-fill (thin)

Date: 2026-10-31 Asia/Shanghai  
Owner: 焊枪  
Branch: `feat/prefer-d5-cardview-gapfill`  
Base tip: `188725b` (Prefer-D3 #10 MERGED) — re-confirmed `origin/main` before branch.

## Knife (LOCKED)

Prefer-D5 ≡ CardView / Feishu adapter gap-fill — every protocol field that already
exists (or is claimed by D2–D4) MUST round-trip to Feishu same-card without silent
drop. No new product invent; no second writer.

## Landed

### A) Identity still paints on Feishu
`role_id` · `display_name` · `task_title` · phase/stage · `heartbeat_age_s` ·
`attempt_id` · `waiting_role` · `from_bot` · `source_thread` · `hitl_*` ·
title compose · actions tokens — mapped in `colleague_card_payload` (attempt_id
was previously silent-dropped; now paints).

### B) Thin additive (optional; blank-ok)
| Field | Source |
|---|---|
| `queued_follow_up_count` | CardFrame/CardView; `mark_queued_follow_up` stamps |
| `liveness_verdict` | CardFrame/CardView; derived from phase / `mark_stalled` |
| `pool_window` | starting\|canceling; `mark_starting` / `mark_canceling` |
| `ack_miss` | bool; phase `ack_miss` or explicit |

`CardView.render()` emits B only when set. Feishu body lines omit blanks.

### C) Feishu Card Kit adapter
One CardWriter ↔ one card_id; sequence monotonic; uuid idempotent unchanged.
`batch_update` body from `colleague_card_payload` now includes A+B.
Fail → existing text fallback (`fallback=True`); never invents delivered Card Kit.

### D) Store
CardHandle keeps identity A (`role_id` / `display_name` / `task_title`) across
restart. CardFrame carries optional B when set (no new product DB).

### D1/C1 regression
Running cards with `pause`/`status`/`cancel` in actions now render those buttons
(reuse `ops_callback_value`). HITL Approve/Deny unchanged.

## Land set

- `agentcore/task_card.py` — CardFrame + emit optional B
- `agentcore/card_emit.py` — CardView B · derive helpers · mark_* stamps
- `agentcore/feishu_transport.py` — A+B map + D1 buttons
- `tests/test_prefer_d5_cardview_gapfill.py` — D5-1–D5-5 / D5-7
- `docs/handoff/2026-10-31-prefer-d5-cardview-gapfill.md` — this archive

## PARK (clean)

Temporal · Electron/Jev · Bun/Chroma · cellular/KAD · Prefer-B1 · invent new IM
product · second CardWriter · D3 durable store invent · D4 stall action invent

## Acceptance D5-0–D5-7

| ID | Result |
|---|---|
| D5-0 | Branch off confirmed tip `188725b` |
| D5-1 | Feishu identity A present in outbound card JSON when set |
| D5-2 | Optional B round-trip when set; blank omitted |
| D5-3 | sequence↑; one writer; terminal lock unchanged |
| D5-4 | Adapter fail → text fallback; not fake delivered |
| D5-5 | Store restart keeps role_id/display_name/task_title |
| D5-6 | PARK held — no Temporal/Electron/Bun/cellular/B1/product invent |
| D5-7 | C1 HITL + D1 pause/status actions still on card |

## Gate

SPEED+: self-test then open PR — **no 找茬 FULL gate**. Boss (扳手) verifies.

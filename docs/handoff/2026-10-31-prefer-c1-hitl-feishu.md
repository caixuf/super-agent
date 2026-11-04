# Handoff — Prefer-C1 HITL Feishu cards (waiting_user + plan + source labels)

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/prefer-c1-hitl-feishu-cards`  
**Floor tip:** `9fc8f0e` (re-confirmed origin/main before branch)  
**Prefer:** Prefer-C1 thin — OSS §8.6 #1 + rails C1-0–C1-10  
**Owners:** 焊枪 primary · 找茬喵 gate · 扳手 merge · 手术刀 only if CardView/HITL ABI gap

## Landed

### 1) Pool → Feishu HITL card (Approve / Deny+reason)
- `CardEmitter.wait_user` / `await_plan` emit HITL frames on the **existing** CardWriter with `hitl_kind` / `hitl_summary`.
- `_ACTIONS`: `WAITING_USER` → `approve`/`deny`/`details`/`logs`; `AWAITING_PLAN_APPROVAL` → `approve`/`deny`/`details`.
- `colleague_card_payload`: source row + interactive Approve/Deny buttons on the **same** card; text-fallback instructions always included for HITL states.
- `agentcore/hitl_bridge.py`: `HitlBridge.enter_waiting_user` / `enter_plan_approval` / `handle_action` / `handle_text_fallback`.
- `Pool.deny_from_user(job, reason=…)` — reason required (≤240); default policy `cancelled`; **no silent resume**.
- Approve → `resume_from_user` + durable `hitl_approved` on `card_store.hitl_pending`.

### 2) Approval source labeling
- CardFrame / CardView fields: `from_bot`, `source_thread`, `hitl_kind`, `hitl_summary`, `hitl_request_id` (+ existing `role_id`/`display_name`).
- `labels_from_origin(origin, handle)` — host/origin/ontology only; blank-ok; never model-forged.
- Feishu body renders `来源：from_bot=… · source_thread=… · role=…` when known.

### 3) Stack / ban held
- One CardWriter ↔ one card_id (no second emitter).
- No new model turn for HITL notify (Prefer-C2 PARK).
- No gateway `input()` — reason from card callback value or text-fallback reply.
- Prefer A memory / Prefer B packs / Feishu identity modules untouched beyond HITL field additions.

### Interactive vs text-fallback
- **Interactive:** Card 2.0 `action` buttons Approve/Deny with `callback_value(hitl=True, task_id, …)`.
- **Text-fallback (deliver-or-reason):** when Kit create/patch fails, plaintext includes `APPROVE` / `DENY: <原因>`; `HitlBridge.handle_text_fallback` records the same durable events. Deny without reason still rejected.

## Land set
| Path | Role |
|---|---|
| `agentcore/task_card.py` | HITL fields on CardFrame + emit kwargs |
| `agentcore/card_emit.py` | Approve/Deny actions + source row on CardView |
| `agentcore/card_store.py` | `hitl_pending` park/resolve correlation |
| `agentcore/pool.py` | `deny_from_user` wire (resume already on tip) |
| `agentcore/feishu_transport.py` | HITL buttons + source row + text-fallback |
| `agentcore/hitl_bridge.py` | callback ↔ job correlate (new, thin) |
| `tests/test_prefer_c1_hitl.py` | C1-1–C1-6 unit coverage |
| `docs/handoff/2026-10-31-prefer-c1-hitl-feishu.md` | this archive |

## PARK / BAN (untouched)
Prefer-C2 notify · Prefer-A2 titles · Lightpanda · Jev/Electron · Bun/Chroma/TeamBrain · Temporal · cellular·KAD · Prefer A/B rework · gateway `input()` · second CardWriter · new model turn for HITL · Bot-box clone · Codex Rust vendor

## Acceptance C1-0–C1-10
| ID | Status |
|---|---|
| C1-0 tip/branch | pass — branched off confirmed `9fc8f0e` |
| C1-1 waiting_user card | pass — unit |
| C1-2 plan card | pass — unit |
| C1-3 Approve→resume | pass — unit + durable event |
| C1-4 Deny reason required | pass — unit; blank rejected; no silent resume |
| C1-5 source labels | pass — unit; blanks honest |
| C1-6 not model-forged | pass — review (`labels_from_origin` host-only) |
| C1-7 single writer | pass — same message_id unit |
| C1-8 no new model turn / no gateway input() | pass — review |
| C1-9 Prefer A/B/identity untouched beyond HITL | pass — review |
| C1-10 PARK/BAN held | pass — review |

## 手术刀
Not needed — CardView/HITL ABI allowed thin Approve/Deny + source fields; interactive buttons + text-fallback deliver-or-reason both land without ABI break.

## Live Feishu
Offline payload + unit coverage. Live tenant Card Kit interactive callback → deliver-or-reason (no live tenant in this weld); text-fallback path covered.

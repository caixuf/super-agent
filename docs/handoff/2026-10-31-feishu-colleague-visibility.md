# Handoff — Feishu colleague visibility (A→B→C + Codex §6 cheap)

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/feishu-colleague-card-visibility`  
**Base tip:** `e38ddc0c` (main)  
**Prefer:** Feishu 同事可见性 — same Prefer; Codex §6 cheap fold (rails amended)

## Landed

### A — Card protocol identity (+ Codex §6-4)
- `agentcore/task_card.py`: `CardState.WAITING_USER` / `AWAITING_PLAN_APPROVAL`; `frees_worker_slot`; optional frame fields `role_id` / `display_name` / `task_title` / `heartbeat_age_s` / `attempt_id`; `should_push_frame` (30–60s throttle OR phase change).
- `agentcore/card_emit.py`: `CardView` identity + `waiting_role` on HITL; `compose_title` / `resolve_display_name`; `CardEmitter.bind_identity` / `wait_user` / `await_plan` / `notify_terminal_or_wait`.
- `agentcore/card_store.py`: persist identity columns + `card_id` via `set_card_id`.
- Identity source: ontology (`colleague_dev` / catalog) only — no second identity source; no business-overlay literals.

### B — Same-card push / poll / notify / interrupt (+ Codex §6-1, §6-3)
- Throttle helper + emitter lifecycle patches one card.
- `CardNotifyBridge` (in `feishu_transport.py`): turn-complete / approval-requested → `notify_terminal_or_wait` — **no new model turn**.
- `task_progress.FollowUpQueue`: mid-run follow-ups queued; drain blocked on irreversible phases (`commit`/`push`/`deploy`/…).
- `poll_progress_from_ledger`: progress ask → liveness inspect only; `second_exec=False`.
- Snapshot/render know `waiting_user` / `awaiting_plan_approval` + identity lines.

### C — Feishu Card Kit adapter
- `FeishuCardKitAdapter` + `colleague_card_payload`: Card 2.0 create → batch_update/PATCH; uuid=`task_id:sequence`; persist `card_id`; 14d expire deliver-or-reason; plaintext fallback.
- **Live Feishu demos:** deliver-or-reason (no live tenant in this weld) — offline payload + unit coverage only.

### Cheap same-PR (Codex §6-2) — thin
- `pool.wait_for_user` / `await_plan_approval` / `resume_from_user`.
- claim() already `status IN ('queued','running')` → waiting_* never claimable (no claim SQL rewrite).
- Sandbox ∩ approval kept as **two layers** (exec boundary ≠ HITL card); no danger-bypass switch.

## PARK (unchanged)
Full TUI · app-server as 2nd scheduler · danger-bypass default · Temporal · zombie deepen · cellular/KAD.

## Tests
- `tests/test_feishu_colleague_visibility.py` — 16 passed (A/B/C/cheap).
- Regressions: `test_task_card` / `test_card_emit` / `test_card_store` / `test_task_progress` / `test_pool` (run in weld).

## Acceptance mapping
| ID | Status |
|---|---|
| A0–A4 | unit + review (ontology wire; HITL waiting_role) |
| B1–B5 | unit (throttle, notify bridge, FollowUpQueue, ledger poll) |
| C1–C4 | unit + offline payload; live Card Kit → deliver-or-reason |
| Cheap waiting_user/plan | thin pool methods + claim skip |
| Boss-visible live Feishu | deliver-or-reason (credentials/tenant not in weld) |

## 手术刀
Not needed — CardWriter/pool gap did not block thin land.

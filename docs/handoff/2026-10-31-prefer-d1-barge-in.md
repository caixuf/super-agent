# Handoff — Prefer-D1 barge-in / 主对话控制短路径

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/prefer-d1-barge-in-control`  
**Floor tip:** `b5b38ab` (re-confirmed `origin/main` before branch; CI #6)  
**Prefer:** Prefer-D1 thin — OSS §2.1-2 + Codex §6 knife 2 / §6.2 #3 + rails D1-0–D1-9  
**Owners:** 焊枪 primary · 找茬喵 gate · 扳手 merge · 手术刀 only if pause/control / CardWriter / pool pause ABI gap

## Landed

### 1) Control short-path router (`agentcore/barge_in_control.py`)
- `classify_control` — slash/button first (`/status` `/pause` `/cancel` / card actions); thin Chinese free-text optional; default Tab = follow-up queue (never opens long exec by classify alone).
- `BargeInController` — status / queue_follow_up / pause / cancel / urgent_inject / resume.
- Fast ack dict: `second_exec=False`, `durable_dispatch_submit=False` on every control path.
- No gateway `input()`; no second AgentScheduler.

### 2) Tab follow-up inbox
- `FollowUpInbox` wraps existing `FollowUpQueue`; bound to task-id + owner + origin subset.
- Irreversible phases (`commit`/`push`/`deploy`/…) block drain/preempt (D1-2).
- Same-card `queued_follow_up` phase patch via CardEmitter.

### 3) Pause / cancel on pool (thin aliases)
- `Pool.pause` → `wait_for_user` (no named suspend / no second state DB).
- `Pool.finalize_cancel` → cancel flag + immediate terminal `cancelled` + outbox receipt (Prefer-D1 fast ack + C2 path). Existing `Pool.cancel` flag behavior for durable_dispatch **unchanged**.

### 4) CardEmitter thin helpers
- `pause(detail=…)` → waiting_user with phase `paused` (colleague identity kept).
- `mark_queued_follow_up` → RUNNING patch phase `queued_follow_up`.
- RUNNING actions gain `pause` / `status` tokens (additive; C1 Approve/Deny map untouched).

### 5) durable_dispatch Tab bind helper
- `enqueue_tab_follow_up` delegates to `bind_follow_up_to_task` — **no** `submit`.

### 6) task_progress slash surface
- Registers `/status` `/pause` `/cancel` when host `register_command` exists (deliver-or-reason if free-text weak).

### 7) Prefer-C1 / Prefer-C2
- Untouched Approve/Deny UX and notify internals; cancel terminal reuses `CardNotifyBridge.notify_terminal` / `notify_on_terminal` (≤1 kick from controller).

## Land set
| Path | Role |
|---|---|
| `agentcore/barge_in_control.py` | classify + short-path controller + FollowUpInbox |
| `agentcore/pool.py` | thin `pause` + `finalize_cancel` |
| `agentcore/card_emit.py` | `pause` / `mark_queued_follow_up` + RUNNING action tokens |
| `plugins/durable_dispatch/__init__.py` | `enqueue_tab_follow_up` helper (no submit) |
| `plugins/task_progress/__init__.py` | `/status` `/pause` `/cancel` slash register |
| `tests/test_prefer_d1_barge_in.py` | D1-1–D1-7 unit |
| `docs/handoff/2026-10-31-prefer-d1-barge-in.md` | this archive |

## PARK / BAN (untouched)
Prefer-A2 · Lightpanda · Jev/Electron · Bun/Chroma/TeamBrain · Temporal · cellular·KAD · second scheduler / app-server-as-authority · status→long-exec · Tab preempt irreversible · C1 Approve/Deny UX rework · C2 notify rework · colleague_pack yaml · memory prefetch · Bot-box clone · Codex TUI Tab/Enter vendor · gateway injection as default

## Acceptance D1-0–D1-9
| ID | Status |
|---|---|
| D1-0 tip/branch | pass — branched off confirmed `b5b38ab` |
| D1-1 进度 short-path zero long exec | pass — unit |
| D1-2 Tab queue no preempt | pass — unit |
| D1-3 cancel + C2 ≤1 | pass — unit |
| D1-4 pause waiting_user | pass — unit |
| D1-5 Enter soft-interrupt resume-or-replan | pass — unit |
| D1-6 colleague fields kept | pass — unit |
| D1-7 C1/C2 unchanged | pass — smoke + review |
| D1-8 no second scheduler | pass — review |
| D1-9 PARK/BAN held | pass — review |

## 手术刀
Not needed — pause maps to existing `wait_for_user`; CardWriter / pool ABI allowed thin aliases without inventing a second state DB.

## Live Feishu
Unit + offline path. Live tenant card action / slash ack → deliver-or-reason (no live tenant in this weld); slash/button verbs are the reliable classify path.

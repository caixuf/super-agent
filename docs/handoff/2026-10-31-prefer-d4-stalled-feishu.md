# Handoff — Prefer-D4 stalled/lost → Feishu action card

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/prefer-d4-stalled-feishu-card`  
**Floor tip:** `9cc59db` (Prefer-D2 #8 squash-MERGED; re-confirmed `origin/main` before branch)  
**Prefer:** Prefer-D4 thin — OSS Prefect zombie cite + rails D4-0–D4-7  
**Owners:** 手术刀 weld (this) · 焊枪 parallel Prefer-D3 OK · 找茬喵 gesture-only / stand-down (SPEED+) · 扳手 merge · boss owns verification

## Landed

### 1) Stalled/lost → same-card bridge (`agentcore/stalled_card_bridge.py`)
- `StalledCardBridge.notify_transition` — on real transition into `stalled|lost`, emit ≤1 actionable same-card patch.
- `StallNotifyCooldown` — same `task_id` + same verdict ≤1 per `STALL_NOTIFY_COOLDOWN_S` (default = stall_seconds).
- Colleague label from card handle / owner / `colleague_dev` catalog — never invent titles.
- Required fields: task_id · verdict · heartbeat_age_s · phase/detail · colleague label; optional pid_alive / inspect note.
- `deliver-or-reason`: no CardHandle → `unavailable` (or `text_fallback` if text_sink bound); never invents `delivered=True`.

### 2) CardEmitter stalled/lost frame (`agentcore/card_emit.py`)
- `mark_stalled` / `mark_lost` — RUNNING + phase stalled|lost (ops card, not C1 HITL).
- `_STALLED_ACTIONS` = cancel / pause / status / resume_observe / details.
- `CardView.verdict` + note copy for stalled/lost.

### 3) Daemon sink (`agentcore/liveness_daemon.py`)
- `stalled_card_sink(bridge)` — thin wire; non-stall events ignored. Compose with C2 `terminal_delivery_sink` via `compose_sinks` when host wants both.

### 4) D1 action wire (`agentcore/barge_in_control.py`)
- `ControlVerb.RESUME_OBSERVE` + slash/button `恢复观察` / `resume_observe`.
- `resume_observe` → ledger.beat (clears stalled/lost) + same-card phase `observing` — **never** fake done/failed.
- 取消 / 暂停 / 进度 reuse existing D1 short-paths (`finalize_cancel` / `pause` / `status`); `second_exec=False`.

### 5) Feishu ops buttons (`agentcore/feishu_transport.py`)
- `colleague_card_payload` renders ops buttons on stalled/lost frames (取消 / 暂停/等待 / 查看进度 / 恢复观察).
- Values use `ops_callback_value` (`ops=True`, not `hitl=True`) — distinct from C1 Approve/Deny.

## Land set
| Path | Role |
|---|---|
| `agentcore/stalled_card_bridge.py` | Prefer-D4 bridge + cooldown + sink + ops callback |
| `agentcore/card_emit.py` | mark_stalled/lost · _STALLED_ACTIONS · verdict |
| `agentcore/liveness_daemon.py` | stalled_card_sink export |
| `agentcore/barge_in_control.py` | resume_observe verb + short-path |
| `agentcore/feishu_transport.py` | stalled/lost ops buttons |
| `tests/test_prefer_d4_stalled_feishu.py` | D4-1–D4-6 unit |
| `docs/handoff/2026-10-31-prefer-d4-stalled-feishu.md` | this archive |

## PARK / BAN (untouched)
Temporal · Electron/Jev · Bun/Chroma · cellular·KAD · Prefer-B1 · Prefer-D2 land set (cancel windows) · Prefer-D6 · Prefer-D3 durable inbox · C1 Approve/Deny rewrite · auto-success on stall · stall spam · C2 notify rework

## Acceptance D4-0–D4-7
| ID | Status |
|---|---|
| D4-0 tip/branch | pass — branched off confirmed `9cc59db` |
| D4-1 sweep→stalled ≤1 actionable card | pass — unit |
| D4-2 lost + colleague label | pass — unit |
| D4-3 cooldown no spam | pass — unit |
| D4-4 cancel → finalize_cancel; no auto-done | pass — unit |
| D4-5 status short-path second_exec=False | pass — unit |
| D4-6 no transport → deliver-or-reason | pass — unit |
| D4-7 C1/C2/D1(/D2) regression; PARK held | pass — review (SPEED+ gesture-only; boss verify) |

## SPEED+
No 找茬 FULL gate — gesture-only / stand-down. Boss owns verification. Honest tests + this handoff still written.

## Live Feishu
Unit + offline path. Live tenant Card Kit stalled patch → deliver-or-reason (no live tenant in this weld).

# Handoff — Prefer-D2 cancel/pause hard windows (Dagster → SQLite pool)

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/prefer-d2-cancel-hard-windows`  
**Floor tip:** `5873ccc` (re-confirmed `origin/main` before branch; Prefer-D1 #7 MERGED)  
**Prefer:** Prefer-D2 thin — OSS §2.2-1 Dagster STARTING/CANCELING cite only + rails D2-0–D2-9  
**Owners:** 焊枪 primary · 找茬喵 gate · 扳手 merge · 手术刀 only if claim/PID/cancel ABI gap

## Landed

### 1) STARTING fail-closed (`agentcore/pool.py`)
- After claim: `status=running` + `pid IS NULL` is the STARTING window.
- Clock: claim `heartbeat` (already stamped) — no new claimed_at required.
- Past `START_WINDOW_S` (default 30s, config 15–60s) → terminal **failed**
  `reason=starting_timeout` / `event=claim_without_pid` + outbox receipt.
- `claim()` calls `enforce_windows()` before selecting work.
- Ban held: pid-null running is never advertised as healthy Running (`stage=starting`).

### 2) CANCELING soft-cancel fence + hard timeout
- Soft `Pool.cancel()` sets `cancel=1` and stamps `cancel_requested_at` once (COALESCE).
- Within `CANCEL_WINDOW_S` (default 60s, config 30–120s): killpg / containment.sweep
  via existing `_stop_expired_runner`; finish/claim still terminal cancelled when observed.
- Past window still non-terminal → force **cancelled** `reason=cancel_timeout` + kill best-effort + outbox.
- Prefer-D1 `finalize_cancel` remains the immediate barge-in path (unchanged semantics).

### 3) Pause fencing (no fake Running)
- `pause` → `wait_for_user` unchanged; `display_stage` / CardView show `paused` (not Running).
- Optional `PAUSE_QUIESCE_S` documented; no second state DB.

### 4) Sweep owner
- `Pool.enforce_windows(now=…)` is the single sweeper (ledger authority stays on pool).
- `LivenessDaemon(pool=…)` tick invokes `enforce_windows` before ledger sweep.
- No Temporal; no second scheduler-as-authority.

### 5) CardView stages
- `display_stage(row)` → starting / canceling / paused / running / …
- CardEmitter: `mark_starting` / `mark_canceling` / `apply_pool_stage`; `_note` never says 运行中 for those phases.
- Colleague identity + Prefer-C1 HITL + Prefer-C2 notify + Prefer-D1 barge_in_control kept.

### 6) Config (D2-9)
| Constant | Default | Suggest |
|---|---|---|
| `START_WINDOW_S` | 30.0 | 15–60s |
| `CANCEL_WINDOW_S` | 60.0 | 30–120s |
| `PAUSE_QUIESCE_S` | 30.0 | optional escalate only |

## Land set
| Path | Role |
|---|---|
| `agentcore/pool.py` | STARTING fail-closed; CANCELING stamp + hard force-terminal; `cancel_requested_at`; `enforce_windows`; `display_stage`; claim sweep |
| `agentcore/liveness_daemon.py` | tick invokes pool.enforce_windows when pool bound |
| `agentcore/card_emit.py` | stage starting/canceling/paused notes + helpers |
| `tests/test_prefer_d2_cancel_windows.py` | D2-1–D2-6 + card stage + config |
| `docs/handoff/2026-10-31-prefer-d2-cancel-windows.md` | this archive |

## PARK / BAN (untouched)
Temporal · 2nd scheduler-as-authority · Electron/Jev · Bun/Chroma · cellular·KAD · Prefer-B1 · Prefer-A2 · C1 HITL UX rework · C2 notify rework · Prefer-D1 barge_in_control rework · Fake Running as acceptable UX · Bot-box private clone · Dagster daemon vendor

## Acceptance D2-0–D2-9
| ID | Status |
|---|---|
| D2-0 tip/branch | pass — branched off confirmed `5873ccc` |
| D2-1 STARTING timeout failed | pass — unit |
| D2-2 CANCELING force cancelled | pass — unit |
| D2-3 soft cancel within window | pass — unit |
| D2-4 finalize_cancel immediate | pass — unit / D1 regression |
| D2-5 pause waiting_user | pass — unit / D1 regression |
| D2-6 pool/liveness sweep | pass — unit |
| D2-7 C1/C2/D1 unchanged | pass — regression suites |
| D2-8 BAN list | pass — review |
| D2-9 config constants documented | pass — module constants + handoff |

## 手术刀
Not needed — claim/PID/cancel ABI already had pid + cancel flag + finalize_cancel; thin windows layered in-place (`cancel_requested_at` column + `enforce_windows`).

## Edge (deliver-or-reason)
If a host process dies holding the flock with pid-null: after lock free, next claim/liveness `enforce_windows` still applies STARTING timeout. Do not require a live worker to clear fake Running.

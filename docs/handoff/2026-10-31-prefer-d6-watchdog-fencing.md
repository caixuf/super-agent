# Handoff — Prefer-D6 watchdog / attempt fencing

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/prefer-d6-watchdog-fencing`  
**Floor tip:** `8d6f832` (Prefer-D4 #9 MERGED; re-confirmed `origin/main` before branch)  
**Prefer:** Prefer-D6 thin — OpenHands-style watchdog reconcile on SQLite pool; Temporal *ideas* in-place only  
**Owners:** 手术刀 weld (this) · 找茬喵 gesture-only / stand-down (SPEED+) · 扳手 merge · boss owns verification

## Landed

### 1) Conditional `_terminal` (`agentcore/pool.py`)
- UPDATE only when `status NOT IN (completed, failed, cancelled)`.
- Optional `attempt=` fence so stale/superseded attempts cannot clobber a live claimer.
- Outbox remains `INSERT OR IGNORE` (first receipt wins).

### 2) Named `reconcile_watchdog` (`agentcore/pool.py`)
- One clear sweeper: Prefer-D2 `enforce_windows` first, then finish-callback-loss reconcile.
- Dead evidence required: PID identity gone + job lock free + past lease (+ containment clean when token present).
- Outcomes: conditional terminal `failed` reason=`finish_callback_lost`, or replay-safe requeue, or cancelled if cancel flag.
- **Ban:** completed/failed from stall silence alone (align Prefer-D4).
- Watchdog always passes the same attempt fence as `finish` / `heartbeat`.

### 3) Outbox observe (`observe_outbox_reclaims`)
- Counts expired-lease reclaimable rows; logs delivered-missing-ack (at-least-once; no exactly-once invent).
- `claim_receipt` / `acknowledge` lease fencing unchanged.

### 4) Liveness daemon wire (`agentcore/liveness_daemon.py`)
- Tick prefers `pool.reconcile_watchdog` (includes D2 windows); falls back to `enforce_windows`.
- Daemon never writes terminal state itself — pool remains ledger authority.

## Land set
| Path | Role |
|---|---|
| `agentcore/pool.py` | conditional `_terminal` · `reconcile_watchdog` · `observe_outbox_reclaims` |
| `agentcore/liveness_daemon.py` | tick → reconcile_watchdog |
| `tests/test_prefer_d6_watchdog_fencing.py` | D6-1..D6-7 unit |
| `docs/handoff/2026-10-31-prefer-d6-watchdog-fencing.md` | this archive |

## PARK / BAN (untouched)
Temporal SDK/server · Hatchet-as-authority · 2nd workflow DB · Electron/Jev · Bun/Chroma · cellular·KAD · Prefer-B1 · Prefer-D2 land rewrite · Prefer-D4 land rewrite · Prefer-D1 barge · C1–C2 · auto-done from silence · C1–D5 UX rewrite

## Acceptance D6-0–D6-7
| ID | Status |
|---|---|
| D6-0 tip/branch | pass — branched off confirmed `8d6f832` |
| D6-1 finish-callback-loss → conditional terminal / requeue + outbox once | pass — unit |
| D6-2 stale attempt finish → no-op; live intact | pass — unit |
| D6-3 stall silence alone ≠ terminal | pass — unit |
| D6-4 conditional terminal no overwrite | pass — unit |
| D6-5 outbox wrong lease cannot ack; expired reclaimable | pass — unit |
| D6-6 D2 enforce_windows + D4 stalled path smoke | pass — unit |
| D6-7 No Temporal / 2nd scheduler / PARK held | pass — review |

## SPEED+
No 找茬 FULL gate — gesture-only / stand-down. Boss owns verification. Honest tests + this handoff still written.

## Confirm
- NO Temporal / Hatchet as ledger
- NO D2/D4 land rewrite
- NO silence→done

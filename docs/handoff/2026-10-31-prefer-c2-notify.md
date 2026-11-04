# Handoff — Prefer-C2 notify → completion_delivery / Feishu side-channel

**Date:** 2026-10-31 Asia/Shanghai  
**Branch:** `feat/prefer-c2-notify-completion`  
**Floor tip:** `ea29881` (re-confirmed `origin/main` before branch)  
**Prefer:** Prefer-C2 thin — OSS §8.6 #2 + rails C2-0–C2-9  
**Owners:** 焊枪 primary · 找茬喵 gate · 扳手 merge · 手术刀 only if completion_delivery / CardWriter ABI gap

## Landed

### 1) Turn-complete envelope on completion_delivery
- `AGENT_TURN_COMPLETE` / `origin_envelope_fields` — Codex `agent-turn-complete` subset map (not vendor).
- `CompletionMessage.render()` now includes `type`, `session`/`chat`/`thread`/`thread_id`/`turn_id`/`route`/`channel` when known from **persisted** `jobs.origin`.
- Bounded summary still ≤ `MAX_SUMMARY_CHARS`.
- Fixed `_colleague_identity` except-path to return 3-tuple.

### 2) Host-tick kick (no LLM turn)
- `kick_deliver_once` / `notify_on_terminal` / `is_terminal_status` — one side-channel deliver attempt on durable terminal (completed/failed/cancelled; card `done` maps).
- Non-terminal → `skipped`; no transport → `unavailable` (inspect_pending still honest; never fake delivered).

### 3) Same-card terminal notify
- `CardNotifyBridge.notify_terminal` — same CardWriter / message_id / card_id patch, then optional `notify_on_terminal` kick.
- Gateway injection **not** default on this path.

### 4) Optional C1 shared helper wire only
- `hitl_bridge.notify_approval_requested` — reuses wait_user / await_plan / HitlBridge.emit_via; **no** Approve/Deny UX rework.

### 5) Liveness thin sink
- `terminal_delivery_sink(pool, transport)` — on liveness terminal transition, kick `notify_on_terminal` (no second scheduler / no auto agent loop).

### 6) direct_completion
- Docstring align only (sync twin of completion_delivery).

## Land set
| Path | Role |
|---|---|
| `agentcore/completion_delivery.py` | envelope + kick helpers |
| `agentcore/direct_completion.py` | thin docstring align |
| `agentcore/feishu_transport.py` | CardNotifyBridge.notify_terminal |
| `agentcore/hitl_bridge.py` | optional shared notify helper |
| `agentcore/liveness_daemon.py` | terminal_delivery_sink |
| `tests/test_prefer_c2_notify.py` | C2-1–C2-5 (+ C1 smoke) |
| `docs/handoff/2026-10-31-prefer-c2-notify.md` | this archive |

## PARK / BAN (untouched)
Prefer-A2 · Lightpanda · Jev/Electron · Bun/Chroma/TeamBrain · Temporal · cellular·KAD · second scheduler / app-server-as-authority · hooks→auto second agent · gateway injection as default · new LLM remind turn · C1 Approve/Deny UX rework · Prefer A/B rework · Bot-box clone · Codex Rust notify.py vendor

## Acceptance C2-0–C2-9
| ID | Status |
|---|---|
| C2-0 tip/branch | pass — branched off confirmed `ea29881` |
| C2-1 terminal ≤1 notify no LLM | pass — unit |
| C2-2 payload shape | pass — unit |
| C2-3 completion_delivery + ack / unowned | pass — unit |
| C2-4 same card_id terminal patch | pass — unit |
| C2-5 failure statuses observable | pass — unit |
| C2-6 no gateway inject default | pass — review (CardNotifyBridge / kick path) |
| C2-7 C1 Approve/Deny unchanged | pass — review + smoke |
| C2-8 no second scheduler / hooks→auto agent | pass — review |
| C2-9 PARK/BAN held | pass — review |

## 手术刀
Not needed — completion_delivery / CardWriter ABI allowed thin envelope + kick + same-card bridge without ABI break.

## Live Feishu
Unit + offline path. Live tenant Card Kit terminal patch → deliver-or-reason (no live tenant in this weld); plaintext completion_delivery path covered by existing colleague_delivery_cli.

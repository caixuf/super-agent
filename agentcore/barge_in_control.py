"""Prefer-D1 barge-in / main-dialog control short-path (thin).

Codex Tab/Enter spirit cite only. Mid-task inbound verbs go through a short
path that must NOT open a second long durable_dispatch / model exec turn.

Control verbs
-------------
- status / progress  -> ledger / pool poll (task_status spirit); zero new submit
- follow-up          -> Tab queue on FollowUpQueue bound to task-id + owner
- pause              -> pool.wait_for_user (thin pause alias) + same-card stage
- cancel             -> pool finalize_cancel + same-card CANCELLED + Prefer-C2 <=1

Urgency
-------
- DEFAULT (Tab): queue follow-up; current turn continues; no preempt of
  irreversible phases
- URGENT (Enter / explicit pause|cancel|urgent): soft-interrupt -> fast ack ->
  resume-or-replan (resume_from_user / await_plan_approval / queued follow-up)

Slash / button verbs are the reliable path; free-text Chinese classify is
optional thin (deliver-or-reason if weak). Prefer-C1 Approve/Deny and
Prefer-C2 notify internals stay untouched — this module only reuses them.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional, TYPE_CHECKING

from agentcore.task_progress import (
    FollowUpQueue,
    IRREVERSIBLE_PHASES,
    poll_progress_from_ledger,
)
from agentcore.task_card import CardState

if TYPE_CHECKING:
    from agentcore.follow_up_store import FollowUpStore

_LOG = logging.getLogger(__name__)

#: Prefer-D3 dual SLO budgets (host classify->ack dict; Feishu attempt outer).
CONTROL_FAST_BUDGET_MS = 2000.0
FEISHU_ATTEMPT_BUDGET_MS = 5000.0


class ControlVerb(str, Enum):
    STATUS = "status"
    FOLLOW_UP = "follow_up"
    PAUSE = "pause"
    CANCEL = "cancel"
    URGENT_INJECT = "urgent_inject"
    RESUME_OBSERVE = "resume_observe"  # Prefer-D4: clear stall note, not fake done
    UNKNOWN = "unknown"


class Urgency(str, Enum):
    TAB = "tab"       # default — queue, do not preempt
    ENTER = "enter"   # urgent — soft-interrupt -> resume-or-replan


# Slash / card-action tokens (primary, reliable).
_SLASH_STATUS = frozenset({"status", "/status", "longtask", "/longtask", "进度"})
_SLASH_PAUSE = frozenset({"pause", "/pause", "暂停"})
_SLASH_CANCEL = frozenset({"cancel", "/cancel", "取消"})
_SLASH_FOLLOW = frozenset({"follow_up", "/follow_up", "followup", "/followup", "补充"})
_SLASH_URGENT = frozenset({"urgent", "/urgent", "紧急", "紧急注入", "enter"})
_SLASH_RESUME_OBSERVE = frozenset({
    "resume_observe", "/resume_observe", "resume-observe", "/resume-observe",
    "恢复观察", "observe", "/observe",
})

# Thin free-text Chinese / English hints (optional; slash wins).
_RE_STATUS = re.compile(
    r"^(?:进度|怎么样了|还在跑|还在吗|status\??|进度\??|查一下进度)\s*$",
    re.IGNORECASE,
)
_RE_PAUSE = re.compile(r"^(?:暂停|先停|pause)\s*$", re.IGNORECASE)
_RE_CANCEL = re.compile(r"^(?:取消|别做了|停下|cancel)\s*$", re.IGNORECASE)
_RE_FOLLOW = re.compile(r"^(?:补充|顺便|另外|follow[-_ ]?up)\b", re.IGNORECASE)
_RE_URGENT = re.compile(r"(?:紧急|立刻|马上注入|urgent)", re.IGNORECASE)


@dataclass(frozen=True)
class ControlDecision:
    verb: ControlVerb
    urgency: Urgency
    text: str = ""
    source: str = "slash"  # slash | button | free_text | default_tab
    opens_long_exec: bool = False


@dataclass
class ControlAck:
    """Fast ack payload for Feishu text / same-card note.

    Prefer-D1 keeps second_exec=false / durable_dispatch_submit=false.
    Prefer-D3 adds SLO fields (ack_id, ack_emitted_at, ack_latency_ms,
    slo_class, slo_met). Miss still acks; never opens LLM.
    """

    ok: bool
    verb: str
    task_id: str
    message: str
    second_exec: bool = False
    durable_dispatch_submit: bool = False
    detail: dict = field(default_factory=dict)
    # Prefer-D3 additive SLO (optional until _emit_ack fills them).
    ack_id: Optional[str] = None
    ack_emitted_at: Optional[str] = None
    ack_latency_ms: Optional[float] = None
    slo_class: str = "control_fast"
    slo_met: Optional[bool] = None

    def as_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "verb": self.verb,
            "task_id": self.task_id,
            "message": self.message,
            "second_exec": self.second_exec,
            "durable_dispatch_submit": self.durable_dispatch_submit,
            "detail": dict(self.detail),
        }
        if self.ack_id is not None:
            out["ack_id"] = self.ack_id
        if self.ack_emitted_at is not None:
            out["ack_emitted_at"] = self.ack_emitted_at
        if self.ack_latency_ms is not None:
            out["ack_latency_ms"] = self.ack_latency_ms
        if self.slo_class:
            out["slo_class"] = self.slo_class
        if self.slo_met is not None:
            out["slo_met"] = self.slo_met
        return out


def classify_control(
    text: str = "",
    *,
    action: str = "",
    urgency_hint: str = "",
) -> ControlDecision:
    """Classify inbound mid-task control. Slash/button first; free-text optional.

    Unknown free-text defaults to Tab follow-up (queue), never a second long exec.
    """
    token = (action or "").strip()
    raw = (text or "").strip()
    hint = (urgency_hint or "").strip().lower()

    def _from_token(tok: str, *, source: str) -> Optional[ControlDecision]:
        low = tok.lower().lstrip("/")
        full = tok.lower() if tok.startswith("/") else "/" + low
        candidates = {tok, low, full, tok.strip()}
        if candidates & _SLASH_STATUS or low in {"status", "longtask"}:
            return ControlDecision(ControlVerb.STATUS, Urgency.TAB, raw or tok, source)
        if candidates & _SLASH_PAUSE or low == "pause":
            return ControlDecision(ControlVerb.PAUSE, Urgency.ENTER, raw or tok, source)
        if candidates & _SLASH_CANCEL or low == "cancel":
            return ControlDecision(ControlVerb.CANCEL, Urgency.ENTER, raw or tok, source)
        if candidates & _SLASH_FOLLOW or low in {"follow_up", "followup"}:
            return ControlDecision(ControlVerb.FOLLOW_UP, Urgency.TAB, raw or tok, source)
        if candidates & _SLASH_URGENT or low in {"urgent", "enter"}:
            return ControlDecision(
                ControlVerb.URGENT_INJECT, Urgency.ENTER, raw or tok, source,
            )
        if candidates & _SLASH_RESUME_OBSERVE or low in {
                "resume_observe", "resume-observe", "observe"}:
            return ControlDecision(
                ControlVerb.RESUME_OBSERVE, Urgency.TAB, raw or tok, source,
            )
        return None

    if token:
        hit = _from_token(token, source="button" if not token.startswith("/") else "slash")
        if hit is not None:
            return hit

    if raw.startswith("/"):
        parts = raw.split(None, 1)
        hit = _from_token(parts[0], source="slash")
        if hit is not None:
            rest = parts[1] if len(parts) > 1 else raw
            return ControlDecision(hit.verb, hit.urgency, rest, "slash")

    if raw:
        if _RE_STATUS.match(raw):
            return ControlDecision(ControlVerb.STATUS, Urgency.TAB, raw, "free_text")
        if _RE_PAUSE.match(raw):
            return ControlDecision(ControlVerb.PAUSE, Urgency.ENTER, raw, "free_text")
        if _RE_CANCEL.match(raw):
            return ControlDecision(ControlVerb.CANCEL, Urgency.ENTER, raw, "free_text")
        if _RE_URGENT.search(raw) and hint in ("", "enter", "urgent"):
            return ControlDecision(ControlVerb.URGENT_INJECT, Urgency.ENTER, raw, "free_text")
        if _RE_FOLLOW.match(raw):
            return ControlDecision(ControlVerb.FOLLOW_UP, Urgency.TAB, raw, "free_text")

    if hint in ("enter", "urgent"):
        return ControlDecision(
            ControlVerb.URGENT_INJECT, Urgency.ENTER, raw, "default_tab",
        )

    # DEFAULT Tab: treat as follow-up inbox on the running task.
    return ControlDecision(
        ControlVerb.FOLLOW_UP if raw else ControlVerb.UNKNOWN,
        Urgency.TAB,
        raw,
        "default_tab",
    )


class FollowUpInbox:
    """Tab follow-up inbox bound to task-id + owner (optional origin).

    Prefer-D3: optional durable FollowUpStore is the ledger authority.
    Process-local FollowUpQueue stays a cache; after restart the ledger wins.
    Enqueue is crash-safe BEFORE ok-ack when a store is bound.
    """

    def __init__(self, store: Optional["FollowUpStore"] = None):
        self._by_task: dict[str, FollowUpQueue] = {}
        self.store = store

    def _queue(self, task_id: str) -> FollowUpQueue:
        key = str(task_id or "")
        if key not in self._by_task:
            self._by_task[key] = FollowUpQueue()
        return self._by_task[key]

    def enqueue(
        self,
        task_id: str,
        text: str,
        *,
        owner: str = "",
        origin: Optional[dict] = None,
        kind: str = "follow_up",
        id: Optional[str] = None,  # noqa: A002 — schema field
        ack_id: Optional[str] = None,
    ) -> dict:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id_required")
        durable_item = None
        if self.store is not None:
            # Crash-safe durable insert FIRST (Prefer-D3 D3-5).
            durable_item = self.store.enqueue(
                task_id, text, id=id, kind=kind, owner=owner,
                origin=origin, ack_id=ack_id,
            )
        q = self._queue(task_id)
        result = q.enqueue(text, kind=kind, task_id=task_id)
        result["owner"] = str(owner or "")
        if origin:
            result["origin"] = {
                k: origin[k]
                for k in ("profile", "session", "route", "channel", "chat", "thread", "turn")
                if isinstance(origin, dict) and k in origin
            }
        if durable_item is not None:
            result["id"] = durable_item["id"]
            result["item"] = durable_item
            result["state"] = durable_item["state"]
            result["created_at"] = durable_item["created_at"]
            result["durable"] = True
            result["queued"] = self.store.queued_count(task_id)
        else:
            result["durable"] = False
        result["second_exec"] = False
        result["durable_dispatch_submit"] = False
        return result

    def peek(self, task_id: str) -> list:
        if self.store is not None:
            return self.store.peek(task_id)
        return self._queue(task_id).peek()

    def drain(self, task_id: str, *, current_phase: str = "", allow_preempt: bool = False) -> list:
        if self.store is not None:
            return self.store.drain(
                task_id, current_phase=current_phase, allow_preempt=allow_preempt,
            )
        return self._queue(task_id).drain(
            current_phase=current_phase, allow_preempt=allow_preempt,
        )

    def queued_count(self, task_id: str) -> int:
        if self.store is not None:
            return self.store.queued_count(task_id)
        return len(self._queue(task_id))


# Process-local default inbox (host may inject its own).
DEFAULT_INBOX = FollowUpInbox()


class BargeInController:
    """Short-path dispatcher for mid-task barge-in.

    Injected deps keep this unit-testable without Feishu / gateway input().
    """

    def __init__(
        self,
        *,
        pool=None,
        ledger=None,
        emitter=None,
        notify_bridge=None,
        inbox: Optional[FollowUpInbox] = None,
        follow_up_store: Optional["FollowUpStore"] = None,
        ack_sink: Optional[Callable[[ControlAck], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        mono_clock: Optional[Callable[[], float]] = None,
    ):
        self.pool = pool
        self.ledger = ledger
        self.emitter = emitter
        self.notify_bridge = notify_bridge
        store = follow_up_store
        if store is None and pool is not None and hasattr(pool, "root"):
            try:
                from agentcore.follow_up_store import FollowUpStore as _FUS
                store = _FUS(pool.root)
            except Exception:  # noqa: BLE001 — thin: fall back to cache-only
                store = None
        if inbox is not None:
            self.inbox = inbox
            if store is not None and getattr(self.inbox, "store", None) is None:
                self.inbox.store = store
        else:
            self.inbox = FollowUpInbox(store=store)
        self.follow_up_store = store
        self.ack_sink = ack_sink
        self._clock = clock or (lambda: 0.0)
        # Prefer-D3: monotonic host clock for classify->ack latency (fakeable).
        self._mono = mono_clock or time.monotonic
        self._llm_calls = 0  # Prefer-D1: must stay 0 on control short-path
        self._handle_t0: Optional[float] = None

    def spawn_llm(self, *a, **k):  # never called by Prefer-D1 path
        self._llm_calls += 1
        raise AssertionError("Prefer-D1 must not spawn an LLM / long exec turn")

    def handle(
        self,
        *,
        task_id: str,
        text: str = "",
        action: str = "",
        urgency_hint: str = "",
        owner: str = "",
        origin: Optional[dict] = None,
        current_phase: str = "",
    ) -> dict:
        self._handle_t0 = self._mono()
        decision = classify_control(text, action=action, urgency_hint=urgency_hint)
        if decision.verb is ControlVerb.STATUS:
            return self.status(task_id, owner=owner)
        if decision.verb is ControlVerb.PAUSE:
            return self.pause(task_id, detail=decision.text or "paused")
        if decision.verb is ControlVerb.CANCEL:
            return self.cancel(task_id, detail=decision.text or "cancelled")
        if decision.verb is ControlVerb.URGENT_INJECT:
            return self.urgent_inject(
                task_id, text=decision.text, owner=owner, origin=origin,
                current_phase=current_phase,
            )
        if decision.verb is ControlVerb.RESUME_OBSERVE:
            return self.resume_observe(task_id, detail=decision.text or "resume_observe")
        if decision.verb is ControlVerb.FOLLOW_UP:
            return self.queue_follow_up(
                task_id, decision.text or text,
                owner=owner, origin=origin, current_phase=current_phase,
            )
        ack = ControlAck(
            ok=False, verb="unknown", task_id=task_id,
            message="未识别的控制指令；可用 /status /pause /cancel",
        )
        return self._emit_ack(ack)

    def status(self, task_id: str, *, owner: str = "") -> dict:
        """D1-1: progress from ledger/pool — zero long exec / durable_dispatch submit."""
        detail: dict[str, Any] = {"source": None}
        message = ""
        if self.ledger is not None:
            now = self._clock()
            out = poll_progress_from_ledger(
                self.ledger, task_id,
                owner=owner or None,
                now=now if now else None,
            )
            detail["ledger"] = out
            detail["source"] = "liveness_ledger"
            if out.get("ok"):
                snap = out.get("snap") or {}
                message = "进度：%s · 阶段 %s · 心跳 %s" % (
                    snap.get("verdict") or out.get("verdict") or "unknown",
                    snap.get("phase") or "—",
                    snap.get("heartbeat_age_s") if snap.get("heartbeat_age_s") is not None else "—",
                )
            else:
                message = "进度：ledger 无记录（%s）" % (out.get("error") or "unknown")
        if self.pool is not None:
            row = self.pool.status(task_id)
            detail["pool"] = row
            if detail["source"] is None:
                detail["source"] = "pool"
            if not message and row:
                message = "进度：pool status=%s cancel=%s" % (
                    row.get("status"), row.get("cancel"),
                )
        if not message:
            message = "进度：无可用 ledger/pool（task=%s）" % task_id
        # Same-card optional refresh (RUNNING heartbeat) — never submit.
        if self.emitter is not None and hasattr(self.emitter, "update"):
            try:
                self.emitter.update(phase="status_poll", detail=message[:200])
            except Exception:
                pass
        ack = ControlAck(
            ok=True, verb="status", task_id=task_id, message=message,
            second_exec=False, durable_dispatch_submit=False, detail=detail,
        )
        return self._emit_ack(ack)

    def queue_follow_up(
        self,
        task_id: str,
        text: str,
        *,
        owner: str = "",
        origin: Optional[dict] = None,
        current_phase: str = "",
    ) -> dict:
        """D1-2 + Prefer-D3: durable Tab enqueue BEFORE ok-ack; no second long exec."""
        phase = str(current_phase or "").strip().lower()
        preempt_blocked = phase in IRREVERSIBLE_PHASES
        # Durable ledger write happens inside inbox.enqueue when store is bound.
        queued = self.inbox.enqueue(
            task_id, text, owner=owner, origin=origin, kind="follow_up",
        )
        count = int(queued.get("queued") or self.inbox.queued_count(task_id) or 1)
        if preempt_blocked:
            hint = "已排队后续补充（当前不可打断：%s）" % phase
        else:
            hint = "已排队后续补充（Tab）· 共 %d 条" % count
        if self.emitter is not None and hasattr(self.emitter, "mark_queued_follow_up"):
            try:
                self.emitter.mark_queued_follow_up(
                    detail=hint, count=count, task_id=task_id,
                )
            except TypeError:
                try:
                    self.emitter.mark_queued_follow_up(detail=hint, count=count)
                except Exception:
                    pass
            except Exception:
                pass
        elif self.emitter is not None and hasattr(self.emitter, "update"):
            try:
                self.emitter.update(phase="queued_follow_up", detail=hint)
            except Exception:
                pass
        ack = ControlAck(
            ok=True, verb="follow_up", task_id=task_id, message=hint,
            second_exec=False, durable_dispatch_submit=False,
            detail={
                "queued": queued,
                "preempt_blocked": preempt_blocked,
                "current_phase": phase,
                "running_continues": True,
                "durable": bool(queued.get("durable")),
            },
        )
        return self._emit_ack(ack)

    def pause(self, task_id: str, *, detail: str = "paused") -> dict:
        """D1-4: soft-interrupt -> waiting_user; free worker slot; fast ack."""
        parked = False
        if self.pool is not None:
            pause = getattr(self.pool, "pause", None)
            if callable(pause):
                parked = bool(pause(task_id, reason="paused"))
            else:
                parked = bool(self.pool.wait_for_user(task_id, reason="paused"))
        view = None
        if self.emitter is not None:
            pause_card = getattr(self.emitter, "pause", None)
            if callable(pause_card):
                view = pause_card(detail=detail or "已暂停，等待继续")
            elif hasattr(self.emitter, "wait_user"):
                view = self.emitter.wait_user(
                    detail=detail or "已暂停，等待继续",
                    phase="paused",
                    hitl_kind="waiting_user",
                    hitl_summary=detail or "paused",
                )
        if parked:
            message = "已暂停（waiting_user）· 工人槽已释放"
        else:
            message = "暂停未生效（任务可能已终态或不存在）"
        ack = ControlAck(
            ok=bool(parked), verb="pause", task_id=task_id, message=message,
            second_exec=False, durable_dispatch_submit=False,
            detail={
                "parked": parked,
                "pool_status": (self.pool.status(task_id) if self.pool else None),
                "card_state": getattr(view, "state", None),
                "resume": "resume_from_user",
                "replan": "await_plan_approval or queued follow-up",
            },
        )
        return self._emit_ack(ack)

    def cancel(self, task_id: str, *, detail: str = "cancelled") -> dict:
        """D1-3: cancel + same-card CANCELLED + <=1 C2 notify attempt."""
        finalized = False
        if self.pool is not None:
            finalize = getattr(self.pool, "finalize_cancel", None)
            if callable(finalize):
                finalized = bool(finalize(task_id))
            else:
                self.pool.cancel(task_id)
                row = self.pool.status(task_id) or {}
                if row.get("status") in ("waiting_user", "awaiting_plan_approval"):
                    try:
                        finalized = bool(self.pool.deny_from_user(
                            task_id, reason=detail or "cancelled by user",
                            policy="cancelled",
                        ))
                    except ValueError:
                        finalized = False
                else:
                    finalized = bool(row.get("cancel")) or row.get("status") == "cancelled"
        view = None
        if self.emitter is not None and hasattr(self.emitter, "cancel"):
            view = self.emitter.cancel(detail=detail or "已取消")
        delivery = None
        if self.notify_bridge is not None and hasattr(self.notify_bridge, "notify_terminal"):
            delivery = self.notify_bridge.notify_terminal(
                CardState.CANCELLED, detail=detail or "cancelled", kick_delivery=True,
            )
        elif self.pool is not None:
            try:
                from agentcore.completion_delivery import notify_on_terminal
                delivery = notify_on_terminal(
                    self.pool, None, status="cancelled",
                )
            except Exception as exc:  # noqa: BLE001
                delivery = {"status": "error", "error": type(exc).__name__}
        ack = ControlAck(
            ok=True, verb="cancel", task_id=task_id,
            message="已取消" if finalized or view is not None else "取消已请求",
            second_exec=False, durable_dispatch_submit=False,
            detail={
                "finalized": finalized,
                "pool_status": (self.pool.status(task_id) if self.pool else None),
                "card_state": getattr(view, "state", None),
                "delivery": delivery,
                "c2_attempts": 1 if delivery is not None else 0,
            },
        )
        return self._emit_ack(ack)

    def urgent_inject(
        self,
        task_id: str,
        *,
        text: str = "",
        owner: str = "",
        origin: Optional[dict] = None,
        current_phase: str = "",
        replan: bool = False,
    ) -> dict:
        """D1-5: Enter soft-interrupt -> fast ack -> resume-or-replan (no second long exec)."""
        pause_out = self.pause(task_id, detail=text or "urgent inject")
        follow = None
        if text and text.strip():
            kind = "replan" if replan else "urgent_follow_up"
            follow = self.inbox.enqueue(
                task_id, text, owner=owner, origin=origin, kind=kind,
            )
        plan_parked = False
        if replan and self.pool is not None and hasattr(self.pool, "await_plan_approval"):
            try:
                plan_parked = bool(self.pool.await_plan_approval(task_id))
            except Exception:
                plan_parked = False
            if self.emitter is not None and hasattr(self.emitter, "await_plan"):
                try:
                    self.emitter.await_plan(detail=text or "紧急注入后待确认计划")
                except Exception:
                    pass
        message = "紧急插话已接收 · 已软中断 · 走 resume-or-replan（无第二长执行）"
        ack = ControlAck(
            ok=True, verb="urgent_inject", task_id=task_id, message=message,
            second_exec=False, durable_dispatch_submit=False,
            detail={
                "pause": pause_out.get("detail") if isinstance(pause_out, dict) else pause_out,
                "follow": follow,
                "plan_parked": plan_parked,
                "resume": "pool.resume_from_user",
                "replan": "await_plan_approval or queued follow-up",
                "current_phase": current_phase,
            },
        )
        return self._emit_ack(ack)

    def resume_observe(self, task_id: str, *, detail: str = "resume_observe") -> dict:
        """Prefer-D4: clear stall note / force beat observe — NEVER fake done/failed."""
        beat_row = None
        if self.ledger is not None and hasattr(self.ledger, "beat"):
            try:
                beat_row = self.ledger.beat(
                    task_id, phase="observing", detail=detail or "resume_observe",
                )
            except Exception as exc:  # noqa: BLE001
                ack = ControlAck(
                    ok=False, verb="resume_observe", task_id=task_id,
                    message="恢复观察失败：%s" % type(exc).__name__,
                    second_exec=False, durable_dispatch_submit=False,
                    detail={"error": type(exc).__name__, "auto_done": False},
                )
                return self._emit_ack(ack)
        view = None
        if self.emitter is not None and hasattr(self.emitter, "update"):
            try:
                view = self.emitter.update(
                    phase="observing",
                    detail=detail or "已恢复观察（未标记完成）",
                )
            except Exception:
                pass
        ok = beat_row is not None or view is not None
        ack = ControlAck(
            ok=ok, verb="resume_observe", task_id=task_id,
            message="已恢复观察（未标记完成/失败）" if ok else "恢复观察未生效",
            second_exec=False, durable_dispatch_submit=False,
            detail={
                "beat": beat_row,
                "card_phase": getattr(view, "phase", None),
                "auto_done": False,
                "auto_failed": False,
            },
        )
        return self._emit_ack(ack)

    def resume(self, task_id: str) -> dict:
        """Resume path after pause / urgent (reuse pool.resume_from_user)."""
        ok = False
        if self.pool is not None:
            ok = bool(self.pool.resume_from_user(task_id))
        ack = ControlAck(
            ok=ok, verb="resume", task_id=task_id,
            message="已恢复排队" if ok else "恢复失败（非 waiting_user/plan）",
            second_exec=False, durable_dispatch_submit=False,
            detail={"pool_status": (self.pool.status(task_id) if self.pool else None)},
        )
        return self._emit_ack(ack)

    def _emit_ack(self, ack: ControlAck) -> dict:
        """Prefer-D3: stamp SLO fields on every ControlAck (still ack on miss)."""
        if not ack.ack_id:
            ack.ack_id = uuid.uuid4().hex
        if not ack.ack_emitted_at:
            ack.ack_emitted_at = (
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )
        t0 = self._handle_t0
        if ack.ack_latency_ms is None and t0 is not None:
            try:
                ack.ack_latency_ms = max(0.0, (self._mono() - t0) * 1000.0)
            except Exception:  # noqa: BLE001
                ack.ack_latency_ms = None
        if not ack.slo_class:
            ack.slo_class = "control_fast"
        if ack.slo_met is None and ack.ack_latency_ms is not None:
            budget = (
                CONTROL_FAST_BUDGET_MS
                if ack.slo_class == "control_fast"
                else FEISHU_ATTEMPT_BUDGET_MS
            )
            ack.slo_met = bool(ack.ack_latency_ms <= budget)
            if not ack.slo_met:
                _LOG.warning(
                    "prefer_d3_ack_slo_miss verb=%s task=%s latency_ms=%s budget_ms=%s",
                    ack.verb, ack.task_id, ack.ack_latency_ms, budget,
                )
                if self.emitter is not None and hasattr(self.emitter, "update"):
                    try:
                        self.emitter.update(
                            phase="ack_miss",
                            detail="ack SLO miss %.0fms" % (ack.ack_latency_ms or 0),
                        )
                    except Exception:
                        pass
        # Bind ack_id onto the durable item when present (ok-ack linkage).
        queued = (ack.detail or {}).get("queued") if isinstance(ack.detail, dict) else None
        if (
            isinstance(queued, dict)
            and queued.get("id")
            and self.inbox is not None
            and getattr(self.inbox, "store", None) is not None
        ):
            try:
                self.inbox.store.bind_ack(queued["id"], ack.ack_id)
            except Exception:
                pass
        if self.ack_sink is not None:
            try:
                self.ack_sink(ack)
            except Exception:
                pass
        return ack.as_dict()


def bind_follow_up_to_task(
    inbox: FollowUpInbox,
    task_id: str,
    text: str,
    *,
    owner: str = "",
    origin: Optional[dict] = None,
) -> dict:
    """durable_dispatch / host helper: Tab enqueue bound to task-id + origin."""
    return inbox.enqueue(task_id, text, owner=owner, origin=origin)

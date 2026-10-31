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

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from agentcore.task_progress import (
    FollowUpQueue,
    IRREVERSIBLE_PHASES,
    poll_progress_from_ledger,
)
from agentcore.task_card import CardState


class ControlVerb(str, Enum):
    STATUS = "status"
    FOLLOW_UP = "follow_up"
    PAUSE = "pause"
    CANCEL = "cancel"
    URGENT_INJECT = "urgent_inject"
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
    """Fast ack payload for Feishu text / same-card note."""

    ok: bool
    verb: str
    task_id: str
    message: str
    second_exec: bool = False
    durable_dispatch_submit: bool = False
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verb": self.verb,
            "task_id": self.task_id,
            "message": self.message,
            "second_exec": self.second_exec,
            "durable_dispatch_submit": self.durable_dispatch_submit,
            "detail": dict(self.detail),
        }


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

    Persists nothing new in pool schema — wraps FollowUpQueue so durable_dispatch /
    host control can enqueue without calling submit (no second long exec).
    """

    def __init__(self):
        self._by_task: dict[str, FollowUpQueue] = {}

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
    ) -> dict:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id_required")
        q = self._queue(task_id)
        result = q.enqueue(text, kind=kind, task_id=task_id)
        result["owner"] = str(owner or "")
        if origin:
            result["origin"] = {
                k: origin[k]
                for k in ("profile", "session", "route", "channel", "chat", "thread", "turn")
                if isinstance(origin, dict) and k in origin
            }
        result["second_exec"] = False
        result["durable_dispatch_submit"] = False
        return result

    def peek(self, task_id: str) -> list:
        return self._queue(task_id).peek()

    def drain(self, task_id: str, *, current_phase: str = "", allow_preempt: bool = False) -> list:
        return self._queue(task_id).drain(
            current_phase=current_phase, allow_preempt=allow_preempt,
        )

    def queued_count(self, task_id: str) -> int:
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
        ack_sink: Optional[Callable[[ControlAck], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.pool = pool
        self.ledger = ledger
        self.emitter = emitter
        self.notify_bridge = notify_bridge
        self.inbox = inbox if inbox is not None else FollowUpInbox()
        self.ack_sink = ack_sink
        self._clock = clock or (lambda: 0.0)
        self._llm_calls = 0  # Prefer-D1: must stay 0 on control short-path

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
        """D1-2: Tab queue — do not preempt irreversible; no second long exec."""
        phase = str(current_phase or "").strip().lower()
        preempt_blocked = phase in IRREVERSIBLE_PHASES
        queued = self.inbox.enqueue(
            task_id, text, owner=owner, origin=origin, kind="follow_up",
        )
        if preempt_blocked:
            hint = "已排队后续补充（当前不可打断：%s）" % phase
        else:
            hint = "已排队后续补充（Tab）· 共 %d 条" % queued.get("queued", 1)
        if self.emitter is not None and hasattr(self.emitter, "mark_queued_follow_up"):
            try:
                self.emitter.mark_queued_follow_up(detail=hint, count=queued.get("queued", 1))
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

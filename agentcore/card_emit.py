"""Adapter-facing card emitter: drive one task's card from open to terminal.

Ties the protocol (:mod:`agentcore.task_card`) and its durable store
(:mod:`agentcore.card_store`) to an injected *transport*. The transport is the only
platform-specific part; everything here is the single source of truth for **what a card
says and when it changes**, so every adapter renders the same structure.

The emitter is the only writer for a card. Construct one per task; after a restart,
construct a new one — it resumes the persisted sequence rather than forking it.

Colleague identity (role_id / display_name) is resolved from the colleague ontology
(:mod:`agentcore.colleague_dev` / :mod:`agentcore.colleague_catalog`) — never from
model text and never as business-overlay literals. Approval / HITL rows may cite which
role is waiting using the same source (Codex §6-4).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Sequence, Tuple

from .card_store import CardStore
from .task_card import CardFrame, CardHandle, CardState

if TYPE_CHECKING:
    from .follow_up_store import FollowUpStore


@dataclass(frozen=True)
class CardView:
    """Structured content for one card. Adapters map this onto their own card schema.

    ``actions`` are platform-neutral tokens (``cancel`` / ``details`` / ``logs`` /
    ``report`` / ``rerun`` / ``approve`` / ``deny`` / ``approve_plan`` /
    ``pause`` / ``status`` / ``resume_observe``); the adapter decides how to
    render them. Prefer-C1 HITL uses ``approve`` / ``deny``. Prefer-D4 stalled/lost
    ops use ``cancel`` / ``pause`` / ``status`` / ``resume_observe`` (not Approve/Deny).

    Identity fields are optional: when present, ``title`` should already include
    ``display_name + task_title`` so a platform that only paints ``title`` still shows
    who is running what.
    """

    title: str
    state: str
    phase: str
    host: str
    artifacts: Tuple[str, ...]
    note: str
    actions: Tuple[str, ...]
    final: bool
    role_id: str = ""
    display_name: str = ""
    task_title: str = ""
    heartbeat_age_s: Optional[float] = None
    attempt_id: str = ""
    waiting_role: str = ""
    # Prefer-C1 HITL source labels + summary (blank-ok; host-bound only).
    from_bot: str = ""
    source_thread: str = ""
    hitl_kind: str = ""
    hitl_summary: str = ""
    hitl_request_id: str = ""
    # Prefer-D4 stalled/lost verdict (blank when not a stall ops card).
    verdict: str = ""
    # Prefer-D3/D5: durable queued follow-up count (0 when unknown).
    queued_follow_up_count: int = 0

    def render(self) -> dict:
        payload = {
            "title": self.title,
            "state": self.state,
            "phase": self.phase,
            "host": self.host,
            "artifacts": list(self.artifacts),
            "note": self.note,
            "actions": list(self.actions),
            "final": self.final,
        }
        if self.role_id:
            payload["role_id"] = self.role_id
        if self.display_name:
            payload["display_name"] = self.display_name
        if self.task_title:
            payload["task_title"] = self.task_title
        if self.heartbeat_age_s is not None:
            payload["heartbeat_age_s"] = self.heartbeat_age_s
        if self.attempt_id:
            payload["attempt_id"] = self.attempt_id
        if self.waiting_role:
            payload["waiting_role"] = self.waiting_role
        if self.from_bot:
            payload["from_bot"] = self.from_bot
        if self.source_thread:
            payload["source_thread"] = self.source_thread
        if self.hitl_kind:
            payload["hitl_kind"] = self.hitl_kind
        if self.hitl_summary:
            payload["hitl_summary"] = self.hitl_summary
        if self.hitl_request_id:
            payload["hitl_request_id"] = self.hitl_request_id
        if self.verdict:
            payload["verdict"] = self.verdict
        if self.queued_follow_up_count:
            payload["queued_follow_up_count"] = int(self.queued_follow_up_count)
        return payload


#: Which buttons a card offers, by state. Tokens, not labels.
_ACTIONS = {
    CardState.QUEUED: ("cancel", "details"),
    CardState.RUNNING: ("cancel", "pause", "status", "details", "logs"),
    CardState.WAITING_USER: ("approve", "deny", "details", "logs"),
    CardState.AWAITING_PLAN_APPROVAL: ("approve", "deny", "details"),
    CardState.DONE: ("details", "report", "rerun"),
    CardState.FAILED: ("details", "logs", "rerun"),
    CardState.CANCELLED: ("details", "rerun"),
}

#: Prefer-D4 ops actions on a stalled/lost same-card (not C1 Approve/Deny).
_STALLED_ACTIONS = ("cancel", "pause", "status", "resume_observe", "details")


def _actions_for(frame: CardFrame) -> Tuple[str, ...]:
    """Pick button tokens: stalled/lost phases get ops actions while non-terminal."""
    phase = (frame.phase or "").strip().lower()
    if phase in ("stalled", "lost") and not frame.state.terminal:
        return _STALLED_ACTIONS
    return _ACTIONS.get(frame.state, ("details",))


def resolve_display_name(role_id: str, *, catalog: Optional[dict] = None) -> str:
    """Look up ``display_name`` from DEV_ROLES / catalog. Empty when unknown.

    The model must not forge owner/role: callers pass a host-bound ``role_id`` and this
    helper only reads the ontology. Never paste job titles into business skill literals.
    """
    if not isinstance(role_id, str) or not role_id.strip():
        return ""
    rid = role_id.strip()
    if isinstance(catalog, dict):
        colleagues = catalog.get("colleagues") if "colleagues" in catalog else None
        if isinstance(colleagues, list):
            for row in colleagues:
                if isinstance(row, dict) and row.get("role") == rid:
                    name = row.get("display_name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()[:80]
        meta = catalog.get(rid)
        if isinstance(meta, dict):
            name = meta.get("display_name")
            if isinstance(name, str) and name.strip():
                return name.strip()[:80]
    try:
        from .colleague_dev import DEV_ROLES
    except Exception:
        return ""
    meta = DEV_ROLES.get(rid) if isinstance(DEV_ROLES, dict) else None
    if isinstance(meta, dict):
        name = meta.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.strip()[:80]
    return ""


def compose_title(*, display_name: str = "", task_title: str = "",
                  fallback: str = "任务") -> str:
    """Boss-visible title: ``display_name · task_title`` when both are known."""
    name = (display_name or "").strip()
    title = (task_title or "").strip()
    if name and title:
        return ("%s · %s" % (name, title))[:120]
    if name:
        return name[:120]
    if title:
        return title[:120]
    return (fallback or "任务")[:120]


def _note(state: CardState, elapsed: int, *, heartbeat_age_s: Optional[float] = None,
          waiting_role: str = "", phase: str = "") -> str:
    """A quiet card still advances its elapsed time, so silence stays informative.

    Prefer-D2: phases starting / canceling / paused must not read as healthy Running.
    """
    phase_l = (phase or "").strip().lower()
    if state is CardState.WAITING_USER or phase_l in ("paused", "waiting_user"):
        who = ("（%s）" % waiting_role) if waiting_role else ""
        if phase_l == "paused":
            return "已暂停%s · 已用 %d 秒" % (who, elapsed)
        return "等待用户确认%s · 已用 %d 秒" % (who, elapsed)
    if state is CardState.AWAITING_PLAN_APPROVAL:
        who = ("（%s）" % waiting_role) if waiting_role else ""
        return "等待计划审批%s · 已用 %d 秒" % (who, elapsed)
    if state.terminal:
        return "已用 %d 秒" % elapsed
    minutes, seconds = divmod(elapsed, 60)
    clock = "%d分%02d秒" % (minutes, seconds) if minutes else "%d秒" % seconds
    if phase_l == "starting":
        base = "启动中 %s" % clock
    elif phase_l == "canceling":
        base = "取消中 %s" % clock
    elif phase_l == "stalled":
        base = "已停滞 %s" % clock
    elif phase_l == "lost":
        base = "进程丢失 %s" % clock
    else:
        base = "运行中 %s" % clock
    if heartbeat_age_s is not None:
        age = max(0, int(heartbeat_age_s))
        base = "%s · 心跳 %d秒前" % (base, age)
    return base


def view_for(handle: CardHandle, frame: CardFrame, *, elapsed: int,
             title: str = "任务") -> CardView:
    display_name = frame.display_name or handle.display_name
    task_title = frame.task_title or handle.task_title
    role_id = frame.role_id or handle.role_id
    composed = compose_title(display_name=display_name, task_title=task_title,
                             fallback=title)
    waiting_role = ""
    if frame.state in (CardState.WAITING_USER, CardState.AWAITING_PLAN_APPROVAL):
        waiting_role = display_name or role_id
    hitl_kind = frame.hitl_kind
    if not hitl_kind and frame.state is CardState.AWAITING_PLAN_APPROVAL:
        hitl_kind = "plan"
    elif not hitl_kind and frame.state is CardState.WAITING_USER:
        hitl_kind = "waiting_user"
    return CardView(
        title=composed,
        state=frame.state.value,
        phase=frame.phase,
        host=frame.host,
        artifacts=tuple(frame.artifacts),
        note=_note(frame.state, elapsed, heartbeat_age_s=frame.heartbeat_age_s,
                   waiting_role=waiting_role, phase=frame.phase),
        actions=_actions_for(frame),
        final=frame.final,
        role_id=role_id,
        display_name=display_name,
        task_title=task_title,
        heartbeat_age_s=frame.heartbeat_age_s,
        attempt_id=frame.attempt_id,
        waiting_role=waiting_role,
        from_bot=frame.from_bot,
        source_thread=frame.source_thread,
        hitl_kind=hitl_kind,
        hitl_summary=frame.hitl_summary or frame.detail,
        hitl_request_id=frame.hitl_request_id,
        verdict=((frame.phase or "").strip().lower()
                 if (frame.phase or "").strip().lower() in ("stalled", "lost")
                 else ""),
    )


class CardEmitter:
    """Drives one card through a transport.

    The transport must expose ``emit(handle, view, *, final)``. Frames the protocol drops
    (stale, duplicate, or after a terminal state) are never handed to the transport, so a
    late progress frame can never overwrite a finished card.
    """

    def __init__(self, store: CardStore, task_id: str, transport, *,
                 title: str = "任务", clock: Optional[Callable[[], float]] = None,
                 follow_up_store: Optional["FollowUpStore"] = None):
        self._store = store
        self._transport = transport
        self._title = title
        self._clock = clock or (lambda: 0.0)
        self._writer = store.writer(task_id)
        self._follow_up_store = follow_up_store
        self._task_id = task_id
        # Seed task_title from constructor when the handle has none yet.
        if title and title != "任务" and not self._writer.handle.task_title:
            self._writer.handle.task_title = title
            self._store.save(self._writer.handle)

    @property
    def handle(self) -> CardHandle:
        return self._writer.handle

    def bind_identity(self, *, role_id: str = "", display_name: str = "",
                      task_title: str = "", catalog: Optional[dict] = None) -> CardHandle:
        """Attach ontology identity to the handle. Model text is never accepted as role."""
        handle = self._writer.handle
        if role_id:
            handle.role_id = role_id.strip()[:64]
            if not display_name:
                display_name = resolve_display_name(handle.role_id, catalog=catalog)
        if display_name:
            handle.display_name = display_name.strip()[:80]
        if task_title:
            handle.task_title = task_title.strip()[:120]
        self._store.save(handle)
        return handle

    def _elapsed(self) -> int:
        started = self._writer.handle.started_at
        return max(0, int(self._clock() - started)) if started else 0

    def _publish(self, frame: Optional[CardFrame]) -> Optional[CardView]:
        if frame is None:
            return None
        view = view_for(self._writer.handle, frame, elapsed=self._elapsed(), title=self._title)
        self._transport.emit(self._writer.handle, view, final=frame.final)
        return view

    def _identity_kwargs(self):
        h = self._writer.handle
        return dict(role_id=h.role_id, display_name=h.display_name, task_title=h.task_title)

    def open(self, *, phase: str = "", host: str = "",
             artifacts: Sequence[str] = (),
             heartbeat_age_s: Optional[float] = None,
             attempt_id: str = "") -> Optional[CardView]:
        return self._publish(self._writer.emit(
            CardState.RUNNING, phase=phase, host=host, artifacts=artifacts,
            heartbeat_age_s=heartbeat_age_s, attempt_id=attempt_id,
            **self._identity_kwargs()))

    def update(self, *, phase: str = "", detail: str = "", host: str = "",
               artifacts: Sequence[str] = (),
               heartbeat_age_s: Optional[float] = None,
               attempt_id: str = "") -> Optional[CardView]:
        return self._publish(self._writer.emit(
            CardState.RUNNING, phase=phase, detail=detail, host=host, artifacts=artifacts,
            heartbeat_age_s=heartbeat_age_s, attempt_id=attempt_id,
            **self._identity_kwargs()))

    def wait_user(self, *, detail: str = "", phase: str = "waiting_user",
                  from_bot: str = "", source_thread: str = "",
                  hitl_kind: str = "waiting_user", hitl_summary: str = "",
                  hitl_request_id: str = "") -> Optional[CardView]:
        """Enter HITL without finishing the card (Codex notify / plan gate)."""
        return self._publish(self._writer.emit(
            CardState.WAITING_USER, phase=phase, detail=detail,
            from_bot=from_bot, source_thread=source_thread,
            hitl_kind=hitl_kind or "waiting_user",
            hitl_summary=hitl_summary or detail,
            hitl_request_id=hitl_request_id,
            **self._identity_kwargs()))

    def await_plan(self, *, detail: str = "", phase: str = "awaiting_plan_approval",
                   from_bot: str = "", source_thread: str = "",
                   hitl_kind: str = "plan", hitl_summary: str = "",
                   hitl_request_id: str = "") -> Optional[CardView]:
        """Plan-before-run gate: card shows awaiting_plan_approval (frees worker slot)."""
        return self._publish(self._writer.emit(
            CardState.AWAITING_PLAN_APPROVAL, phase=phase, detail=detail,
            from_bot=from_bot, source_thread=source_thread,
            hitl_kind=hitl_kind or "plan",
            hitl_summary=hitl_summary or detail,
            hitl_request_id=hitl_request_id,
            **self._identity_kwargs()))

    def finish(self, *, phase: str = "", host: str = "",
               artifacts: Sequence[str] = ()) -> Optional[CardView]:
        """Terminal success. Always the card's last frame — never a second message."""
        return self._publish(self._writer.emit(
            CardState.DONE, phase=phase, host=host, artifacts=artifacts, final=True,
            **self._identity_kwargs()))

    def fail(self, *, detail: str = "", artifacts: Sequence[str] = ()) -> Optional[CardView]:
        """Terminal failure. Never rendered as success, even if the host raised."""
        return self._publish(self._writer.emit(
            CardState.FAILED, detail=detail, artifacts=artifacts, final=True,
            **self._identity_kwargs()))

    def cancel(self, *, detail: str = "") -> Optional[CardView]:
        return self._publish(self._writer.emit(
            CardState.CANCELLED, detail=detail, final=True, **self._identity_kwargs()))

    def pause(self, *, detail: str = "", phase: str = "paused") -> Optional[CardView]:
        """Prefer-D1: soft-pause card (waiting_user + phase paused). Not a C1 UX rework."""
        return self.wait_user(detail=detail or "已暂停", phase=phase or "paused",
                              hitl_kind="waiting_user", hitl_summary=detail or "paused")

    def mark_queued_follow_up(
        self,
        *,
        detail: str = "",
        count: int = 1,
        task_id: str = "",
    ) -> Optional[CardView]:
        """Prefer-D1/D3 Tab: same-card hint; count from durable peek when store bound."""
        tid = (task_id or getattr(self, "_task_id", "") or "").strip()
        durable_count = None
        store = getattr(self, "_follow_up_store", None)
        if store is not None and tid:
            try:
                durable_count = int(store.queued_count(tid))
            except Exception:  # noqa: BLE001
                durable_count = None
        n = durable_count if durable_count is not None else max(1, int(count or 1))
        note = detail or ("queued follow-up x%d" % n)
        view = self.update(phase="queued_follow_up", detail=note)
        return view

    def durable_queued_follow_up_count(self, task_id: str = "") -> int:
        """Prefer-D3: peek durable inbox count for this card's task."""
        tid = (task_id or getattr(self, "_task_id", "") or "").strip()
        store = getattr(self, "_follow_up_store", None)
        if store is None or not tid:
            return 0
        try:
            return int(store.queued_count(tid))
        except Exception:  # noqa: BLE001
            return 0


    def mark_starting(self, *, detail: str = "") -> Optional[CardView]:
        """Prefer-D2: claim-without-PID window — stage starting, never healthy Running."""
        return self.update(phase="starting", detail=detail or "starting")

    def mark_canceling(self, *, detail: str = "") -> Optional[CardView]:
        """Prefer-D2: soft-cancel fence — stage canceling until terminal or timeout."""
        return self.update(phase="canceling", detail=detail or "canceling")

    def mark_stalled(self, *, detail: str = "", heartbeat_age_s: Optional[float] = None,
                     verdict: str = "stalled") -> Optional[CardView]:
        """Prefer-D4: same-card stalled/lost ops frame (RUNNING + phase; not HITL)."""
        phase = "lost" if str(verdict or "").strip().lower() == "lost" else "stalled"
        return self.update(
            phase=phase,
            detail=detail or phase,
            heartbeat_age_s=heartbeat_age_s,
        )

    def mark_lost(self, *, detail: str = "",
                  heartbeat_age_s: Optional[float] = None) -> Optional[CardView]:
        """Prefer-D4: process-gone observation — actionable same-card, not auto-failed."""
        return self.mark_stalled(
            detail=detail or "lost", heartbeat_age_s=heartbeat_age_s, verdict="lost",
        )

    def apply_pool_stage(self, stage: str, *, detail: str = "") -> Optional[CardView]:
        """Map pool.display_stage onto CardView (starting/canceling/paused/terminal)."""
        s = (stage or "").strip().lower()
        if s == "starting":
            return self.mark_starting(detail=detail)
        if s == "canceling":
            return self.mark_canceling(detail=detail)
        if s in ("paused", "waiting_user"):
            return self.pause(detail=detail or "paused", phase="paused")
        if s == "failed":
            return self.fail(detail=detail or "failed")
        if s == "cancelled":
            return self.cancel(detail=detail or "cancelled")
        if s in ("completed", "done"):
            return self.finish(phase=detail or "done")
        if s == "running":
            return self.update(phase="running", detail=detail)
        if s == "stalled":
            return self.mark_stalled(detail=detail or "stalled")
        if s == "lost":
            return self.mark_lost(detail=detail or "lost")
        return self.update(phase=s or "queued", detail=detail)

    def notify_terminal_or_wait(self, state: CardState, *, detail: str = "",
                                phase: str = "") -> Optional[CardView]:
        """Same-card notify for turn-complete / approval-requested — no new model turn.

        Codex §6-1: terminal / waiting_user / approval notify patches this card (and/or
        rides completion_delivery). Callers must not open a fresh model turn just to say
        "done" or "waiting".
        """
        if state is CardState.DONE:
            return self.finish(phase=phase or detail)
        if state is CardState.FAILED:
            return self.fail(detail=detail)
        if state is CardState.CANCELLED:
            return self.cancel(detail=detail)
        if state is CardState.AWAITING_PLAN_APPROVAL:
            return self.await_plan(detail=detail, phase=phase or "awaiting_plan_approval")
        if state is CardState.WAITING_USER:
            return self.wait_user(detail=detail, phase=phase or "waiting_user")
        return self.update(phase=phase, detail=detail)

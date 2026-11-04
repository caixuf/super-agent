"""Task-card handle contract — one durable card per task, one writer, monotonic frames.

Scope
-----
A long-running job needs a *stable observable surface* in a chat platform: a card that
is born with the job, keeps a single identity across process restarts, and ends in a
terminal state that is never overwritten. This module owns that protocol and nothing
else:

* **ordering** — strictly increasing ``sequence`` per card; stale frames are dropped;
* **idempotency** — every frame carries a deterministic ``uuid`` so a retry cannot
  double-apply;
* **terminal locking** — once a card reaches a terminal state, later frames are dropped,
  so a late progress update can never overwrite the final answer;
* **coarse routing** — decide whether a request earns a card at all, or should be
  answered inline, or should be streamed as a reply;
* **colleague visibility** — optional ``role_id`` / ``display_name`` / ``task_title`` /
  ``heartbeat_age_s`` / ``attempt_id`` ride on frames so platform adapters can render
  "who is running what" without inventing a second identity source.

Durability is deliberately *not* here. The mapping ``task_id -> CardHandle`` belongs to
the host job store; this module is pure logic so it can be exercised without a database,
a network, or a clock it does not own. ``clock`` is injected for the same reason.

No third-party imports: the package import-boundary gate forbids them, and the host
supplies persistence and transport by injection.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Tuple


#: Heartbeat patches on the same card must be throttled (Feishu single-card QPS + UX).
#: Phase changes always push; quiet heartbeats wait at least this many seconds.
HEARTBEAT_THROTTLE_S = 45.0
HEARTBEAT_THROTTLE_MIN_S = 30.0
HEARTBEAT_THROTTLE_MAX_S = 60.0


class CardState(str, enum.Enum):
    """Lifecycle of a task card. Terminal states are write-once.

    ``WAITING_USER`` and ``AWAITING_PLAN_APPROVAL`` are non-terminal HITL states: the
    card stays open, but the pool must not claim the job while it waits (see pool
    ``waiting_user``). Plan-before-run lands as ``AWAITING_PLAN_APPROVAL`` then
    transitions to ``WAITING_USER`` / ``QUEUED`` after the user confirms.
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def frees_worker_slot(self) -> bool:
        """HITL / plan-await states must not occupy an execution slot."""
        return self in _FREES_SLOT


_TERMINAL = frozenset({CardState.DONE, CardState.FAILED, CardState.CANCELLED})
_FREES_SLOT = frozenset({CardState.WAITING_USER, CardState.AWAITING_PLAN_APPROVAL})


@dataclass(frozen=True)
class CardFrame:
    """One observable update of a card. Immutable; the writer stamps ``sequence``/``uuid``."""

    sequence: int
    state: CardState
    uuid: str
    phase: str = ""
    detail: str = ""
    host: str = ""
    artifacts: Tuple[str, ...] = ()
    final: bool = False
    # Colleague visibility (optional; ontology / liveness — never model-forged).
    role_id: str = ""
    display_name: str = ""
    task_title: str = ""
    heartbeat_age_s: Optional[float] = None
    attempt_id: str = ""
    # Prefer-C1 HITL source labels (optional; host/origin/ontology — never model-forged).
    from_bot: str = ""
    source_thread: str = ""
    hitl_kind: str = ""  # "waiting_user" | "plan" | ""
    hitl_summary: str = ""
    hitl_request_id: str = ""
    # Prefer-D5 thin additive for D-stream (optional; blank-ok).
    queued_follow_up_count: int = 0
    liveness_verdict: str = ""  # running|stalled|lost|quiet
    pool_window: str = ""  # starting|canceling|""
    ack_miss: bool = False

    def render(self) -> dict:
        """Platform-agnostic view; adapters map this onto their own card schema."""
        payload = {
            "sequence": self.sequence,
            "uuid": self.uuid,
            "state": self.state.value,
            "phase": self.phase,
            "detail": self.detail,
            "host": self.host,
            "artifacts": list(self.artifacts),
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
        if self.queued_follow_up_count:
            payload["queued_follow_up_count"] = int(self.queued_follow_up_count)
        if self.liveness_verdict:
            payload["liveness_verdict"] = self.liveness_verdict
        if self.pool_window:
            payload["pool_window"] = self.pool_window
        if self.ack_miss:
            payload["ack_miss"] = True
        return payload


@dataclass
class CardHandle:
    """The durable identity of one task's card.

    Persist this in the host job store keyed by ``task_id``; do not keep it only in
    adapter memory. A restarted worker that can re-read this handle can keep updating the
    *same* card instead of orphaning it.

    ``card_id`` is the platform Card Kit entity id (when the adapter created one). It is
    optional so text-fallback transports still bind a message without a Kit card.
    """

    task_id: str
    chat_id: str
    message_id: str
    sequence: int = 0
    state: CardState = CardState.QUEUED
    started_at: float = 0.0
    card_id: str = ""
    role_id: str = ""
    display_name: str = ""
    task_title: str = ""

    @property
    def terminal(self) -> bool:
        return self.state.terminal


def should_push_frame(*, last_push_at: float, now: float, phase_changed: bool,
                      min_interval: float = HEARTBEAT_THROTTLE_S) -> bool:
    """Same-card push gate: phase change always; otherwise throttle heartbeats.

    ``min_interval`` is clamped into [30, 60] so callers cannot silently spam or stall
    forever under a misconfig.
    """
    interval = max(HEARTBEAT_THROTTLE_MIN_S, min(HEARTBEAT_THROTTLE_MAX_S, float(min_interval)))
    if phase_changed:
        return True
    if last_push_at <= 0:
        return True
    return (float(now) - float(last_push_at)) >= interval


class CardWriter:
    """Single-writer frame emitter for one card.

    Exactly one instance per card owns emission. Every commit either returns the frame it
    accepted or ``None`` when the frame was dropped. Dropping is silent by design: a late
    or duplicated frame must never resurrect or overwrite a card that already reached its
    final state.

    ``clock`` is only consulted to stamp ``started_at`` on the first accepted frame, so it
    must be a wall clock: ``0.0`` is the "unset" sentinel.
    """

    def __init__(self, handle: CardHandle, *, clock: Optional[Callable[[], float]] = None):
        self.handle = handle
        self._clock = clock or (lambda: 0.0)
        self._seen: set = set()

    def commit(self, frame: CardFrame) -> Optional[CardFrame]:
        """Validate an already-built frame against the ordering/idempotency/terminal rules."""
        if self.handle.terminal:
            return None
        if frame.uuid in self._seen:
            return None
        if frame.sequence <= self.handle.sequence:
            return None
        if frame.final and not frame.state.terminal:
            raise ValueError("final frame must carry a terminal state")
        self._seen.add(frame.uuid)
        self.handle.sequence = frame.sequence
        self.handle.state = frame.state
        if frame.role_id:
            self.handle.role_id = frame.role_id
        if frame.display_name:
            self.handle.display_name = frame.display_name
        if frame.task_title:
            self.handle.task_title = frame.task_title
        if self.handle.started_at == 0.0:
            self.handle.started_at = self._clock()
        return frame

    def emit(self, state: CardState, *, phase: str = "", detail: str = "", host: str = "",
             artifacts: Sequence[str] = (), final: bool = False,
             role_id: str = "", display_name: str = "", task_title: str = "",
             heartbeat_age_s: Optional[float] = None, attempt_id: str = "",
             from_bot: str = "", source_thread: str = "", hitl_kind: str = "",
             hitl_summary: str = "", hitl_request_id: str = "",
             queued_follow_up_count: int = 0, liveness_verdict: str = "",
             pool_window: str = "", ack_miss: bool = False) -> Optional[CardFrame]:
        """Build the next frame for this card and commit it.

        Returns the committed frame, or ``None`` if the card is already terminal.
        Identity fields default from the handle when the caller leaves them blank, so a
        heartbeat does not wipe the colleague row.
        """
        seq = self.handle.sequence + 1
        frame = CardFrame(
            sequence=seq,
            state=state,
            uuid="%s:%d" % (self.handle.task_id, seq),
            phase=phase,
            detail=detail,
            host=host,
            artifacts=tuple(artifacts),
            final=final,
            role_id=role_id or self.handle.role_id,
            display_name=display_name or self.handle.display_name,
            task_title=task_title or self.handle.task_title,
            heartbeat_age_s=heartbeat_age_s,
            attempt_id=attempt_id,
            from_bot=from_bot,
            source_thread=source_thread,
            hitl_kind=hitl_kind,
            hitl_summary=hitl_summary or detail,
            hitl_request_id=hitl_request_id,
            queued_follow_up_count=int(queued_follow_up_count or 0),
            liveness_verdict=(liveness_verdict or "").strip(),
            pool_window=(pool_window or "").strip(),
            ack_miss=bool(ack_miss),
        )
        return self.commit(frame)

    def heartbeat(self, *, phase: Optional[str] = None,
                  heartbeat_age_s: Optional[float] = None) -> Optional[CardFrame]:
        """Emit a RUNNING frame so the card never freezes while the host is quiet.

        Silence should be informative: a card with no news still advances its elapsed
        time instead of looking stuck. Callers should gate this with
        :func:`should_push_frame` so quiet heartbeats stay within the throttle window.
        """
        return self.emit(
            CardState.RUNNING,
            phase=self.handle.state.value if phase is None else phase,
            heartbeat_age_s=heartbeat_age_s,
        )


class RouteKind(str, enum.Enum):
    """How a request should be handled, by time scale rather than by wording."""

    LOOKUP = "lookup"              # deterministic; answer inline, no card
    LONG_JOB = "long_job"          # open a card, then notify on completion
    CONVERSATION = "conversation"  # stream a reply


@dataclass(frozen=True)
class RouteDecision:
    kind: RouteKind
    reason: str
    opens_card: bool
    streams: bool


#: Requests estimated at or above this many seconds get a card instead of a streamed
#: reply. Streaming a multi-minute job pretends to be busy while the user only wants to
#: be told when it is done.
LONG_JOB_SECONDS = 20.0


def route(*, deterministic_hit: bool = False, estimated_seconds: Optional[float] = None,
          needs_reasoning: bool = False,
          threshold: float = LONG_JOB_SECONDS) -> RouteDecision:
    """Choose an interaction shape from *signals*, never from the request's wording.

    Callers pass what they already know: whether a deterministic lookup matched, how long
    the work is expected to take, and whether the turn is a reasoning turn.
    """
    if deterministic_hit:
        return RouteDecision(RouteKind.LOOKUP, "deterministic hit — answer inline",
                             opens_card=False, streams=False)
    if estimated_seconds is not None and estimated_seconds >= threshold:
        return RouteDecision(
            RouteKind.LONG_JOB,
            "estimated %gs >= %gs — card + notify on completion" % (estimated_seconds, threshold),
            opens_card=True, streams=False)
    if needs_reasoning:
        return RouteDecision(RouteKind.CONVERSATION, "reasoning turn — stream the reply",
                             opens_card=False, streams=True)
    return RouteDecision(RouteKind.CONVERSATION, "short turn — reply",
                         opens_card=False, streams=True)

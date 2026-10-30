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
  answered inline, or should be streamed as a reply.

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


class CardState(str, enum.Enum):
    """Lifecycle of a task card. Terminal states are write-once."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset({CardState.DONE, CardState.FAILED, CardState.CANCELLED})


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

    def render(self) -> dict:
        """Platform-agnostic view; adapters map this onto their own card schema."""
        return {
            "sequence": self.sequence,
            "uuid": self.uuid,
            "state": self.state.value,
            "phase": self.phase,
            "detail": self.detail,
            "host": self.host,
            "artifacts": list(self.artifacts),
            "final": self.final,
        }


@dataclass
class CardHandle:
    """The durable identity of one task's card.

    Persist this in the host job store keyed by ``task_id``; do not keep it only in
    adapter memory. A restarted worker that can re-read this handle can keep updating the
    *same* card instead of orphaning it.
    """

    task_id: str
    chat_id: str
    message_id: str
    sequence: int = 0
    state: CardState = CardState.QUEUED
    started_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.state.terminal


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
        if self.handle.started_at == 0.0:
            self.handle.started_at = self._clock()
        return frame

    def emit(self, state: CardState, *, phase: str = "", detail: str = "", host: str = "",
             artifacts: Sequence[str] = (), final: bool = False) -> Optional[CardFrame]:
        """Build the next frame for this card and commit it.

        Returns the committed frame, or ``None`` if the card is already terminal.
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
        )
        return self.commit(frame)

    def heartbeat(self, *, phase: Optional[str] = None) -> Optional[CardFrame]:
        """Emit a RUNNING frame so the card never freezes while the host is quiet.

        Silence should be informative: a card with no news still advances its elapsed
        time instead of looking stuck.
        """
        return self.emit(CardState.RUNNING, phase=self.handle.state.value if phase is None else phase)


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

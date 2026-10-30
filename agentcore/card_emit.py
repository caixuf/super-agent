"""Adapter-facing card emitter: drive one task's card from open to terminal.

Ties the protocol (:mod:`agentcore.task_card`) and its durable store
(:mod:`agentcore.card_store`) to an injected *transport*. The transport is the only
platform-specific part; everything here is the single source of truth for **what a card
says and when it changes**, so every adapter renders the same structure.

The emitter is the only writer for a card. Construct one per task; after a restart,
construct a new one — it resumes the persisted sequence rather than forking it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

from .card_store import CardStore
from .task_card import CardFrame, CardHandle, CardState


@dataclass(frozen=True)
class CardView:
    """Structured content for one card. Adapters map this onto their own card schema.

    ``actions`` are platform-neutral tokens (``cancel`` / ``details`` / ``logs`` /
    ``report`` / ``rerun``); the adapter decides how to render them.
    """

    title: str
    state: str
    phase: str
    host: str
    artifacts: Tuple[str, ...]
    note: str
    actions: Tuple[str, ...]
    final: bool

    def render(self) -> dict:
        return {
            "title": self.title,
            "state": self.state,
            "phase": self.phase,
            "host": self.host,
            "artifacts": list(self.artifacts),
            "note": self.note,
            "actions": list(self.actions),
            "final": self.final,
        }


#: Which buttons a card offers, by state. Tokens, not labels.
_ACTIONS = {
    CardState.QUEUED: ("cancel", "details"),
    CardState.RUNNING: ("cancel", "details", "logs"),
    CardState.DONE: ("details", "report", "rerun"),
    CardState.FAILED: ("details", "logs", "rerun"),
    CardState.CANCELLED: ("details", "rerun"),
}


def _note(state: CardState, elapsed: int) -> str:
    """A quiet card still advances its elapsed time, so silence stays informative."""
    if state.terminal:
        return "已用 %d 秒" % elapsed
    minutes, seconds = divmod(elapsed, 60)
    return "运行中 %d分%02d秒" % (minutes, seconds) if minutes else "运行中 %d秒" % seconds


def view_for(handle: CardHandle, frame: CardFrame, *, elapsed: int,
             title: str = "任务") -> CardView:
    return CardView(
        title=title,
        state=frame.state.value,
        phase=frame.phase,
        host=frame.host,
        artifacts=tuple(frame.artifacts),
        note=_note(frame.state, elapsed),
        actions=_ACTIONS[frame.state],
        final=frame.final,
    )


class CardEmitter:
    """Drives one card through a transport.

    The transport must expose ``emit(handle, view, *, final)``. Frames the protocol drops
    (stale, duplicate, or after a terminal state) are never handed to the transport, so a
    late progress frame can never overwrite a finished card.
    """

    def __init__(self, store: CardStore, task_id: str, transport, *,
                 title: str = "任务", clock: Optional[Callable[[], float]] = None):
        self._store = store
        self._transport = transport
        self._title = title
        self._clock = clock or (lambda: 0.0)
        self._writer = store.writer(task_id)

    @property
    def handle(self) -> CardHandle:
        return self._writer.handle

    def _elapsed(self) -> int:
        started = self._writer.handle.started_at
        return max(0, int(self._clock() - started)) if started else 0

    def _publish(self, frame: Optional[CardFrame]) -> Optional[CardView]:
        if frame is None:
            return None
        view = view_for(self._writer.handle, frame, elapsed=self._elapsed(), title=self._title)
        self._transport.emit(self._writer.handle, view, final=frame.final)
        return view

    def open(self, *, phase: str = "", host: str = "",
             artifacts: Sequence[str] = ()) -> Optional[CardView]:
        return self._publish(self._writer.emit(
            CardState.RUNNING, phase=phase, host=host, artifacts=artifacts))

    def update(self, *, phase: str = "", detail: str = "", host: str = "",
               artifacts: Sequence[str] = ()) -> Optional[CardView]:
        return self._publish(self._writer.emit(
            CardState.RUNNING, phase=phase, detail=detail, host=host, artifacts=artifacts))

    def finish(self, *, phase: str = "", host: str = "",
               artifacts: Sequence[str] = ()) -> Optional[CardView]:
        """Terminal success. Always the card's last frame — never a second message."""
        return self._publish(self._writer.emit(
            CardState.DONE, phase=phase, host=host, artifacts=artifacts, final=True))

    def fail(self, *, detail: str = "", artifacts: Sequence[str] = ()) -> Optional[CardView]:
        """Terminal failure. Never rendered as success, even if the host raised."""
        return self._publish(self._writer.emit(
            CardState.FAILED, detail=detail, artifacts=artifacts, final=True))

    def cancel(self, *, detail: str = "") -> Optional[CardView]:
        return self._publish(self._writer.emit(CardState.CANCELLED, detail=detail, final=True))

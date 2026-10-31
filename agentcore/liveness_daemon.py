"""Turn liveness state transitions into bounded, transport-neutral events.

The ledger answers "what is true now"; this daemon answers "what changed". It is
deliberately not a platform client: a sink receives one JSON-able event per
transition, and an adapter (card, webhook, local log) decides how to render it.
That split is what keeps routine liveness cheap: a quiet task produces no event,
and a card adapter can refresh elapsed time without starting a model turn.

No third-party imports: the package import-boundary gate forbids them.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .liveness import LivenessLedger, _bounded_tail

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 20.0


class JsonlSink:
    """Append-only JSONL sink. Useful for local inspection and adapter replay."""

    def __init__(self, path: Any, *, fsync: bool = False):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync

    def __call__(self, event: Dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            if self._fsync:
                import os
                os.fsync(handle.fileno())


class LivenessDaemon:
    """One sweep loop. The sink is injected; failures are isolated per event."""

    def __init__(self, ledger: LivenessLedger, sink: Callable[[Dict[str, Any]], Any], *,
                 interval: float = DEFAULT_INTERVAL_SECONDS,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], Any]] = None,
                 pool: Any = None):
        if not callable(sink):
            raise ValueError("sink must be callable")
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            raise ValueError("interval must be a number")
        if not interval > 0:
            raise ValueError("interval must be positive")
        self.ledger = ledger
        self.sink = sink
        self.interval = interval
        self._clock = clock or time.time
        self._sleep = sleep
        self.errors = 0
        # Prefer-D2: optional pool reference — tick sweeps STARTING/CANCELING windows.
        # Pool remains ledger authority; daemon never becomes a second scheduler.
        self.pool = pool

    def _event(self, row: Dict[str, Any]) -> Dict[str, Any]:
        now = float(self._clock())
        elapsed = max(0.0, now - float(row.get("started_at") or now))
        heartbeat_age = max(0.0, now - float(row.get("heartbeat_at") or now))
        return {
            "type": "task_state",
            "task_id": row.get("task_id"),
            "kind": row.get("kind"),
            "owner": row.get("owner"),
            "label": row.get("label"),
            "previous_state": row.get("previous_state"),
            "state": row.get("state"),
            "elapsed_s": round(elapsed, 1),
            "heartbeat_age_s": round(heartbeat_age, 1),
            "phase": row.get("phase") or "",
            "detail": row.get("detail") or "",
            "pid": row.get("pid"),
            "exit_code": row.get("exit_code"),
            "artifacts": list(row.get("artifacts") or ()),
            "tail": _bounded_tail(row.get("tail") or "", 1200),
        }

    def run_once(self) -> List[Dict[str, Any]]:
        """Sweep once, emit one event per transition, return what was emitted.

        Prefer-D2: when a pool is bound, enforce STARTING/CANCELING hard windows first
        so pid-null running and soft-cancel fences cannot fake healthy Running forever.
        """
        if self.pool is not None and hasattr(self.pool, "enforce_windows"):
            try:
                self.pool.enforce_windows(now=self._clock())
            except Exception:  # noqa: BLE001 — window sweep must not stop the daemon
                self.errors += 1
                logger.warning("prefer-d2 pool.enforce_windows failed", exc_info=True)
        emitted: List[Dict[str, Any]] = []
        for row in self.ledger.sweep(now=self._clock()):
            event = self._event(row)
            try:
                self.sink(event)
            except Exception:  # noqa: BLE001 — one broken adapter must not stop the loop
                self.errors += 1
                logger.warning("liveness sink failed for task %s", row.get("task_id"),
                               exc_info=True)
                continue
            emitted.append(event)
        return emitted

    def run(self, *, stop: Optional[threading.Event] = None,
            max_iterations: Optional[int] = None) -> Dict[str, int]:
        """Sweep until *stop* is set or *max_iterations* is reached."""
        if max_iterations is not None and int(max_iterations) < 0:
            raise ValueError("max_iterations must be non-negative")
        stop = stop or threading.Event()
        iterations = 0
        while not stop.is_set():
            self.run_once()
            iterations += 1
            if max_iterations is not None and iterations >= int(max_iterations):
                break
            if stop.is_set():
                break
            if self._sleep is not None:
                self._sleep(self.interval)
            else:
                stop.wait(self.interval)
        return {"iterations": iterations, "errors": self.errors}


#: Prefer-C2 — liveness terminal states that may kick completion_delivery (no LLM).
_TERMINAL_LIVENESS = frozenset({"done", "failed", "cancelled", "completed"})


def terminal_delivery_sink(pool, transport=None):
    """Build a liveness sink that kicks ``notify_on_terminal`` on terminal transitions.

    Thin Prefer-C2 wire: one side-channel deliver attempt per terminal event. Failures
    stay observable via the returned status; never fakes delivered when transport is
    missing. Does not open a model turn or a second scheduler.
    """
    from agentcore.completion_delivery import notify_on_terminal

    def _sink(event):
        if not isinstance(event, dict):
            return None
        state = event.get("state")
        if str(state or "").lower() not in _TERMINAL_LIVENESS:
            return None
        return notify_on_terminal(pool, transport, status=state)

    return _sink


__all__ = ["LivenessDaemon", "JsonlSink", "DEFAULT_INTERVAL_SECONDS",
           "terminal_delivery_sink"]

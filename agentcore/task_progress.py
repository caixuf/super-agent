"""Long-task progress: one editable entry per task, driven by evidence only.

Why this exists
---------------
A long job currently produces either silence (the user asks "还在做吗") or a burst of
per-event messages. Both are the same underlying gap: no single observable surface that
changes when the task *actually* changes.

This module is the pure policy between the liveness ledger and any transport:

* **evidence only** — state, phase, progress counters, artifacts and the heartbeat come
  from the ledger row; nothing here estimates a percentage, a finish time or a next step;
* **silence is reported as silence** — a running task with no news says so instead of
  looking frozen or inventing motion;
* **change-driven** — an update is emitted on a state/phase/progress transition, on a
  verdict flip (``lost`` / ``stalled``), and at most once per ``min_interval`` to advance
  the elapsed clock. A poll with no change emits nothing;
* **terminal is write-once** — ``done`` / ``failed`` / ``cancelled`` end the entry.

No I/O, no clock of its own, no third-party imports: the caller supplies the row, ``now``
and the transport. :mod:`agentcore.card_store` owns identity, :mod:`agentcore.card_emit`
owns the rendered view; this module owns *what may be claimed and when it changes*.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .task_card import CardState

#: Verdicts that mean "this task will not progress on its own".
_STUCK = ("lost", "stalled")
#: Ledger states that are terminal.
_TERMINAL_STATES = {
    "done": CardState.DONE,
    "failed": CardState.FAILED,
    "cancelled": CardState.CANCELLED,
}
#: A single editable entry stays well inside one chat message.
MAX_TEXT = 1200


def verdict(row: Optional[Dict[str, Any]]) -> str:
    """The user-facing verdict a ledger row supports (never a guess)."""
    if not isinstance(row, dict):
        return "unknown"
    state = str(row.get("state") or "").strip()
    if state in _TERMINAL_STATES:
        return state
    if state in _STUCK:
        return state
    if state in ("queued", "running"):
        return state
    return "unknown"


def terminal(row: Optional[Dict[str, Any]]) -> bool:
    """Whether this row will not progress on its own (done/failed/cancelled, or stuck)."""
    word = verdict(row)
    return word in _TERMINAL_STATES or word in _STUCK


def card_state(row: Optional[Dict[str, Any]]) -> CardState:
    """Map a ledger row to the card protocol's state (stuck jobs end as failures)."""
    word = verdict(row)
    if word in _TERMINAL_STATES:
        return _TERMINAL_STATES[word]
    if word in _STUCK:
        return CardState.FAILED
    if word == "running":
        return CardState.RUNNING
    return CardState.QUEUED


def snapshot(row: Optional[Dict[str, Any]], *, now: float) -> Dict[str, Any]:
    """Evidence-only view of one task at ``now``.

    ``percent`` is present only when the ledger carries a real ``total``; ``phase``,
    ``detail`` and ``artifacts`` are only what the worker wrote. Missing evidence stays
    missing — the renderer says so instead of filling the gap.
    """
    data = row if isinstance(row, dict) else {}
    started = data.get("started_at")
    try:
        started = float(started) if started else 0.0
    except (TypeError, ValueError):
        started = 0.0
    elapsed = max(0, int(now - started)) if started else 0
    done, total = data.get("progress_done"), data.get("progress_total")
    percent = None
    try:
        if total:
            percent = max(0.0, min(100.0, 100.0 * float(done) / float(total)))
    except (TypeError, ValueError):
        percent = None
    artifacts = data.get("artifacts")
    if isinstance(artifacts, str):
        artifacts = (artifacts,)
    return {
        "task_id": str(data.get("task_id") or ""),
        "kind": str(data.get("kind") or ""),
        "label": str(data.get("label") or "").strip()[:120],
        "state": str(data.get("state") or "unknown"),
        "verdict": verdict(data),
        "phase": str(data.get("phase") or "").strip()[:120],
        "detail": str(data.get("detail") or "").strip()[:200],
        "percent": percent,
        "elapsed": elapsed,
        "artifacts": tuple(str(a) for a in (artifacts or ()))[:6],
        "host": str(data.get("host") or ""),
        "terminal": card_state(data) in (CardState.DONE, CardState.FAILED, CardState.CANCELLED),
    }


def _clock_text(seconds: int) -> str:
    minutes, rest = divmod(max(0, int(seconds)), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return "%d时%02d分" % (hours, minutes)
    return "%d分%02d秒" % (minutes, rest) if minutes else "%d秒" % rest


_STATE_WORD = {
    CardState.QUEUED: "已接收",
    CardState.RUNNING: "进行中",
    CardState.DONE: "已完成",
    CardState.FAILED: "未完成",
    CardState.CANCELLED: "已取消",
}


def render(snap: Dict[str, Any], *, title: str = "") -> str:
    """One editable entry's text. Every line comes from a field, none from imagination."""
    lines = []
    head = title.strip() or snap.get("label") or snap.get("task_id") or "任务"
    lines.append("**%s**" % head[:90])
    lines.append("状态：%s%s" % (_STATE_WORD[card_state(snap)], _state_suffix(snap)))
    if snap.get("phase"):
        lines.append("当前：%s" % snap["phase"])
    if snap.get("percent") is not None:
        lines.append("进度：%.0f%%" % snap["percent"])
    lines.append("已用：%s" % _clock_text(int(snap.get("elapsed") or 0)))
    if snap.get("detail") and snap["detail"] != snap.get("phase"):
        lines.append("说明：%s" % snap["detail"])
    if snap.get("artifacts"):
        lines.append("产物：%s" % "、".join(snap["artifacts"]))
    if not snap.get("phase") and not snap.get("percent") and not snap.get("detail"):
        lines.append("（暂无新进展，收到进度会更新这一条）")
    lines.append("`%s`" % (snap.get("task_id") or "")[:40])
    text = "\n".join(line for line in lines if line)
    return text[:MAX_TEXT]


def _state_suffix(snap: Dict[str, Any]) -> str:
    word = snap.get("verdict")
    if word == "lost":
        return "（进程已不在）"
    if word == "stalled":
        return "（长时间无心跳）"
    return ""


def changed(prev: Optional[Dict[str, Any]], snap: Dict[str, Any]) -> bool:
    """Whether ``snap`` is a *different claim* from ``prev`` (state/phase/progress/verdict)."""
    if not prev:
        return True
    for key in ("state", "verdict", "phase", "percent", "detail", "terminal"):
        if prev.get(key) != snap.get(key):
            return True
    return False


def should_emit(prev: Optional[Dict[str, Any]], snap: Dict[str, Any], *,
                last_emit_at: float, now: float, min_interval: float = 60.0) -> bool:
    """Emit on a real change, or at most once per interval to advance the elapsed clock.

    A terminal snapshot may be emitted once even if nothing "changed" from the caller's
    point of view (the ledger row can already be terminal when the entry opens).
    """
    if (prev or {}).get("terminal"):
        return False  # write-once: a finished entry is never reopened by a later frame
    if snap.get("terminal"):
        return True
    if changed(prev, snap):
        return True
    if min_interval <= 0:
        return False
    return (now - last_emit_at) >= min_interval

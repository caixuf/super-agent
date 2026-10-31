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
    if state in ("queued", "running", "waiting_user", "awaiting_plan_approval"):
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
    if word == "waiting_user":
        return CardState.WAITING_USER
    if word == "awaiting_plan_approval":
        return CardState.AWAITING_PLAN_APPROVAL
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
        "role_id": str(data.get("role_id") or "").strip()[:64],
        "display_name": str(data.get("display_name") or "").strip()[:80],
        "task_title": str(data.get("task_title") or data.get("label") or "").strip()[:120],
        "heartbeat_age_s": data.get("heartbeat_age_s"),
        "attempt_id": str(data.get("attempt_id") or "").strip()[:64],
    }


def foreground_snapshot(event: Optional[Dict[str, Any]], *, now: float) -> Dict[str, Any]:
    """Project trusted turn lifecycle and safe phase hints onto the card schema.

    event is host lifecycle evidence; callers must not pass model/user text or tool
    arguments. Unknown terminal evidence remains active with an explicit waiting detail.
    """
    data = event if isinstance(event, dict) else {}
    state = data.get("state")
    if state not in {"queued", "running", "done", "failed", "cancelled"}:
        state = "unknown"
    row = {
        "task_id": str(data.get("task_id") or ""),
        "kind": "foreground",
        "label": str(data.get("label") or "前台回合"),
        "state": state,
        "started_at": data.get("started_at"),
        "phase": str(data.get("phase") or ""),
        "detail": str(data.get("detail") or ""),
        "progress_done": None,
        "progress_total": None,
    }
    result = snapshot(row, now=now)
    if state == "unknown":
        result["verdict"] = "running"
        result["terminal"] = False
    return result


def aggregate_with_foreground(task_snaps, foreground: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate tracked background jobs with one foreground turn in the same card."""
    items = list(task_snaps or ())
    if isinstance(foreground, dict):
        items.insert(0, foreground)
    return aggregate(items)


def _clock_text(seconds: int) -> str:
    minutes, rest = divmod(max(0, int(seconds)), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return "%d时%02d分" % (hours, minutes)
    return "%d分%02d秒" % (minutes, rest) if minutes else "%d秒" % rest


_STATE_WORD = {
    CardState.QUEUED: "已接收",
    CardState.RUNNING: "进行中",
    CardState.WAITING_USER: "等待用户",
    CardState.AWAITING_PLAN_APPROVAL: "等待计划审批",
    CardState.DONE: "已完成",
    CardState.FAILED: "未完成",
    CardState.CANCELLED: "已取消",
}


def render(snap: Dict[str, Any], *, title: str = "") -> str:
    """One editable entry's text. Every line comes from a field, none from imagination."""
    lines = []
    display_name = str(snap.get("display_name") or "").strip()
    task_title = str(snap.get("task_title") or snap.get("label") or "").strip()
    if display_name and task_title:
        composed = "%s · %s" % (display_name, task_title)
    else:
        composed = display_name or task_title
    head = title.strip() or composed or snap.get("label") or snap.get("task_id") or "任务"
    lines.append("**%s**" % head[:90])
    if display_name and display_name not in head:
        lines.append("岗位：%s" % display_name[:80])
    lines.append("状态：%s%s" % (_STATE_WORD[card_state(snap)], _state_suffix(snap)))
    age = snap.get("heartbeat_age_s")
    if age is not None and not snap.get("terminal"):
        try:
            lines.append("心跳：%d秒前" % max(0, int(float(age))))
        except (TypeError, ValueError):
            pass
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
    for key in ("state", "verdict", "phase", "percent", "detail", "terminal", "signature"):
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


# --------------------------------------------------------------------------- #
# One entry over several tasks (one turn can spawn more than one background job)
# --------------------------------------------------------------------------- #

def aggregate(snaps) -> Dict[str, Any]:
    """Collapse per-task snapshots into the single claim one entry may make.

    ``any task still running`` → running (the entry is not finished);
    otherwise failed wins over cancelled, which wins over done. Elapsed is the longest.
    """
    items = [s for s in (snaps or ()) if isinstance(s, dict)]
    if not items:
        return {"verdict": "unknown", "terminal": False, "elapsed": 0, "tasks": 0}
    words = [s.get("verdict") or "unknown" for s in items]
    if any(w in ("running", "unknown") for w in words):
        verdict = "running"
    elif any(w in ("failed", "lost", "stalled") for w in words):
        verdict = "failed"          # a failure is never hidden behind a success
    elif any(w == "done" for w in words):
        verdict = "done"            # part of the work landed; one cancelled sibling is not the story
    elif any(w == "cancelled" for w in words):
        verdict = "cancelled"
    else:
        verdict = "done"
    signature = "|".join("%s:%s:%s:%s" % (s.get("task_id") or "", s.get("verdict") or "",
                                          s.get("phase") or "", s.get("percent")) for s in items)
    return {"verdict": verdict, "terminal": verdict != "running",
            "elapsed": max(int(s.get("elapsed") or 0) for s in items), "tasks": len(items),
            "signature": signature}


#: Ledger phases that carry no information for a reader.
_QUIET_PHASES = frozenset({"", "started", "queued", "running"})

_HEAD_ICON = {
    CardState.QUEUED: "⏳",
    CardState.RUNNING: "🔵",
    CardState.WAITING_USER: "⏳",
    CardState.AWAITING_PLAN_APPROVAL: "⏳",
    CardState.DONE: "🟢",
    CardState.FAILED: "🔴",
    CardState.CANCELLED: "⚪",
}

_VERDICT_ICON = {
    "queued": "⏳",
    "running": "🔵",
    "done": "🟢",
    "failed": "🔴",
    "cancelled": "⚪",
    "stalled": "🟡",
    "lost": "🔴",
    "waiting_user": "⏳",
    "awaiting_plan_approval": "⏳",
}


def format_verdict_pill(verdict: Optional[str]) -> str:
    """Format a verdict into a clean status pill with an icon."""
    word = verdict_word(verdict)
    icon = _VERDICT_ICON.get(str(verdict or ""), "▫️")
    return f"{icon} `{word}`" if word else ""


#: One word per ledger verdict, for every line a reader sees. The ledger's
#: vocabulary is code (running / lost / stalled); a card that prints it verbatim
#: leaks English internals into a Chinese sentence, and `lost`/`stalled` -- the
#: two states a killed background process actually lands in -- were missing
#: entirely before this table existed.
VERDICT_WORD = {
    "queued": "已接收",
    "running": "进行中",
    "done": "已完成",
    "failed": "未完成",
    "cancelled": "已取消",
    "stalled": "无响应",
    "lost": "进程已不在",
    "waiting_user": "等待用户",
    "awaiting_plan_approval": "等待计划审批",
}


def verdict_word(verdict: Optional[str]) -> str:
    """The reader-facing word for a ledger verdict; unknown values stay as-is."""
    return VERDICT_WORD.get(str(verdict or ""), str(verdict or ""))


def render_many(snaps, *, title: str = "任务") -> str:
    """One entry over several tasks: a rich header, structured bullet items, and elapsed time."""
    items = [s for s in (snaps or ()) if isinstance(s, dict)]
    if not items:
        return "**%s**\n状态：⏳ **已接收**\n（暂无新进展，收到进度会更新这一条）" % title
    head = aggregate(items)
    heading = ("任务" if head["tasks"] == 1
               else "任务（本轮 %d 个后台任务）" % head["tasks"])
    state = card_state({"state": head["verdict"]})
    state_icon = _HEAD_ICON.get(state, "▫️")
    state_str = _STATE_WORD[state]
    elapsed_str = _clock_text(int(head["elapsed"]))

    lines = [
        "**%s**" % heading,
        f"状态：{state_icon} **{state_str}** ｜ 已用（最长）：`{elapsed_str}`",
        "",
    ]
    for snap in items[:6]:
        label = str(snap.get("label") or snap.get("task_id") or "任务").strip()
        pill = format_verdict_pill(snap.get("verdict"))
        extra_bits = []
        if snap.get("phase") and snap["phase"] not in _QUIET_PHASES:
            extra_bits.append(str(snap["phase"]))
        if snap.get("percent") is not None:
            extra_bits.append("%.0f%%" % snap["percent"])
        extra_str = f" ({' · '.join(extra_bits)})" if extra_bits else ""
        lines.append(f"• **{label}** ｜ {pill}{extra_str}")
    if len(items) > 6:
        lines.append("• …另有 %d 个" % (len(items) - 6))
    return "\n".join(lines)[:MAX_TEXT]

# --------------------------------------------------------------------------- #
# Mid-run interrupt queue (Codex section 6-3) — queue follow-ups; never preempt irreversible
# --------------------------------------------------------------------------- #

IRREVERSIBLE_PHASES = frozenset({
    "commit", "push", "deploy", "send", "payment", "delete", "destructive",
})


class FollowUpQueue:
    """Queue mid-run user follow-ups on the main-dialog control path.

    Drain only when the current phase is not irreversible. Never starts a second
    long execution by itself — the host control path decides how to apply items.
    """

    def __init__(self):
        self._items = []

    def enqueue(self, text, *, kind="follow_up", task_id=""):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("follow_up_text_required")
        item = {"kind": kind, "text": text.strip()[:2000], "task_id": str(task_id or "")}
        self._items.append(item)
        return {"ok": True, "queued": len(self._items), "item": item}

    def __len__(self):
        return len(self._items)

    def peek(self):
        return list(self._items)

    def drain(self, *, current_phase="", allow_preempt=False):
        phase = str(current_phase or "").strip().lower()
        if not allow_preempt and phase in IRREVERSIBLE_PHASES:
            return []
        items, self._items = self._items, []
        return items


def poll_progress_from_ledger(ledger, task_id, *, owner=None, now=None):
    """User progress ask -> read liveness ledger only; never start a second long job."""
    if ledger is None or not hasattr(ledger, "inspect"):
        return {"ok": False, "error": "ledger_unavailable", "task_id": task_id}
    kwargs = {}
    if owner is not None:
        kwargs["require_owner"] = owner
    if now is not None:
        kwargs["now"] = now
    row = ledger.inspect(task_id, **kwargs)
    if not row.get("ok"):
        return row
    started = float(row.get("started_at") or 0)
    at = float(now if now is not None else started + float(row.get("elapsed_s") or 0))
    snap = snapshot(row, now=at)
    if "heartbeat_age_s" in row:
        snap["heartbeat_age_s"] = row["heartbeat_age_s"]
    return {
        "ok": True,
        "task_id": task_id,
        "snap": snap,
        "verdict": row.get("verdict"),
        "source": "liveness_ledger",
        "second_exec": False,
    }

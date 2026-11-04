"""Prefer-D4 — liveness stalled/lost → actionable Feishu same-card (thin).

Prefect zombie "state change must have an action" cite only. On a real
sweep transition into ``stalled`` or ``lost``, emit ≤1 actionable same-card
patch (CardWriter) with colleague label + D1 short-path buttons. Cooldown
prevents spam. Never auto-marks done/failed from stall alone.

Stack: LivenessLedger.sweep → LivenessDaemon sink → this bridge → CardEmitter
→ Feishu adapter. Actions wire to Prefer-D1 barge_in_control short-paths.
Prefer-C1 Approve/Deny and Prefer-C2 terminal notify stay untouched.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from agentcore.card_emit import CardEmitter, resolve_display_name
from agentcore.liveness import DEFAULT_STALL_SECONDS

logger = logging.getLogger(__name__)

#: Same task_id + same verdict ≤1 action card per this window.
#: Default matches ledger stall threshold so a quiet row cannot spam.
STALL_NOTIFY_COOLDOWN_S = float(DEFAULT_STALL_SECONDS)

#: Ops action tokens (platform-neutral). Feishu adapter maps labels.
STALLED_ACTIONS = ("cancel", "pause", "status", "resume_observe", "details")

_STALL_VERDICTS = frozenset({"stalled", "lost"})


def ops_callback_value(*, task_id: str, action: str) -> dict:
    """Platform-neutral button value for stalled/lost ops (not HITL Approve/Deny)."""
    return {
        "action": str(action or "").strip(),
        "task_id": str(task_id or "").strip(),
        "ops": True,
        "kind": "stalled_ops",
    }


def stalled_text_fallback_instructions(*, task_id: str = "") -> str:
    """Honest text-fallback when interactive ops buttons cannot render."""
    tip = "回复 /cancel 取消 · /pause 暂停 · /status 查看进度 · /resume_observe 恢复观察"
    if task_id:
        return "%s · task=%s" % (tip, task_id[:24])
    return tip


class StallNotifyCooldown:
    """In-memory throttle: (task_id, verdict) → last notify epoch."""

    def __init__(self, cooldown_s: float = STALL_NOTIFY_COOLDOWN_S,
                 clock: Optional[Callable[[], float]] = None):
        try:
            cooldown_s = float(cooldown_s)
        except (TypeError, ValueError):
            raise ValueError("cooldown_s must be a number")
        if cooldown_s < 0:
            raise ValueError("cooldown_s must be non-negative")
        self.cooldown_s = cooldown_s
        self._clock = clock or time.time
        self._last: Dict[tuple, float] = {}

    def blocked(self, task_id: str, verdict: str) -> bool:
        key = (str(task_id), str(verdict))
        last = self._last.get(key)
        if last is None:
            return False
        return (float(self._clock()) - float(last)) < self.cooldown_s

    def mark(self, task_id: str, verdict: str) -> None:
        self._last[(str(task_id), str(verdict))] = float(self._clock())

    def clear(self, task_id: str, verdict: Optional[str] = None) -> None:
        if verdict is None:
            for key in list(self._last):
                if key[0] == str(task_id):
                    self._last.pop(key, None)
        else:
            self._last.pop((str(task_id), str(verdict)), None)


class StalledCardBridge:
    """Prefer-D4 bridge: transition → same-card ops patch + deliver-or-reason.

    Inject store + transport (or a ready emitter factory). Failures stay
    observable; never invents ``delivered=True`` when transport/handle is missing.
    """

    def __init__(
        self,
        *,
        store=None,
        transport=None,
        ledger=None,
        controller=None,
        catalog: Optional[dict] = None,
        cooldown: Optional[StallNotifyCooldown] = None,
        cooldown_s: float = STALL_NOTIFY_COOLDOWN_S,
        clock: Optional[Callable[[], float]] = None,
        emitter_factory: Optional[Callable[[str], Any]] = None,
        text_sink: Optional[Callable[[dict], Any]] = None,
    ):
        self.store = store
        self.transport = transport
        self.ledger = ledger
        self.controller = controller
        self.catalog = catalog
        self._clock = clock or time.time
        self.cooldown = cooldown or StallNotifyCooldown(cooldown_s, clock=self._clock)
        self.emitter_factory = emitter_factory
        self.text_sink = text_sink
        self.errors = 0

    def _resolve_label(self, task_id: str, event: dict) -> Dict[str, str]:
        """Colleague label from card handle / catalog / owner — never invent titles."""
        role_id = ""
        display_name = ""
        task_title = ""
        handle = None
        if self.store is not None and hasattr(self.store, "load"):
            try:
                handle = self.store.load(task_id)
            except Exception:
                handle = None
        if handle is not None:
            role_id = str(getattr(handle, "role_id", "") or "")
            display_name = str(getattr(handle, "display_name", "") or "")
            task_title = str(getattr(handle, "task_title", "") or "")
        owner = str(event.get("owner") or "")
        if not role_id and owner:
            role_id = owner
        if role_id and not display_name:
            display_name = resolve_display_name(role_id, catalog=self.catalog)
        label = str(event.get("label") or "")
        if not task_title and label:
            task_title = label[:120]
        return {
            "role_id": role_id,
            "display_name": display_name,
            "task_title": task_title,
        }

    def _emitter_for(self, task_id: str):
        if self.emitter_factory is not None:
            return self.emitter_factory(task_id)
        if self.store is None or self.transport is None:
            return None
        try:
            if self.store.load(task_id) is None:
                return None
            return CardEmitter(self.store, task_id, self.transport, clock=self._clock)
        except KeyError:
            return None
        except Exception:
            self.errors += 1
            logger.warning("prefer-d4 emitter open failed for %s", task_id, exc_info=True)
            return None

    def notify_transition(self, event: dict) -> dict:
        """Emit ≤1 actionable same-card patch for a stalled/lost transition.

        Returns a status dict. ``delivered`` is True only when a transport emit
        (or honest text fallback sink) succeeded — never invented.
        """
        if not isinstance(event, dict):
            return {"status": "skipped", "reason": "bad_event", "delivered": False}
        verdict = str(event.get("state") or event.get("verdict") or "").strip().lower()
        if verdict not in _STALL_VERDICTS:
            return {"status": "skipped", "reason": "not_stalled_or_lost",
                    "delivered": False, "verdict": verdict}
        task_id = str(event.get("task_id") or "").strip()
        if not task_id:
            return {"status": "skipped", "reason": "missing_task_id", "delivered": False}

        previous = str(event.get("previous_state") or "")
        if previous and previous == verdict:
            return {"status": "skipped", "reason": "no_transition", "delivered": False,
                    "task_id": task_id, "verdict": verdict}

        if self.cooldown.blocked(task_id, verdict):
            return {
                "status": "throttled",
                "reason": "stall_notify_cooldown",
                "delivered": False,
                "task_id": task_id,
                "verdict": verdict,
                "cooldown_s": self.cooldown.cooldown_s,
            }

        heartbeat_age_s = event.get("heartbeat_age_s")
        try:
            if heartbeat_age_s is not None:
                heartbeat_age_s = float(heartbeat_age_s)
        except (TypeError, ValueError):
            heartbeat_age_s = None

        phase = str(event.get("phase") or "")
        detail = str(event.get("detail") or "")
        identity = self._resolve_label(task_id, event)

        inspect_note = ""
        pid_alive = None
        if self.ledger is not None and hasattr(self.ledger, "inspect"):
            try:
                snap = self.ledger.inspect(task_id, now=self._clock())
                if isinstance(snap, dict) and snap.get("ok"):
                    if heartbeat_age_s is None:
                        heartbeat_age_s = snap.get("heartbeat_age_s")
                    pid_alive = snap.get("pid_alive")
                    inspect_note = str(snap.get("liveness_note") or "")
                    if not phase:
                        phase = str(snap.get("phase") or "")
                    if not detail:
                        detail = str(snap.get("detail") or "")
            except Exception:
                self.errors += 1

        note_bits = []
        if detail:
            note_bits.append(detail)
        if inspect_note:
            note_bits.append(inspect_note)
        card_detail = " · ".join(note_bits)[:400] if note_bits else verdict

        emitter = self._emitter_for(task_id)
        view = None

        if emitter is None:
            reason = "no_card_handle"
            fallback_payload = {
                "task_id": task_id,
                "verdict": verdict,
                "heartbeat_age_s": heartbeat_age_s,
                "phase": phase,
                "detail": card_detail,
                "role_id": identity["role_id"],
                "display_name": identity["display_name"],
                "task_title": identity["task_title"],
                "pid_alive": pid_alive,
                "actions": list(STALLED_ACTIONS),
                "instructions": stalled_text_fallback_instructions(task_id=task_id),
            }
            if self.text_sink is not None:
                try:
                    self.text_sink(fallback_payload)
                    self.cooldown.mark(task_id, verdict)
                    return {
                        "status": "text_fallback",
                        "reason": reason,
                        "delivered": True,
                        "task_id": task_id,
                        "verdict": verdict,
                        "heartbeat_age_s": heartbeat_age_s,
                        "colleague": identity,
                        "pid_alive": pid_alive,
                        "actions": list(STALLED_ACTIONS),
                        "fallback": True,
                    }
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    return {
                        "status": "unavailable",
                        "reason": reason,
                        "error": type(exc).__name__,
                        "delivered": False,
                        "task_id": task_id,
                        "verdict": verdict,
                    }
            return {
                "status": "unavailable",
                "reason": reason,
                "delivered": False,
                "task_id": task_id,
                "verdict": verdict,
                "heartbeat_age_s": heartbeat_age_s,
                "colleague": identity,
                "actions": list(STALLED_ACTIONS),
                "instructions": stalled_text_fallback_instructions(task_id=task_id),
            }

        if hasattr(emitter, "bind_identity") and (
                identity["role_id"] or identity["display_name"] or identity["task_title"]):
            try:
                emitter.bind_identity(
                    role_id=identity["role_id"],
                    display_name=identity["display_name"],
                    task_title=identity["task_title"],
                    catalog=self.catalog,
                )
            except Exception:
                self.errors += 1

        try:
            if verdict == "lost" and hasattr(emitter, "mark_lost"):
                view = emitter.mark_lost(
                    detail=card_detail, heartbeat_age_s=heartbeat_age_s,
                )
            elif hasattr(emitter, "mark_stalled"):
                view = emitter.mark_stalled(
                    detail=card_detail, heartbeat_age_s=heartbeat_age_s,
                    verdict=verdict,
                )
            elif hasattr(emitter, "update"):
                view = emitter.update(
                    phase=verdict, detail=card_detail,
                    heartbeat_age_s=heartbeat_age_s,
                )
            emit_ok = view is not None
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            logger.warning("prefer-d4 card patch failed for %s", task_id, exc_info=True)
            return {
                "status": "error",
                "reason": "card_patch_failed",
                "error": type(exc).__name__,
                "delivered": False,
                "task_id": task_id,
                "verdict": verdict,
            }

        if not emit_ok:
            return {
                "status": "unavailable",
                "reason": "card_frame_dropped",
                "delivered": False,
                "task_id": task_id,
                "verdict": verdict,
            }

        self.cooldown.mark(task_id, verdict)
        handle = getattr(emitter, "handle", None)
        return {
            "status": "patched",
            "delivered": True,
            "task_id": task_id,
            "verdict": verdict,
            "heartbeat_age_s": heartbeat_age_s,
            "phase": phase,
            "detail": card_detail,
            "colleague": identity,
            "pid_alive": pid_alive,
            "actions": list(STALLED_ACTIONS),
            "card_id": getattr(handle, "card_id", "") if handle is not None else "",
            "message_id": getattr(handle, "message_id", "") if handle is not None else "",
            "same_card": bool(getattr(handle, "message_id", "") if handle is not None else ""),
            "view_state": getattr(view, "state", None),
            "view_phase": getattr(view, "phase", None),
        }

    def handle_action(
        self,
        *,
        task_id: str,
        action: str,
        text: str = "",
        owner: str = "",
    ) -> dict:
        """Wire stalled-card button → Prefer-D1 short-path (no long exec)."""
        token = str(action or "").strip().lower().lstrip("/")
        label_map = {
            "取消": "cancel",
            "暂停": "pause",
            "暂停/等待": "pause",
            "等待": "pause",
            "查看进度": "status",
            "进度": "status",
            "恢复观察": "resume_observe",
        }
        # Re-map Chinese before lowercasing wiped them — use original too.
        raw = str(action or "").strip()
        if raw in label_map:
            token = label_map[raw]
        elif token in label_map:
            token = label_map[token]
        if token in ("resume_observe", "resume-observe", "observe"):
            return self.resume_observe(task_id, detail=text or "resume_observe")
        if self.controller is not None and hasattr(self.controller, "handle"):
            return self.controller.handle(
                task_id=task_id, action=token, text=text, owner=owner,
            )
        if token == "status" and self.ledger is not None:
            snap = self.ledger.inspect(task_id)
            return {
                "ok": True,
                "verb": "status",
                "task_id": task_id,
                "second_exec": False,
                "durable_dispatch_submit": False,
                "message": "进度：%s" % (snap.get("verdict") if isinstance(snap, dict) else "?"),
                "detail": {"ledger": snap},
            }
        if token == "cancel":
            return {
                "ok": False,
                "verb": "cancel",
                "task_id": task_id,
                "second_exec": False,
                "message": "cancel requires pool/controller",
                "detail": {"reason": "no_controller"},
            }
        return {
            "ok": False,
            "verb": token or "unknown",
            "task_id": task_id,
            "second_exec": False,
            "message": "unhandled stalled action",
            "detail": {},
        }

    def resume_observe(self, task_id: str, *, detail: str = "resume_observe") -> dict:
        """Clear stall observation / force beat — NEVER fake done/failed."""
        beat_row = None
        if self.ledger is not None and hasattr(self.ledger, "beat"):
            try:
                beat_row = self.ledger.beat(
                    task_id, phase="observing", detail=detail or "resume_observe",
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "verb": "resume_observe",
                    "task_id": task_id,
                    "second_exec": False,
                    "durable_dispatch_submit": False,
                    "message": "恢复观察失败：%s" % type(exc).__name__,
                    "detail": {"error": type(exc).__name__},
                }
        view = None
        emitter = self._emitter_for(task_id)
        if emitter is not None and hasattr(emitter, "update"):
            try:
                view = emitter.update(
                    phase="observing",
                    detail=detail or "已恢复观察（未标记完成）",
                )
            except Exception:
                self.errors += 1
        self.cooldown.clear(task_id)
        ok = beat_row is not None or view is not None
        return {
            "ok": ok,
            "verb": "resume_observe",
            "task_id": task_id,
            "second_exec": False,
            "durable_dispatch_submit": False,
            "message": "已恢复观察（未标记完成/失败）" if ok else "恢复观察未生效",
            "detail": {
                "beat": beat_row,
                "card_phase": getattr(view, "phase", None),
                "auto_done": False,
                "auto_failed": False,
            },
        }


def stalled_card_sink(bridge: StalledCardBridge):
    """Build a liveness sink that kicks Prefer-D4 on stalled/lost transitions."""

    def _sink(event):
        if not isinstance(event, dict):
            return None
        state = str(event.get("state") or "").lower()
        if state not in _STALL_VERDICTS:
            return None
        return bridge.notify_transition(event)

    return _sink


def compose_sinks(*sinks: Callable[[dict], Any]) -> Callable[[dict], Any]:
    """Fan-out helper so C2 terminal + D4 stalled can share one daemon."""

    def _composed(event):
        results = []
        for sink in sinks:
            if not callable(sink):
                continue
            try:
                results.append(sink(event))
            except Exception:
                logger.warning("composed liveness sink failed", exc_info=True)
                results.append(None)
        return results

    return _composed


__all__ = [
    "STALL_NOTIFY_COOLDOWN_S",
    "STALLED_ACTIONS",
    "StallNotifyCooldown",
    "StalledCardBridge",
    "stalled_card_sink",
    "compose_sinks",
    "ops_callback_value",
    "stalled_text_fallback_instructions",
]
